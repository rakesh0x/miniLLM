"""Shared API schemas for keys and credits."""

from datetime import datetime

from pydantic import BaseModel, Field


class KeySummary(BaseModel):
    id: int
    name: str
    masked_key: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    credits_allocated: int = 0
    credits_spent: int = 0
    credits_reserved: int = 0
    credits_available: int = 0
    credits_expire_at: datetime | None = None
    credits_expired: bool = False

    model_config = {"from_attributes": True}


class CreatedKey(BaseModel):
    key: KeySummary
    plaintext_key: str


class CreditBalance(BaseModel):
    key_id: int
    allocated: int
    spent: int
    reserved: int
    available: int
    expires_at: datetime | None
    expired: bool


class AllocateCreditsRequest(BaseModel):
    amount: int = Field(gt=0)
    ttl_days: int | None = Field(default=None, gt=0)
    expires_at: datetime | None = None


class SpendCreditsRequest(BaseModel):
    amount: int = Field(default=1, gt=0)
    reference: str | None = Field(default=None, max_length=200)


class CreateKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    credits: int | None = Field(default=None, gt=0)
    ttl_days: int | None = Field(default=None, gt=0)


class LedgerEntry(BaseModel):
    id: int
    kind: str
    amount: int
    available_after: int
    reference: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class UsageResponse(BaseModel):
    charged: int
    reference: str | None
    balance: CreditBalance


class ReclaimResponse(BaseModel):
    reclaimed: int
