"""`POST /chat` — token-metered chat against Groq Cloud, cached and filtered.

The endpoint is the reference implementation of *metered streaming work*, the
one case `credits.charge` cannot express because the cost is only known when
the stream ends:

1. **Reserve an upper bound.** `estimate_charge` bounds the prompt by a token
   estimate and the completion by `llm_max_tokens`, so the reservation always
   covers what the provider can bill. While those credits are held, no
   concurrent request can spend them — the same atomic guard as everywhere else.
2. **Prime the upstream.** The first stream frame is withheld until the model
   has produced text, so an exhausted model chain surfaces as a real HTTP
   status instead of a half-written answer.
3. **Settle the exact cost.** The usage the provider reports in its final chunk
   becomes the charge; the unused part of the reservation returns to the
   available pool in the same transaction (see `credits.commit(amount=...)`).
4. **Clean up no matter what.** A failure or a client that hangs up mid-stream
   charges an estimate of what was actually delivered, or releases the
   reservation when nothing was.

Cross-cutting concerns:

- **Cache.** A hit replays a stored answer and its recorded charge, so billing
  is identical to a fresh request and only the upstream call is skipped. See
  `cache.py`.
- **Guardrails.** Every token passes through the output redactor before it
  reaches the client or the cache, so an identity or secret leak cannot be
  cached *or* served. See `guardrails.py`.
- **Errors.** Upstream failures are mapped to generic client messages. The
  provider's own text — which names models and may echo request bodies — is
  logged, never returned.

Chain: client -> Next proxy (passthrough) -> here -> Groq Cloud -> back.
"""

import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from . import cache, credits
from .auth import get_api_key
from .config import settings
from .db import Session, engine
from .errors import CreditsExpired, InsufficientCredits
from .guardrails import (
    REDACTIONS,
    OutputFilter,
    client_error,
    identity_redactions,
    log_redactions,
    system_prompt,
)
from .llm import (
    Completed,
    LLMError,
    MessageStreamBuilder,
    SSE_HEADERS,
    credits_for_tokens,
    estimate_charge,
    estimate_tokens,
    normalize_messages,
    stream_with_fallback,
)
from .models import ApiKey, ResponseCache

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])


class ChatMessage(BaseModel):
    """One transport message. Only text is used; other parts are ignored."""

    model_config = {"extra": "ignore"}

    role: str
    content: str | list[Any] | None = None
    parts: list[Any] | None = None


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(default_factory=list)


def _session() -> Session:
    """A fresh session for work that outlives the request dependency."""
    return Session(engine)


def _release(reservation_id: int) -> None:
    with _session() as session:
        credits.release(session, reservation_id=reservation_id)


def _bypass_cache(request: Request) -> bool:
    """A client can opt out with `Cache-Control: no-cache` (or `no-store`)."""
    header = (request.headers.get("cache-control") or "").lower()
    return "no-cache" in header or "no-store" in header


def hit_charge(entry: ResponseCache) -> int:
    """What a cache hit costs, per `cache_hit_mode`.

    `metered` replays the recorded charge, which is the only mode that cannot
    be farmed: repeating a prompt pays what it pays, exactly as if freshly
    generated. `flat` and `free` trade that for cheaper repeats, and are
    operator opt-ins for that reason.
    """
    mode = settings.cache_hit_mode.strip().lower()
    if mode == "free":
        return 0
    if mode == "flat":
        return max(0, int(settings.cache_hit_credits))
    return max(0, int(entry.credits_charged))


def settle_usage(
    *,
    key_id: int,
    reservation_id: int,
    amount: int,
    reserved: int,
    reference: str,
) -> None:
    """Charge a metered cost against a reservation, in its own transaction.

    The reservation is committed for up to `reserved` credits (a partial commit
    hands the remainder back); anything the estimate missed is charged directly
    so usage is never under-billed.

    Never raises: the answer is already on the wire by the time this runs, so a
    billing failure is logged rather than delivered to the client. The
    idempotent status guard keeps the ledger correct either way.
    """
    try:
        with _session() as session:
            if amount <= 0:
                credits.release(session, reservation_id=reservation_id)
                return
            if amount <= reserved:
                credits.commit(
                    session,
                    reservation_id=reservation_id,
                    amount=amount,
                    reference=reference,
                )
                return
            # The real cost outran the estimate: spend the whole reservation,
            # then charge the excess as a direct spend.
            credits.commit(session, reservation_id=reservation_id, reference=reference)
            excess = amount - reserved
            credits.charge(session, key_id=key_id, amount=excess, reference=reference)
    except HTTPException:
        logger.exception(
            "could not settle chat usage (key=%s reservation=%s amount=%s); "
            "reconciliation needed",
            key_id,
            reservation_id,
            amount,
        )
    except Exception:  # noqa: BLE001 — billing must not break a delivered answer
        logger.exception(
            "unexpected error settling chat usage (key=%s reservation=%s)",
            key_id,
            reservation_id,
        )


def _guardrails() -> OutputFilter:
    """The output filter for one request, including this gateway's models.

    The identity terms come from the configured chain, so the filter always
    covers the models actually in service — change the chain and the filter
    follows.
    """
    return OutputFilter(REDACTIONS + identity_redactions(settings.model_chain))


def _cached_stream(entry: ResponseCache) -> AsyncIterator[str]:
    """Replay a stored answer with the same frames a live stream produces.

    The text is re-filtered before replay: an entry written before a redaction
    rule existed still must not replay a secret.
    """
    builder = MessageStreamBuilder()
    guard = _guardrails()
    for piece in cache.chunk_text(entry.response_text, settings.cache_stream_chunk_chars):
        safe = guard.feed(piece)
        if not safe:
            continue
        if builder.started:
            yield builder.delta(safe)
        else:
            yield builder.open(safe)
    tail = guard.flush()
    if tail:
        if builder.started:
            yield builder.delta(tail)
        else:
            yield builder.open(tail)
    if guard.redacted_count:
        log_redactions(guard.matches, context="cached replay")
    yield builder.finish()


async def _ui_stream(
    *,
    key_id: int,
    reservation_id: int,
    reserved: int,
    messages: list[dict],
    query_hash_by_model: dict[str, str],
) -> AsyncIterator[str]:
    """Stream the answer as UI-message SSE, settling credits exactly once.

    Every exit path is accounted for: success settles the reported usage, an
    error after text started charges an estimate of what was delivered, and a
    failure with nothing delivered releases the reservation untouched.
    """
    builder = MessageStreamBuilder()
    guard = _guardrails()
    settled = False
    delivered: list[str] = []

    def cleanup() -> None:
        """Charge what was delivered, or release when nothing was."""
        try:
            if not delivered:
                _release(reservation_id)
                return
            prompt_tokens = sum(
                estimate_tokens(str(message.get("content") or ""))
                for message in messages
            )
            estimate = credits_for_tokens(
                prompt_tokens + estimate_tokens("".join(delivered))
            )
            if estimate <= 0:
                _release(reservation_id)
                return
            settle_usage(
                key_id=key_id,
                reservation_id=reservation_id,
                amount=estimate,
                reserved=reserved,
                reference="chat (interrupted)",
            )
        except Exception:  # noqa: BLE001 — cleanup must never raise
            logger.exception(
                "could not clean up chat reservation %s", reservation_id
            )

    def _emit(text: str) -> str:
        """Filter a delta, buffer it for metering and caching, frame it."""
        safe = guard.feed(text)
        if not safe:
            return ""
        delivered.append(safe)
        return builder.delta(safe) if builder.started else builder.open(safe)

    def _flush_guard() -> str:
        tail = guard.flush()
        if not tail:
            return ""
        delivered.append(tail)
        return builder.delta(tail) if builder.started else builder.open(tail)

    try:
        async for event in stream_with_fallback(
            messages=messages,
            api_key=settings.groq_api_key,
            base_url=settings.groq_base_url,
            timeout_seconds=settings.llm_timeout_seconds,
            max_tokens=settings.llm_max_tokens,
            temperature=settings.llm_temperature,
            chain=settings.model_chain,
        ):
            if isinstance(event, Completed):
                charge = credits_for_tokens(
                    event.usage.total_tokens, settings.credits_per_1k_tokens
                )
                frame = _flush_guard()
                logger.info(
                    "chat key=%s model=%s tokens=%s%s charged=%s reserved=%s",
                    key_id,
                    event.model,
                    event.usage.total_tokens,
                    " (estimated)" if event.usage.estimated else "",
                    charge,
                    reserved,
                )
                settle_usage(
                    key_id=key_id,
                    reservation_id=reservation_id,
                    amount=charge,
                    reserved=reserved,
                    reference=f"chat:{event.model}",
                )
                settled = True
                _store_answer(
                    key_id=key_id,
                    model=event.model,
                    query_hash_by_model=query_hash_by_model,
                    delivered=delivered,
                    usage=event.usage,
                    charge=charge,
                )
                if guard.redacted_count:
                    log_redactions(guard.matches, context=f"key={key_id}")
                yield frame
                yield builder.finish()
                return

            frame = _emit(event.text)
            if frame:
                yield frame
    except LLMError as exc:
        if builder.started:
            # Text is already on the wire: report the failure in-stream. Flush
            # the filter's held tail first, or a character that was only held
            # pending a hint ("...cache" + "d") is silently dropped when the
            # stream ends in an error.
            frame = _flush_guard()
            if frame:
                yield frame
            logger.error("chat stream failed mid-answer: %s", exc.message)
            yield builder.error(client_error(exc.status_code))
            return
        raise
    finally:
        if not settled:
            cleanup()


def _store_answer(
    *,
    key_id: int,
    model: str,
    query_hash_by_model: dict[str, str],
    delivered: list[str],
    usage,
    charge: int,
) -> None:
    """Persist a completed, filtered answer. Never raises.

    Only called after `Completed`, so partial generations and failures are never
    cached. The text stored is the filtered text that was streamed, so a replay
    is byte-identical to what the client already saw.
    """
    query_hash = query_hash_by_model.get(model)
    if not query_hash or not settings.cache_enabled:
        return
    response_text = "".join(delivered)
    if not response_text:
        return
    try:
        with _session() as session:
            stored = cache.store(
                session,
                query_hash=query_hash,
                api_key_id=key_id,
                model=model,
                response_text=response_text,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                credits_charged=charge,
            )
        if stored:
            cache.metrics.bump("stores")
        else:
            cache.metrics.bump("store_skips")
    except Exception:  # noqa: BLE001 — caching must not break a delivered answer
        cache.metrics.bump("store_skips")
        logger.exception("could not store chat answer for key=%s", key_id)


@router.post(
    "/chat",
    summary="Token-metered chat completion (SSE, AI SDK UI-message stream)",
    response_description="AI SDK v7 UI-message stream; credits metered on usage",
)
async def create_chat_completion(
    payload: ChatRequest,
    request: Request,
    api_key: ApiKey = Depends(get_api_key),
) -> StreamingResponse:
    started = time.monotonic()
    cache.metrics.bump("requests")
    if not settings.groq_api_key:
        raise HTTPException(status_code=503, detail="chat is not configured")

    try:
        messages = normalize_messages(
            [message.model_dump() for message in payload.messages],
            max_messages=settings.llm_max_history_messages,
            max_chars=settings.llm_max_message_chars,
            system_prompt=(
                system_prompt(settings.llm_system_prompt)
                if settings.guardrail_persona
                else settings.llm_system_prompt
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Fail fast, exactly like require_credits but metered on the estimate.
    reserved = estimate_charge(
        messages, settings.llm_max_tokens, settings.credits_per_1k_tokens
    )
    key_id = api_key.id or 0

    # --- cache lookup, before any reservation is taken ----------------------
    if settings.cache_enabled and not _bypass_cache(request):
        candidates = cache.candidate_keys(
            api_key_id=key_id,
            chain=settings.model_chain,
            messages=messages,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
        )
        with _session() as session:
            entry = cache.lookup(session, candidates)
        if entry is not None:
            charge = hit_charge(entry)
            # An expired key gets no answers, cached or not — otherwise
            # "free" hit mode would extend an expired key indefinitely.
            if api_key.credits_expired:
                raise CreditsExpired(
                    f"Credits expired on {api_key.credits_expire_at.isoformat()}"
                )
            if charge > 0:
                # Atomic, single-statement guard: same guarantee as a fresh
                # request, and the answer is never served to a key that cannot
                # pay for it. A 402/403 propagates as-is.
                with _session() as session:
                    credits.charge(
                        session, key_id=key_id, amount=charge, reference="cache-hit"
                    )
            elapsed = (time.monotonic() - started) * 1000
            cache.metrics.bump("hits")
            cache.metrics.bump("tokens_saved", int(entry.total_tokens))
            cache.metrics.bump("credits_served", charge)
            cache.metrics.bump("cache_ms", int(elapsed))
            logger.info(
                "chat-cache key=%s model=%s tokens_saved=%s charged=%s age_hits=%s ms=%.0f",
                key_id,
                entry.model,
                entry.total_tokens,
                charge,
                entry.hit_count,
                elapsed,
            )
            return StreamingResponse(
                _cached_stream(entry),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )

    if api_key.credits_expired:
        raise CreditsExpired(
            f"Credits expired on {api_key.credits_expire_at.isoformat()}"
        )
    if api_key.credits_available < reserved:
        raise InsufficientCredits(
            f"This request needs about {reserved} credit(s); "
            f"{api_key.credits_available} available"
        )

    # --- miss: reserve, then stream ----------------------------------------
    with _session() as session:
        reservation_id = await run_in_threadpool(
            credits.reserve,
            session,
            key_id=key_id,
            amount=reserved,
            reference="chat",
        )

    query_hashes = {
        model: cache.build_key(
            api_key_id=key_id,
            model=model,
            messages=messages,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
        )
        for model in settings.model_chain
    }
    stream = _ui_stream(
        key_id=key_id,
        reservation_id=reservation_id,
        reserved=reserved,
        messages=messages,
        query_hash_by_model=query_hashes,
    )

    # Prime: open the upstream connection (walking the fallback chain) before
    # committing to a 200, so an exhausted chain is an honest error status and
    # the reservation is already released by the stream's cleanup.
    try:
        first = await stream.__anext__()
    except StopAsyncIteration:
        _release(reservation_id)
        raise HTTPException(status_code=502, detail="model returned no content")
    except LLMError as exc:
        cache.metrics.bump("misses")
        logger.warning(
            "chat rejected key=%s status=%s upstream=%s",
            key_id,
            exc.status_code,
            exc.message,
        )
        raise HTTPException(
            status_code=exc.status_code, detail=client_error(exc.status_code)
        ) from exc

    async def prefixed() -> AsyncIterator[str]:
        yield first
        async for frame in stream:
            yield frame

    miss_ms = int((time.monotonic() - started) * 1000)
    cache.metrics.bump("misses")
    cache.metrics.bump("upstream_ms", miss_ms)
    return StreamingResponse(prefixed(), media_type="text/event-stream", headers=SSE_HEADERS)


# --- operator endpoints ----------------------------------------------------


@router.get(
    "/cache/stats",
    summary="Cache counters: hit rate, latency, avoided cost",
    tags=["cache"],
)
def cache_stats() -> dict:
    with _session() as session:
        rows = session.execute(
            select(
                func.count().label("rows"),
                func.coalesce(func.sum(ResponseCache.hit_count), 0).label("hits"),
            ).select_from(ResponseCache)
        ).one()
    return {
        "enabled": settings.cache_enabled,
        "scope": settings.cache_scope,
        "hit_mode": settings.cache_hit_mode,
        "ttl_seconds": settings.cache_ttl_seconds,
        "entries": int(rows.rows),
        "total_hits": int(rows.hits),
        "counters": cache.metrics.derived(),
    }


@router.post(
    "/cache/cleanup",
    summary="Drop expired entries and enforce the size cap",
    tags=["cache"],
)
def cache_cleanup() -> dict:
    """Run the sweep now instead of waiting for the next insert.

    Safe to call repeatedly; intended for a cron or a manual admin pass.
    """
    with _session() as session:
        result = cache.purge(session)
    cache.metrics.bump("purged", result["expired_deleted"] + result["evicted"])
    return result
