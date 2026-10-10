import hashlib
import secrets
from datetime import datetime, timezone

from sqlmodel import Session, select

from .config import settings
from . import credits as credits_module
from .models import ApiKey

SECRET_BYTES = 32


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def generate_key() -> str:
    return f"{settings.key_prefix}{secrets.token_urlsafe(SECRET_BYTES)}"


def create_api_key(
    session: Session,
    name: str,
    *,
    credits: int | None = None,
    ttl_days: int | None = None,
) -> tuple[ApiKey, str]:
    raw_key = generate_key()
    record = ApiKey(
        name=name,
        key_hash=hash_key(raw_key),
        key_prefix=settings.key_prefix,
        last_four=raw_key[-4:],
    )
    session.add(record)
    session.commit()
    session.refresh(record)

    if credits:
        credits_module.allocate(
            session,
            key_id=record.id or 0,
            amount=credits,
            ttl_days=ttl_days,
            reference="initial grant",
        )
        session.refresh(record)
    return record, raw_key


def verify_key(session: Session, raw_key: str) -> ApiKey | None:
    record = session.exec(
        select(ApiKey).where(ApiKey.key_hash == hash_key(raw_key))
    ).first()
    if record is None or record.revoked_at is not None:
        return None
    record.last_used_at = datetime.now(timezone.utc)
    session.add(record)
    session.commit()
    return record
