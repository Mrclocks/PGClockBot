"""«بزن به کارت» settlements: create the remote order, verify it, settle once, notify once.

One settlement row per payment (idempotency key ``bbc:<payment id>``). Its
``provider_payload`` holds the exact create request (saved before the call, so a retry
replays it unchanged and the API's ``Idempotency-Key`` returns the same order), the order
the API reported, the token of the return/callback links, and tokens of expired attempts.

Payment truth only comes from reading the order with the shop's own key. A paid order
settles only when the order id we sent, the base amount and the unique amount all match;
anything else, and any paid order we cannot settle, goes to the shop's staff for review
instead of being approved or retried blindly.
"""

from __future__ import annotations

import hmac
import html
import json
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import case, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, Payment, PaymentMethod, PaymentSettlement, PaymentStatus, SettlementStatus
from app.services.payment_providers import bezanbecard as bbc
from app.services.payment_settlement import (
    BBC_PROVIDER,
    CHANNEL_BBC,
    _as_utc,
    _get_by_idem,
    _insert_settlement_race_safe,
    _on,
    _shop_ids_equal,
    idem_key_bbc,
    notify_after_settlement,
    psp_poll_delay,
    resolve_payment_shop_owner_id,
    settle_and_approve,
    tenant_key_for,
)
from app.services.public_https import https_public_base
from app.services.users import get_all_settings

logger = logging.getLogger(__name__)

ENABLED_KEY = "pay_bbc_enabled"
API_KEY = "bbc_api_key"
# What a customer is told when the shop's gateway setup is incomplete; the cause goes to the log.
CUSTOMER_UNAVAILABLE = "این روش پرداخت موقتاً در دسترس نیست؛ روش دیگری را انتخاب کنید."
HTTPS_REQUIRED = "برای «بزن به کارت» آدرس عمومی پنل باید https باشد؛ SSL را روی پنل اصلی فعال کنید."


class BbcNotReady(ValueError):
    """The gateway cannot be used until its setup is fixed; customers only see ``CUSTOMER_UNAVAILABLE``."""


def https_base() -> str:
    """The https origin for BezanBeCard callbacks, or an empty string when there is none."""
    return https_public_base()


def gateway_origin(base: str) -> str:
    """``https://host`` of ``base`` without a non-default port.

    BezanBeCard refuses links on a panel port such as ``:9000``. Payment truth comes
    from the poll, so a callback that cannot reach the panel only delays settling.
    """
    parts = urlsplit(base)
    return urlunsplit(("https", parts.hostname or "", "", "", "")) if parts.hostname else ""


def https_base_ready() -> bool:
    """BezanBeCard calls back and returns over https only."""
    return bool(https_base())



async def bbc_enable_error(session: AsyncSession, payload: dict[str, str], *, reseller_id: int | None) -> str | None:
    """A message when a saved payment tab would newly switch the gateway on without an https address.

    A gateway that is already on does not block unrelated payment settings from being saved.
    """
    if str(payload.get(ENABLED_KEY, "")).strip() != "1" or https_base_ready():
        return None
    from app.services.users import get_setting

    already_on = _on(await get_setting(session, ENABLED_KEY, "0", reseller_id=reseller_id))
    return None if already_on else HTTPS_REQUIRED
POLL_BATCH = 40
# Expired orders are still read for a while: staff may approve a late transfer in the dashboard.
LATE_PAYMENT_WINDOW = timedelta(hours=48)
MAX_EXPIRED_ATTEMPTS = 5
REVIEW_PREFIX = "review:"
REJECTED_PREFIX = "rejected:"
REFUSAL_TITLE = "❌ «بزن به کارت» ساخت سفارش را نپذیرفت"
REFUSAL_NOTICE_INTERVAL = 30 * 60
_last_refusal_notice: dict[int, float] = {}

REVIEW_REASONS = {
    "needs_review": "بزن به کارت این واریز را «نیاز به بررسی» زده است. در داشبورد بزن به کارت بررسی و تأییدش کنید؛ ربات بعد از تأیید خودش تحویل می‌دهد.",
    "order_mismatch": "شناسهٔ سفارشِ پرداخت‌شده با این پرداخت یکی نیست. خودکار تأیید نشد.",
    "amount_mismatch": "مبلغ سفارشِ پرداخت‌شده با مبلغ این پرداخت یکی نیست. خودکار تأیید نشد.",
    "unknown_token": "بزن به کارت این سفارش را نمی‌شناسد. خودکار تأیید نشد.",
    "late_payment": "سفارش بعد از پایان مهلت پرداخت شده است. خودکار تأیید نشد؛ دستی بررسی کنید.",
    "not_settled": "سفارش در بزن به کارت پرداخت شده ولی تأیید خودکار در ربات انجام نشد. دستی بررسی کنید.",
    "create_conflict": "ساخت سفارش در بزن به کارت با درخواست قبلی همین پرداخت ناسازگار بود. دستی بررسی کنید.",
}

# Failed rows are closed or in review; settled rows are done.
_VERIFIABLE = frozenset(
    {SettlementStatus.CREATED.value, SettlementStatus.AWAITING.value, SettlementStatus.EXPIRED.value}
)


@dataclass(frozen=True, slots=True)
class BbcCheckout:
    """What the payer needs: the link and the exact amount in Rial."""

    settlement_id: int
    payment_url: str
    unique_amount_rial: int


def _payload(settlement: PaymentSettlement) -> dict[str, Any]:
    try:
        data = json.loads(settlement.provider_payload or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _store(settlement: PaymentSettlement, data: dict[str, Any]) -> None:
    settlement.provider_payload = json.dumps(data, ensure_ascii=False)


def checkout_of(settlement: PaymentSettlement) -> BbcCheckout | None:
    order = _payload(settlement).get("order") or {}
    url = str(order.get("payment_url") or "")
    unique = order.get("unique_amount_rial")
    if settlement.status != SettlementStatus.AWAITING.value or not url.startswith("https://") or not unique:
        return None
    return BbcCheckout(settlement_id=int(settlement.id), payment_url=url, unique_amount_rial=int(unique))


async def _shop_api_key(session: AsyncSession, shop_owner_id: int | None) -> str:
    ui = await get_all_settings(session, reseller_id=shop_owner_id)
    if not _on(ui.get(ENABLED_KEY)):
        raise BbcNotReady("درگاه بزن به کارت غیرفعال است")
    key = str(ui.get(API_KEY) or "").strip()
    if len(key) < 8:
        raise BbcNotReady("کلید API بزن به کارت تنظیم نشده")
    return key


async def ensure_gateway_ready(session: AsyncSession, shop_owner_id: int | None) -> None:
    """Fail before any payment row is created when the shop's gateway cannot work."""
    await _shop_api_key(session, shop_owner_id)
    if not https_base_ready():
        raise BbcNotReady(HTTPS_REQUIRED)


# --- Create ------------------------------------------------------------------------


REUSE_TOPUP_WINDOW = timedelta(hours=1)


async def reusable_bbc_topup(
    session: AsyncSession, user_id: int, amount: int, *, shop_id: int | None
) -> Payment | None:
    """The person's own unpaid top-up of this amount in this shop whose link is still open.

    Tapping the method again shows that link instead of opening one more remote order.
    """
    since = datetime.now(timezone.utc) - REUSE_TOPUP_WINDOW
    shop = Payment.wallet_shop_id.is_(None) if shop_id is None else Payment.wallet_shop_id == int(shop_id)
    return (
        await session.execute(
            select(Payment)
            .join(PaymentSettlement, PaymentSettlement.payment_id == Payment.id)
            .where(
                Payment.user_id == int(user_id),
                Payment.is_wallet_topup.is_(True),
                shop,
                Payment.method == PaymentMethod.BBC.value,
                Payment.status == PaymentStatus.PENDING.value,
                Payment.amount == int(amount),
                PaymentSettlement.channel == CHANNEL_BBC,
                PaymentSettlement.status == SettlementStatus.AWAITING.value,
                PaymentSettlement.created_at >= since,
            )
            .order_by(Payment.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def start_bbc_checkout(
    session: AsyncSession,
    payment: Payment,
    *,
    reseller_id: int | None = None,
    description: str = "",
) -> BbcCheckout:
    """Create or reuse the remote order of ``payment``; raises ``ValueError`` with a safe text."""
    if payment.status != PaymentStatus.PENDING.value or int(payment.amount) <= 0:
        raise ValueError("این پرداخت قابل شروع نیست")
    shop_owner_id = await resolve_payment_shop_owner_id(session, payment)
    if reseller_id is not None and not _shop_ids_equal(shop_owner_id, int(reseller_id)):
        raise ValueError("پرداخت خارج از محدوده فروشگاه است")
    api_key = await _shop_api_key(session, shop_owner_id)
    if not https_base_ready():
        raise BbcNotReady(HTTPS_REQUIRED)
    settlement = await _settlement_for(session, payment, shop_owner_id)
    ready = checkout_of(settlement)
    if ready is not None:
        return ready
    if settlement.status in {SettlementStatus.SETTLING.value, SettlementStatus.SETTLED.value}:
        raise ValueError("این پرداخت قبلاً انجام شده است")
    request = await _saved_or_new_request(session, settlement, payment, description)
    return await _create_remote(session, settlement, api_key, request)


async def _settlement_for(session: AsyncSession, payment: Payment, shop_owner_id: int | None) -> PaymentSettlement:
    idem = idem_key_bbc(int(payment.id))
    existing = await _get_by_idem(session, idem)
    if existing is not None:
        return existing
    return await _insert_settlement_race_safe(
        session,
        PaymentSettlement(
            payment_id=int(payment.id),
            shop_owner_id=shop_owner_id,
            tenant_key=tenant_key_for(shop_owner_id),
            channel=CHANNEL_BBC,
            provider=BBC_PROVIDER,
            status=SettlementStatus.CREATED.value,
            amount=int(payment.amount),
            currency="IRT",
            idempotency_key=idem,
        ),
    )


async def _saved_or_new_request(
    session: AsyncSession, settlement: PaymentSettlement, payment: Payment, description: str
) -> bbc.BbcOrderRequest:
    """Reuse the saved request of an unfinished attempt; start a new attempt otherwise.

    A new attempt is allowed only after the API refused the previous one or it expired;
    an attempt in review is never replaced.
    """
    data = _payload(settlement)
    saved = data.get("request")
    if saved and settlement.status == SettlementStatus.CREATED.value:
        return bbc.BbcOrderRequest.from_dict(saved)
    if settlement.status == SettlementStatus.FAILED.value and not str(settlement.error_message or "").startswith(
        REJECTED_PREFIX
    ):
        raise ValueError("این پرداخت در حال بررسی است؛ با پشتیبانی در تماس باشید")
    if settlement.status == SettlementStatus.EXPIRED.value:
        _archive_expired_attempt(data)
    request = _new_request(settlement, payment, description, data)
    data["request"] = request.to_dict()
    data.pop("order", None)
    # Saved before the network call, so a crash after it leaves a request to replay.
    if await _swap_attempt(session, settlement, json.dumps(data, ensure_ascii=False)):
        return request
    # Another tap started this attempt first: replay its request instead of making a second order.
    saved = _payload(settlement).get("request")
    if saved and settlement.status == SettlementStatus.CREATED.value:
        return bbc.BbcOrderRequest.from_dict(saved)
    raise ValueError("پرداخت در حال آماده شدن است؛ چند لحظهٔ دیگر دوباره بزنید")


async def _swap_attempt(session: AsyncSession, settlement: PaymentSettlement, new_payload: str) -> bool:
    """Compare-and-set the attempt: only one caller replaces a given state."""
    old_payload = settlement.provider_payload
    same_payload = (
        PaymentSettlement.provider_payload.is_(None)
        if old_payload is None
        else PaymentSettlement.provider_payload == old_payload
    )
    result = await session.execute(
        update(PaymentSettlement)
        .where(PaymentSettlement.id == int(settlement.id), PaymentSettlement.status == settlement.status, same_payload)
        .values(
            provider_payload=new_payload,
            status=SettlementStatus.CREATED.value,
            external_ref=None,
            checkout_url=None,
            error_message=None,
        )
    )
    await session.commit()
    await session.refresh(settlement)
    return result.rowcount == 1


def _new_request(
    settlement: PaymentSettlement,
    payment: Payment,
    description: str,
    data: dict[str, Any],
) -> bbc.BbcOrderRequest:
    origin = gateway_origin(https_base())
    if not origin:
        raise BbcNotReady(HTTPS_REQUIRED)
    link_token = secrets.token_urlsafe(24)
    data["link_token"] = link_token
    attempt = secrets.token_hex(4)
    prefix = f"{origin}/payments/settlement/bbc/tenant/{int(settlement.tenant_key)}"
    # Both links stay on the panel host: the return page verifies at once and links to the shop's bot.
    return bbc.build_order_request(
        amount_toman=int(payment.amount),
        merchant_order_id=str(int(payment.id)),
        callback_url=f"{prefix}/callback/{int(settlement.id)}?t={link_token}",
        return_url=f"{prefix}/return/{int(settlement.id)}?t={link_token}",
        description=description or f"payment-{int(payment.id)}",
        idempotency_key=f"bbc:{int(payment.id)}:{attempt}",
    )


def _archive_expired_attempt(data: dict[str, Any]) -> None:
    token = str((data.get("order") or {}).get("payment_token") or "")
    if not token:
        return
    expired = [t for t in data.get("expired_tokens") or [] if isinstance(t, str)]
    data["expired_tokens"] = [*expired, token][-MAX_EXPIRED_ATTEMPTS:]


async def _create_remote(
    session: AsyncSession, settlement: PaymentSettlement, api_key: str, request: bbc.BbcOrderRequest
) -> BbcCheckout:
    try:
        order = await bbc.create_order(api_key=api_key, request=request)
    except bbc.BbcRejected as exc:
        if exc.status_code == 409:
            await _send_to_review(session, settlement, "create_conflict")
            raise ValueError(REVIEW_REASONS["create_conflict"]) from None
        settlement.status = SettlementStatus.FAILED.value
        settlement.error_message = f"{REJECTED_PREFIX}{exc.code or exc.status_code}"
        await session.commit()
        await _notify_refusal(session, settlement, exc)
        raise
    # BbcUnreachable propagates: the row stays CREATED with its request for a replay.
    data = _payload(settlement)
    data["order"] = {
        "order_id": order.order_id,
        "payment_token": order.payment_token,
        "payment_url": order.payment_url,
        "unique_amount_rial": order.unique_amount_rial,
        "base_amount_rial": order.base_amount_rial,
    }
    _store(settlement, data)
    settlement.external_ref = order.payment_token
    settlement.checkout_url = order.payment_url
    settlement.status = SettlementStatus.AWAITING.value
    await session.commit()
    return BbcCheckout(
        settlement_id=int(settlement.id),
        payment_url=order.payment_url,
        unique_amount_rial=int(order.unique_amount_rial or 0),
    )


# --- Verify and settle ---------------------------------------------------------------


async def verify_bbc_settlement(session: AsyncSession, settlement: PaymentSettlement) -> PaymentSettlement:
    """Read the order with the shop's key and act on its status; safe to run any number of times."""
    if settlement.channel != CHANNEL_BBC or settlement.status not in _VERIFIABLE:
        return settlement
    token = str(settlement.external_ref or "")
    if not token:
        return settlement
    api_key = await _shop_api_key(session, settlement.shop_owner_id)
    try:
        order = await bbc.get_order(api_key=api_key, payment_token=token)
    except bbc.BbcRejected as exc:
        if exc.code == "not_found" or exc.status_code == 404:
            await _send_to_review(session, settlement, "unknown_token")
        return settlement
    await _act_on(session, settlement, order)
    return settlement


async def _act_on(session: AsyncSession, settlement: PaymentSettlement, order: bbc.BbcOrder) -> None:
    if order.status == bbc.STATUS_NEEDS_REVIEW:
        await _send_to_review(session, settlement, "needs_review", keep_open=True)
    elif order.status == bbc.STATUS_EXPIRED:
        await _expire(session, settlement)
    elif order.status == bbc.STATUS_PAID:
        await _settle_paid(session, settlement, order)


def _mismatch(settlement: PaymentSettlement, order: bbc.BbcOrder) -> str | None:
    """Why a paid order must not settle this payment, or None when everything matches."""
    if order.merchant_order_id != str(int(settlement.payment_id)):
        return "order_mismatch"
    stored = (_payload(settlement).get("order") or {}).get("unique_amount_rial")
    if order.base_amount_rial != int(settlement.amount) * 10:
        return "amount_mismatch"
    if stored is None or order.unique_amount_rial != int(stored):
        return "amount_mismatch"
    return None


async def _settle_paid(session: AsyncSession, settlement: PaymentSettlement, order: bbc.BbcOrder) -> None:
    reason = _mismatch(settlement, order)
    if reason is not None:
        await _send_to_review(session, settlement, reason)
        return
    if settlement.status == SettlementStatus.EXPIRED.value:
        # Paid after its window: settle only if the payment still waits for it.
        payment = await session.get(Payment, int(settlement.payment_id))
        if payment is None or payment.status != PaymentStatus.PENDING.value or not await _reopen(session, settlement):
            await _send_to_review(session, settlement, "late_payment")
            return
    try:
        await settle_and_approve(
            session, settlement, expected_amount=int(settlement.amount), external_ref=order.payment_token
        )
    except ValueError:
        logger.warning("bbc settle failed settlement=%s", settlement.id, exc_info=True)
        await _send_to_review(session, settlement, "not_settled")


async def _reopen(session: AsyncSession, settlement: PaymentSettlement) -> bool:
    result = await session.execute(
        update(PaymentSettlement)
        .where(PaymentSettlement.id == int(settlement.id), PaymentSettlement.status == SettlementStatus.EXPIRED.value)
        .values(status=SettlementStatus.AWAITING.value, error_message=None)
    )
    await session.commit()
    await session.refresh(settlement)
    return result.rowcount == 1


async def _expire(session: AsyncSession, settlement: PaymentSettlement) -> None:
    await session.execute(
        update(PaymentSettlement)
        .where(
            PaymentSettlement.id == int(settlement.id),
            PaymentSettlement.status.in_((SettlementStatus.CREATED.value, SettlementStatus.AWAITING.value)),
        )
        .values(status=SettlementStatus.EXPIRED.value)
    )
    await session.commit()
    await session.refresh(settlement)


# --- Review and notices -------------------------------------------------------------


async def _send_to_review(
    session: AsyncSession, settlement: PaymentSettlement, reason: str, *, keep_open: bool = False
) -> None:
    """Hand the payment to the shop's staff, once per reason; never approve it here.

    ``keep_open`` leaves the row polled (the API may still settle it, as with
    ``needs_review``); otherwise it is closed as failed so nothing retries it.
    """
    marker = f"{REVIEW_PREFIX}{reason}"
    values: dict[str, Any] = {"error_message": marker}
    if not keep_open:
        values["status"] = SettlementStatus.FAILED.value
    claimed = await session.execute(
        update(PaymentSettlement)
        .where(
            PaymentSettlement.id == int(settlement.id),
            PaymentSettlement.status != SettlementStatus.SETTLED.value,
            or_(PaymentSettlement.error_message.is_(None), PaymentSettlement.error_message != marker),
        )
        .values(**values)
    )
    await session.commit()
    await session.refresh(settlement)
    if claimed.rowcount == 1:
        await _notify_staff(session, settlement, reason)


async def _notify_staff(session: AsyncSession, settlement: PaymentSettlement, reason: str) -> None:
    # Local import: notifications pulls in bot keyboards, which import payment modules.
    from app.services.notifications import notify_settlement_review

    payment = await session.get(Payment, int(settlement.payment_id))
    if payment is None:
        return
    try:
        await notify_settlement_review(
            session, payment, shop_id=settlement.shop_owner_id, reason=REVIEW_REASONS[reason]
        )
    except Exception:
        # The review marker is saved; the staff queue still shows the payment.
        logger.warning("bbc review notice failed settlement=%s", settlement.id, exc_info=True)


def _refusal_notice_due(tenant_key: int) -> bool:
    """At most one refusal notice per shop per window, so a broken setup does not flood staff."""
    now = time.monotonic()
    last = _last_refusal_notice.get(tenant_key)
    if last is not None and now - last < REFUSAL_NOTICE_INTERVAL:
        return False
    _last_refusal_notice[tenant_key] = now
    return True


async def _notify_refusal(session: AsyncSession, settlement: PaymentSettlement, exc: bbc.BbcRejected) -> None:
    """Tell the shop's staff why BezanBeCard refused to create the order; customers keep the fixed text."""
    if not _refusal_notice_due(int(settlement.tenant_key)):
        return
    # Local import: notifications pulls in bot keyboards, which import payment modules.
    from app.services.notifications import notify_settlement_review

    payment = await session.get(Payment, int(settlement.payment_id))
    if payment is None:
        return
    reason = html.escape(exc.reason or "بدون توضیح")
    code = html.escape(exc.code or str(exc.status_code))
    text = (
        f"پاسخ بزن به کارت ({code}): <code>{reason}</code>\n\n"
        "هیچ سفارشی ساخته نشد و مبلغی کسر نشده است. تنظیمات حساب و دامنهٔ پنل را در بزن به کارت بررسی کنید."
    )
    try:
        await notify_settlement_review(
            session, payment, shop_id=settlement.shop_owner_id, reason=text, title=REFUSAL_TITLE,
        )
    except Exception:
        # Best effort: the refusal is saved on the settlement and in the log.
        logger.warning("bbc refusal notice failed settlement=%s", settlement.id, exc_info=True)


async def claim_delivery_notice(session: AsyncSession, settlement: PaymentSettlement) -> bool:
    """True for exactly one caller once the settlement is settled: that caller notifies."""
    claimed = await session.execute(
        update(PaymentSettlement)
        .where(
            PaymentSettlement.id == int(settlement.id),
            PaymentSettlement.status == SettlementStatus.SETTLED.value,
            PaymentSettlement.notified_at.is_(None),
        )
        .values(notified_at=datetime.now(timezone.utc))
    )
    await session.commit()
    return claimed.rowcount == 1


async def deliver_once(session: AsyncSession, settlement: PaymentSettlement) -> None:
    """Send the delivery and the staff notice of a settled payment, at most once."""
    if not await claim_delivery_notice(session, settlement):
        return
    payment = await session.get(Payment, int(settlement.payment_id))
    if payment is None:
        return
    order = await session.get(Order, int(payment.order_id)) if payment.order_id else None
    try:
        await notify_after_settlement(session, payment, order)
    except Exception:
        # notify_after_settlement records delivery failures for the staff queue itself.
        logger.exception("bbc delivery notice failed payment=%s", payment.id)


# --- Callback / return links ---------------------------------------------------------


def link_token_matches(settlement: PaymentSettlement, token: str | None) -> bool:
    expected = str(_payload(settlement).get("link_token") or "")
    got = (token or "").strip()
    return bool(expected) and len(expected) == len(got) and hmac.compare_digest(expected, got)


async def handle_bbc_link(
    session: AsyncSession, *, settlement_id: int, tenant_key: int, token: str
) -> PaymentSettlement:
    """Callback and return link: right shop and token, then verify with the API and deliver once."""
    settlement = await session.get(PaymentSettlement, int(settlement_id))
    if (
        settlement is None
        or settlement.channel != CHANNEL_BBC
        or int(settlement.tenant_key) != int(tenant_key)
        or not link_token_matches(settlement, token)
    ):
        raise ValueError("تسویه یافت نشد")
    try:
        await verify_bbc_settlement(session, settlement)
    except (bbc.BbcUnreachable, ValueError):
        logger.info("bbc link could not verify settlement=%s now", settlement.id)
    await deliver_once(session, settlement)
    return settlement


# --- Background poll -----------------------------------------------------------------


async def poll_bbc_settlements(session: AsyncSession, *, limit: int = POLL_BATCH) -> int:
    """Re-read open orders and recently expired ones that are due; deliver what settled.

    Never-checked rows come first, then the oldest due. Each read pushes the row back
    by its age (``psp_poll_delay``), so expired or abandoned orders cannot fill every
    batch ahead of a new payment. Returns the count settled.
    """
    now = datetime.now(timezone.utc)
    since = now - LATE_PAYMENT_WINDOW
    rows = (
        await session.execute(
            select(PaymentSettlement)
            .where(
                PaymentSettlement.channel == CHANNEL_BBC,
                PaymentSettlement.external_ref.is_not(None),
                or_(
                    PaymentSettlement.status == SettlementStatus.AWAITING.value,
                    (PaymentSettlement.status == SettlementStatus.EXPIRED.value)
                    & (PaymentSettlement.updated_at >= since),
                ),
                or_(PaymentSettlement.next_check_at.is_(None), PaymentSettlement.next_check_at <= now),
            )
            .order_by(
                case((PaymentSettlement.next_check_at.is_(None), 0), else_=1),
                PaymentSettlement.next_check_at.asc(),
                PaymentSettlement.id.asc(),
            )
            .limit(max(1, min(100, int(limit))))
        )
    ).scalars().all()
    settled = 0
    for settlement in rows:
        await _schedule_next_check(session, settlement, now=now)
        if await _poll_one(session, settlement):
            settled += 1
    return settled


async def _schedule_next_check(session: AsyncSession, settlement: PaymentSettlement, *, now: datetime) -> None:
    """Commit the next read time before the API call.

    An expired order's age counts from its expiry (``updated_at``), which this write
    keeps unchanged so the late-payment window still ends on time.
    """
    expired = settlement.status == SettlementStatus.EXPIRED.value
    since = _as_utc(settlement.updated_at if expired else settlement.created_at) or now
    await session.execute(
        update(PaymentSettlement)
        .where(PaymentSettlement.id == int(settlement.id))
        .values(next_check_at=now + psp_poll_delay(now - since), updated_at=PaymentSettlement.updated_at)
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def _poll_one(session: AsyncSession, settlement: PaymentSettlement) -> bool:
    try:
        await verify_bbc_settlement(session, settlement)
        if settlement.status == SettlementStatus.EXPIRED.value:
            await _check_expired_attempts(session, settlement)
        await deliver_once(session, settlement)
    except (bbc.BbcUnreachable, ValueError):
        logger.info("bbc poll skipped settlement=%s", settlement.id)
        return False
    except Exception:
        logger.exception("bbc poll failed settlement=%s", settlement.id)
        await _rollback_quiet(session)
        return False
    return settlement.status == SettlementStatus.SETTLED.value


async def _check_expired_attempts(session: AsyncSession, settlement: PaymentSettlement) -> None:
    """An earlier attempt paid after a new one replaced it is never settled, only reviewed."""
    api_key = await _shop_api_key(session, settlement.shop_owner_id)
    for token in _payload(settlement).get("expired_tokens") or []:
        try:
            order = await bbc.get_order(api_key=api_key, payment_token=str(token))
        except bbc.BbcError:
            continue
        if order.status == bbc.STATUS_PAID:
            await _send_to_review(session, settlement, "late_payment")
            return


async def _rollback_quiet(session: AsyncSession) -> None:
    from app.services.db_safe import rollback_quiet

    await rollback_quiet(session)
