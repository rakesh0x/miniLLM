"""Bearer authentication and the credit guard.

`POST /usage/record` is the shape any credit-metered endpoint should take:

    @router.post("", dependencies=[Depends(require_credits(cost))])
    def metered_endpoint(
        cost: int,
        api_key: ApiKey = Depends(get_api_key),
        session: Session = Depends(get_session),
    ):
        credits.charge(session, key_id=api_key.id, amount=cost, reference="...")
        ...

`require_credits` is a fail-fast guard: it rejects requests that are already
doomed before they reach endpoint logic, which saves a round trip for a caller
with an empty balance. It is NOT the enforcement — `credits.charge` /
`credits.reserve` are, because they re-check the invariant atomically in SQL at
the moment of spending. A guard that only read the balance would let two
concurrent requests both pass and overspend.
"""

from fastapi import Depends, Header, HTTPException
from sqlmodel import Session

from .config import settings
from .db import Session as DBSession
from .db import engine
from .errors import CreditsExpired, InsufficientCredits
from .keys import verify_key
from .models import ApiKey


def get_api_key(authorization: str | None = Header(default=None)) -> ApiKey:
    """Resolve the caller's API key from `Authorization: Bearer <key>`.

    The session is opened and closed *inside* this function on purpose. As a
    yield-dependency it would be held until the response finished — and for
    `POST /chat` that means the whole streamed generation. Since concurrency is
    bounded by the connection pool, one connection pinned per in-flight request
    halves the number of requests the gateway can serve, and on a slow database
    the pool becomes the throughput limit.

    `verify_key` also stamps `last_used_at`, so any authenticated call counts as
    usage even before credits are charged.
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=401,
            detail="Expected 'Authorization: Bearer <api-key>'",
        )

    with DBSession(engine, expire_on_commit=False) as session:
        record = verify_key(session, token.strip())
    if record is None:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")
    # Safe to hand out: `expire_on_commit=False` means the commit inside
    # `verify_key` leaves the attributes loaded, so reading them off the
    # detached instance cannot need a refresh on a closed session.
    return record


def require_credits(cost: int = 1):
    """Reject requests that cannot cover `cost` credits before they run."""

    async def dependency(api_key: ApiKey = Depends(get_api_key)) -> ApiKey:
        required = max(1, int(cost))
        if api_key.credits_expired:
            raise CreditsExpired(
                f"Credits expired on {api_key.credits_expire_at.isoformat()}"
            )
        if api_key.credits_available < required:
            raise InsufficientCredits(
                f"This request costs {required} credit(s); "
                f"{api_key.credits_available} available"
            )
        return api_key

    return dependency


def usage_cost() -> int:
    """Default cost for the demo metered endpoint."""
    return max(1, int(settings.credits_per_usage))
