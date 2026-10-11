import hashlib
import secrets
from datetime import datetime, timedelta, timezone

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
    """Resolve a key by its hash, stamping when it was last used.

    The stamp is stale-tolerant: it is rewritten only when the recorded value is
    older than `last_used_stamp_seconds`. Each round trip to a remote database
    costs a few hundred milliseconds, so a write on every authenticated call
    turns one round trip into three. The value stays accurate to within the
    window, which is all "last used" is for.
    """
    record = session.exec(
        select(ApiKey).where(ApiKey.key_hash == hash_key(raw_key))
    ).first()
    if record is None or record.revoked_at is not None:
        return None

    now = datetime.now(timezone.utc)
    last = record.last_used_at
    if last is None or (now - last) > timedelta(seconds=settings.last_used_stamp_seconds):
        record.last_used_at = now
        session.add(record)
        session.commit()
    return record
