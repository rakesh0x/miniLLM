from fastapi import HTTPException, status


class InsufficientCredits(HTTPException):
    """402 — the key exists and is live, but cannot cover the cost."""

    status_code = status.HTTP_402_PAYMENT_REQUIRED

    def __init__(self, detail: str = "Insufficient credits") -> None:
        super().__init__(status_code=self.status_code, detail=detail)


class CreditsExpired(HTTPException):
    """403 — the key's credits have passed their TTL."""

    status_code = status.HTTP_403_FORBIDDEN

    def __init__(self, detail: str = "Credits have expired") -> None:
        super().__init__(status_code=self.status_code, detail=detail)


class ReservationNotFound(HTTPException):
    status_code = status.HTTP_404_NOT_FOUND

    def __init__(self, detail: str = "Reservation not found") -> None:
        super().__init__(status_code=self.status_code, detail=detail)
