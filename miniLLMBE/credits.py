"""Credit accounting for API keys.

Every balance mutation is a single atomic SQL UPDATE whose WHERE clause
re-checks the accounting invariant inside the same statement:

    credits_allocated - credits_spent - credits_reserved >= amount

Postgres evaluates that predicate and applies the write while holding the row
lock, so two concurrent requests can never both succeed against the same last
credit. There is deliberately no read-modify-write here: the guard and the
write are one statement, which is what makes an overspend impossible without
needing SELECT ... FOR UPDATE.

Each mutation is paired with an append-only `credit_ledger` row, and
reservations transition through guarded atomic UPDATEs so a reservation can
only ever be settled once (a retried commit cannot double-charge).
"""

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import or_, select, update
from sqlmodel import Session

from .config import settings
from .errors import CreditsExpired, InsufficientCredits, ReservationNotFound
from .models import ApiKey, CreditLedger, CreditReservation
from .schemas import CreditBalance

# Reservation lifecycle
PENDING = "pending"
COMMITTED = "committed"
RELEASED = "released"
EXPIRED = "expired"

# Ledger entry kinds
KIND_ALLOCATE = "allocate"
KIND_RESERVE = "reserve"
KIND_COMMIT = "commit"
KIND_RELEASE = "release"
KIND_EXPIRE = "expire"
KIND_CHARGE = "charge"

MAX_AMOUNT = 1_000_000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _available_expression():
    """SQL for `available` — used inside WHERE clauses so the check and the
    write happen in one statement."""
    return ApiKey.credits_allocated - ApiKey.credits_spent - ApiKey.credits_reserved


def _live_clause():
    return [
        ApiKey.revoked_at.is_(None),
        or_(ApiKey.credits_expire_at.is_(None), ApiKey.credits_expire_at > _now()),
    ]


def _returning_balances(statement):
    return statement.returning(
        ApiKey.credits_allocated,
        ApiKey.credits_spent,
        ApiKey.credits_reserved,
    )


def _reject(session: Session, key_id: int, amount: int) -> None:
    """Diagnose why an atomic UPDATE affected no rows, and raise accordingly.

    `populate_existing` matters: earlier statements in this transaction mutated
    the row through Core UPDATEs, which do not refresh the ORM identity map.
    """
    session.rollback()
    key = session.get(ApiKey, key_id, populate_existing=True)
    if key is None or key.revoked_at is not None:
        raise HTTPException(status_code=404, detail="API key not found")
    if key.credits_expired:
        raise CreditsExpired(f"Credits expired on {key.credits_expire_at.isoformat()}")
    raise InsufficientCredits(
        f"Request needs {amount} credit(s); {key.credits_available} available"
    )


def to_credit_balance(record: ApiKey) -> CreditBalance:
    return CreditBalance(
        key_id=record.id or 0,
        allocated=record.credits_allocated,
        spent=record.credits_spent,
        reserved=record.credits_reserved,
        available=record.credits_available,
        expires_at=record.credits_expire_at,
        expired=record.credits_expired,
    )


def _check_amount(amount: int, *, field: str = "amount") -> int:
    if not isinstance(amount, int) or isinstance(amount, bool):
        raise HTTPException(status_code=422, detail=f"{field} must be an integer")
    if amount <= 0:
        raise HTTPException(status_code=422, detail=f"{field} must be positive")
    if amount > MAX_AMOUNT:
        raise HTTPException(status_code=422, detail=f"{field} must be <= {MAX_AMOUNT}")
    return amount


def _resolve_expiry(
    now: datetime, *, ttl_days: int | None, expires_at: datetime | None
) -> datetime:
    """Grant credits for a fixed TTL.

    Always replaces, so a fresh top-up revives or extends an expired allocation
    instead of inheriting its past.
    """
    if expires_at is not None:
        if expires_at <= now:
            raise HTTPException(status_code=422, detail="expires_at must be in the future")
        return expires_at
    if ttl_days is not None:
        if ttl_days <= 0:
            raise HTTPException(status_code=422, detail="ttl_days must be positive")
        return now + timedelta(days=ttl_days)
    return now + timedelta(days=settings.credit_ttl_days)


def _settle(
    session: Session,
    reservation: CreditReservation,
    new_status: str,
    *,
    reference: str | None = None,
) -> dict[str, int] | None:
    """Settle a reservation exactly once, atomically.

    The reservation row itself is the guard: the UPDATE only matches rows still
    in `pending`, so a duplicate commit or release is a no-op rather than a
    double charge. The key-side write happens in the same transaction, so a
    failure there rolls the whole settlement back.
    """
    transition = (
        update(CreditReservation)
        .where(CreditReservation.id == reservation.id)
        .where(CreditReservation.status == PENDING)
        .values(status=new_status, settled_at=_now())
        .returning(
            CreditReservation.api_key_id,
            CreditReservation.amount,
            CreditReservation.reference,
        )
    )
    row = session.execute(transition).first()
    if row is None:
        # Already settled (or gone) — idempotent no-op.
        return None

    key_id, amount = row.api_key_id, row.amount
    settled_reference = reference or row.reference

    if new_status == COMMITTED:
        # reserved -> spent. available is unchanged: both buckets are already
        # excluded from the available total.
        key_update = (
            update(ApiKey)
            .where(ApiKey.id == key_id)
            .where(ApiKey.credits_reserved >= amount)
            .values(
                credits_reserved=ApiKey.credits_reserved - amount,
                credits_spent=ApiKey.credits_spent + amount,
            )
        )
    else:
        # release / expire: reserved -> available.
        key_update = (
            update(ApiKey)
            .where(ApiKey.id == key_id)
            .where(ApiKey.credits_reserved >= amount)
            .values(credits_reserved=ApiKey.credits_reserved - amount)
        )

    balance = session.execute(_returning_balances(key_update)).first()
    if balance is None:
        # Unreachable unless a reservation somehow outruns its balance; roll
        # back rather than corrupt the accounting.
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Reservation {reservation.id} is out of sync with the balance",
        )

    available_after = (
        balance.credits_allocated - balance.credits_spent - balance.credits_reserved
    )
    session.add(
        CreditLedger(
            api_key_id=key_id,
            kind=new_status,
            amount=amount,
            available_after=available_after,
            reference=settled_reference,
        )
    )
    return {
        "allocated": balance.credits_allocated,
        "spent": balance.credits_spent,
        "reserved": balance.credits_reserved,
        "available": available_after,
    }


def allocate(
    session: Session,
    *,
    key_id: int,
    amount: int,
    ttl_days: int | None = None,
    expires_at: datetime | None = None,
    reference: str | None = None,
) -> dict[str, int | datetime | None]:
    """Grant credits to a key, resetting its TTL."""
    _check_amount(amount)
    now = _now()
    new_expiry = _resolve_expiry(now, ttl_days=ttl_days, expires_at=expires_at)

    balance = session.execute(
        _returning_balances(
            update(ApiKey)
            .where(ApiKey.id == key_id, ApiKey.revoked_at.is_(None))
            .values(
                credits_allocated=ApiKey.credits_allocated + amount,
                credits_expire_at=new_expiry,
            )
        )
    ).first()
    if balance is None:
        raise HTTPException(status_code=404, detail="API key not found")

    available = (
        balance.credits_allocated - balance.credits_spent - balance.credits_reserved
    )
    session.add(
        CreditLedger(
            api_key_id=key_id,
            kind=KIND_ALLOCATE,
            amount=amount,
            available_after=available,
            reference=reference,
        )
    )
    session.commit()
    return {
        "allocated": balance.credits_allocated,
        "spent": balance.credits_spent,
        "reserved": balance.credits_reserved,
        "available": available,
        "expires_at": new_expiry,
    }


def charge(
    session: Session,
    *,
    key_id: int,
    amount: int,
    reference: str | None = None,
) -> dict[str, int]:
    """Spend credits immediately, for short non-reservable operations.

    One atomic guarded statement, so concurrent charges can never overspend.
    Use `reserve` + `commit`/`release` for work that can fail or take time.
    """
    _check_amount(amount)

    balance = session.execute(
        _returning_balances(
            update(ApiKey)
            .where(ApiKey.id == key_id, *_live_clause())
            .where(_available_expression() >= amount)
            .values(credits_spent=ApiKey.credits_spent + amount)
        )
    ).first()
    if balance is None:
        _reject(session, key_id, amount)

    available = (
        balance.credits_allocated - balance.credits_spent - balance.credits_reserved
    )
    session.add(
        CreditLedger(
            api_key_id=key_id,
            kind=KIND_CHARGE,
            amount=amount,
            available_after=available,
            reference=reference,
        )
    )
    session.commit()
    return {
        "allocated": balance.credits_allocated,
        "spent": balance.credits_spent,
        "reserved": balance.credits_reserved,
        "available": available,
    }


def reserve(
    session: Session,
    *,
    key_id: int,
    amount: int,
    reference: str | None = None,
    ttl_seconds: int | None = None,
) -> int:
    """Hold credits for work in progress. Returns the reservation id.

    Reclaims this key's expired reservations first so credits are never locked
    away permanently by a caller that never committed.
    """
    _check_amount(amount)
    reclaim_expired(session, key_id=key_id)

    ttl = settings.reservation_ttl_seconds if ttl_seconds is None else ttl_seconds
    expires_at = _now() + timedelta(seconds=max(1, ttl))

    balance = session.execute(
        _returning_balances(
            update(ApiKey)
            .where(ApiKey.id == key_id, *_live_clause())
            .where(_available_expression() >= amount)
            .values(credits_reserved=ApiKey.credits_reserved + amount)
        )
    ).first()
    if balance is None:
        _reject(session, key_id, amount)

    available = (
        balance.credits_allocated - balance.credits_spent - balance.credits_reserved
    )
    reservation = CreditReservation(
        api_key_id=key_id,
        amount=amount,
        reference=reference,
        status=PENDING,
        expires_at=expires_at,
    )
    session.add(reservation)
    session.flush()  # assign the primary key before logging
    session.add(
        CreditLedger(
            api_key_id=key_id,
            kind=KIND_RESERVE,
            amount=amount,
            available_after=available,
            reference=reference,
        )
    )
    session.commit()
    session.refresh(reservation)
    return reservation.id


def commit(session: Session, *, reservation_id: int, reference: str | None = None) -> bool:
    """Convert a reservation into spent credits.

    Returns False when the reservation was already settled, so a retried commit
    cannot double-charge.
    """
    reservation = session.get(CreditReservation, reservation_id)
    if reservation is None:
        raise ReservationNotFound()
    settled = _settle(session, reservation, COMMITTED, reference=reference)
    session.commit()
    return settled is not None


def release(session: Session, *, reservation_id: int, reference: str | None = None) -> bool:
    """Return a reservation's credits to the available pool."""
    reservation = session.get(CreditReservation, reservation_id)
    if reservation is None:
        raise ReservationNotFound()
    settled = _settle(session, reservation, RELEASED, reference=reference)
    session.commit()
    return settled is not None


def reclaim_expired(
    session: Session,
    *,
    key_id: int | None = None,
    limit: int | None = None,
) -> int:
    """Release reservations whose TTL has elapsed.

    Does not commit: it runs inside the caller's transaction so a later reserve
    or settle in the same call sees the reclaimed credits. The id list is
    bounded by `limit`; the settlement itself stays atomic through the
    status-guarded UPDATE in `_settle`.
    """
    now = _now()
    bound = settings.reservation_reclaim_limit if limit is None else limit

    id_query = (
        select(CreditReservation.id)
        .where(CreditReservation.status == PENDING)
        .where(CreditReservation.expires_at <= now)
        .order_by(CreditReservation.expires_at)
        .limit(bound)
    )
    if key_id is not None:
        id_query = id_query.where(CreditReservation.api_key_id == key_id)

    expired_ids = list(session.execute(id_query).scalars().all())
    for reservation_id in expired_ids:
        reservation = session.get(CreditReservation, reservation_id)
        if reservation is not None:
            _settle(session, reservation, EXPIRED, reference="reservation ttl elapsed")
    return len(expired_ids)
