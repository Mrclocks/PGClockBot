"""Wallet top-up outside the bot chat (Mini App): options, start, receipt, status.

The money rules stay where they already live: ``create_wallet_topup`` creates the
pending payment, ``create_psp_checkout`` the gateway session, ``submit_receipt``
reviews or auto-approves a receipt, and approval credits the wallet once. This module
only chooses a destination, enforces the Mini App's limits and turns an uploaded
image into the Telegram file the reviewers already work with.

Every function takes the purse's ``shop_id`` (None is the platform's). It is stored on
the payment at creation, and receipts and status are only ever read back for that same
purse, so a shop's top-up can never credit, or be shown as, another purse.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Payment, PaymentMethod, PaymentStatus
from app.services.db_safe import rollback_quiet
from app.services.message_variables import DOMAIN_PAYMENT, render_message_template
from app.services.orders import MAX_TOPUP_AMOUNT, MIN_TOPUP_AMOUNT, create_wallet_topup
from app.services.payment_destinations import (
    card_by_id,
    crypto_by_id,
    enabled_cards,
    enabled_crypto_wallets,
    enabled_gateways,
    gateway_by_id,
    pick_card,
    random_card_enabled,
)
from app.services.payment_settlement import (
    PspCheckOutcome,
    create_card_auto_awaiting,
    create_psp_checkout,
    public_checkout_button_url,
)
from app.services.psp_gateways import enabled_psp_gateways
from app.services.receipts import submit_receipt
from app.services.rich_text import visible_text
from app.services.users import on
from app.services.chat_records import keeps_sent

logger = logging.getLogger(__name__)

# key -> (enable flag, Payment.method)
TOPUP_METHODS: dict[str, tuple[str, str]] = {
    "card": ("pay_card_enabled", PaymentMethod.CARD.value),
    "gateway": ("pay_gateway_enabled", PaymentMethod.GATEWAY.value),
    "psp": ("pay_psp_enabled", PaymentMethod.PSP.value),
    "bbc": ("pay_bbc_enabled", PaymentMethod.BBC.value),
    "crypto": ("pay_crypto_enabled", PaymentMethod.CRYPTO.value),
}
# BezanBeCard needs its matching checkout screen, which only the bot has.
OUTSIDE_BOT_METHODS = ("card", "gateway", "psp", "crypto")
RECEIPT_METHODS = frozenset({"card", "gateway", "crypto"})
METHOD_LABELS = {
    "card": ("btn_pay_card", "کارت به کارت"),
    "gateway": ("btn_pay_gateway", "درگاه پرداخت"),
    "psp": ("btn_pay_psp", "پرداخت آنلاین"),
    "crypto": ("btn_pay_crypto", "رمزارز"),
}
DEFAULT_TOPUP_PRESETS: tuple[int, ...] = (50_000, 100_000, 200_000, 500_000)
PRESET_LIMIT = 8
# Unpaid top-ups without a receipt a customer may hold at once (stale ones expire).
OPEN_TOPUP_LIMIT = 3
OPEN_TOPUP_WINDOW = timedelta(days=1)
RECENT_TOPUP_LIMIT = 5
# Telegram accepts photos up to 10 MB; stay below it.
RECEIPT_MAX_BYTES = 8 * 1024 * 1024
_DEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_IMAGE_SIGNATURES = (
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
)

STATUS_LABELS = {
    PaymentStatus.PENDING.value: "در انتظار",
    PaymentStatus.APPROVED.value: "تأیید شد",
    PaymentStatus.REJECTED.value: "رد شد",
}


PSP_CHECK_MESSAGES = {
    PspCheckOutcome.SETTLED: "پرداخت تأیید شد و کیف پول شارژ می‌شود.",
    PspCheckOutcome.REVIEW: "پرداخت ثبت شده و در حال بررسی است؛ نتیجه در ربات اطلاع داده می‌شود.",
    PspCheckOutcome.PENDING: "هنوز پرداختی در درگاه ثبت نشده است. اگر پرداخت کرده‌اید، کمی بعد دوباره بررسی کنید.",
    PspCheckOutcome.CLOSED: "این پرداخت دیگر باز نیست.",
}


class TopupError(ValueError):
    """A top-up rule the customer must see (Persian, safe to show)."""


def parse_topup_presets(ui: dict | None = None) -> list[int]:
    """CSV of toman amounts from ``wallet_topup_presets`` (falls back to defaults)."""
    raw = str((ui or {}).get("wallet_topup_presets") or "").strip()
    out: list[int] = []
    if raw:
        for part in raw.replace("،", ",").split(","):
            part = part.replace(",", "").replace("٬", "").strip()
            if not part:
                continue
            try:
                n = int(part)
            except ValueError:
                continue
            if n >= MIN_TOPUP_AMOUNT:
                out.append(n)
    return out or list(DEFAULT_TOPUP_PRESETS)


def _destination_label(method: str, item: dict[str, Any]) -> str:
    if method == "card":
        label = f"…{str(item.get('number') or '')[-4:]}"
        return f"{label} ({str(item['holder'])[:12]})" if item.get("holder") else label
    if method in ("gateway", "psp"):
        return str(item.get("name") or "درگاه")[:24]
    return f"{item.get('asset') or 'USDT'} …{str(item.get('address') or '')[-6:]}"


def _destinations(method: str, ui: dict) -> list[dict[str, Any]]:
    if method == "card":
        return enabled_cards(ui)
    if method == "gateway":
        return enabled_gateways(ui)
    if method == "crypto":
        return enabled_crypto_wallets(ui)
    if method == "psp":
        return [{"id": g.id, "name": g.name} for g in enabled_psp_gateways(ui)]
    return []


def _psp_gateway_by_id(ui: dict, dest_id: str) -> dict[str, Any] | None:
    return next((item for item in _destinations("psp", ui) if item["id"] == dest_id), None)


def _method_choices(method: str, ui: dict) -> list[dict[str, str]]:
    """Destinations the customer picks from; empty when the shop picks one for them."""
    items = _destinations(method, ui)
    if len(items) < 2 or (method == "card" and random_card_enabled(ui)):
        return []
    return [{"id": str(item["id"]), "label": _destination_label(method, item)} for item in items[:PRESET_LIMIT]]


def topup_options(ui: dict) -> dict[str, Any]:
    """Methods usable outside the bot (with destination choices), presets and bounds."""
    methods = []
    for key in OUTSIDE_BOT_METHODS:
        flag, _method = TOPUP_METHODS[key]
        if not on(ui.get(flag)):
            continue
        if not _destinations(key, ui):
            continue
        label_key, fallback = METHOD_LABELS[key]
        methods.append({
            "key": key,
            "label": visible_text(ui.get(label_key)) or fallback,
            "needs_receipt": key in RECEIPT_METHODS,
            "choices": _method_choices(key, ui),
        })
    return {
        "methods": methods,
        "presets": parse_topup_presets(ui)[:PRESET_LIMIT],
        "min_amount": MIN_TOPUP_AMOUNT,
        "max_amount": MAX_TOPUP_AMOUNT,
        "bot_only": on(ui.get(TOPUP_METHODS["bbc"][0])),
    }


@dataclass(slots=True)
class TopupStart:
    """What the customer needs to pay one top-up; only the chosen method's fields are set."""

    payment_id: int
    method: str
    amount: int
    needs_receipt: bool
    card_number: str | None = None
    card_holder: str | None = None
    gateway_name: str | None = None
    gateway_link: str | None = None
    crypto_asset: str | None = None
    crypto_network: str | None = None
    crypto_address: str | None = None
    checkout_url: str | None = None
    settlement_id: int | None = None
    note: str | None = None

    def payload(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


def _validate_amount(amount: object) -> int:
    if type(amount) is not int or not MIN_TOPUP_AMOUNT <= amount <= MAX_TOPUP_AMOUNT:
        raise TopupError(f"مبلغ شارژ باید بین {MIN_TOPUP_AMOUNT:,} و {MAX_TOPUP_AMOUNT:,} باشد")
    return amount


def _choose_destination(method: str, ui: dict, dest_id: object, seed: str) -> dict[str, Any]:
    items = _destinations(method, ui)
    if not items:
        raise TopupError("این روش پرداخت در حال حاضر تنظیم نشده است")
    if dest_id is not None:
        if not isinstance(dest_id, str) or not _DEST_ID.match(dest_id):
            raise TopupError("مقصد پرداخت نامعتبر است")
        lookup = {"card": card_by_id, "gateway": gateway_by_id, "crypto": crypto_by_id, "psp": _psp_gateway_by_id}[method]
        chosen = lookup(ui, dest_id)
        if chosen is None:
            raise TopupError("این مقصد پرداخت دیگر فعال نیست")
        if method == "card" and random_card_enabled(ui):
            raise TopupError("کارت را فروشگاه انتخاب می‌کند")
        return chosen
    if len(items) > 1 and _method_choices(method, ui):
        raise TopupError("یکی از مقصدهای پرداخت را انتخاب کنید")
    return pick_card(items, seed) if method == "card" and len(items) > 1 else items[0]


def _purse(shop_id: int | None):
    return Payment.wallet_shop_id.is_(None) if shop_id is None else Payment.wallet_shop_id == int(shop_id)


async def _assert_open_topup_room(session: AsyncSession, user_id: int, shop_id: int | None, now: datetime) -> None:
    open_count = await session.scalar(
        select(func.count())
        .select_from(Payment)
        .where(
            Payment.user_id == int(user_id),
            Payment.is_wallet_topup.is_(True),
            _purse(shop_id),
            Payment.status == PaymentStatus.PENDING.value,
            Payment.receipt_file_id.is_(None),
            Payment.created_at >= now - OPEN_TOPUP_WINDOW,
        )
    )
    if int(open_count or 0) >= OPEN_TOPUP_LIMIT:
        raise TopupError("چند شارژ پرداخت‌نشده دارید؛ ابتدا رسید یکی را بفرستید یا کمی بعد دوباره تلاش کنید")


def _gateway_link(item: dict[str, Any], payment: Payment) -> str | None:
    link = str(item.get("link") or "").strip()
    if not link:
        return None
    try:
        link = render_message_template(
            link, domain=DOMAIN_PAYMENT, amount=payment.amount, order_id=0,
            payment_id=payment.id, html=False,
        )
    except ValueError:
        logger.warning("top-up gateway link template rejected payment=%s", payment.id, exc_info=True)
    return link if link.startswith(("https://", "http://")) else None


async def start_topup(
    session: AsyncSession,
    *,
    user_id: int,
    shop_id: int | None,
    amount: object,
    method: object,
    dest_id: object,
    ui: dict,
    now: datetime | None = None,
) -> TopupStart:
    """Create one top-up payment for the ``shop_id`` purse and return how to pay it.

    ``ui`` must be that purse's own settings, so only its cards and gateways are offered.

    Raises ``TopupError`` for any rule the customer must see; nothing is created then.
    A ``ValueError`` from preparing the checkout closes the new payment before it is
    re-raised: the customer never saw where to pay, so it cannot have been paid.
    """
    if not isinstance(method, str) or method not in OUTSIDE_BOT_METHODS:
        raise TopupError("روش پرداخت نامعتبر است")
    flag, payment_method = TOPUP_METHODS[method]
    if not on(ui.get(flag)):
        raise TopupError("این روش پرداخت غیرفعال است")
    value = _validate_amount(amount)
    destination = _choose_destination(method, ui, dest_id, f"topup:{user_id}:{value}")
    await _assert_open_topup_room(session, user_id, shop_id, now or datetime.now(timezone.utc))
    payment = await create_wallet_topup(session, int(user_id), value, method=payment_method, wallet_shop_id=shop_id)
    payment_id = int(payment.id)
    try:
        return await _prepare_payment(session, payment, method, destination, ui)
    except ValueError:
        await _close_unshown_topup(session, payment_id)
        raise


async def _close_unshown_topup(session: AsyncSession, payment_id: int) -> None:
    await rollback_quiet(session)
    await session.execute(
        update(Payment)
        .where(
            Payment.id == payment_id,
            Payment.status == PaymentStatus.PENDING.value,
            Payment.receipt_file_id.is_(None),
        )
        .values(status=PaymentStatus.REJECTED.value, review_note="checkout could not be prepared")
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def _prepare_payment(
    session: AsyncSession, payment: Payment, method: str, destination: dict[str, Any] | None, ui: dict
) -> TopupStart:
    value = int(payment.amount)
    start = TopupStart(
        payment_id=int(payment.id), method=method, amount=value, needs_receipt=method in RECEIPT_METHODS,
    )
    if method == "psp":
        settlement = await create_psp_checkout(
            session, payment, description=f"شارژ کیف پول #{payment.id}", gateway_id=str(destination["id"])
        )
        url = public_checkout_button_url(settlement, ui=ui)
        start.checkout_url = url if url.startswith(("https://", "http://")) else None
        start.settlement_id = int(settlement.id)
    elif method == "card":
        start.card_number = str(destination.get("number") or "")
        start.card_holder = str(destination.get("holder") or "") or None
        if on(ui.get("pay_card_auto_enabled")):
            await create_card_auto_awaiting(session, payment)
            start.note = visible_text(ui.get("card_auto_hint_text")) or None
    elif method == "gateway":
        start.gateway_name = str(destination.get("name") or "درگاه")
        start.gateway_link = _gateway_link(destination, payment)
    else:
        start.crypto_asset = str(destination.get("asset") or "USDT")
        start.crypto_network = str(destination.get("network") or "") or None
        start.crypto_address = str(destination.get("address") or "")
    return start


def receipt_image_kind(data: bytes) -> str | None:
    """'jpg' or 'png' when the bytes really are that image, else None."""
    for signature, kind in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return kind
    return None


async def _receipt_payment(session: AsyncSession, *, user_id: int, shop_id: int | None, payment_id: int) -> Payment:
    """The caller's own pending top-up in this purse that still waits for a receipt."""
    payment = await session.get(Payment, int(payment_id))
    purse = int(payment.wallet_shop_id) if payment is not None and payment.wallet_shop_id else None
    if (
        payment is None
        or int(payment.user_id) != int(user_id)
        or not payment.is_wallet_topup
        or purse != shop_id
    ):
        raise TopupError("پرداخت پیدا نشد")
    if payment.method not in {TOPUP_METHODS[key][1] for key in RECEIPT_METHODS}:
        raise TopupError("این پرداخت رسید لازم ندارد")
    if payment.status != PaymentStatus.PENDING.value:
        raise TopupError("این پرداخت دیگر باز نیست")
    if payment.receipt_file_id:
        raise TopupError("رسید این پرداخت قبلاً ثبت شده است")
    return payment


@dataclass(slots=True)
class ReceiptResult:
    status: str
    message: str


@keeps_sent
async def submit_topup_receipt(
    session: AsyncSession,
    *,
    bot: Bot,
    user_id: int,
    shop_id: int | None,
    telegram_id: int,
    payment_id: int,
    image: bytes,
) -> ReceiptResult:
    """Send the image to the customer's own chat, then hand the photo to ``submit_receipt``.

    The photo in their chat is the customer's copy and gives reviewers the same
    Telegram file as a receipt sent in the bot, so ``bot`` must be the purse's own bot.
    Only the first receipt is accepted.
    """
    if not image or len(image) > RECEIPT_MAX_BYTES:
        raise TopupError("حجم تصویر رسید باید کمتر از ۸ مگابایت باشد")
    kind = receipt_image_kind(image)
    if kind is None:
        raise TopupError("فقط تصویر JPG یا PNG رسید را بفرستید")
    payment = await _receipt_payment(session, user_id=user_id, shop_id=shop_id, payment_id=payment_id)
    # End the read transaction before talking to Telegram.
    await session.commit()
    try:
        sent = await bot.send_photo(
            chat_id=int(telegram_id),
            photo=BufferedInputFile(image, filename=f"receipt-{int(payment.id)}.{kind}"),
            caption=f"رسید شارژ کیف پول #{int(payment.id)} از مینی‌اپ",
        )
    except TelegramAPIError as exc:
        logger.warning("mini app receipt upload failed payment=%s", payment.id, exc_info=True)
        raise TopupError("ارسال رسید ناموفق بود؛ مطمئن شوید ربات را در تلگرام Start کرده‌اید و دوباره تلاش کنید") from exc
    photo = (sent.photo or [None])[-1]
    if photo is None:
        raise TopupError("ارسال رسید ناموفق بود؛ دوباره تلاش کنید")
    try:
        await submit_receipt(
            session, payment, bot=bot, file_id=photo.file_id,
            file_unique_id=photo.file_unique_id, user_tg_id=int(telegram_id), first_only=True,
        )
    except ValueError as exc:
        raise TopupError("رسید این پرداخت قبلاً ثبت شده یا پرداخت دیگر باز نیست") from exc
    await session.refresh(payment)
    if not payment.receipt_file_id:
        # Blocked by the duplicate-receipt policy: nothing was attached.
        raise TopupError("این تصویر قبلاً برای پرداخت دیگری ثبت شده است")
    if payment.status == PaymentStatus.APPROVED.value:
        return ReceiptResult(status="approved", message="کیف پول شارژ شد")
    return ReceiptResult(status="pending", message="رسید ثبت شد؛ پس از تأیید، کیف پول شارژ می‌شود")


async def recent_topups(
    session: AsyncSession, user_id: int, *, shop_id: int | None, limit: int = RECENT_TOPUP_LIMIT
) -> list[dict[str, Any]]:
    """The caller's latest top-ups to this purse and where each one stands."""
    rows = await session.scalars(
        select(Payment)
        .where(
            Payment.user_id == int(user_id),
            Payment.is_wallet_topup.is_(True),
            _purse(shop_id),
        )
        .order_by(Payment.id.desc())
        .limit(limit)
    )
    out = []
    for p in rows:
        method = next((key for key, (_flag, value) in TOPUP_METHODS.items() if value == p.method), p.method)
        waiting_receipt = p.status == PaymentStatus.PENDING.value and not p.receipt_file_id and method in RECEIPT_METHODS
        out.append({
            "id": int(p.id),
            "amount": int(p.amount),
            "method": method,
            "status": p.status,
            "status_fa": "منتظر رسید" if waiting_receipt else STATUS_LABELS.get(p.status, "بسته شد"),
            "waiting_receipt": waiting_receipt,
            "created_at": _iso(p.created_at),
        })
    return out


def _iso(value: datetime | None) -> str | None:
    if not isinstance(value, datetime):
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()
