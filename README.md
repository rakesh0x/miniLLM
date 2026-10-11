# miniLLM

A self-hosted LLM gateway: a Next.js frontend and a FastAPI backend where
**every API key carries its own credit balance with a TTL**, metered on real
token usage, enforced atomically in Postgres.

```
miniLLMFE/   Next.js 16 (App Router) chat UI + API-key management dialog
miniLLMBE/   FastAPI service: keys, credits, bearer auth, chat, cache, guardrails
scripts/     verification suites + a database benchmark
```

## Architecture

```
browser ──► miniLLMFE (Next.js)  ──► /api/*  passthrough
                                        │
                                        ▼
                                miniLLMBE (FastAPI)
                                   │            │
                                   │            └──► cache (exact-match, Postgres)
                                   │
                     credit gate ──┤──► reserve ──► stream ──► settle
                                   │
                                   ▼
                            Groq Cloud (SSE)
                                   │
                                   └──► OutputFilter ──► AI SDK v7 SSE ──► browser
```

Two independent guarantees, and they compose:

- **Credits** (`miniLLMBE/credits.py`) — the balance invariant is checked and
  written in a single SQL statement, so two concurrent requests can never both
  spend the same last credit.
- **Cache** (`miniLLMBE/cache.py`) — a hit replays a stored answer *and its
  original charge*, so billing is identical to a fresh request and only the
  upstream call is skipped.

## Running

```bash
# backend (expects Postgres; see DATABASE_URL in miniLLMBE/.env)
.venv/bin/python -m uvicorn miniLLMBE.main:app          # :8000

# frontend
cd miniLLMFE && npm run dev                             # :3000
```

`miniLLMBE/.env.example` documents every setting, with the reasoning for the
ones that matter. `miniLLMFE/.env.example` covers the frontend.

### Where to put the database

Database latency is multiplied by the ~8 statements a chat request issues, so
region choice is the single biggest lever on time-to-first-token:

| region | median query | 12 concurrent chats |
|---|---|---|
| `ap-southeast-1` (Singapore) | 96ms | 6.4s |
| `us-east-2` (Ohio) | 280ms | 25s |
| local Postgres | 0.1ms | 0.1s |

Use the **pooled** endpoint (`-pooler` in the hostname) and check yours any
time: `scripts/bench_db.py "<dsn>"`.

## Credits

```
Available = Allocated − Spent − Reserved
```

| Bucket | Meaning |
| --- | --- |
| `credits_allocated` | Granted by a top-up. A top-up always resets the TTL. |
| `credits_spent` | Permanently consumed. |
| `credits_reserved` | Held for work in flight. Not spendable. |

Two TTLs are involved:

- **Credit TTL** (`CREDIT_TTL_DAYS`) — when it elapses, `available` drops to 0
  regardless of the arithmetic, and usage is rejected with `403`.
- **Reservation TTL** (`RESERVATION_TTL_SECONDS`) — a reservation that never
  settles is reclaimed automatically, so a crashed caller cannot lock credits
  forever. `POST /credits/reclaim-expired` sweeps on demand.

A top-up with a new TTL revives an expired key.

### Atomicity

Every balance mutation is a **single atomic SQL UPDATE with the invariant in
its `WHERE` clause**, so the check and the write happen in one statement:

```sql
UPDATE api_keys
   SET credits_reserved = credits_reserved + :amount
 WHERE id = :id
   AND revoked_at IS NULL
   AND (credits_expire_at IS NULL OR credits_expire_at > now())
   AND (credits_allocated - credits_spent - credits_reserved) >= :amount
```

Zero rows matched means the request is rejected (`402` insufficient, `403`
expired). There is deliberately no read-modify-write, so two concurrent
requests can never both succeed against the same last credit — `SELECT … FOR
UPDATE` is unnecessary.

Reservations settle through a status-guarded update
(`WHERE status = 'pending'`), so a retried `commit` cannot double-charge, and
every movement is appended to `credit_ledger` for audit.

### Metering an endpoint

```python
from .auth import get_api_key, require_credits
from . import credits

@router.post("/thing", dependencies=[Depends(require_credits(cost))])
def create_thing(
    cost: int,
    api_key: ApiKey = Depends(get_api_key),
    session: Session = Depends(get_session),
):
    credits.charge(session, key_id=api_key.id, amount=cost, reference="thing")
```

`require_credits` is a fail-fast shortcut; `credits.charge` /
`credits.reserve` are the enforcement, because they re-check the invariant at
the moment of spending.

For work that can fail or take time, reserve and settle:

```python
rid = credits.reserve(session, key_id=..., amount=cost, reference=job_id)
try:
    do_work()
    credits.commit(session, reservation_id=rid)   # reserved -> spent
except Exception:
    credits.release(session, reservation_id=rid)  # reserved -> available
```

`POST /usage/record` is a documented example of a guarded, credit-charging
endpoint.

### Chat

`POST /chat` is the metered streaming endpoint — the case `charge` cannot
express, because the cost is only known when the stream ends.

```
browser -> /api/chat (Next passthrough) -> POST /chat -> Groq Cloud -> back
```

1. **Reserve an upper bound.** The prompt is bounded by a token estimate and
   the completion by `LLM_MAX_TOKENS`, so the reservation always covers what
   the provider can bill. While held, no concurrent request can spend those
   credits.
2. **Prime the upstream.** No bytes are sent to the client until the model has
   produced text, so an exhausted model chain is an honest `429`/`503` rather
   than a half-written answer.
3. **Settle the exact cost.** The usage the provider reports in its final
   chunk becomes the charge; the unused part of the reservation returns to the
   available pool in the same transaction. When the provider reports no usage,
   the charge falls back to an estimate.
4. **Clean up no matter what.** A failure, or a client that hangs up
   mid-stream, charges an estimate of what was delivered — or releases the
   reservation when nothing was.

```bash
curl -N -X POST http://127.0.0.1:8000/chat \
  -H "Authorization: Bearer <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"hi"}]}'
```

The response is the AI SDK v7 UI-message stream (`start`, `text-start`,
`text-delta`, `text-end`, `finish`, `[DONE]`) — the format
`AssistantChatTransport` already parses, so the Next route only carries bytes.

**Fallback.** `LLM_MODEL_CHAIN` lists models in the order to try
(`qwen/qwen3.8-27b`, `openai/gpt-oss-120b`, `openai/gpt-oss-20b` by default). A model
that refuses a request *before any token streams* — rate limit, timeout,
upstream 5xx — hands the request to the next model. Once tokens are flowing a
failure aborts instead, because splicing two models' answers would corrupt the
message. The ledger records which model served each request
(`chat:<model>`).

**Metering.** `CREDITS_PER_1K_TOKENS` credits per 1K tokens the provider
reports (prompt + completion), rounded up, minimum 1.

### Response cache

A completed answer is stored under a hash of everything that determines it —
scope, model, generation parameters, and the **entire** canonical message list.
On a hit the answer is replayed with the same frames a live stream produces, so
the client cannot tell the difference.

```
miss:  reserve -> stream -> settle exact usage -> store
hit:                  charge the recorded cost -> replay
```

- **Exact match, not semantic.** A semantic tier needs an embedding call on
  every request — including misses — turning a latency win into a guaranteed
  latency cost, and it can confidently return the answer to a *different*
  question. Exact match has neither problem. `cache.build_key` is the seam
  where a semantic tier would be added later.
- **A hit costs what a miss costs** (`CACHE_HIT_MODE=metered`). A free cache
  would let a client repeat one prompt to farm unlimited output. The gateway
  still saves the upstream call, which is the point.
- **Per-API-key scope by default**, so one key's answers stay invisible to
  another even when the prompts are identical.
- **Failures are never cached.** A stream that ends without `[DONE]` was
  truncated, so it is neither charged as final nor stored.
- **Expired entries are invisible** — expiry is enforced in the lookup query
  rather than by a sweeper.

Measure it: `GET /cache/stats` reports hit rate, latency, and the tokens and
credits served from cache.

### Guardrails

Two layers, because a prompt instruction is not access control:

1. **Prompt** — a persona that never names its own model, provider, or
   infrastructure, refuses extraction attempts, and explicitly keeps answering
   ordinary questions normally (the anti-over-refusal half of the problem).
2. **Filter** — every token passes through a redactor before it reaches the
   client *or the cache*, covering credentials, connection strings, and
   self-referential identity claims. The terms come from the configured model
   chain, so changing the chain changes what gets suppressed.

The filter is streaming-safe: it holds back only the trailing window where a
match could still be forming, and `verify_cache.py` proves completeness across
every possible chunk seam. Upstream errors are mapped to generic messages, so
provider names and request bodies never reach a client.

What it is **not**: a general safety filter, or a guarantee. A model can still
describe itself obliquely or in another language. This is a real second layer,
not a proof.

### Endpoints

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/keys` | `{name, credits?, ttl_days?}` — plaintext key returned once |
| `GET` | `/keys` | Summaries include the credit balance |
| `DELETE` | `/keys/{id}` | Soft revoke |
| `GET` | `/keys/{id}/credits` | Balance + expiry |
| `POST` | `/keys/{id}/credits` | Top-up; resets the TTL (`{amount, ttl_days?, expires_at?}`) |
| `POST` | `/keys/{id}/credits/spend` | Immediate spend (`{amount, reference?}`) |
| `GET` | `/keys/{id}/credits/ledger` | Movement history, newest first |
| `POST` | `/usage/record` | Metered demo; `Authorization: Bearer <key>` |
| `POST` | `/chat` | Metered streaming chat (SSE); `Authorization: Bearer <key>` |
| `POST` | `/credits/reclaim-expired` | Release expired reservations |
| `GET` | `/cache/stats` | Hit rate, latency, tokens and credits served from cache |
| `POST` | `/cache/cleanup` | Drop expired entries, enforce the size cap |

Auth: `Authorization: Bearer <api-key>` → `401` invalid/revoked,
`402` insufficient credits, `403` expired credits.

### UI

The chat sidebar's **API keys** entry opens the management dialog: create a key
(with an optional initial grant and TTL), copy it once, revoke it, and expand a
key to see its balance, expiry, a top-up form, a one-credit spend button, and
its ledger.

The chat header has an **API key picker** (`ChatKeyPicker`): it lists the keys
this browser has opted into — saved when a key is created in the dialog, or
pasted — with each key's live balance, and sends the selected key as the bearer
token on every message. With no key selected, chat requests are rejected with
`401`.

## Verifying

```bash
.venv/bin/python -m uvicorn miniLLMBE.main:app &      # backend on :8000
.venv/bin/python scripts/verify_credits.py             # 57 checks
.venv/bin/python scripts/verify_chat.py                # 48 checks
.venv/bin/python scripts/verify_cache.py               # 78 checks
```

The credit suite covers the balance invariant, TTL expiry and revival,
overspend rejection, the ledger, reserve/commit/release, TTL reclaim,
double-settle idempotency, the bearer guard, revocation, and a **40-thread
concurrency race** that must land exactly its last credit — the atomicity proof.

`verify_chat.py` needs no API key and no network: it runs the backend against
a **stub Groq** that rate-limits the first model in the chain and serves the
second. It covers the stream shape, fallback across the chain, exact token
metering, partial settlement of the reservation, estimated usage when the
provider reports none, the rejection paths (402/403/401/422), a fully
rate-limited chain, a client hanging up mid-stream, and chat disabled.

`verify_cache.py` does the same for caching and guardrails: hits and misses,
normalization, per-key isolation, TTL expiry, bypass, billing, failed
generations, 12-way concurrency, and the redactor's completeness across every
chunk seam. It defaults to a private SQLite database; pass `--dsn=<url>` to
run against the real engine (it cleans up the rows it creates).

## Deployment

This is **two services**, and they must both be running for chat to work:

| Service | What | Where it can run |
| --- | --- | --- |
| `miniLLMFE` | Next.js UI + `/api/*` passthrough | Vercel, or any Node host |
| `miniLLMBE` | FastAPI: credits, chat, cache | Any Python host |

The frontend never talks to the database or Groq directly; it forwards
`/api/*` to the backend, so `BACKEND_URL` is the only wiring between them.

### Frontend (Vercel)

```bash
cd miniLLMFE
vercel link                                   # project name: miniLLM
vercel env add BACKEND_URL production         # the backend's public URL
vercel env add NEXT_PUBLIC_API_URL production # shown in the usage snippet
vercel --prod
```

`OPENAI_API_KEY` in the starter `.env.example` is vestigial and can be ignored —
the frontend proxies, it does not call a model.

### Backend

Run it as a long-lived Python process (it holds streaming connections open):

```bash
.venv/bin/python -m uvicorn miniLLMBE.main:app --host 0.0.0.0 --port 8000
```

Required environment: `DATABASE_URL` (use the **pooled** endpoint), and
`GROQ_API_KEY`. CORS must list the frontend origin in `CORS_ORIGINS`.

Serverless platforms are a poor fit without adjustment: the app streams for as
long as the model takes, and the process-local caches (hit counters, eviction
and reclaim throttles) reset on every cold start — they are optimisations, so
nothing breaks, but the counters do.

## Operations

- `GET /cache/stats` — hit rate, average latency per path, tokens and credits
  served from cache. The counters are per process; the request log line is the
  durable signal.
- `POST /cache/cleanup` — drop expired entries and enforce `CACHE_MAX_ENTRIES`.
  Worth scheduling; the request path only sweeps every
  `CACHE_EVICT_EVERY` writes, by design, to keep the write path cheap.
- Credit moves are all in `credit_ledger`. `spent` and `available` must always
  reconcile; if they ever diverge, the ledger is the source of truth.
- `scripts/bench_db.py "<dsn>"` before changing database region.

## Migration note

There is no migration tooling yet. `init_db()` only creates *missing* tables, so
model changes against an existing `api_keys` table need the DDL applied by hand
(the credit columns were added with `ALTER TABLE … ADD COLUMN IF NOT EXISTS`).
Adding Alembic is the natural next step.

## Known gaps

- **Guardrails are a second layer, not a proof.** The persona is an instruction
  and the redactor is a pattern list; neither is access control. A model can
  still describe itself obliquely or in another language.
- The chat UI keeps the plaintext keys you opt into in **browser
  localStorage** (`lib/chat-key-store.ts`). The backend only stores a hash, so
  the alternative is pasting a key on every message. Keys stay in your browser;
  they are sent only as the `Authorization` header on chat requests.
- Chat streams **text only**. Image/file attachments and tool calls in the
  composer are dropped before the request reaches the model.
- The `/keys` and `/cache/*` endpoints are unauthenticated — they assume a
  trusted operator. Only the metered `/usage/record` and `/chat` paths enforce
  a bearer token. **Do not expose the backend to the public internet as-is**;
  put it behind whatever fronts your operator-only endpoints.
- Cache counters, eviction state, and reclaim throttles are per-process, so on
  a multi-process or serverless deployment they reset on restart and are not
  consistent across workers. The request log line is the durable signal.
- The cache write still happens on the request path (2 round trips). Moving it
  to a background task would take it off the critical path; billing settlement
  must stay synchronous either way.
- Mid-stream failures charge an **estimated** cost, since the provider never
  reported final usage for a stream that died.
- The app exits silently if its port is already bound, which makes a stale
  process easy to mistake for the new one. Worth a loud failure.
