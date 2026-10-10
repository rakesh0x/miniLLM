from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ApiKey(SQLModel, table=True):
    __tablename__ = "api_keys"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    key_hash: str = Field(index=True, unique=True)
    key_prefix: str
    last_four: str
    created_at: datetime = Field(default_factory=_utcnow)
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None

    # Credit accounting. Available credits are always derived, never stored:
    #     available = allocated - spent - reserved
    credits_allocated: int = Field(default=0)
    credits_spent: int = Field(default=0)
    credits_reserved: int = Field(default=0)
    credits_expire_at: datetime | None = None

    @property
    def credits_expired(self) -> bool:
        return self.credits_expire_at is not None and self.credits_expire_at <= _utcnow()

    @property
    def credits_available(self) -> int:
        """Credits this key can spend right now.

        Expiry wins over balance: an expired key has zero available credits even
        if the arithmetic says otherwise.
        """
        if self.credits_expired:
            return 0
        return max(0, self.credits_allocated - self.credits_spent - self.credits_reserved)

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


class CreditLedger(SQLModel, table=True):
    """Append-only history of every credit movement for a key.

    `kind`: allocate | reserve | commit | release | expire | charge
    `available_after`: available credits immediately after this movement, so the
    ledger alone is enough to reconcile a balance.
    """

    __tablename__ = "credit_ledger"

    id: int | None = Field(default=None, primary_key=True)
    api_key_id: int = Field(foreign_key="api_keys.id", index=True)
    kind: str = Field(index=True)
    amount: int
    available_after: int
    reference: str | None = Field(default=None, max_length=200)
    created_at: datetime = Field(default_factory=_utcnow)


class CreditReservation(SQLModel, table=True):
    """Credits held for an in-flight operation.

    A reservation moves credits from `allocated` into `reserved` so two
    concurrent calls cannot both claim the same credit. It is later committed
    (reserved -> spent) or released (reserved -> available). Reservations whose
    TTL elapses are reclaimed automatically, so a crashed caller cannot leak
    credits forever.
    """

    __tablename__ = "credit_reservations"

    id: int | None = Field(default=None, primary_key=True)
    api_key_id: int = Field(foreign_key="api_keys.id", index=True)
    amount: int
    reference: str | None = Field(default=None, max_length=200)
    status: str = Field(default="pending", index=True)
    created_at: datetime = Field(default_factory=_utcnow)
    expires_at: datetime
    settled_at: datetime | None = None
