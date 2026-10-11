"""Exact-match response cache for chat completions.

**What it is.** A completed answer, keyed by a hash of the request that
produced it. On a miss the request runs normally and the answer is stored; on a
hit the answer is replayed with the same streaming frames, so the client cannot
tell the difference.

**Why exact match, and not semantic.** The alternative — store an embedding of
the prompt and match "similar" ones — needs an embedding call on every request,
*including misses*, which trades a latency win for a guaranteed latency cost,
and it can return a confident wrong answer for a question that merely looks
similar. Exact match has neither problem: no extra upstream call on a miss, and
a hit is by construction the answer to an equivalent request.

**The seam for semantic later.** `build_key` returns a hash; `lookup` matches
on hashes. A semantic tier becomes "also try an embedding index, behind the
same admin controls" without touching the billing or streaming paths.

**Safety properties.**

- *Isolation.* `scope_id` is folded into the hash, so two keys can never share
  an entry even with identical prompts (default `key` scope). A shared `global`
  scope is opt-in, for operators serving identical prompts to many keys.
- *Billing transparency.* An entry records what it cost. A hit replays that
  charge through `credits.charge` — an atomic single-statement guard — so a
  client cannot mine the cache for free answers and the balance behaves
  identically to a fresh request. A key that cannot pay gets the same 402.
- *Only completed answers.* Failures and interrupted streams are never stored,
  so a partial response can never be served as if it were complete.
"""

import hashlib
import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, select

from .config import settings
from .models import ResponseCache

logger = logging.getLogger(__name__)

# Bump to invalidate every existing entry (e.g. after a key-format change).
CACHE_KEY_VERSION = "v1"

_WHITESPACE = re.compile(r"\s+")


# --- key construction ------------------------------------------------------


def canonical_text(text: str) -> str:
    """Fold a message to the form the key is built from.

    Lowercasing and collapsing runs of whitespace, so `"What  is  1 + 1?"` and
    `"what is 1 + 1?"` share a key. Deliberately conservative beyond that:
    punctuation, spacing around operators, and spelling are preserved, so
    `"1 + 1"` and `"1+1"` are treated as different prompts. Two questions that
    differ in punctuation are often different questions, and the cache is only
    safe to share answers between requests that are genuinely equivalent.
    """
    return _WHITESPACE.sub(" ", text).strip().lower()


def canonical_messages(messages: list[dict]) -> list[dict]:
    return [
        {
            "role": str(message.get("role") or "").lower(),
            "content": canonical_text(str(message.get("content") or "")),
        }
        for message in messages
    ]


def scope_id(api_key_id: int, scope: str | None = None) -> str:
    """The isolation scope baked into every key for this request."""
    mode = (scope or settings.cache_scope).strip().lower()
    if mode == "global":
        return "global"
    return f"key:{api_key_id}"


def build_key(
    *,
    api_key_id: int,
    model: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
    scope: str | None = None,
) -> str:
    """Hash of everything that determines the answer.

    The *entire* message list is included, not just the last turn: a response
    is only valid for the context that produced it, so a keyed-on-last-turn
    cache would serve answers that ignore the conversation.
    """
    payload = {
        "v": CACHE_KEY_VERSION,
        "scope": scope_id(api_key_id, scope),
        "model": model,
        "params": {"temperature": round(float(temperature), 3), "max_tokens": int(max_tokens)},
        "messages": canonical_messages(messages),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def candidate_keys(
    *,
    api_key_id: int,
    chain: list[str],
    messages: list[dict],
    temperature: float,
    max_tokens: int,
    scope: str | None = None,
) -> list[tuple[str, str]]:
    """`(model, hash)` for every model that could serve this request, in chain
    order. A hit on a later model means the primary was unavailable last time
    and the fallback answered — worth reusing, since it is an approved
    substitute for that request."""
    return [
        (
            model,
            build_key(
                api_key_id=api_key_id,
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                scope=scope,
            ),
        )
        for model in chain
    ]


# --- lookup / store --------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _record_hit(session: Session, row: ResponseCache) -> None:
    """Count the hit and stamp it, in one atomic statement.

    A core UPDATE rather than a read-modify-write, so concurrent hits cannot
    lose a count or overwrite each other. `populate_existing` reloads the row
    afterwards: core statements bypass the ORM identity map, so without it the
    caller would see the pre-update `hit_count`.
    """
    session.execute(
        update(ResponseCache)
        .where(ResponseCache.id == row.id)
        .values(
            hit_count=ResponseCache.hit_count + 1,
            last_hit_at=_now(),
        )
    )
    session.commit()
    session.refresh(row)


def lookup(session: Session, candidates: list[tuple[str, str]]) -> ResponseCache | None:
    """Newest unexpired entry for the first candidate that has one.

    Expiry is enforced in the query (`expires_at > now`), not by a sweeper, so
    an expired entry is simply invisible.
    """
    if not candidates:
        return None
    hashes = [hash_ for _, hash_ in candidates]
    rows = session.exec(
        select(ResponseCache).where(ResponseCache.query_hash.in_(hashes))
    ).all()
    by_hash = {row.query_hash: row for row in rows}
    for _, hash_ in candidates:
        row = by_hash.get(hash_)
        if row is not None and row.expires_at > _now():
            _record_hit(session, row)
            return row
    return None


def _upsert(session: Session, values: dict) -> None:
    """Insert, or refresh the entry that already holds this key.

    Dialect-specific ON CONFLICT because both backends need it and the ORM
    gives no portable form; the fallback keeps a plain dialect working.
    """
    from sqlalchemy import update as sa_update

    try:
        dialect = session.bind.dialect.name
    except Exception:  # pragma: no cover - defensive
        dialect = ""

    if dialect == "postgresql":
        stmt = pg_insert(ResponseCache).values(**values)
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=["query_hash"],
                set_={
                    key: getattr(stmt.excluded, key)
                    for key in values
                    if key != "query_hash"
                },
            )
        )
    elif dialect == "sqlite":
        stmt = sqlite_insert(ResponseCache).values(**values)
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=["query_hash"],
                set_={
                    key: getattr(stmt.excluded, key)
                    for key in values
                    if key != "query_hash"
                },
            )
        )
    else:  
        session.execute(
            sa_update(ResponseCache)
            .where(ResponseCache.query_hash == values["query_hash"])
            .values(**{k: v for k, v in values.items() if k != "query_hash"})
        )
    session.commit()

def store(
    session: Session,
    *,
    query_hash: str,
    api_key_id: int,
    model: str,
    response_text: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    credits_charged: int,
) -> bool:
    """Persist a completed answer. False when it is not worth storing.

    Refuses empty and over-long responses: an empty answer is a degenerate
    result worth re-generating rather than replaying, and a very long one would
    dominate the table for a single key.

    Cap enforcement is throttled — see `_maybe_evict`. On a high-latency
    database the sweep costs several round trips, which would make every cache
    write as expensive as the miss it avoids saving.
    """
    if not response_text:
        return False
    if len(response_text) > settings.cache_max_response_chars:
        return False

    now = _now()
    _upsert(
        session,
        {
            "query_hash": query_hash,
            "api_key_id": api_key_id,
            "model": model[:100],
            "response_text": response_text,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "credits_charged": credits_charged,
            "hit_count": 0,
            "created_at": now,
            "last_hit_at": None,
            "expires_at": now + timedelta(seconds=settings.cache_ttl_seconds),
        },
    )
    _maybe_evict(session, api_key_id)
    return True


# Store counter for eviction throttling. Thread-local and in-process on purpose:
# it bounds work, it does not need to be exact, and a shared counter would cost
# a round trip of its own.
_evict_state = threading.local()


def _maybe_evict(session: Session, api_key_id: int) -> None:
    """Enforce the per-key cap, but only every `cache_evict_every` stores.

    A sweep costs several queries, and on a remote database each is a round
    trip. Running it per write would make a cache store cost more than the miss
    it saves. `POST /cache/cleanup` forces the same sweep on demand, so a cron
    keeps tables tight without slowing the request path.
    """
    every = settings.cache_evict_every
    if every <= 0:
        return
    seen = getattr(_evict_state, "stores", 0)
    _evict_state.stores = seen + 1
    if seen % every:
        return
    _evict_for_key(session, api_key_id)


def _evict_for_key(session: Session, api_key_id: int) -> None:
    """Keep one key's footprint inside `cache_max_entries_per_key`.

    Least-recently-hit first, never-hit before used, so a key's active prompts
    survive and stale ones go. Expired rows are dropped as a side effect.
    """
    limit = settings.cache_max_entries_per_key
    if limit <= 0:
        return
    stale = session.exec(
        select(ResponseCache.id)
        .where(ResponseCache.api_key_id == api_key_id)
        .where(ResponseCache.expires_at <= _now())
    ).all()
    if stale:
        session.execute(delete(ResponseCache).where(ResponseCache.id.in_(stale)))
        session.commit()

    over = session.execute(
        select(func.count())
        .select_from(ResponseCache)
        .where(ResponseCache.api_key_id == api_key_id)
    ).scalar_one()
    excess = over - limit
    if excess <= 0:
        return
    victims = session.exec(
        select(ResponseCache.id)
        .where(ResponseCache.api_key_id == api_key_id)
        .order_by(
            ResponseCache.last_hit_at.is_(None).desc(),
            ResponseCache.last_hit_at.asc(),
            ResponseCache.created_at.asc(),
        )
        .limit(excess)
    ).all()
    if victims:
        session.execute(delete(ResponseCache).where(ResponseCache.id.in_(victims)))
        session.commit()


def purge(session: Session) -> dict[str, int]:
    """Drop expired rows, then enforce the global cap. Returns counts."""
    now = _now()
    expired = session.execute(
        delete(ResponseCache).where(ResponseCache.expires_at <= now)
    ).rowcount
    session.commit()

    cap = settings.cache_max_entries
    evicted = 0
    if cap > 0:
        total = session.execute(select(func.count()).select_from(ResponseCache)).scalar_one()
        excess = total - cap
        if excess > 0:
            victims = session.exec(
                select(ResponseCache.id)
                .order_by(
                    ResponseCache.last_hit_at.is_(None).desc(),
                    ResponseCache.last_hit_at.asc(),
                    ResponseCache.created_at.asc(),
                )
                .limit(excess)
            ).all()
            if victims:
                evicted = session.execute(
                    delete(ResponseCache).where(ResponseCache.id.in_(victims))
                ).rowcount
                session.commit()
    return {"expired_deleted": expired or 0, "evicted": evicted or 0}


# --- replay ----------------------------------------------------------------


def chunk_text(text: str, size: int = 32) -> list[str]:
    """Split stored text into stream-sized pieces, on word boundaries.

    Word boundaries only where one is nearby, so markdown and code keep their
    structure and the reassembled text is byte-identical to what was stored.
    """
    if not text:
        return []
    pieces: list[str] = []
    index = 0
    while index < len(text):
        end = index + size
        if end >= len(text):
            pieces.append(text[index:])
            break
        boundary = text.rfind(" ", index, end)
        if boundary <= index:
            pieces.append(text[index:end])
            index = end
        else:
            pieces.append(text[index : boundary + 1])
            index = boundary + 1
    return pieces


# --- metrics ---------------------------------------------------------------


class Metrics:
    """In-process counters for hit rate, latency, and avoided cost.

    Deliberately in-process: a single gateway process is the deployment today.
    With multiple workers these become per-process, so the documented route to
    shared counters is the log line (one per request) rather than this object.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters = {
            "requests": 0,
            "hits": 0,
            "misses": 0,
            "stores": 0,
            "store_skips": 0,
            "bypasses": 0,
            "purged": 0,
            "redactions": 0,
            "tokens_saved": 0,
            "credits_served": 0,
            "upstream_ms": 0,
            "cache_ms": 0,
        }

    def bump(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counters)

    def derived(self) -> dict[str, object]:
        """Rates a dashboard wants, computed from the raw counters."""
        counters = self.snapshot()
        looked_up = counters["hits"] + counters["misses"]
        hit_rate = (counters["hits"] / looked_up) if looked_up else 0.0
        upstream_avg = (
            counters["upstream_ms"] / counters["misses"] if counters["misses"] else 0.0
        )
        cache_avg = counters["cache_ms"] / counters["hits"] if counters["hits"] else 0.0
        return {
            **counters,
            "hit_rate": round(hit_rate, 4),
            "avg_upstream_ms": round(upstream_avg, 1),
            "avg_cache_ms": round(cache_avg, 1),
            "latency_saved_ms_estimate": round(
                (upstream_avg - cache_avg) * counters["hits"], 1
            ),
        }


metrics = Metrics()
