"""Documented, credit-metered endpoints.

`POST /usage/record` exists so the credit system has a real, verifiable
consumer, and so there is a reference implementation to copy when adding a
metered feature. Any endpoint that spends credits should follow the same
three-part shape:

1. `dependencies=[Depends(require_credits(cost))]` — fail fast, before running
   any expensive work, with 402 for an empty balance or 403 for an expired one.
2. `api_key: ApiKey = Depends(get_api_key)` — the authenticated caller.
3. `credits.charge(...)` — the actual spend, which re-checks the invariant
   atomically in SQL. This is the enforcement; the guard is only a shortcut.

Endpoints that can fail or take time should use `credits.reserve` +
`credits.commit`/`credits.release` instead of `charge`, so a failure returns
the credits to the caller instead of consuming them.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlmodel import Session

from .auth import get_api_key, require_credits
from .config import settings
from .credits import charge, reclaim_expired, to_credit_balance
from .db import get_session
from .models import ApiKey
from .schemas import ReclaimResponse, UsageResponse

router = APIRouter(tags=["usage"])


class UsageRequest(BaseModel):
    reference: str | None = Field(default=None, max_length=200)


@router.post(
    "/usage/record",
    response_model=UsageResponse,
    dependencies=[Depends(require_credits(settings.credits_per_usage))],
    summary="Demo metered endpoint: charges credits_per_usage for the caller's key",
)
def record_usage(
    payload: UsageRequest | None = None,
    api_key: ApiKey = Depends(get_api_key),
    session: Session = Depends(get_session),
) -> UsageResponse:
    reference = (payload.reference if payload else None) or "usage/record"
    charge(
        session,
        key_id=api_key.id or 0,
        amount=settings.credits_per_usage,
        reference=reference,
    )
    return UsageResponse(
        charged=settings.credits_per_usage,
        reference=reference,
        balance=to_credit_balance(
            session.get(ApiKey, api_key.id, populate_existing=True)
        ),
    )


@router.post(
    "/credits/reclaim-expired",
    response_model=ReclaimResponse,
    summary="Release reservations whose TTL elapsed",
)
def reclaim_expired_reservations(
    session: Session = Depends(get_session),
) -> ReclaimResponse:
    reclaimed = reclaim_expired(session)
    session.commit()
    return ReclaimResponse(reclaimed=reclaimed)
