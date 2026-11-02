"""The shop's rule for approving a card receipt after a wait instead of at once.

It belongs to the «تأیید خودکار رسید» switch: with the switch on and a wait set, a new service
purchase paid by card is approved after that wait; every other receipt is still approved at once.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.db.models import Order, Payment, PaymentMethod

SWITCH_KEY = "auto_approve_payments"
MINUTES_KEY = "auto_approve_after_minutes"
MAX_AMOUNT_KEY = "auto_approve_after_max_amount"
RETURNING_ONLY_KEY = "auto_approve_after_returning_only"
MAX_MINUTES = 24 * 60
MUTATION_NOTE_PREFIXES = ("renew:", "svc_addon:", "reseller_app:")
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True, slots=True)
class ApprovalRule:
    minutes: int
    max_amount: int
    returning_only: bool

    @property
    def reason(self) -> str:
        return f"بدون بررسی ادمین تا {self.minutes} دقیقه"


def _whole_number(raw: object) -> int:
    text = str(raw or "").strip()
    return int(text) if text.isascii() and text.isdigit() and len(text) <= 12 else 0


def rule_from_settings(settings: Mapping[str, str]) -> ApprovalRule | None:
    """The shop's rule, or None while the switch is off or no wait is set (0, empty, malformed)."""
    if str(settings.get(SWITCH_KEY, "0")).strip().lower() not in TRUE_VALUES:
        return None
    minutes = _whole_number(settings.get(MINUTES_KEY))
    if minutes < 1:
        return None
    return ApprovalRule(
        minutes=min(minutes, MAX_MINUTES),
        max_amount=_whole_number(settings.get(MAX_AMOUNT_KEY)),
        returning_only=str(settings.get(RETURNING_ONLY_KEY, "1")).strip().lower() in TRUE_VALUES,
    )


def is_delayed_purchase(payment: Payment, order: Order | None) -> bool:
    """A card payment for a new service purchase: the kind the wait applies to."""
    if payment.is_wallet_topup or payment.method != PaymentMethod.CARD.value or order is None:
        return False
    note = order.note or ""
    return not note.startswith(MUTATION_NOTE_PREFIXES)
