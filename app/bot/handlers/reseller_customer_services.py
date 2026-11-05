"""Shop-bot staff: live service info and edit for a shop customer.

Mirrors platform-admin / web ``/users`` service actions, but every step is shop-scoped:
shop from the bot the update arrived on (never from callback data), ``customer_services``
permission, ``is_shop_customer``, and ``get_owned_service(..., shop_id=owner)``.

Renew / extend / delete call the same service helpers as the admin bot and web panel.
Those helpers do not charge the customer or shop wallet (management tools). Panel
timeouts stay fail-closed where the service layer already does; handlers roll the
session back and tell staff not to tap again when the outcome is uncertain.
"""

from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass

import httpx
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.nav_input import ask_text, finish_text_step, try_legacy_cancel
from app.bot.service_adjust import (
    NOTHING_STAGED,
    StagedAdjustment,
    restore_staged,
    service_adjust_lock,
    take_staged,
)
from app.bot.tg_utils import parse_bot_float, parse_bot_int, safe_edit_text
from app.db.models import BotUser, Plan, ResellerProfile
from app.services import bot_feedback_texts as feedback
from app.services.bot_principal_identity import shop_bot_panel_staff
from app.services.bot_user_admin import (
    MAX_EXTEND_DAYS,
    MAX_EXTEND_GB,
    ServiceSnapshot,
    admin_delete_service,
    admin_extend_service,
    admin_renew_service,
    get_owned_service,
    list_service_snapshots,
    service_snapshot,
    snapshot_telegram_lines,
)
from app.services.chat_records import keep_sent
from app.services.db_safe import rollback_quiet
from app.services.pasarguard import PasarGuardError
from app.services.redact import user_safe_error
from app.services.reseller_access import load_reseller_actor
from app.services.resellers import has_bot_perm
from app.services.service_import import admin_delete_pg_flag, is_external_service
from app.services.shop_membership import is_shop_customer
from app.services.staff_cards import format_adjust_prompt, format_services_list_header

logger = logging.getLogger(__name__)
router = Router(name="reseller_customer_services")

PERMISSION = "customer_services"
CANCEL_CODE = "rs_svc"
PREFIX = "res"
UNKNOWN_OUTCOME = (
    "وضعیت نامشخص است — ممکن است اعمال شده باشد. دوباره نزنید؛ "
    "جزئیات سرویس را باز کنید و وضعیت را ببینید."
)
PANEL_UNCERTAIN = (asyncio.TimeoutError, PasarGuardError, httpx.HTTPError)


class ResellerServiceStates(StatesGroup):
    adjust_days = State()
    adjust_gb = State()


@dataclass(frozen=True, slots=True)
class ShopServiceTarget:
    owner_id: int
    profile: ResellerProfile
    user_id: int
    service_id: int | None = None


def services_button(user_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text="📦 اطلاعات و ویرایش سرویس",
        callback_data=f"{PREFIX}:svcs:{int(user_id)}",
    )


def manage_service_button(user_id: int, service_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text="⚙️ مدیریت سرویس",
        callback_data=f"{PREFIX}:svc:{int(user_id)}:{int(service_id)}",
    )


def may_manage_customer_services(profile: ResellerProfile | None) -> bool:
    return has_bot_perm(profile, PERMISSION)


def _services_keyboard(user_id: int, service_ids: list[int]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"سرویس #{sid}", callback_data=f"{PREFIX}:svc:{user_id}:{sid}")]
        for sid in service_ids[:20]
    ]
    rows.append([kb.back_button(None, f"{PREFIX}:user:{user_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _service_actions(user_id: int, service_id: int) -> InlineKeyboardMarkup:
    ids = f"{user_id}:{service_id}"
    actions = [
        InlineKeyboardButton(text="🔗 لینک سرویس", callback_data=f"{PREFIX}:svclink:{ids}"),
        InlineKeyboardButton(
            text="🔄 تمدید مدیریتی",
            callback_data=f"{PREFIX}:svcrenew:{ids}",
        ),
        InlineKeyboardButton(text="⏱ تغییر مانده", callback_data=f"{PREFIX}:svcadj:{ids}"),
        InlineKeyboardButton(text="🗑 حذف سرویس", callback_data=f"{PREFIX}:svcdelask:{ids}"),
    ]
    rows = kb.chunk_buttons(actions, cols=2)
    rows.append([kb.back_button(None, f"{PREFIX}:svcs:{user_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _delete_confirm(user_id: int, service_id: int, *, external: bool) -> InlineKeyboardMarkup:
    cancel = InlineKeyboardButton(text="انصراف", callback_data=f"{PREFIX}:svc:{user_id}:{service_id}")
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


async def _resolve_target(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    *,
    is_reseller_bot: bool,
    reseller_owner_id: int | None,
    need_service: bool,
) -> ShopServiceTarget | None:
    """Authz + shop scope + customer (+ optional service id). Answers the callback on deny."""
    owner_id, profile = await load_reseller_actor(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not owner_id or not profile or not has_bot_perm(profile, PERMISSION):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return None
    parts = (callback.data or "").split(":")
    try:
        if need_service:
            if len(parts) < 4:
                raise ValueError
            user_id = int(parts[2])
            service_id = int(parts[3])
        else:
            user_id = int(parts[-1])
            service_id = None
    except ValueError:
        await callback.answer(feedback.INVALID, show_alert=True)
        return None
    user = await session.get(BotUser, int(user_id))
    if not user or not await is_shop_customer(session, user, int(owner_id)):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return None
    return ShopServiceTarget(
        owner_id=int(owner_id),
        profile=profile,
        user_id=int(user_id),
        service_id=service_id,
    )


async def _panel_staff(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    target: ShopServiceTarget,
) -> dict | None:
    """The shop's panel role for a service change; answers the callback when it is missing."""
    staff = await shop_bot_panel_staff(session, profile=target.profile, db_user=db_user)
    if staff is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
    return staff


async def _fail_uncertain(callback: CallbackQuery, session: AsyncSession, *, sid: int, op: str) -> None:
    await rollback_quiet(session)
    logger.warning("shop service %s uncertain sid=%s", op, sid, exc_info=True)
    await callback.answer(UNKNOWN_OUTCOME, show_alert=True)


async def _fail_known(callback: CallbackQuery, session: AsyncSession, exc: Exception) -> None:
    await rollback_quiet(session)
    await callback.answer(user_safe_error(exc, limit=160), show_alert=True)


@router.callback_query(F.data.startswith(f"{PREFIX}:svcs:"))
async def res_user_services(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    target = await _resolve_target(
        callback,
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
        need_service=False,
    )
    if target is None:
        return
    await callback.answer()
    snaps = await list_service_snapshots(session, target.user_id, shop_id=target.owner_id)
    if not snaps:
        if callback.message:
            await safe_edit_text(
                callback.message,
                "این مشتری هنوز سرویسی در فروشگاه شما ندارد.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[kb.back_button(None, f"{PREFIX}:user:{target.user_id}")]]
                ),
            )
        return
    lines = [format_services_list_header(target.user_id), ""]
    for snap in snaps[:15]:
        lines.append(snapshot_telegram_lines(snap))
        lines.append("")
    if callback.message:
        await safe_edit_text(
            callback.message,
            "\n".join(lines).strip(),
            reply_markup=_services_keyboard(target.user_id, [s.service.id for s in snaps]),
        )


@router.callback_query(F.data.regexp(rf"^{PREFIX}:svc:\d+:\d+$"))
async def res_user_service_one(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    target = await _resolve_target(
        callback,
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
        need_service=True,
    )
    if target is None or target.service_id is None:
        return
    try:
        svc = await get_owned_service(
            session,
            bot_user_id=target.user_id,
            service_id=target.service_id,
            shop_id=target.owner_id,
        )
    except ValueError as exc:
        await _fail_known(callback, session, exc)
        return
    await callback.answer()
    snap = await service_snapshot(session, svc)
    await session.commit()
    if callback.message:
        await safe_edit_text(
            callback.message,
            snapshot_telegram_lines(snap),
            reply_markup=_service_actions(target.user_id, target.service_id),
        )


@router.callback_query(F.data.startswith(f"{PREFIX}:svclink:"))
async def res_user_service_link(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    target = await _resolve_target(
        callback,
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
        need_service=True,
    )
    if target is None or target.service_id is None:
        return
    try:
        svc = await get_owned_service(
            session,
            bot_user_id=target.user_id,
            service_id=target.service_id,
            shop_id=target.owner_id,
        )
        snap = await service_snapshot(session, svc)
        await session.commit()
    except ValueError as exc:
        await _fail_known(callback, session, exc)
        return
    except PANEL_UNCERTAIN as exc:
        await _fail_uncertain(callback, session, sid=target.service_id, op="link")
        return
    url = snap.subscription_url
    if not url:
        await callback.answer("لینک اشتراک برای این سرویس موجود نیست", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        with keep_sent():
            await callback.message.answer(
                f"🔗 لینک سرویس #{target.service_id}:\n<code>{html.escape(url)}</code>"
            )


@router.callback_query(F.data.startswith(f"{PREFIX}:svcrenew:"))
async def res_user_service_renew(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    """Management renew — no wallet debit (same as admin bot / web ``/users`` renew)."""
    target = await _resolve_target(
        callback,
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
        need_service=True,
    )
    if target is None or target.service_id is None:
        return
    try:
        svc = await get_owned_service(
            session,
            bot_user_id=target.user_id,
            service_id=target.service_id,
            shop_id=target.owner_id,
        )
        plan = await session.get(Plan, int(svc.plan_id)) if svc.plan_id else None
        if plan is None:
            await callback.answer("پلن این سرویس مشخص نیست", show_alert=True)
            return
        staff = await _panel_staff(callback, session, db_user, target)
        if staff is None:
            return
        await admin_renew_service(session, svc, plan=plan, reset_traffic=True, staff=staff)
        snap = await service_snapshot(session, svc)
    except ValueError as exc:
        await _fail_known(callback, session, exc)
        return
    except PANEL_UNCERTAIN:
        await _fail_uncertain(callback, session, sid=target.service_id, op="renew")
        return
    except Exception as exc:
        await rollback_quiet(session)
        logger.warning("shop service renew failed sid=%s", target.service_id, exc_info=True)
        await callback.answer(user_safe_error(exc, limit=160), show_alert=True)
        return
    await callback.answer(feedback.RENEWED)
    if callback.message:
        await safe_edit_text(
            callback.message,
            "✅ تمدید مدیریتی انجام شد (بدون کسر از کیف پول)\n\n" + snapshot_telegram_lines(snap),
            reply_markup=_service_actions(target.user_id, target.service_id),
        )


@router.callback_query(F.data.startswith(f"{PREFIX}:svcdelask:"))
async def res_user_service_delete_ask(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    target = await _resolve_target(
        callback,
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
        need_service=True,
    )
    if target is None or target.service_id is None:
        return
    try:
        svc = await get_owned_service(
            session,
            bot_user_id=target.user_id,
            service_id=target.service_id,
            shop_id=target.owner_id,
        )
    except ValueError as exc:
        await _fail_known(callback, session, exc)
        return
    await callback.answer()
    label = svc.pg_username or f"#{target.service_id}"
    external = is_external_service(svc)
    text = (
        f"⚠️ <b>حذف سرویس #{target.service_id}</b>\n\n"
        f"سرویس <code>{html.escape(str(label))}</code> از بیرون ربات اضافه شده است.\n"
        "«جدا شود» فقط آن را از لیست مشتری برمی‌دارد و حساب VPN در پاسارگارد می‌ماند. "
        "«حذف همراه حساب» حساب پاسارگارد را هم برای همیشه حذف می‌کند و برگشت‌ناپذیر است."
        if external
        else (
            f"⚠️ <b>حذف سرویس #{target.service_id}</b>\n\n"
            f"سرویس <code>{html.escape(str(label))}</code> و کاربر پاسارگارد مرتبط "
            f"برای همیشه حذف شوند؟\nاین عمل برگشت‌ناپذیر است."
        )
    )
    if callback.message:
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=_delete_confirm(target.user_id, target.service_id, external=external),
        )


@router.callback_query(F.data.regexp(rf"^{PREFIX}:svcdel:\d+:\d+(:pg)?$"))
async def res_user_service_delete(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    target = await _resolve_target(
        callback,
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
        need_service=True,
    )
    if target is None or target.service_id is None:
        return
    parts = (callback.data or "").split(":")
    explicit_pg = len(parts) > 4 and parts[4] == "pg"
    try:
        svc = await get_owned_service(
            session,
            bot_user_id=target.user_id,
            service_id=target.service_id,
            shop_id=target.owner_id,
        )
        detach_only = is_external_service(svc) and not explicit_pg
        staff = await _panel_staff(callback, session, db_user, target)
        if staff is None:
            return
        await admin_delete_service(
            session, svc, delete_pg=admin_delete_pg_flag(svc, explicit=explicit_pg), staff=staff
        )
    except ValueError as exc:
        await _fail_known(callback, session, exc)
        return
    except PANEL_UNCERTAIN:
        await _fail_uncertain(callback, session, sid=target.service_id, op="delete")
        return
    except Exception as exc:
        await rollback_quiet(session)
        logger.warning("shop service delete failed sid=%s", target.service_id, exc_info=True)
        await callback.answer(user_safe_error(exc, limit=160), show_alert=True)
        return
    done = (
        f"سرویس #{target.service_id} از لیست مشتری جدا شد؛ حساب پاسارگارد حفظ شد."
        if detach_only
        else f"سرویس #{target.service_id} حذف شد."
    )
    await callback.answer(done, show_alert=True)
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"✅ {done}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[kb.back_button(None, f"{PREFIX}:svcs:{target.user_id}")]]
            ),
        )


def _clamp_days(value: int) -> int:
    return max(-MAX_EXTEND_DAYS, min(MAX_EXTEND_DAYS, int(value)))


def _clamp_gb(value: float) -> float:
    return max(-float(MAX_EXTEND_GB), min(float(MAX_EXTEND_GB), float(value)))


async def _adj_bump(
    state: FSMContext,
    *,
    adj_key: str,
    days: int,
    gb: float,
    field: str,
    detail: str,
) -> tuple[int, float]:
    if field == "days":
        days = _clamp_days(days + (1 if detail == "+" else -1))
    else:
        gb = _clamp_gb(gb + (1 if detail == "+" else -1))
    await state.update_data(**{adj_key: {"days": days, "gb": gb}})
    return days, gb


async def _adj_ask_input(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    adj_key: str,
    user_id: int,
    service_id: int,
    days: int,
    gb: float,
    kind: str,
) -> None:
    await state.update_data(
        **{adj_key: {"days": days, "gb": gb}, "rsvcadj_uid": user_id, "rsvcadj_sid": service_id}
    )
    await callback.answer()
    if not callback.message:
        return
    if kind == "days":
        prompt = f"تعداد روز تغییر را وارد کنید (±{MAX_EXTEND_DAYS}، منفی = کاهش):"
        fsm = ResellerServiceStates.adjust_days
    else:
        prompt = f"مقدار گیگ تغییر را وارد کنید (±{MAX_EXTEND_GB}، منفی = کاهش):"
        fsm = ResellerServiceStates.adjust_gb
    await ask_text(
        callback,
        state,
        prompt=prompt,
        cancel_code=CANCEL_CODE,
        return_to=f"{PREFIX}:svcadj:{user_id}:{service_id}",
        fsm_state=fsm,
        edit=False,
    )


async def _apply_staged(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    *,
    target: ShopServiceTarget,
    adj_key: str,
    staged: StagedAdjustment,
) -> ServiceSnapshot | None:
    """Apply a staged change; a refused change is staged again, an uncertain one is not."""
    service_id = int(target.service_id or 0)
    try:
        svc = await get_owned_service(
            session, bot_user_id=target.user_id, service_id=service_id, shop_id=target.owner_id
        )
        staff = await _panel_staff(callback, session, db_user, target)
        if staff is None:
            await restore_staged(state, adj_key, staged)
            return None
        await admin_extend_service(
            session, svc, extra_days=staged.days, extra_gb=staged.gb, staff=staff
        )
        return await service_snapshot(session, svc)
    except ValueError as exc:
        await restore_staged(state, adj_key, staged)
        await _fail_known(callback, session, exc)
    except PANEL_UNCERTAIN:
        await _fail_uncertain(callback, session, sid=service_id, op="extend")
    except Exception as exc:
        await rollback_quiet(session)
        logger.warning("shop service extend failed sid=%s", service_id, exc_info=True)
        await callback.answer(user_safe_error(exc, limit=160), show_alert=True)
    return None


async def _adj_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    *,
    target: ShopServiceTarget,
    adj_key: str,
) -> None:
    if target.service_id is None:
        return
    async with service_adjust_lock(target.service_id):
        staged = await take_staged(state, adj_key)
        if staged.is_empty:
            await callback.answer(NOTHING_STAGED, show_alert=True)
            return
        snap = await _apply_staged(
            callback, state, session, db_user, target=target, adj_key=adj_key, staged=staged
        )
    if snap is None:
        return
    await callback.answer("مانده به‌روز شد")
    if callback.message:
        await safe_edit_text(
            callback.message,
            "✅ مانده سرویس به‌روز شد\n\n" + snapshot_telegram_lines(snap),
            reply_markup=_service_actions(target.user_id, target.service_id),
        )


@router.callback_query(F.data.startswith(f"{PREFIX}:svcadj:"))
async def res_user_service_adjust(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    target = await _resolve_target(
        callback,
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
        need_service=True,
    )
    if target is None or target.service_id is None:
        return
    parts = (callback.data or "").split(":")
    action = parts[4] if len(parts) > 4 else ""
    detail = parts[5] if len(parts) > 5 else ""
    adj_key = f"rsvcadj:{target.user_id}:{target.service_id}"
    data = await state.get_data()
    stored = data.get(adj_key) if isinstance(data.get(adj_key), dict) else {}
    days = int(stored.get("days") or 0)
    gb = float(stored.get("gb") or 0)

    if action in {"days", "gb"} and detail in {"+", "-"}:
        days, gb = await _adj_bump(
            state, adj_key=adj_key, days=days, gb=gb, field=action, detail=detail
        )
        await callback.answer()
    elif action in {"days", "gb"} and detail == "input":
        await _adj_ask_input(
            callback,
            state,
            adj_key=adj_key,
            user_id=target.user_id,
            service_id=target.service_id,
            days=days,
            gb=gb,
            kind=action,
        )
        return
    elif action == "confirm":
        await _adj_confirm(callback, state, session, db_user, target=target, adj_key=adj_key)
        return
    else:
        await state.update_data(**{adj_key: {"days": days, "gb": gb}})
        await callback.answer()

    if callback.message:
        await safe_edit_text(
            callback.message,
            format_adjust_prompt(target.service_id, days=days, gb=gb),
            reply_markup=_adjust_keyboard(
                target.user_id, target.service_id, days=days, gb=gb
            ),
        )


async def _finish_adjust_input(
    message: Message,
    state: FSMContext,
    *,
    field: str,
    raw: str | None,
) -> None:
    try:
        value: int | float = parse_bot_int(raw) if field == "days" else parse_bot_float(raw)
    except ValueError:
        await message.answer(feedback.ENTER_VALID_NUMBER_SHORT)
        return
    limit = MAX_EXTEND_DAYS if field == "days" else MAX_EXTEND_GB
    if abs(value) > limit:
        unit = "روز" if field == "days" else "گیگ"
        await message.answer(f"{unit} باید بین ±{limit} باشد")
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
    days = int(value) if field == "days" else int(stored.get("days") or 0)
    gb = float(value) if field == "gb" else float(stored.get("gb") or 0)
    await state.set_state(None)
    await state.update_data(**{adj_key: {"days": days, "gb": gb}})
    await finish_text_step(
        message,
        state,
        text=format_adjust_prompt(service_id, days=days, gb=gb),
        inline=_adjust_keyboard(user_id, service_id, days=days, gb=gb),
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
    await _finish_adjust_input(message, state, field="days", raw=message.text)


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
    await _finish_adjust_input(message, state, field="gb", raw=message.text)
