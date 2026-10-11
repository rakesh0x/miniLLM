"""Chat completions against Groq Cloud, with model-level fallback.

One request flows through three layers:

1. `stream_with_fallback` walks the configured model chain. While the upstream
   rejects a request *before a single token streams* — rate limit, timeout,
   upstream 5xx — the next model is tried immediately, so the caller never sees
   a truncated or spliced-together answer. Once tokens are flowing, a failure
   aborts the stream instead of falling back.
2. Token usage from the provider's final chunk is what billing settles against,
   so a request pays for what it consumed rather than for an estimate.
3. `sse_chunk` encodes the AI SDK v7 UI-message stream (`data: {json}\\n\\n`,
   terminated by `data: [DONE]\\n\\n`) that `AssistantChatTransport` already
   parses on the frontend — the Next proxy stays a passthrough.

The provider is OpenAI-compatible, so this client talks plain
`/chat/completions` over HTTP without pinning a vendor SDK.
"""

import json
import logging
import math
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx

from .config import settings

logger = logging.getLogger(__name__)

# Upstream statuses worth trying the next model for. 429 is the interesting
# one: the *model's* quota is exhausted, which is a capacity problem on the
# provider side, not a bug in the request.
RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504, 529})

# Roles the upstream accepts; anything else in history is dropped, not failed.
ALLOWED_ROLES = ("system", "user", "assistant")


class LLMError(Exception):
    """An upstream chat failure, with enough detail to map onto an HTTP status."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 502,
        retryable: bool = False,
        model: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.model = model


@dataclass
class Usage:
    """Token counts used for metering. `estimated` marks a derived count."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated: bool = False


@dataclass
class Delta:
    """One piece of assistant text."""

    text: str


@dataclass
class Completed:
    """The provider finished; carries the metering facts."""

    model: str
    usage: Usage
    finish_reason: str | None = None


@dataclass
class _StreamState:
    """What one model attempt produced, so the caller can meter it."""

    model: str
    chunks: int = 0
    text: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None

    def estimated_usage(self, prompt_messages: list[dict]) -> Usage:
        """Fallback metering when the provider reports no usage."""
        prompt_tokens = sum(
            estimate_tokens(str(message.get("content") or ""))
            for message in prompt_messages
        )
        completion_tokens = estimate_tokens("".join(self.text))
        return Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            estimated=True,
        )


# ---------------------------------------------------------------------------
# Token metering math
# ---------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """Rough token count (~4 characters per token).

    Only used before any usage is known: to bound the reservation, and as a
    fallback when a provider streams no usage in its final chunk.
    """
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


def credits_for_tokens(total_tokens: int, per_1k: int = 1) -> int:
    """Metered cost of a token count. Rounds up; never charges less than 1."""
    if total_tokens <= 0:
        return 0
    return max(1, math.ceil(total_tokens / 1000 * per_1k))


def estimate_charge(
    messages: list[dict], max_tokens: int, per_1k: int = 1
) -> int:
    """Worst-case cost of a request, reserved before streaming starts.

    Bounds the prompt by the token estimate and the completion by
    `max_tokens`, so the reservation always covers what the provider can bill.
    """
    prompt_tokens = sum(
        estimate_tokens(str(message.get("content") or "")) for message in messages
    )
    return credits_for_tokens(prompt_tokens + max_tokens, per_1k)


# ---------------------------------------------------------------------------
# Request normalization
# ---------------------------------------------------------------------------


def _message_text(content: object, parts: object, max_chars: int) -> str:
    """Flatten a UI message's content into plain text."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    elif isinstance(parts, list):
        text = " ".join(
            str(part.get("text") or "")
            for part in parts
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    else:
        text = ""
    text = text.strip()
    if len(text) > max_chars:
        raise ValueError(f"message exceeds {max_chars} characters")
    return text


def normalize_messages(
    raw_messages: list,
    *,
    max_messages: int = 50,
    max_chars: int = 8000,
    system_prompt: str = "",
) -> list[dict]:
    """Turn transport messages into provider chat messages.

    Keeps the newest `max_messages` non-empty messages in chronological order.
    Non-text parts (images, files, tool calls) are dropped — Groq's text
    models cannot use them, and silently ignoring them beats failing a chat.

    Raises ValueError for a body with no usable text at all.
    """
    flattened: list[dict] = []
    for message in raw_messages[-max_messages:]:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip()
        if role not in ALLOWED_ROLES:
            continue
        text = _message_text(message.get("content"), message.get("parts"), max_chars)
        if not text:
            continue
        flattened.append({"role": role, "content": text})

    if system_prompt:
        flattened.insert(0, {"role": "system", "content": system_prompt})

    if not flattened:
        raise ValueError("no text messages to send to the model")
    return flattened


# ---------------------------------------------------------------------------
# AI SDK v7 UI-message stream encoding
# ---------------------------------------------------------------------------


def sse_chunk(chunk: dict) -> str:
    """One UI-message chunk as an SSE frame."""
    return f"data: {json.dumps(chunk)}\n\n"


SSE_DONE = "data: [DONE]\n\n"

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
    "X-Vercel-AI-UI-Message-Stream": "v1",
}


class MessageStreamBuilder:
    """Builds the UI-message stream for one assistant message.

    The first frame is deliberately withheld until the first text delta
    exists, so the caller can prime the upstream connection and turn a
    pre-stream failure (an exhausted model chain) into an HTTP status rather
    than a mid-stream surprise.
    """

    def __init__(self) -> None:
        self.text_id = f"msg_{uuid.uuid4().hex}"
        self._started = False

    @property
    def started(self) -> bool:
        """True once the first text delta has been framed."""
        return self._started

    def open(self, delta: str) -> str:
        """First text: the message/step/text-start frames plus the delta."""
        self._started = True
        return (
            sse_chunk({"type": "start", "messageId": self.text_id})
            + sse_chunk({"type": "start-step"})
            + sse_chunk({"type": "text-start", "id": self.text_id})
            + sse_chunk({"type": "text-delta", "id": self.text_id, "delta": delta})
        )

    def delta(self, text: str) -> str:
        return sse_chunk({"type": "text-delta", "id": self.text_id, "delta": text})

    def finish(self) -> str:
        frames = ""
        if not self._started:
            self._started = True
            frames += sse_chunk({"type": "start", "messageId": self.text_id})
            frames += sse_chunk({"type": "start-step"})
            frames += sse_chunk({"type": "text-start", "id": self.text_id})
        frames += sse_chunk({"type": "text-end", "id": self.text_id})
        frames += sse_chunk({"type": "finish-step"})
        frames += sse_chunk(
            {"type": "finish", "finishReason": "stop", "messageMetadata": {}}
        )
        return frames + SSE_DONE

    def error(self, message: str) -> str:
        return sse_chunk({"type": "error", "errorText": message})


# ---------------------------------------------------------------------------
# Groq client
# ---------------------------------------------------------------------------


def _map_upstream_status(status_code: int) -> int:
    """Upstream failure -> status the miniLLM client should see."""
    if status_code in (401, 403):
        return 502  # our provider credential is wrong; not the caller's fault
    if status_code == 429:
        return 429  # capacity, surfaced as-is
    if status_code >= 500:
        return 502
    return 400 if 400 <= status_code < 500 else 502


async def _stream_model(
    client: httpx.AsyncClient,
    *,
    model: str,
    messages: list[dict],
    api_key: str,
    base_url: str,
    max_tokens: int,
    temperature: float,
    state: _StreamState,
) -> AsyncIterator[str]:
    """Stream one model's deltas, recording usage into `state`.

    Raises LLMError before yielding anything when the upstream refuses the
    request, which is what makes fallback invisible to the caller.
    """
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        # Groq omits usage from the final chunk unless asked for it.
        "stream_options": {"include_usage": True},
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    async with client.stream("POST", url, json=payload, headers=headers) as response:
        if response.status_code != 200:
            body = (await response.aread()).decode("utf-8", "replace")[:400]
            raise LLMError(
                f"{model} returned HTTP {response.status_code}: {body}",
                status_code=_map_upstream_status(response.status_code),
                retryable=response.status_code in RETRYABLE_STATUS_CODES,
                model=model,
            )

        async for line in response.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if not data:
                continue
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                logger.warning("unparseable stream chunk from %s", model)
                continue

            # Some OpenAI-compatible servers report failures in-band, with an
            # HTTP 200 and an error frame instead of a status code.
            if chunk.get("error"):
                detail = chunk["error"]
                message = (
                    detail.get("message", "unknown error")
                    if isinstance(detail, dict)
                    else str(detail)
                )
                status = (
                    int(detail.get("code"))
                    if isinstance(detail, dict) and str(detail.get("code", "")).isdigit()
                    else 500
                )
                raise LLMError(
                    f"{model} reported an error: {message}",
                    status_code=_map_upstream_status(status),
                    retryable=status in RETRYABLE_STATUS_CODES,
                    model=model,
                )

            usage = chunk.get("usage")
            if usage:
                state.usage = Usage(
                    prompt_tokens=int(usage.get("prompt_tokens") or 0),
                    completion_tokens=int(usage.get("completion_tokens") or 0),
                    total_tokens=int(usage.get("total_tokens") or 0),
                )
            for choice in chunk.get("choices") or []:
                text = (choice.get("delta") or {}).get("content")
                if text:
                    state.chunks += 1
                    state.text.append(text)
                    yield text
                if choice.get("finish_reason"):
                    state.finish_reason = choice.get("finish_reason")


async def stream_with_fallback(
    *,
    messages: list[dict],
    api_key: str,
    base_url: str | None = None,
    timeout_seconds: float | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    chain: list[str] | None = None,
) -> AsyncIterator[Delta | Completed]:
    """Stream a chat answer, degrading across the model chain on failure.

    Yields `Delta` objects as text arrives, then exactly one `Completed`.
    A model that fails before any delta is skipped; once text is flowing the
    first failure aborts the whole request, because splicing two models'
    answers together would corrupt the message.
    """
    models = list(chain if chain is not None else settings.model_chain)
    if not models:
        raise LLMError("no chat models configured", status_code=503)

    timeout = httpx.Timeout(
        timeout_seconds if timeout_seconds is not None else settings.llm_timeout_seconds,
        connect=10.0,
    )
    last_error: LLMError | None = None

    async with httpx.AsyncClient(timeout=timeout) as client:
        for index, model in enumerate(models):
            state = _StreamState(model=model)
            emitted = False
            try:
                async for text in _stream_model(
                    client,
                    model=model,
                    messages=messages,
                    api_key=api_key,
                    base_url=base_url or settings.groq_base_url,
                    max_tokens=(
                        max_tokens if max_tokens is not None else settings.llm_max_tokens
                    ),
                    temperature=(
                        temperature
                        if temperature is not None
                        else settings.llm_temperature
                    ),
                    state=state,
                ):
                    emitted = True
                    yield Delta(text)

                usage = (
                    state.usage
                    if state.usage.total_tokens
                    else state.estimated_usage(messages)
                )
                yield Completed(model=model, usage=usage, finish_reason=state.finish_reason)
                return
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                error = LLMError(
                    f"{model} unreachable: {exc.__class__.__name__}",
                    status_code=504,
                    retryable=True,
                    model=model,
                )
            except LLMError as exc:
                error = exc

            last_error = error
            if emitted:
                # Text is already on the wire; falling back would splice two
                # answers into one message.
                raise error
            if not error.retryable or index == len(models) - 1:
                raise error
            logger.warning(
                "chat model %s unavailable (%s); trying next model in chain",
                model,
                error.message[:200],
            )

    raise LLMError(
        f"all chat models failed; last error: {last_error.message if last_error else 'unknown'}",
        status_code=503,
        retryable=False,
    )
