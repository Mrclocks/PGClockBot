"""Payment provider adapters — protocol only; no Mirza/third-party copies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class CheckoutRequest:
    settlement_id: int
    payment_id: int
    amount: int
    currency: str
    description: str
    callback_url: str
    return_url: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class CheckoutResult:
    external_ref: str
    checkout_url: str
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    external_ref: str
    amount: int | None = None
    message: str = ""
    raw: dict[str, Any] | None = None
    # Gateway tracking number and payment time of a paid transaction, when reported.
    reference: str = ""
    paid_at: str = ""
    # Set when the gateway says the money was paid but it cannot settle this row
    # (e.g. a different amount): staff must review it instead of a retry.
    review_reason: str = ""


class PspAdapter(Protocol):
    name: str

    async def create_checkout(self, req: CheckoutRequest) -> CheckoutResult: ...

    async def verify(
        self,
        *,
        external_ref: str,
        amount: int,
        callback_params: dict[str, Any],
        expected_invoice_id: int | None = None,
    ) -> VerifyResult: ...
