"""Explicit per-service wallet consent, shop isolation and durable purchase claims."""

from __future__ import annotations

import html
import logging
import math
import secrets
from datetime import datetime, timedelta, timezone

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BotUser,
    Order,
    OrderStatus,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Plan,
    ServiceAddonPack,
    ServiceAutomation,
    UserService,
)
from app.services.catalog_pricing import annotate_prices, displayed_quote, sale_price
from app.services.service_scope import service_shop_id
from app.services.formatting import format_toman, parse_expire
from app.services.users import (
    current_shop_reseller_id,
    get_all_settings,
    on,
    reset_shop_reseller_id,
    set_shop_reseller_id,
)

log = logging.getLogger(__name__)
ACTIONS = ("renew", "duration", "volume")
LABELS = {
    "renew": "تمدید خودکار",
    "duration": "افزایش خودکار زمان",
    "volume": "افزایش خودکار حجم",
}
CHOICE_FIELDS = {
    "renew": "renew_plan_id",
    "duration": "duration_pack_id",
    "volume": "volume_pack_id",
}


async def owned_automation_service(
    session: AsyncSession,
    user: BotUser,
    service_id: int,
) -> UserService:
    service = await session.get(UserService, service_id)
    if not service or service.bot_user_id != user.id:
        raise ValueError("سرویس یافت نشد")
    if user.is_blocked:
        raise ValueError("حساب شما مسدود است")
    if service.is_cancelled or service.cancellation_pending:
        raise ValueError("این سرویس لغو شده یا لغو آن در حال بررسی است")
    if not service.pg_user_id or (service.remark or "").strip() == "linked":
        raise ValueError("تنظیمات خودکار برای این سرویس قابل استفاده نیست")
    if await service_shop_id(session, service) != current_shop_reseller_id():
        raise ValueError("تنظیمات این سرویس را از فروشگاه خودش تغییر دهید")
    return service


async def get_automation(
    session: AsyncSession, service: UserService
) -> ServiceAutomation:
    row = await session.get(ServiceAutomation, service.id)
    if row:
        return row
    row = ServiceAutomation(
        service_id=service.id, shop_id=await service_shop_id(session, service)
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        row = await session.get(ServiceAutomation, service.id)
        if row is None:
            raise
    return row


async def automation_choices(session: AsyncSession) -> dict[str, list]:
    from app.services.orders import list_active_plans
    from app.services.service_addons import list_shop_packs

    plans = await list_active_plans(session, include_trial=False)
    packs = await list_shop_packs(session)
    return {
        "renew": [p for p in plans if p.price >= 0],
        "duration": [p for p in packs if p.kind == "duration" and valid_pack(p)],
        "volume": [p for p in packs if p.kind == "volume" and valid_pack(p)],
    }


async def automation_settings(
    session: AsyncSession, user: BotUser, service_id: int
) -> dict:
    service = await owned_automation_service(session, user, service_id)
    row = await get_automation(session, service)
    choices = await automation_choices(session)
    result = {
        "service_id": service_id,
        "needs_review": row.needs_review,
        "pending_order_id": row.pending_order_id,
        "actions": [],
    }
    for action in ACTIONS:
        enabled = getattr(row, f"{action}_enabled")
        choice_id = getattr(row, CHOICE_FIELDS[action])
        if action != "renew" and not (
            choices[action] or enabled or choice_id or getattr(row, f"{action}_notice")
        ):
            continue
        result["actions"].append(
            {
                "action": action,
                "label": LABELS[action],
                "enabled": enabled,
                "choice_id": choice_id,
                "unavailable": bool(enabled or choice_id)
                and await selected_choice(session, row, action) is None,
                "choices": [
                    {"id": c.id, "name": c.name, **displayed_quote(c).payload()}
                    for c in choices[action]
                ],
            }
        )
    await session.commit()
    return result


def valid_pack(pack: ServiceAddonPack) -> bool:
    return bool(
        pack.price >= 0
        and math.isfinite(pack.amount)
        and (
            int(pack.amount) >= 1
            if pack.kind == "duration"
            else int(pack.amount * 1024**3) >= 1
        )
    )


async def selected_choice(session: AsyncSession, row: ServiceAutomation, action: str):
    choice_id = getattr(row, CHOICE_FIELDS[action])
    if not choice_id:
        return None
    with session.no_autoflush:
        choice = await session.get(
            Plan if action == "renew" else ServiceAddonPack, choice_id
        )
    if not choice or not choice.is_active or choice.owner_reseller_id != row.shop_id:
        return None
    if action == "renew":
        return choice if not choice.is_trial and choice.price >= 0 else None
    return choice if choice.kind == action and valid_pack(choice) else None


async def claim_automation(session: AsyncSession, service_id: int) -> str | None:
    now = datetime.now(timezone.utc)
    token = secrets.token_hex(16)
    result = await session.execute(
        update(ServiceAutomation)
        .where(
            ServiceAutomation.service_id == service_id,
            or_(
                ServiceAutomation.locked_until.is_(None),
                ServiceAutomation.locked_until < now,
            ),
        )
        .values(lock_token=token, locked_until=now + timedelta(minutes=10))
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return token if result.rowcount == 1 else None


async def release_automation(
    session: AsyncSession, service_id: int, token: str
) -> None:
    await session.execute(
        update(ServiceAutomation)
        .where(
            ServiceAutomation.service_id == service_id,
            ServiceAutomation.lock_token == token,
        )
        .values(lock_token=None, locked_until=None)
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def configure_automation(
    session: AsyncSession,
    user: BotUser,
    service_id: int,
    action: str,
    *,
    enabled: bool,
    choice_id: int | None = None,
) -> ServiceAutomation:
    if action not in ACTIONS:
        raise ValueError("تنظیم نامعتبر است")
    service = await owned_automation_service(session, user, service_id)
    row = await get_automation(session, service)
    token = await claim_automation(session, service_id)
    if not token:
        raise ValueError("عملیات سرویس در حال اجراست؛ کمی بعد تلاش کنید")
    try:
        await session.refresh(row)
        if choice_id is not None:
            setattr(row, CHOICE_FIELDS[action], choice_id)
            if await selected_choice(session, row, action) is None:
                raise ValueError("پلن یا بسته در دسترس نیست؛ گزینه جدید انتخاب کنید")
        if enabled:
            ui = await get_all_settings(session)
            if not on(ui.get("pay_wallet_enabled", "1")):
                raise ValueError("پرداخت با کیف پول غیرفعال است")
            if row.needs_review or row.pending_order_id:
                raise ValueError("سفارش خودکار قبلی نیاز به بررسی دارد")
            if await selected_choice(session, row, action) is None:
                raise ValueError("پلن یا بسته در دسترس نیست؛ گزینه جدید انتخاب کنید")
            if service.quota_synced_at is not None:
                if action == "duration" and service.quota_expire_at is None:
                    raise ValueError("این سرویس زمان نامحدود دارد")
                if action == "volume" and int(service.quota_data_limit_bytes or 0) <= 0:
                    raise ValueError("این سرویس حجم نامحدود دارد")
        setattr(row, f"{action}_enabled", enabled)
        setattr(row, f"{action}_notice", None)
        await session.commit()
        return row
    except Exception:
        await session.rollback()
        raise
    finally:
        await release_automation(session, service_id, token)


def exhausted_dimensions(info: dict, now: datetime) -> tuple[bool, bool]:
    """Only actual finite quotas trigger purchases; disabled/on-hold do not."""
    status = str(info.get("status") or "").lower()
    if status not in {"active", "expired", "limited"}:
        return False, False
    expire = parse_expire(info.get("expire") or info.get("expire_date"))
    time_done = expire is not None and expire <= now
    try:
        limit = float(info.get("data_limit") or 0)
        used = float(info.get("used_traffic") or 0)
        volume_done = (
            math.isfinite(limit) and math.isfinite(used) and limit > 0 and used >= limit
        )
    except (TypeError, ValueError):
        volume_done = False
    return time_done, volume_done


def actions_for_exhaustion(
    row: ServiceAutomation, time_done: bool, volume_done: bool
) -> list[str]:
    # A full renewal restores both dimensions: never also buy quota packs.
    if (time_done and not row.duration_enabled) or (
        volume_done and not row.volume_enabled
    ):
        if row.renew_enabled:
            return ["renew"]
    return [
        a
        for a, due in (("duration", time_done), ("volume", volume_done))
        if due and getattr(row, f"{a}_enabled")
    ]


async def notify_once(
    session: AsyncSession,
    row: ServiceAutomation,
    service: UserService,
    user: BotUser,
    bot,
    action: str,
    code: str,
    body: str,
) -> bool:
    if getattr(row, f"{action}_notice") == code:
        return True
    try:
        await bot.send_message(
            user.telegram_id,
            f"⚙️ <b>{LABELS[action]}</b> — {html.escape(service.pg_username)}\n\n{body}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="⚙️ تنظیمات خودکار سرویس",
                            callback_data=f"svc:auto:{service.id}",
                        ),
                    ]
                ]
            ),
        )
    except Exception:
        log.warning(
            "automation notification failed service=%s", service.id, exc_info=True
        )
        return False
    setattr(row, f"{action}_notice", code)
    await session.commit()
    return True


async def _finish_order(session, row, service, user, bot, order) -> None:
    action = row.pending_action
    if not action or action not in ACTIONS:
        row.needs_review = True
        await session.commit()
        return
    if await notify_once(
        session,
        row,
        service,
        user,
        bot,
        action,
        f"ok:{order.id}",
        f"✅ با موفقیت انجام شد.\nسفارش #{order.id}\n"
        f"مبلغ پرداختی از کیف پول: <b>{format_toman(order.amount)}</b>",
    ):
        row.pending_order_id = None
        row.pending_action = None
        row.needs_review = False
        await session.commit()


async def _process_locked(session, row, service, user, bot) -> None:
    from app.services.orders import (
        _claim_payable_order,
        apply_renewal,
        renew_service_with_plan,
    )
    from app.services.service_addons import apply_service_addon, create_addon_order
    from app.services.wallet import debit_wallet, get_wallet_balance

    if row.pending_order_id:
        order = await session.get(Order, row.pending_order_id)
        if order and order.status == OrderStatus.DELIVERED.value:
            await _finish_order(session, row, service, user, bot, order)
        elif order:
            # A restart or network failure after debit is ambiguous. Never buy
            # again or repeat a remote mutation; the normal admin order flow
            # can reconcile/retry this paid order.
            row.needs_review = True
            await session.commit()
            await notify_once(
                session,
                row,
                service,
                user,
                bot,
                row.pending_action,
                "review",
                f"⚠️ سفارش خودکار #{order.id} نیاز به بررسی پشتیبانی دارد. "
                "تا تعیین وضعیت سفارش، اجرای خودکار متوقف است و مبلغ دوباره کسر نمی‌شود.",
            )
        else:
            row.needs_review = True
            await session.commit()
        return
    if row.needs_review:
        return
    # Wait for any already-paid manual mutation of this service to finish.
    outstanding = (
        await session.execute(
            select(Order.id)
            .where(
                Order.service_id == service.id,
                Order.status.in_(
                    [OrderStatus.PAID.value, OrderStatus.DELIVERING.value]
                ),
            )
            .limit(1)
        )
    ).first()
    if outstanding:
        return

    choices = {}
    for action in ACTIONS:
        if not getattr(row, f"{action}_enabled"):
            continue
        choice = await selected_choice(session, row, action)
        if choice is None:
            await notify_once(
                session,
                row,
                service,
                user,
                bot,
                action,
                "unavailable",
                "⚠️ پلن یا بسته انتخاب‌شده حذف یا غیرفعال شده است. "
                "از تنظیمات خودکار سرویس، پلن یا بسته جدید انتخاب کنید؛ این گزینه تا آن زمان اجرا نمی‌شود.",
            )
        else:
            choices[action] = choice
    if not choices:
        return
    ui = await get_all_settings(session)
    if not on(ui.get("pay_wallet_enabled", "1")):
        for action in choices:
            await notify_once(
                session,
                row,
                service,
                user,
                bot,
                action,
                "wallet_off",
                "پرداخت کیف پول فروشگاه غیرفعال است؛ اجرای خودکار متوقف شده است.",
            )
        return
    from app.services.pasarguard import get_pg, get_pg_for_reseller

    pg = await get_pg_for_reseller(session, row.shop_id) if row.shop_id else get_pg()
    info = await pg.get_user_by_id(service.pg_user_id)
    if not isinstance(info, dict):
        return
    from app.services.bot_user_admin import sync_service_quota_cache

    sync_service_quota_cache(service, info)
    await session.commit()
    time_done, volume_done = exhausted_dimensions(info, datetime.now(timezone.utc))
    await annotate_prices(session, list(choices.values()), shop_id=row.shop_id)
    for action in actions_for_exhaustion(row, time_done, volume_done):
        choice = choices.get(action)
        if choice is None:
            # Missing selected pack must not silently fall back to a full plan.
            continue
        await session.refresh(user)
        if user.is_blocked:
            return
        if await get_wallet_balance(session, user, shop_id=row.shop_id) < sale_price(choice):
            await notify_once(
                session,
                row,
                service,
                user,
                bot,
                action,
                "low_wallet",
                f"موجودی کیف پول کافی نیست. هزینه فعلی: <b>{format_toman(sale_price(choice))}</b>. "
                "کیف پول همین فروشگاه را شارژ کنید؛ اجرای خودکار دوباره تلاش می‌کند.",
            )
            continue
        # Revalidate immediately before taking money (catalog may change during PG read).
        await session.refresh(choice)
        if await selected_choice(session, row, action) is None:
            return
        if action == "renew":
            order = await renew_service_with_plan(
                session, user_id=user.id, service=service, plan=choice, commit=False
            )
            # Keep automatic renewal semantics on normal admin delivery recovery.
            order.note = f"renew:{service.id}:auto"
        else:
            order = await create_addon_order(
                session, user_id=user.id, service=service, pack=choice, commit=False
            )
        row.pending_order_id = order.id
        row.pending_action = action
        await session.flush()
        await _claim_payable_order(
            session, order, payment_method=PaymentMethod.WALLET.value
        )
        try:
            if order.amount > 0:
                await debit_wallet(
                    session,
                    user,
                    order.amount,
                    f"{LABELS[action]} #{order.id}",
                    shop_id=row.shop_id,
                    commit=False,
                )
            session.add(
                Payment(
                    order_id=order.id,
                    user_id=user.id,
                    amount=order.amount,
                    method=PaymentMethod.WALLET.value,
                    status=PaymentStatus.APPROVED.value,
                    wallet_shop_id=row.shop_id,
                )
            )
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        # Payment is durable before PG. Uncertain delivery keeps the order PAID
        # for review, avoiding a refund followed by another capacity purchase.
        try:
            if action == "renew":
                order = await apply_renewal(
                    session, order, service, choice, reset_traffic=True
                )
            else:
                order = await apply_service_addon(session, order)
        except Exception:
            log.exception(
                "automatic delivery needs review service=%s order=%s",
                service.id,
                order.id,
            )
            await session.rollback()
            await session.refresh(row)
            await session.refresh(order)
            await session.refresh(service)
            await session.refresh(user)
            if order.status != OrderStatus.DELIVERED.value:
                row.needs_review = True
                await session.commit()
                await notify_once(
                    session,
                    row,
                    service,
                    user,
                    bot,
                    action,
                    "review",
                    f"⚠️ اجرای سفارش خودکار #{order.id} نیاز به بررسی پشتیبانی دارد. تا تعیین وضعیت، خرید خودکار دیگری انجام نمی‌شود.",
                )
                return
        await _finish_order(session, row, service, user, bot, order)
        if row.pending_order_id:
            return


async def process_service_automation(
    session: AsyncSession, service_id: int, bot
) -> None:
    token = await claim_automation(session, service_id)
    if not token:
        return
    context_token = None
    try:
        row = await session.get(ServiceAutomation, service_id, populate_existing=True)
        service = await session.get(UserService, service_id)
        user = await session.get(BotUser, service.bot_user_id) if service and service.bot_user_id is not None and service.integration_id is None else None
        if not row or not service or not user or user.is_blocked:
            return
        if not service.pg_user_id or (service.remark or "").strip() == "linked":
            return
        if await service_shop_id(session, service) != row.shop_id:
            return
        context_token = set_shop_reseller_id(row.shop_id)
        await _process_locked(session, row, service, user, bot)
    finally:
        if context_token is not None:
            reset_shop_reseller_id(context_token)
        await session.rollback()
        await release_automation(session, service_id, token)
