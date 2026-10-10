from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from . import credits
from .credits import to_credit_balance
from .db import get_session
from .keys import create_api_key
from .models import ApiKey, CreditLedger
from .schemas import (
    AllocateCreditsRequest,
    CreateKeyRequest,
    CreatedKey,
    CreditBalance,
    KeySummary,
    LedgerEntry,
    SpendCreditsRequest,
)

router = APIRouter(prefix="/keys", tags=["keys"])


def to_summary(record: ApiKey) -> KeySummary:
    return KeySummary(
        id=record.id or 0,
        name=record.name,
        masked_key=f"{record.key_prefix}{'•' * 8}{record.last_four}",
        created_at=record.created_at,
        last_used_at=record.last_used_at,
        revoked_at=record.revoked_at,
        credits_allocated=record.credits_allocated,
        credits_spent=record.credits_spent,
        credits_reserved=record.credits_reserved,
        credits_available=record.credits_available,
        credits_expire_at=record.credits_expire_at,
        credits_expired=record.credits_expired,
    )


def require_key(key_id: int, session: Session) -> ApiKey:
    record = session.get(ApiKey, key_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Key not found")
    return record


def fresh_key(key_id: int, session: Session) -> ApiKey:
    """Re-read a row mutated through Core UPDATE statements.

    Core statements bypass the ORM identity map, so a `session.get` right after
    one would return stale attributes. `populate_existing` forces the reload.
    """
    return session.get(ApiKey, key_id, populate_existing=True)


# --------------------------------------------------------------------------
# Key CRUD
# --------------------------------------------------------------------------


@router.post("", response_model=CreatedKey, status_code=status.HTTP_201_CREATED)
def create_key(
    payload: CreateKeyRequest, session: Session = Depends(get_session)
) -> CreatedKey:
    record, plaintext = create_api_key(
        session,
        payload.name,
        credits=payload.credits,
        ttl_days=payload.ttl_days,
    )
    return CreatedKey(key=to_summary(record), plaintext_key=plaintext)


@router.get("", response_model=list[KeySummary])
def list_keys(session: Session = Depends(get_session)) -> list[KeySummary]:
    records = session.exec(select(ApiKey).order_by(ApiKey.created_at.desc())).all()
    return [to_summary(r) for r in records]


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_key(key_id: int, session: Session = Depends(get_session)) -> None:
    record = session.get(ApiKey, key_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Key not found")
    record.revoked_at = datetime.now(timezone.utc)
    session.add(record)
    session.commit()


# --------------------------------------------------------------------------
# Credits
# --------------------------------------------------------------------------


@router.get(
    "/{key_id}/credits",
    response_model=CreditBalance,
    summary="Read a key's credit balance",
)
def get_credits(key_id: int, session: Session = Depends(get_session)) -> CreditBalance:
    return to_credit_balance(require_key(key_id, session))


@router.post(
    "/{key_id}/credits",
    response_model=CreditBalance,
    summary="Grant credits to a key, resetting its TTL",
)
def allocate_credits(
    key_id: int,
    payload: AllocateCreditsRequest,
    session: Session = Depends(get_session),
) -> CreditBalance:
    require_key(key_id, session)
    credits.allocate(
        session,
        key_id=key_id,
        amount=payload.amount,
        ttl_days=payload.ttl_days,
        expires_at=payload.expires_at,
        reference="top-up",
    )
    return to_credit_balance(fresh_key(key_id, session))


@router.post(
    "/{key_id}/credits/spend",
    response_model=CreditBalance,
    summary="Spend credits against a key",
)
def spend_credits(
    key_id: int,
    payload: SpendCreditsRequest,
    session: Session = Depends(get_session),
) -> CreditBalance:
    require_key(key_id, session)
    credits.charge(
        session,
        key_id=key_id,
        amount=payload.amount,
        reference=payload.reference or "manual spend",
    )
    return to_credit_balance(fresh_key(key_id, session))


@router.get(
    "/{key_id}/credits/ledger",
    response_model=list[LedgerEntry],
    summary="Credit movement history for a key",
)
def credit_ledger(
    key_id: int, session: Session = Depends(get_session)
) -> list[LedgerEntry]:
    require_key(key_id, session)
    entries = session.exec(
        select(CreditLedger)
        .where(CreditLedger.api_key_id == key_id)
        .order_by(CreditLedger.created_at.desc())
        .limit(200)
    ).all()
    return list(entries)
