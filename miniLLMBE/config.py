from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://rakesh@127.0.0.1:5432/minillm"
    key_prefix: str = "mnl_"
    cors_origins: str = "http://localhost:3000"

    # Connection pool tuning. Postgres only — SQLite ignores these.
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_timeout: int = 30
    db_pool_recycle: int = 1800

    # Credits: TTL on an allocation, and TTL on an in-flight reservation so a
    # caller that dies mid-job cannot lock credits forever.
    credit_ttl_days: int = 30
    reservation_ttl_seconds: int = 600
    reservation_reclaim_limit: int = 100

    # Cost charged by the demo metered endpoint (POST /usage/record).
    credits_per_usage: int = 1

    # Optional initial grant handed to a key at creation.
    default_key_credits: int = 0
    default_key_ttl_days: int = 30

    # --- LLM chat (Groq Cloud) ---
    # OpenAI-compatible upstream. Leave groq_api_key empty to disable /chat.
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"

    # Fallback chain, tried in order. A model that rejects the request before
    # any token streams (rate limit, timeout, upstream 5xx) hands the request
    # to the next model. Once tokens are flowing a failure aborts instead, so
    # the caller never receives a spliced-together answer.
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

    @property
    def model_chain(self) -> list[str]:
        """The fallback chain, empty entries removed."""
        return [m.strip() for m in self.llm_model_chain.split(",") if m.strip()]


settings = Settings()
