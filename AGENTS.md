# miniLLM — Codebase Index & Context

> Working index of the whole repo: what we're building, every page, every route,
> every module, and the invariants that must not be broken.
> Last indexed: 2026-10-11.

---

## 1. What we're building

**miniLLM** is a self-hosted LLM app with a credit-metered API:

- A **Next.js 16 (App Router)** chat UI, built on the assistant-ui starter
  (`@assistant-ui/react` + Vercel AI SDK v7), plus an in-app **API key
  management dialog**.
- A **FastAPI** backend where **every API key carries its own credit balance
  with a TTL**, enforced atomically in SQL (Postgres).
- **`POST /chat`** streams Groq Cloud answers with **token-metered billing**,
  falling back across a model chain when a model is rate-limited.

The chat surface and the credit system are both live: the credit engine in
`miniLLMBE/credits.py` is the invariant core, and `miniLLMBE/llm.py` +
`miniLLMBE/chat.py` are the metered-streaming reference implementation.

```
miniLLMFE/   Next.js 16 chat UI + API-key management dialog (port 3000)
miniLLMBE/   FastAPI service: keys, credits, bearer auth, usage (port 8000)
scripts/     verification suite (verify_credits.py)
.venv/       Python env — always run as `.venv/bin/python`
```

---

## 2. Page Index (Frontend)

There is exactly **one UI page**; everything else is API routes.

### 2.1 UI pages

| Route | File | What it is |
| --- | --- | --- |
| `/` | `miniLLMFE/app/page.tsx` | Renders `<Assistant />`. The only screen. |

`app/page.tsx` is 5 lines; all UI lives in `app/assistant.tsx`.

**`/` composition** (`miniLLMFE/app/assistant.tsx`, `"use client"`):

| Region | Component | File |
| --- | --- | --- |
| Runtime | `useChatRuntime` + `AssistantChatTransport({ api: "/api/chat" })` — sends the selected key as `Authorization: Bearer` | `app/assistant.tsx` |
| **Chat header** | **`ChatKeyPicker`** ← picks the key a message is billed to | `components/assistant-ui/elements/chat-key-picker.tsx` |
| Sidebar shell | `ThreadListSidebar` | `components/assistant-ui/elements/threadlist-sidebar.aui.tsx` |
| Sidebar header | Brand "miniLLM" → links to assistant-ui.com | same file |
| Sidebar content | `ThreadList` (new / search / rename / archive / delete) | `components/assistant-ui/elements/thread-list.aui.tsx` |
| **Sidebar footer** | **`ApiKeysDialog`** | `components/assistant-ui/elements/api-keys.aui.tsx` |
| Sidebar footer | GitHub link | same file |
| Header bar | `SidebarTrigger` + breadcrumb (starter leftovers) | `app/assistant.tsx` |
| Main pane | `Thread` — welcome/suggestions, messages, markdown, reasoning, attachments, tools, composer | `components/assistant-ui/elements/thread.aui.tsx` |

`Thread` renders (in order): welcome screen → history skeleton → earlier-messages
loader → message list → scroll-to-bottom → follow-up suggestions → composer →
empty-state suggestions. Sub-elements live in `components/assistant-ui/elements/`
(`markdown-text.tsx`, `reasoning.tsx`, `attachment.aui.tsx`, `file.tsx`,
`image.tsx`, `media-player.tsx`, `tool-group.aui.tsx`, `tool-fallback.aui.tsx`,
`surfaces.tsx`, `follow-up-suggestions.aui.tsx`, `collapsible-root.tsx`).
Design system: Tailwind v4 tokens in `app/globals.css`; shadcn-style primitives in
`components/ui/` (`sidebar.tsx`, `dialog.tsx`, `button.tsx`, `input.tsx`,
`sheet.tsx`, `avatar.tsx`, `tooltip.tsx`, `separator.tsx`, `skeleton.tsx`,
`textarea.tsx`, `breadcrumb.tsx`). Hooks: `hooks/use-copy-to-clipboard.ts`,
`hooks/use-attachment-src.ts`, `hooks/use-mobile.ts`.

**Thread persistence:** none. `useChatRuntime` is called with no `cloud` and no
`NEXT_PUBLIC_ASSISTANT_BASE_URL`, so assistant-ui falls back to an
**in-memory thread list** — chats vanish on reload. Cloud persistence is the
commented-out `NEXT_PUBLIC_ASSISTANT_BASE_URL` in `miniLLMFE/.env.example`.

### 2.2 The API keys dialog (`api-keys.aui.tsx`, 611 lines)

The one screen worth studying. Trigger: sidebar footer "API keys" (subtitle shows
`N active · M credits`).

- **Create key** — name (required), initial credits + TTL days (optional) →
  `POST /api/keys`. Plaintext key is shown **once** in a destructive-styled
  warning panel with copy/dismiss.
- **Key row** — name, Active/Revoked badge (+ "Credits expired" badge), masked
  key `mnl_••••••••abcd`, `created · last-used · N credits`, two-step revoke
  confirm, chevron to expand.
- **CreditsPanel** (expanded row) — `Available` big number + Allocated/Spent/
  Reserved, expiry line ("Expires 10 Nov 2025 · 30d left" / `text-destructive`
  when expired), top-up form (amount + TTL days), **Spend 1** button, and the
  last 6 ledger entries (`allocate`=Top-up, `reserve`=Reserved, `commit`=Spent,
  `release`=Released, `expire`=Expired, `charge`=Usage; green `+` for
  allocate/release/expire, red `-` otherwise; `N left` per row).
- **Usage block** — a curl snippet built from `NEXT_PUBLIC_API_URL` (default
  `http://127.0.0.1:8000`) showing `GET /keys/<id>/credits` and
  `POST /usage/record` with a bearer token.
- TS types `ApiKeySummary`, `CreditBalance`, `LedgerEntry` are **hand-duplicated**
  from the backend Pydantic schemas — a drift risk, not a shared contract.

### 2.3 Frontend API routes (all thin proxies to the backend)

Every route proxies to `${process.env.BACKEND_URL ?? "http://127.0.0.1:8000"}`
with `cache: "no-store"` and passes the upstream status through. JSON parse
failures degrade to `null` / `{}` / `[]` depending on the route.

| Method | Route | File | Upstream | Notes |
| --- | --- | --- | --- | --- |
| POST | `/api/chat` | `app/api/chat/route.ts` | `POST /chat` | SSE passthrough; forwards `Authorization` and the raw AI SDK body. `dynamic = "force-dynamic"`. |
| GET | `/api/keys` | `app/api/keys/route.ts` | `GET /keys` | Key list with balances. `force-dynamic`. |
| POST | `/api/keys` | `app/api/keys/route.ts` | `POST /keys` | Forwards `{name, credits?, ttl_days?}`. |
| DELETE | `/api/keys/[id]` | `app/api/keys/[id]/route.ts` | `DELETE /keys/{id}` | Soft revoke. |
| GET | `/api/keys/[id]/credits` | `app/api/keys/[id]/credits/route.ts` | `GET /keys/{id}/credits` | Balance + expiry. |
| POST | `/api/keys/[id]/credits` | `app/api/keys/[id]/credits/route.ts` | `POST /keys/{id}/credits` | Top-up; resets TTL. |
| POST | `/api/keys/[id]/credits/spend` | `app/api/keys/[id]/credits/spend/route.ts` | `POST /keys/{id}/credits/spend` | Immediate spend. |
| GET | `/api/keys/[id]/credits/ledger` | `app/api/keys/[id]/credits/ledger/route.ts` | `GET /keys/{id}/credits/ledger` | Movement history. |

---

## 3. Backend Index (`miniLLMBE/`)

| File | Lines | Role |
| --- | --- | --- |
| `main.py` | 35 | FastAPI app "miniLLM API"; CORS from `settings.cors_origins`; `lifespan` runs `init_db()`; mounts `keys` + `usage` routers; `GET /health`. |
| `config.py` | 31 | `Settings` (pydantic-settings, `.env`, `extra="ignore"`). Defaults: `database_url=postgresql+psycopg://rakesh@127.0.0.1:5432/minillm`, `key_prefix=mnl_`, `cors_origins=http://localhost:3000`, `db_pool_size=5`, `db_max_overflow=10`, `db_pool_timeout=30`, `db_pool_recycle=1800`, `credit_ttl_days=30`, `reservation_ttl_seconds=600`, `reservation_reclaim_limit=100`, `credits_per_usage=1`, `default_key_credits=0`, `default_key_ttl_days=30`. **LLM:** `groq_api_key=""`, `groq_base_url=https://api.groq.com/openai/v1`, `llm_model_chain=qwen/qwen3.8-27b,openai/gpt-oss-120b,openai/gpt-oss-20b`, `llm_max_tokens=1024`, `llm_temperature=0.7`, `llm_timeout_seconds=60`, `credits_per_1k_tokens=1`, `llm_max_history_messages=50`, `llm_max_message_chars=8000`, `llm_system_prompt=""`. |
| `db.py` | 46 | `create_db_engine` per dialect: SQLite gets `check_same_thread=False`; anything else gets `pool_pre_ping` + pool tuning. Module-level `engine`. `init_db()` = `SQLModel.metadata.create_all` (**creates missing tables only — no migrations**). `get_session` dependency. |
| `models.py` | 87 | `ApiKey`, `CreditLedger`, `CreditReservation` (SQLModel). Derived props: `credits_expired`, `credits_available`, `is_active`. |
| `keys.py` | 61 | `hash_key` (SHA-256), `generate_key` (`mnl_` + `token_urlsafe(32)`), `create_api_key` (optional initial grant via `credits.allocate`), `verify_key` (hash lookup, reject revoked, stamps `last_used_at`, commits). |
| `auth.py` | 78 | `get_api_key` (`Authorization: Bearer <key>` → 401), `require_credits(cost)` fail-fast dependency (402/403), `usage_cost()`. |
| `credits.py` | 420 | **The core.** `allocate`, `charge`, `reserve`, `commit`, `release`, `reclaim_expired`, `_settle`, `_reject`, `to_credit_balance`. |
| `router.py` | 162 | `APIRouter(prefix="/keys")`: key CRUD + credit endpoints. `to_summary` builds `masked_key`. `fresh_key` = `session.get(..., populate_existing=True)`. |
| `usage.py` | 74 | `POST /usage/record` (the reference metered endpoint), `POST /credits/reclaim-expired`. |
| `schemas.py` | 75 | `KeySummary`, `CreatedKey`, `CreditBalance`, `AllocateCreditsRequest`, `SpendCreditsRequest`, `CreateKeyRequest`, `LedgerEntry`, `UsageResponse`, `ReclaimResponse`. |
| `errors.py` | 26 | `InsufficientCredits` → 402, `CreditsExpired` → 403, `ReservationNotFound` → 404. |
| `llm.py` | ~400 | **Groq client.** `stream_with_fallback` walks `LLM_MODEL_CHAIN`: a model that fails *before any token streams* (429, timeout, 5xx) hands off to the next; once tokens flow, a failure aborts. Also holds the metering math (`estimate_tokens`, `credits_for_tokens`, `estimate_charge`), `normalize_messages`, and the AI SDK v7 SSE encoder (`MessageStreamBuilder`, `SSE_HEADERS`). |
| `chat.py` | ~250 | **`POST /chat`** — metered streaming: reserve an upper bound → prime the upstream → stream UI-message frames → settle the exact reported usage (partial commit) → release/estimate on every failure path. |
| `requirements.txt` | — | `fastapi`, `uvicorn[standard]`, `sqlmodel`, `pydantic-settings`, `psycopg[binary]`, `httpx` (streaming client + suites). |

### 3.1 HTTP surface

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/health` | — | `{"status": "ok"}` |
| POST | `/keys` | none (operator-trusted) | `{name, credits?, ttl_days?}` → 201 `{key, plaintext_key}` — plaintext returned **once** |
| GET | `/keys` | none | Summaries with balance, newest first |
| DELETE | `/keys/{id}` | none | Soft revoke (`revoked_at`), 204 |
| GET | `/keys/{id}/credits` | none | `CreditBalance` |
| POST | `/keys/{id}/credits` | none | Top-up `{amount, ttl_days?, expires_at?}` → new `CreditBalance`; resets TTL |
| POST | `/keys/{id}/credits/spend` | none | Immediate spend `{amount=1, reference?}` → new `CreditBalance` |
| GET | `/keys/{id}/credits/ledger` | none | ≤200 entries, newest first |
| POST | `/usage/record` | **Bearer** + `require_credits(credits_per_usage)` | Demo metered endpoint → `UsageResponse` |
| POST | `/chat` | **Bearer** | Metered streaming chat → AI SDK v7 SSE. Reserves an upper bound, streams, settles exact token usage. 503 if `groq_api_key` is unset. |
| POST | `/credits/reclaim-expired` | none | `ReclaimResponse {reclaimed}` |

Status codes: `401` invalid/revoked/missing token, `402` insufficient credits,
`403` expired credits, `404` unknown key, `409` reservation/balance desync,
`422` validation, `429`/`503` model chain exhausted.

### 3.2 The credit model (must not be broken)

```
Available = Allocated − Spent − Reserved        (derived, never stored)
```

| Bucket | Meaning |
| --- | --- |
| `credits_allocated` | Granted by a top-up. A top-up always resets the TTL. |
| `credits_spent` | Permanently consumed. |
| `credits_reserved` | Held for in-flight work. Not spendable. |

Two TTLs: **credit TTL** (`CREDIT_TTL_DAYS`) — once elapsed, `available` is 0
regardless of arithmetic and usage is rejected `403`; **reservation TTL**
(`RESERVATION_TTL_SECONDS`) — unsettled reservations are reclaimed so a crashed
caller can't lock credits forever. A top-up with a new TTL revives an expired key.

**Atomicity rule:** every balance mutation is *one* Core `UPDATE ... WHERE` whose
predicate re-checks the invariant — the check and the write are a single
statement, so no `SELECT ... FOR UPDATE` is needed and no read-modify-write race
exists:

```sql
UPDATE api_keys
   SET credits_reserved = credits_reserved + :amount
 WHERE id = :id
   AND revoked_at IS NULL
   AND (credits_expire_at IS NULL OR credits_expire_at > now())
   AND (credits_allocated - credits_spent - credits_reserved) >= :amount
```

Zero rows matched → `_reject()` rolls back, re-reads with `populate_existing`,
and raises 402 or 403 based on `credits_expired`.

**Settlement rule:** `_settle` updates `credit_reservations ... WHERE status =
'pending'` with `RETURNING`, so a retried `commit`/`release` is an idempotent
no-op. `commit` moves `reserved → spent`; `release`/`expire` move
`reserved → available` (both guarded by `credits_reserved >= amount`, with a 409
if a reservation outruns the balance). Every movement appends a
`credit_ledger` row: kinds `allocate | reserve | commit | release | expire |
charge`, each carrying `available_after`.

`reclaim_expired` sweeps `status='pending' AND expires_at <= now()` ordered by
expiry, bounded by `reservation_reclaim_limit`, and does **not** commit (runs
inside the caller's transaction). `MAX_AMOUNT = 1_000_000`; non-positive/non-int
amounts → 422.

**Partial settlement** (`credits.commit(..., amount=N)`): settles part of a
reservation — `N` credits move `reserved → spent`, the remainder returns to
`available` in the *same* transaction, with a release ledger row and a commit
ledger row. This is what metered streaming needs: reserve the worst case, pay
for what was actually used. `amount` defaults to the whole reservation, so
every existing caller is unchanged.

### 3.3 Chat lifecycle (`chat.py` — the metered-streaming reference)

```
reserve(upper bound) -> prime upstream -> stream frames -> settle exact usage
```

| Phase | What happens |
| --- | --- |
| Meter the estimate | `estimate_charge` = estimated prompt tokens + `llm_max_tokens`, rounded up to credits |
| Fail fast | `credits_expired` → 403; `credits_available < estimate` → 402 |
| Reserve | `credits.reserve(amount=estimate)`, so no concurrent request can spend it |
| Prime | First stream frame is withheld until the model produces text, so a dead chain is an HTTP error, not a half-answer |
| Stream | `MessageStreamBuilder` emits AI SDK v7 UI-message frames (`start` → `start-step` → `text-start` → `text-delta`… → `text-end` → `finish-step` → `finish` → `[DONE]`) |
| Settle | Usage from the provider's final chunk → `credits.commit(amount=exact)`; the remainder is released |
| Cleanup | Failure or client hang-up charges an estimate of delivered text, or releases if nothing was delivered |

Fallback rule in `llm.stream_with_fallback`: only **before the first token**.
A model that 429s / times out / 5xxs hands off to the next in `LLM_MODEL_CHAIN`
(same provider, different model). Once tokens are flowing, a failure aborts —
the client gets an `error` frame — because splicing two models' answers into
one message would corrupt it. The ledger records the serving model
(`chat:<model>`), so a fallback is visible in the audit trail.

### 3.4 Data model (Postgres `minillm`, live)

- `api_keys` — `id, name, key_hash (unique, indexed), key_prefix, last_four,
  created_at, last_used_at, revoked_at, credits_allocated, credits_spent,
  credits_reserved, credits_expire_at`
- `credit_ledger` — `api_key_id→api_keys.id, kind, amount, available_after,
  reference (≤200), created_at`
- `credit_reservations` — `api_key_id→api_keys.id, amount, reference, status
  (pending/committed/released/expired), created_at, expires_at, settled_at`

Current state: 3 keys in `api_keys` (2 revoked), empty ledger/reservations.

---

## 4. Run & Verify

```bash
# backend — expects Postgres (see DATABASE_URL in miniLLMBE/.env.example)
.venv/bin/python -m uvicorn miniLLMBE.main:app          # :8000

# frontend
cd miniLLMFE && npm run dev                              # :3000

# verification (credits needs the backend running; chat spawns its own)
.venv/bin/python scripts/verify_credits.py              # 57 checks, 11 sections
.venv/bin/python scripts/verify_chat.py                 # 48 checks
```

`scripts/verify_credits.py` hits the live server over httpx and covers: balance
math, charge/top-up, overspend rejection, TTL expiry, top-up revival, ledger
reconciliation, a **40-thread concurrency race on 25 credits that must land
exactly its last credit** (the atomicity proof), reserve/commit/release,
double-settle idempotency, reservation TTL reclaim, the bearer guard, and
`last_used_at` stamping.

`scripts/verify_chat.py` needs **no API key and no network**: it boots the
backend against a **stub Groq** that rate-limits the first model in the chain
and serves the second. It covers the AI SDK stream shape, fallback across the
chain, exact token metering, partial settlement of the reservation, estimated
usage when the provider reports none, the rejection paths (402/403/401/422), a
fully rate-limited chain, a client hanging up mid-stream, and chat disabled.

Tooling: Python deps live in `.venv` (already installed). Frontend uses
**oxlint/oxfmt** (not ESLint/Prettier): `npm run lint`, `npm run lint:fix`,
`npm run format`. Note: 31 pre-existing FE files already fail `oxfmt --check`;
format only the files you touch, or the diff explodes.

---

## 5. Known Gaps & Traps

1. **Plaintext keys live in browser localStorage.** The chat key picker
   (`lib/chat-key-store.ts`) keeps keys the user opts into, because the backend
   only stores a hash and the bearer token cannot be recovered later. They
   never leave the browser except as the `Authorization` header. The API keys
   dialog saves a key on create; anything older must be pasted once.
2. **Chat is text-only.** Image/file/tool parts in the composer are dropped by
   `normalize_messages` before the request reaches Groq.
3. **`/keys` endpoints are unauthenticated.** They assume a trusted operator;
   only `/usage/record` and `/chat` enforce a bearer token.
4. **No migration tooling.** `init_db()` only creates *missing* tables; schema
   changes to existing tables need hand-applied DDL. Alembic is the suggested
   next step.
5. **Threads are in-memory.** No persistence adapter; every reload starts a
   blank thread list.
6. **Mid-stream failures charge an estimate**, since the provider never
   reported final usage for a stream that died.
7. **Stale `minillm.db`** — an untracked SQLite file with the pre-credits
   `api_keys` schema sitting in the repo root. `.gitignore` covers
   `.minillm.db/` but not `minillm.db`. The real database is Postgres `minillm`.
8. **`verify_key` writes on every call** — read + `last_used_at` update per
   authenticated request; fine now, worth batching later.
9. **Type drift** — the FE redefines backend schemas by hand in
   `api-keys.aui.tsx`.
10. **`__pycache__/*.pyc` are tracked in git** — they keep showing up as
    modified when Python runs. Worth `git rm -r --cached` + `.gitignore`.
11. **`miniLLMFE/AGENTS.md` is tool-generated** by `next dev` (Next.js agent
    rules) — regenerate on dev, and commit it rather than ignore it.

---

## 6. Frontend files added for chat

| File | Role |
| --- | --- |
| `miniLLMFE/lib/chat-key-store.ts` | Plaintext chat keys in localStorage (`loadChatKeys`, `saveChatKey`, `removeChatKey`, `useChatKeys`, selected-id persistence). |
| `miniLLMFE/components/assistant-ui/elements/chat-key-picker.tsx` | Header dropdown: lists opt-in keys with live balances from `/api/keys`, selects the bearer key, pastes a new one (matched to its id by prefix + last four), revokes/forgets. |
| `miniLLMFE/app/assistant.tsx` | Holds the selected key in a ref and sends it as `Authorization: Bearer` per request via the transport's `headers` function; restores the previous selection on mount. |
| `miniLLMFE/app/api/chat/route.ts` | Passthrough: forwards `Authorization` + the raw AI SDK body to `POST /chat`, streams SSE back, and surfaces pre-stream failures (402/403/429/503) as JSON with the real status. |
| `miniLLMFE/app/api/keys/route.ts` | Forwards the full `{name, credits?, ttl_days?}` body (it used to drop credits/TTL). |
| `miniLLMFE/components/assistant-ui/elements/api-keys.aui.tsx` | Saves a newly created key into the chat-key store so the picker can use it. |

---

## 7. Environment State (as indexed)

- Python: `.venv` present; `httpx` added to `requirements.txt` (streaming
  client + verification suites).
- Postgres: running on `127.0.0.1:5432`, database `minillm` with all three
  credit tables (live data from prior verification runs).
- `GROQ_API_KEY` is **not set** — chat answers `503` until it is.
- `minillm.db` (SQLite): stale, untracked, wrong schema.
- Git: modified/untracked as of the chat feature; `.agent-logs/` holds prior
  session logs (gitignored).
