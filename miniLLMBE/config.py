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


settings = Settings()
