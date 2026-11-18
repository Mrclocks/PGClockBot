"""Which paid-but-undelivered orders the bot may finish by itself, and what the buyer is told.

A renewal writes an absolute target that is saved on the order before the panel call, so
repeating it cannot add time or volume twice. A new purchase creates nothing when its
attempt failed with a known outcome (the order keeps no service, and a create the panel
may have accepted is stamped ``delivery_outcome_unknown`` on the payment). Add-ons and
reseller capacity are additive and keep no saved target, so they stay with staff.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import ColumnElement, or_

from app.db.models import Order, OrderStatus, Payment

# Kept equal to orders.UNKNOWN_DELIVERY_NOTE; orders imports the delivery stack, so the
# value is repeated here instead of importing it (a test pins the two together).
UNKNOWN_DELIVERY_NOTE = "delivery_outcome_unknown"
STAFF_ONLY_NOTE_PREFIXES = ("svc_addon:", "res_cap:", "res_pkg:", "reseller_app:")
# Orders whose service_id names the service to change, so it is set before delivery.
MUTATION_NOTE_PREFIXES = ("renew:", *STAFF_ONLY_NOTE_PREFIXES)
# A delivery still DELIVERING after this long has crashed; a younger one may be running in
# another worker, and reopening it would start a second attempt.
DELIVERING_STALE_AFTER = timedelta(minutes=10)
DELIVERY_IN_PROGRESS = "تحویل این سفارش همین حالا در جریان است؛ چند دقیقه بعد دوباره امتحان کنید"
# The first automatic retry waits this long, so a short panel blip has passed.
FIRST_AUTO_RETRY_DELAY = timedelta(minutes=2)
# Attempts made while the panel answers; attempts are not spent while it is down.
MAX_AUTO_ATTEMPTS = 6
# After this long the order is refunded (or handed to staff) even if the panel never came back.
AUTO_RETRY_WINDOW_HOURS = 24

STAFF_AUTO_RETRY_NOTE = (
    "🔁 ربات هر چند دقیقه، هر وقت پاسارگارد در دسترس باشد، خودکار دوباره تلاش می‌کند و نتیجه را "
    "به مشتری و شما خبر می‌دهد. اگر تا "
    f"{AUTO_RETRY_WINDOW_HOURS} ساعت انجام نشود، مبلغ به کیف پول مشتری برمی‌گردد؛ "
    "اگر نتیجهٔ تلاش قبلی در پنل نامعلوم باشد، بازگشت وجه انجام نمی‌شود و بررسی به شما سپرده می‌شود."
)
STAFF_MANUAL_NOTE = (
    "✋ این مورد خودکار تکرار نمی‌شود (ممکن است در پنل اعمال شده باشد یا از نوع افزونه/نمایندگی است)؛ "
    "وضعیت را در پنل بررسی کنید و از «تحویل ناموفق» تکلیفش را روشن کنید."
)

PENDING_RENEWAL_NOTE = (
    "پرداخت شما دریافت شد، ولی تمدید سرویس هنوز روی سرور اعمال نشده است "
    "(معمولاً به‌خاطر در دسترس نبودن موقت سرور). "
    "ربات چند بار به‌طور خودکار دوباره تلاش می‌کند و نتیجه را همین‌جا به شما خبر می‌دهد؛ "
    "اگر تمدید انجام نشود، مبلغ به کیف پول شما برمی‌گردد. نیازی به پرداخت دوباره نیست."
)
PENDING_PURCHASE_NOTE = (
    "پرداخت شما دریافت شد، ولی ساخت سرویس هنوز کامل نشده است "
    "(معمولاً به‌خاطر در دسترس نبودن موقت سرور). "
    "ربات چند بار به‌طور خودکار دوباره تلاش می‌کند و سرویس را همین‌جا برایتان می‌فرستد؛ "
    "اگر ساخت انجام نشود، مبلغ به کیف پول شما برمی‌گردد. نیازی به پرداخت دوباره نیست."
)
PENDING_STAFF_NOTE = (
    "پرداخت شما دریافت و تأیید شد، ولی سفارش هنوز کامل نشده است. "
    "پشتیبانی در جریان است و نتیجه را به شما خبر می‌دهد؛ نیازی به پرداخت دوباره نیست."
)


def order_note(order: Order) -> str:
    return (order.note or "").strip()


def is_renewal_order(order: Order) -> bool:
    return order_note(order).startswith("renew:")


def is_new_purchase_order(order: Order) -> bool:
    note = order_note(order)
    return not note.startswith("renew:") and not note.startswith(STAFF_ONLY_NOTE_PREFIXES)


def is_auto_recoverable_kind(order: Order) -> bool:
    """True for a renewal or a new purchase: the kinds a retry cannot apply twice."""
    return is_renewal_order(order) or is_new_purchase_order(order)


def payment_outcome_unknown(payment: Payment | None) -> bool:
    return payment is not None and (payment.review_note or "").startswith(UNKNOWN_DELIVERY_NOTE)


def buyer_can_expect_auto_retry(order: Order, payment: Payment | None) -> bool:
    """Whether the buyer may be promised an automatic retry followed by a wallet refund."""
    if not is_auto_recoverable_kind(order):
        return False
    return is_renewal_order(order) or not payment_outcome_unknown(payment)


def staff_follow_up_note(order: Order, payment: Payment | None) -> str:
    return STAFF_AUTO_RETRY_NOTE if buyer_can_expect_auto_retry(order, payment) else STAFF_MANUAL_NOTE


def is_delivered(order: Order) -> bool:
    return order.status == OrderStatus.DELIVERED.value


def pending_buyer_note(order: Order, payment: Payment | None) -> str:
    if not buyer_can_expect_auto_retry(order, payment):
        return PENDING_STAFF_NOTE
    return PENDING_RENEWAL_NOTE if is_renewal_order(order) else PENDING_PURCHASE_NOTE


def missing_delivery_clause() -> ColumnElement[bool]:
    """SQL: an order whose service_id does not prove delivery (none yet, or a change to apply)."""
    return or_(Order.service_id.is_(None), *(Order.note.like(f"{prefix}%") for prefix in MUTATION_NOTE_PREFIXES))
