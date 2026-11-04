"""Shop-bot staff: live service info and edit for a shop customer (mirrors platform admin).

Scoped like the web ``/users`` service actions: shop from the bot the update arrived on,
``dashboard`` permission, and ``get_owned_service(..., shop_id=owner)`` on every mutation.
"""

from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.nav_input import ask_text, finish_text_step, try_legacy_cancel
from app.bot.tg_utils import parse_bot_float, parse_bot_int, safe_edit_text
from app.db.models import BotUser, Plan
from app.services import bot_feedback_texts as feedback
from app.services.chat_records import keep_sent
from app.services.redact import user_safe_error
from app.services.reseller_access import load_reseller_actor
from app.services.resellers import has_bot_perm
from app.services.shop_membership import is_shop_customer
from app.services.staff_cards import format_adjust_prompt, format_services_list_header

logger = logging.getLogger(__name__)
router = Router(name="reseller_customer_services")

PERMISSION = "dashboard"
CANCEL_CODE = "rs_svc"
PREFIX = "res"


class ResellerServiceStates(StatesGroup):
    adjust_days = State()
    adjust_gb = State()


def services_button(user_id: int) -> InlineKeyboardButton:
    """Entry from the customer card into live service management."""
    return InlineKeyboardButton(
        text="📦 اطلاعات و ویرایش سرویس",
        callback_data=f"{PREFIX}:svcs:{int(user_id)}",
    )


def manage_service_button(user_id: int, service_id: int) -> InlineKeyboardButton:
    """Jump straight to one service (subscription-link search)."""
    return InlineKeyboardButton(
        text="⚙️ مدیریت سرویس",
        callback_data=f"{PREFIX}:svc:{int(user_id)}:{int(service_id)}",
    )


def _services_keyboard(user_id: int, service_ids: list[int]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"سرویس #{sid}",
                callback_data=f"{PREFIX}:svc:{user_id}:{sid}",
            )
        ]
        for sid in service_ids[:20]
    ]
    rows.append([kb.back_button(None, f"{PREFIX}:user:{user_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _service_actions(user_id: int, service_id: int) -> InlineKeyboardMarkup:
    ids = f"{user_id}:{service_id}"
    actions = [
        InlineKeyboardButton(text="🔗 لینک سرویس", callback_data=f"{PREFIX}:svclink:{ids}"),
        InlineKeyboardButton(text="🔄 تمدید با پلن فعلی", callback_data=f"{PREFIX}:svcrenew:{ids}"),
        InlineKeyboardButton(text="⏱ تغییر مانده", callback_data=f"{PREFIX}:svcadj:{ids}"),
        InlineKeyboardButton(text="🗑 حذف سرویس", callback_data=f"{PREFIX}:svcdelask:{ids}"),
    ]
    rows = kb.chunk_buttons(actions, cols=2)
    rows.append([kb.back_button(None, f"{PREFIX}:svcs:{user_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _delete_confirm(user_id: int, service_id: int, *, external: bool) -> InlineKeyboardMarkup:
    cancel = InlineKeyboardButton(
        text="انصراف", callback_data=f"{PREFIX}:svc:{user_id}:{service_id}"
    )
    if external:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔗 فقط جدا شود (حساب پنل می‌ماند)",
                        callback_data=f"{PREFIX}:svcdel:{user_id}:{service_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🗑 حذف همراه حساب پاسارگارد",
                        callback_data=f"{PREFIX}:svcdel:{user_id}:{service_id}:pg",
                    )
                ],
                [cancel],
            ]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 بله، حذف شود",
                    callback_data=f"{PREFIX}:svcdel:{user_id}:{service_id}",
                ),
                cancel,
            ]
        ]
    )


def _adjust_keyboard(
    user_id: int, service_id: int, *, days: int = 0, gb: float = 0
) -> InlineKeyboardMarkup:
    gb_disp = int(gb) if float(gb) == int(gb) else gb
    base = f"{PREFIX}:svcadj:{user_id}:{service_id}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➖", callback_data=f"{base}:days:-"),
                InlineKeyboardButton(text=f"{days} روز", callback_data=f"{base}:days:input"),
                InlineKeyboardButton(text="➕", callback_data=f"{base}:days:+"),
            ],
            [
                InlineKeyboardButton(text="➖", callback_data=f"{base}:gb:-"),
                InlineKeyboardButton(text=f"{gb_disp} گیگ", callback_data=f"{base}:gb:input"),
                InlineKeyboardButton(text="➕", callback_data=f"{base}:gb:+"),
            ],
            [InlineKeyboardButton(text="✅ اعمال تغییر", callback_data=f"{base}:confirm")],
            [kb.back_button(None, f"{PREFIX}:svc:{user_id}:{service_id}")],
        ]
    )


async def _shop_actor(
    session: AsyncSession,
    db_user: BotUser,
    *,
    is_reseller_bot: bool,
    reseller_owner_id: int | None,
) -> tuple[int, object] | tuple[None, None]:
    owner_id, profile = await load_reseller_actor(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not owner_id or not profile or not has_bot_perm(profile, PERMISSION):
        return None, None
    return int(owner_id), profile


async def _scoped_customer(
    session: AsyncSession, owner_id: int, user_id: int
) -> BotUser | None:
    user = await session.get(BotUser, int(user_id))
    if not user or not await is_shop_customer(session, user, int(owner_id)):
        return None
    return user


def _parse_uid_sid(data: str | None, *, min_parts: int = 4) -> tuple[int, int] | None:
    parts = (data or "").split(":")
    if len(parts) < min_parts:
        return None
    try:
        return int(parts[2]), int(parts[3])
    except ValueError:
        return None


@router.callback_query(F.data.startswith(f"{PREFIX}:svcs:"))
async def res_user_services(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    owner_id, _profile = await _shop_actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if owner_id is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    try:
        user_id = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    if await _scoped_customer(session, owner_id, user_id) is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    from app.services.bot_user_admin import list_service_snapshots, snapshot_telegram_lines

    await callback.answer()
    snaps = await list_service_snapshots(session, user_id, shop_id=owner_id)
    if not snaps:
        if callback.message:
            await safe_edit_text(
                callback.message,
                "این مشتری هنوز سرویسی در فروشگاه شما ندارد.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[kb.back_button(None, f"{PREFIX}:user:{user_id}")]]
                ),
            )
        return
    lines = [format_services_list_header(user_id), ""]
    for snap in snaps[:15]:
        lines.append(snapshot_telegram_lines(snap))
        lines.append("")
    if callback.message:
        await safe_edit_text(
            callback.message,
            "\n".join(lines).strip(),
            reply_markup=_services_keyboard(user_id, [s.service.id for s in snaps]),
        )


@router.callback_query(F.data.regexp(rf"^{PREFIX}:svc:\d+:\d+$"))
async def res_user_service_one(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    owner_id, _profile = await _shop_actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if owner_id is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    parsed = _parse_uid_sid(callback.data)
    if parsed is None:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    user_id, service_id = parsed
    if await _scoped_customer(session, owner_id, user_id) is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    from app.services.bot_user_admin import get_owned_service, service_snapshot, snapshot_telegram_lines

    try:
        svc = await get_owned_service(
            session, bot_user_id=user_id, service_id=service_id, shop_id=owner_id
        )
    except ValueError as exc:
        await callback.answer(user_safe_error(exc, limit=160), show_alert=True)
        return
    await callback.answer()
    snap = await service_snapshot(session, svc)
    await session.commit()
    if callback.message:
        await safe_edit_text(
            callback.message,
            snapshot_telegram_lines(snap),
            reply_markup=_service_actions(user_id, service_id),
        )


@router.callback_query(F.data.startswith(f"{PREFIX}:svclink:"))
async def res_user_service_link(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    owner_id, _profile = await _shop_actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if owner_id is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    parsed = _parse_uid_sid(callback.data)
    if parsed is None:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    user_id, service_id = parsed
    if await _scoped_customer(session, owner_id, user_id) is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    from app.services.bot_user_admin import get_owned_service, service_snapshot

    try:
        svc = await get_owned_service(
            session, bot_user_id=user_id, service_id=service_id, shop_id=owner_id
        )
        snap = await service_snapshot(session, svc)
        await session.commit()
    except ValueError as exc:
        await callback.answer(user_safe_error(exc, limit=160), show_alert=True)
        return
    url = snap.subscription_url
    if not url:
        await callback.answer("لینک اشتراک برای این سرویس موجود نیست", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        with keep_sent():
            await callback.message.answer(
                f"🔗 لینک سرویس #{service_id}:\n<code>{html.escape(url)}</code>"
            )


@router.callback_query(F.data.startswith(f"{PREFIX}:svcrenew:"))
async def res_user_service_renew(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    owner_id, _profile = await _shop_actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if owner_id is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    parsed = _parse_uid_sid(callback.data)
    if parsed is None:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    user_id, service_id = parsed
    if await _scoped_customer(session, owner_id, user_id) is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    from app.services.bot_user_admin import (
        admin_renew_service,
        get_owned_service,
        service_snapshot,
        snapshot_telegram_lines,
    )

    try:
        svc = await get_owned_service(
            session, bot_user_id=user_id, service_id=service_id, shop_id=owner_id
        )
        plan = await session.get(Plan, int(svc.plan_id)) if svc.plan_id else None
        if plan is None:
            await callback.answer("پلن این سرویس مشخص نیست", show_alert=True)
            return
        await admin_renew_service(session, svc, plan=plan, reset_traffic=True)
        snap = await service_snapshot(session, svc)
    except ValueError as exc:
        await callback.answer(user_safe_error(exc, limit=160), show_alert=True)
        return
    except Exception as exc:
        logger.warning("shop service renew failed sid=%s", service_id, exc_info=True)
        await callback.answer(user_safe_error(exc, limit=160), show_alert=True)
        return
    await callback.answer(feedback.RENEWED)
    if callback.message:
        await safe_edit_text(
            callback.message,
            "✅ تمدید انجام شد\n\n" + snapshot_telegram_lines(snap),
            reply_markup=_service_actions(user_id, service_id),
        )


@router.callback_query(F.data.startswith(f"{PREFIX}:svcdelask:"))
async def res_user_service_delete_ask(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    owner_id, _profile = await _shop_actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if owner_id is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    parsed = _parse_uid_sid(callback.data)
    if parsed is None:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    user_id, service_id = parsed
    if await _scoped_customer(session, owner_id, user_id) is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    from app.services.bot_user_admin import get_owned_service
    from app.services.service_import import is_external_service

    try:
        svc = await get_owned_service(
            session, bot_user_id=user_id, service_id=service_id, shop_id=owner_id
        )
    except ValueError as exc:
        await callback.answer(user_safe_error(exc, limit=160), show_alert=True)
        return
    await callback.answer()
    label = svc.pg_username or f"#{service_id}"
    external = is_external_service(svc)
    text = (
        f"⚠️ <b>حذف سرویس #{service_id}</b>\n\n"
        f"سرویس <code>{html.escape(str(label))}</code> از بیرون ربات اضافه شده است.\n"
        "«جدا شود» فقط آن را از لیست مشتری برمی‌دارد و حساب VPN در پاسارگارد می‌ماند. "
        "«حذف همراه حساب» حساب پاسارگارد را هم برای همیشه حذف می‌کند و برگشت‌ناپذیر است."
        if external
        else (
            f"⚠️ <b>حذف سرویس #{service_id}</b>\n\n"
            f"سرویس <code>{html.escape(str(label))}</code> و کاربر پاسارگارد مرتبط "
            f"برای همیشه حذف شوند؟\nاین عمل برگشت‌ناپذیر است."
        )
    )
    if callback.message:
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=_delete_confirm(user_id, service_id, external=external),
        )


@router.callback_query(F.data.regexp(rf"^{PREFIX}:svcdel:\d+:\d+(:pg)?$"))
async def res_user_service_delete(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    owner_id, _profile = await _shop_actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if owner_id is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) < 4 or parts[1] != "svcdel":
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    try:
        user_id = int(parts[2])
        service_id = int(parts[3])
    except ValueError:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    if await _scoped_customer(session, owner_id, user_id) is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    from app.services.bot_user_admin import admin_delete_service, get_owned_service
    from app.services.service_import import admin_delete_pg_flag, is_external_service

    explicit_pg = len(parts) > 4 and parts[4] == "pg"
    try:
        svc = await get_owned_service(
            session, bot_user_id=user_id, service_id=service_id, shop_id=owner_id
        )
        detach_only = is_external_service(svc) and not explicit_pg
        await admin_delete_service(
            session, svc, delete_pg=admin_delete_pg_flag(svc, explicit=explicit_pg)
        )
    except ValueError as exc:
        await callback.answer(user_safe_error(exc, limit=160), show_alert=True)
        return
    except Exception as exc:
        logger.warning("shop service delete failed sid=%s", service_id, exc_info=True)
        await callback.answer(user_safe_error(exc, limit=160), show_alert=True)
        return
    done = (
        f"سرویس #{service_id} از لیست مشتری جدا شد؛ حساب پاسارگارد حفظ شد."
        if detach_only
        else f"سرویس #{service_id} حذف شد."
    )
    await callback.answer(done, show_alert=True)
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"✅ {done}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[kb.back_button(None, f"{PREFIX}:svcs:{user_id}")]]
            ),
        )


def _clamp_days(value: int, limit: int) -> int:
    return max(-limit, min(limit, int(value)))


def _clamp_gb(value: float, limit: float) -> float:
    return max(-float(limit), min(float(limit), float(value)))


@router.callback_query(F.data.startswith(f"{PREFIX}:svcadj:"))
async def res_user_service_adjust(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    owner_id, _profile = await _shop_actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if owner_id is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    parts = (callback.data or "").split(":")
    # res:svcadj:{uid}:{sid}[ :days|gb|confirm : +/-|input ]
    if len(parts) < 4:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    try:
        user_id = int(parts[2])
        service_id = int(parts[3])
    except ValueError:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    if await _scoped_customer(session, owner_id, user_id) is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return

    from app.services.bot_user_admin import MAX_EXTEND_DAYS, MAX_EXTEND_GB

    data = await state.get_data()
    adj_key = f"rsvcadj:{user_id}:{service_id}"
    stored = data.get(adj_key) if isinstance(data.get(adj_key), dict) else {}
    days = int(stored.get("days") or 0)
    gb = float(stored.get("gb") or 0)
    action = parts[4] if len(parts) > 4 else ""
    detail = parts[5] if len(parts) > 5 else ""

    if action == "days" and detail in {"+", "-"}:
        days = _clamp_days(days + (1 if detail == "+" else -1), MAX_EXTEND_DAYS)
        await state.update_data(**{adj_key: {"days": days, "gb": gb}})
        await callback.answer()
    elif action == "gb" and detail in {"+", "-"}:
        gb = _clamp_gb(gb + (1 if detail == "+" else -1), float(MAX_EXTEND_GB))
        await state.update_data(**{adj_key: {"days": days, "gb": gb}})
        await callback.answer()
    elif action == "days" and detail == "input":
        await state.update_data(
            **{
                adj_key: {"days": days, "gb": gb},
                "rsvcadj_uid": user_id,
                "rsvcadj_sid": service_id,
            }
        )
        await callback.answer()
        if callback.message:
            await ask_text(
                callback,
                state,
                prompt=f"تعداد روز تغییر را وارد کنید (±{MAX_EXTEND_DAYS}، منفی = کاهش):",
                cancel_code=CANCEL_CODE,
                return_to=f"{PREFIX}:svcadj:{user_id}:{service_id}",
                fsm_state=ResellerServiceStates.adjust_days,
                edit=False,
            )
        return
    elif action == "gb" and detail == "input":
        await state.update_data(
            **{
                adj_key: {"days": days, "gb": gb},
                "rsvcadj_uid": user_id,
                "rsvcadj_sid": service_id,
            }
        )
        await callback.answer()
        if callback.message:
            await ask_text(
                callback,
                state,
                prompt=f"مقدار گیگ تغییر را وارد کنید (±{MAX_EXTEND_GB}، منفی = کاهش):",
                cancel_code=CANCEL_CODE,
                return_to=f"{PREFIX}:svcadj:{user_id}:{service_id}",
                fsm_state=ResellerServiceStates.adjust_gb,
                edit=False,
            )
        return
    elif action == "confirm":
        from app.services.bot_user_admin import (
            admin_extend_service,
            get_owned_service,
            service_snapshot,
            snapshot_telegram_lines,
        )

        try:
            svc = await get_owned_service(
                session, bot_user_id=user_id, service_id=service_id, shop_id=owner_id
            )
            await admin_extend_service(session, svc, extra_days=days, extra_gb=gb)
            snap = await service_snapshot(session, svc)
        except ValueError as exc:
            await callback.answer(user_safe_error(exc, limit=160), show_alert=True)
            return
        except Exception as exc:
            logger.warning("shop service extend failed sid=%s", service_id, exc_info=True)
            await callback.answer(user_safe_error(exc, limit=160), show_alert=True)
            return
        await state.update_data(**{adj_key: {"days": 0, "gb": 0.0}})
        await callback.answer("مانده به‌روز شد")
        if callback.message:
            await safe_edit_text(
                callback.message,
                "✅ مانده سرویس به‌روز شد\n\n" + snapshot_telegram_lines(snap),
                reply_markup=_service_actions(user_id, service_id),
            )
        return
    else:
        await state.update_data(**{adj_key: {"days": days, "gb": gb}})
        await callback.answer()

    if callback.message:
        await safe_edit_text(
            callback.message,
            format_adjust_prompt(service_id, days=days, gb=gb),
            reply_markup=_adjust_keyboard(user_id, service_id, days=days, gb=gb),
        )


@router.message(ResellerServiceStates.adjust_days)
async def res_svc_adjust_days_entered(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    if kb.is_cancel_text(message.text):
        await state.set_state(None)
        if await try_legacy_cancel(
            message,
            state,
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        ):
            return
        await message.answer(feedback.CANCELLED)
        return
    from app.services.bot_user_admin import MAX_EXTEND_DAYS

    try:
        days = parse_bot_int(message.text)
    except ValueError:
        await message.answer(feedback.ENTER_VALID_NUMBER_SHORT)
        return
    if abs(days) > MAX_EXTEND_DAYS:
        await message.answer(f"روز باید بین ±{MAX_EXTEND_DAYS} باشد")
        return
    data = await state.get_data()
    user_id = int(data.get("rsvcadj_uid") or 0)
    service_id = int(data.get("rsvcadj_sid") or 0)
    if not user_id or not service_id:
        await state.clear()
        await message.answer(feedback.CANCELLED)
        return
    adj_key = f"rsvcadj:{user_id}:{service_id}"
    stored = data.get(adj_key) if isinstance(data.get(adj_key), dict) else {}
    gb = float(stored.get("gb") or 0)
    await state.set_state(None)
    await state.update_data(**{adj_key: {"days": days, "gb": gb}})
    await finish_text_step(
        message,
        state,
        text=format_adjust_prompt(service_id, days=days, gb=gb),
        inline=_adjust_keyboard(user_id, service_id, days=days, gb=gb),
    )


@router.message(ResellerServiceStates.adjust_gb)
async def res_svc_adjust_gb_entered(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    if kb.is_cancel_text(message.text):
        await state.set_state(None)
        if await try_legacy_cancel(
            message,
            state,
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        ):
            return
        await message.answer(feedback.CANCELLED)
        return
    from app.services.bot_user_admin import MAX_EXTEND_GB

    try:
        gb = parse_bot_float(message.text)
    except ValueError:
        await message.answer(feedback.ENTER_VALID_NUMBER_SHORT)
        return
    if abs(gb) > MAX_EXTEND_GB:
        await message.answer(f"گیگ باید بین ±{MAX_EXTEND_GB} باشد")
        return
    data = await state.get_data()
    user_id = int(data.get("rsvcadj_uid") or 0)
    service_id = int(data.get("rsvcadj_sid") or 0)
    if not user_id or not service_id:
        await state.clear()
        await message.answer(feedback.CANCELLED)
        return
    adj_key = f"rsvcadj:{user_id}:{service_id}"
    stored = data.get(adj_key) if isinstance(data.get(adj_key), dict) else {}
    days = int(stored.get("days") or 0)
    await state.set_state(None)
    await state.update_data(**{adj_key: {"days": days, "gb": gb}})
    await finish_text_step(
        message,
        state,
        text=format_adjust_prompt(service_id, days=days, gb=gb),
        inline=_adjust_keyboard(user_id, service_id, days=days, gb=gb),
    )
