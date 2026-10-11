"""`POST /chat` — token-metered chat against Groq Cloud.

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

Chain: client -> Next proxy (passthrough) -> here -> Groq Cloud -> back.
"""

import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from . import credits
from .auth import get_api_key
from .config import settings
from .db import Session, engine
from .errors import CreditsExpired, InsufficientCredits
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
from .models import ApiKey

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


async def _ui_stream(
    *,
    key_id: int,
    reservation_id: int,
    reserved: int,
    messages: list[dict],
) -> AsyncIterator[str]:
    """Stream the answer as UI-message SSE, settling credits exactly once.

    Every exit path is accounted for: success settles the reported usage, an
    error after text started charges an estimate of what was delivered, and a
    failure with nothing delivered releases the reservation untouched.
    """
    builder = MessageStreamBuilder()
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
                yield builder.finish()
                return

            delivered.append(event.text)
            if builder.started:
                yield builder.delta(event.text)
            else:
                yield builder.open(event.text)
    except LLMError as exc:
        if builder.started:
            # Text is already on the wire: report the failure in-stream.
            logger.error("chat stream failed mid-answer: %s", exc.message)
            yield builder.error(exc.message)
            return
        raise
    finally:
        if not settled:
            cleanup()


@router.post(
    "/chat",
    summary="Token-metered chat completion (SSE, AI SDK UI-message stream)",
    response_description="AI SDK v7 UI-message stream; credits metered on usage",
)
async def create_chat_completion(
    payload: ChatRequest,
    api_key: ApiKey = Depends(get_api_key),
) -> StreamingResponse:
    if not settings.groq_api_key:
        raise HTTPException(status_code=503, detail="chat is not configured")

    try:
        messages = normalize_messages(
            [message.model_dump() for message in payload.messages],
            max_messages=settings.llm_max_history_messages,
            max_chars=settings.llm_max_message_chars,
            system_prompt=settings.llm_system_prompt,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Fail fast, exactly like require_credits but metered on the estimate.
    reserved = estimate_charge(
        messages, settings.llm_max_tokens, settings.credits_per_1k_tokens
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

    key_id = api_key.id or 0
    with _session() as session:
        reservation_id = await run_in_threadpool(
            credits.reserve,
            session,
            key_id=key_id,
            amount=reserved,
            reference="chat",
        )

    stream = _ui_stream(
        key_id=key_id,
        reservation_id=reservation_id,
        reserved=reserved,
        messages=messages,
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
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    async def prefixed() -> AsyncIterator[str]:
        yield first
        async for frame in stream:
            yield frame

    return StreamingResponse(prefixed(), media_type="text/event-stream", headers=SSE_HEADERS)
