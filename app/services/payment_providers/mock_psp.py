"""Mock PSP — full request/verify cycle without a real merchant."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from app.services.payment_providers.base import CheckoutRequest, CheckoutResult, VerifyResult


class MockPspAdapter:
    name = "mock"

    def __init__(self, *, public_base_url: str):
        self._base = (public_base_url or "").rstrip("/") or "http://127.0.0.1:8000"

    async def create_checkout(self, req: CheckoutRequest) -> CheckoutResult:
        # Local mock pay page settles after explicit POST (tests / demo).
        q = urlencode(
            {
                "settlement_id": str(req.settlement_id),
                "payment_id": str(req.payment_id),
                "amount": str(req.amount),
            }
        )
        ref = f"mock-{req.settlement_id}-{req.payment_id}"
        return CheckoutResult(
            external_ref=ref,
            checkout_url=f"{self._base}/payments/settlement/mock/checkout?{q}",
            raw={"provider": "mock"},
        )

    async def verify(
        self,
        *,
        external_ref: str,
        amount: int,
        callback_params: dict[str, Any],
        expected_invoice_id: int | None = None,
    ) -> VerifyResult:
        status = str(callback_params.get("status") or "").lower()
        paid_amount = callback_params.get("amount")
        try:
            paid_i = int(paid_amount) if paid_amount is not None else None
        except (TypeError, ValueError):
            paid_i = None
        if status not in {"ok", "paid", "success"}:
            return VerifyResult(
                ok=False,
                external_ref=external_ref,
                amount=paid_i,
                message="mock payment not successful",
            )
        if paid_i is not None and paid_i != int(amount):
            return VerifyResult(
                ok=False,
                external_ref=external_ref,
                amount=paid_i,
                message="amount mismatch",
            )
        return VerifyResult(
            ok=True,
            external_ref=external_ref,
            amount=int(amount),
            message="mock verified",
        )
