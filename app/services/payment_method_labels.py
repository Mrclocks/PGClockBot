"""Persian labels for payment methods (orders / payments / admin notices)."""

from __future__ import annotations

from app.db.models import PaymentMethod

PAYMENT_METHOD_FA: dict[str, str] = {
    PaymentMethod.WALLET.value: "کیف پول",
    PaymentMethod.CARD.value: "کارت به کارت",
    PaymentMethod.GATEWAY.value: "درگاه لینک",
    PaymentMethod.CRYPTO.value: "رمزارز",
    PaymentMethod.STARS.value: "استارز",
    PaymentMethod.PSP.value: "درگاه آنلاین",
    PaymentMethod.BBC.value: "بزن به کارت",
}


def payment_method_fa(method: str | None, *, fallback: str | None = None) -> str:
    key = str(method or "").strip().lower()
    if not key:
        return fallback if fallback is not None else "—"
    return PAYMENT_METHOD_FA.get(key, fallback if fallback is not None else key)
