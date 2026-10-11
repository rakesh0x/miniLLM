from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://rakesh@127.0.0.1:5432/minillm"
    key_prefix: str = "mnl_"
    cors_origins: str = "http://localhost:3000"

    # Connection pool tuning. Postgres only — SQLite uses NullPool (see db.py).
    #
    # Sizing rule: the pool bounds how many requests can be in flight, and it is
    # a hard ceiling the database will refuse beyond its own connection limit.
    # Keep `pool_size + max_overflow` under the database's allowance — on a
    # hosted tier that is often 10, so point DATABASE_URL at the pooled
    # endpoint, which multiplexes many clients over few server connections.
    # Sized so a dozen concurrent chats never queue for a connection.
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_pool_timeout: int = 30
    db_pool_recycle: int = 1800

    # Credits: TTL on an allocation, and TTL on an in-flight reservation so a
    # caller that dies mid-job cannot lock credits forever.
    credit_ttl_days: int = 30
    reservation_ttl_seconds: int = 600
    reservation_reclaim_limit: int = 100
    # How often `reserve` sweeps a key's expired reservations. 0 sweeps on every
    # reserve. A sweep costs a round trip and cannot free anything before the
    # TTL has elapsed, so it is throttled rather than run per request.
    reservation_reclaim_interval_seconds: int = 60

    # Cost charged by the demo metered endpoint (POST /usage/record).
    credits_per_usage: int = 1

    # Optional initial grant handed to a key at creation.
    default_key_credits: int = 0
    default_key_ttl_days: int = 30

    # How coarse `last_used_at` may be, in seconds. 0 stamps on every call.
    # The stamp costs a write per authenticated request, which on a remote
    # database is two extra round trips — so it is refreshed periodically
    # rather than on every call.
    last_used_stamp_seconds: int = 60

    # --- LLM chat (Groq Cloud) ---
    # OpenAI-compatible upstream. Leave groq_api_key empty to disable /chat.
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"

    # Fallback chain, tried in order. A model that rejects the request before
    # any token streams (rate limit, timeout, upstream 5xx) hands the request
    # to the next model. Once tokens are flowing a failure aborts instead, so
    # the caller never receives a spliced-together answer.
    # These are the models Groq serves today. Groq retires models with little
    # notice, and a dead primary silently turns every request into a fallback
    # (or a 404), so re-check `GET {groq_base_url}/models` when changing
    # providers or after a quiet period.
    llm_model_chain: str = "qwen/qwen3.8-27b,openai/gpt-oss-120b,openai/gpt-oss-20b"
    llm_max_tokens: int = 1024
    llm_temperature: float = 0.7
    llm_timeout_seconds: float = 60.0
    # Metering: credits charged per 1K tokens the provider reports. A chat
    # reserves an upper bound before streaming, then settles the exact cost.
    credits_per_1k_tokens: int = 1
    # Context bounds, so one request cannot meter an unbounded prompt.
    llm_max_history_messages: int = 50
    llm_max_message_chars: int = 8000
    llm_system_prompt: str = ""

    # --- Response cache ---
    # Exact-match cache of completed answers. A hit replays the stored answer
    # and its original charge, so billing is identical to a fresh request and
    # the gateway simply skips the upstream call.
    cache_enabled: bool = True
    # "key" isolates entries per API key (the default, and the safe choice);
    # "global" shares identical prompts across keys — only for operators who
    # know their prompts carry no user-specific data.
    cache_scope: str = "key"
    cache_ttl_seconds: int = 86_400
    # Per-key and global footprint caps; least-recently-hit entries go first.
    cache_max_entries_per_key: int = 200
    cache_max_entries: int = 10_000
    # Answers longer than this are not stored: one giant entry would dominate
    # a key's budget for a prompt nobody repeats.
    cache_max_response_chars: int = 20_000
    # Replay granularity, in characters. Word-aligned where possible.
    cache_stream_chunk_chars: int = 32
    # How often a store triggers the eviction sweep. 0 = never (rely on
    # POST /cache/cleanup); higher = fewer queries on the write path. On a
    # high-latency database the sweep costs several round trips, so it must not
    # run on every write.
    cache_evict_every: int = 100
    # How a hit is billed: "metered" replays the recorded charge, "flat"
    # charges `cache_hit_credits`, "free" charges nothing. Only "metered" is
    # safe by default — "free" lets a client repeat a prompt to farm output.
    cache_hit_mode: str = "metered"
    cache_hit_credits: int = 1

    # --- Guardrails ---
    # Layer 1 is the persona in guardrails.py. `llm_system_prompt` is empty by
    # default, which *uses* the persona; set it to replace the persona with an
    # operator-supplied one. `guardrail_persona = false` disables it outright.
    guardrail_persona: bool = True
    # Layer 2 is the output redactor, applied to every token before it reaches
    # the client or the cache.
    guardrail_filter_output: bool = True

    @property
    def model_chain(self) -> list[str]:
        """The fallback chain, empty entries removed."""
        return [m.strip() for m in self.llm_model_chain.split(",") if m.strip()]


settings = Settings()
