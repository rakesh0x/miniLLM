# miniLLM

A self-hosted LLM app: a Next.js frontend and a FastAPI backend where **every
API key carries its own credit balance with a TTL**, enforced atomically in
Postgres.

```
miniLLMFE/   Next.js 16 (App Router) chat UI + API-key management dialog
miniLLMBE/   FastAPI service: keys, credits, bearer auth, usage
scripts/     verification suites
```

## Running

```bash
# backend (expects Postgres; see DATABASE_URL in .env)
.venv/bin/python -m uvicorn miniLLMBE.main:app          # :8000

# frontend
cd miniLLMFE && npm run dev                             # :3000
```

`miniLLMBE/.env.example` documents every setting. Postgres settings can be
tuned through `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` / `DB_POOL_RECYCLE`.

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
(`qwen/qwen3-32b,openai/gpt-oss-120b,openai/gpt-oss-20b` by default). A model
that refuses a request *before any token streams* — rate limit, timeout,
upstream 5xx — hands the request to the next model. Once tokens are flowing a
failure aborts instead, because splicing two models' answers would corrupt the
message. The ledger records which model served each request
(`chat:<model>`).

**Metering.** `CREDITS_PER_1K_TOKENS` credits per 1K tokens the provider
reports (prompt + completion), rounded up, minimum 1.

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

## Migration note

There is no migration tooling yet. `init_db()` only creates *missing* tables, so
model changes against an existing `api_keys` table need the DDL applied by hand
(the credit columns were added with `ALTER TABLE … ADD COLUMN IF NOT EXISTS`).
Adding Alembic is the natural next step.

## Known gaps

- The chat UI keeps the plaintext keys you opt into in **browser
  localStorage** (`lib/chat-key-store.ts`). The backend only stores a hash, so
  the alternative is pasting a key on every message. Keys stay in your browser;
  they are sent only as the `Authorization` header on chat requests.
- Chat streams **text only**. Image/file attachments and tool calls in the
  composer are dropped before the request reaches the model.
- The `/keys` endpoints are unauthenticated — they assume a trusted operator.
  Only the metered `/usage/record` and `/chat` paths enforce a bearer token.
- `verify_key` does a read plus a `last_used_at` write on every call; fine at
  this scale, worth batching if it becomes hot.
- Mid-stream failures charge an **estimated** cost, since the provider never
  reported final usage for a stream that died.
