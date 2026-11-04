"""Owner-bot CRUD for per-plan reseller extra GB/user stairs."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser, ResellerPlan, ResellerPlanExtraPack
from app.services import bot_feedback_texts as feedback
from app.services.reseller_plan_extra_packs import (
    KIND_GB,
    KIND_USERS,
    ensure_default_packs,
    get_pack_for_plan,
    list_packs,
    parse_pack_amount,
    resolve_extra_pack_button_style,
    telegram_style_kwargs,
)

logger = logging.getLogger(__name__)

router = Router(name="reseller_plan_extra_packs_admin")

_STYLE_CODES = {
    "i": None,  # inherit
    "w": "",
    "p": "primary",
    "s": "success",
    "d": "danger",
}
_STYLE_LABELS = {
    None: "ارث",
    "": "سفید",
    "primary": "آبی",
    "success": "سبز",
    "danger": "قرمز",
}


class ExtraPackStates(StatesGroup):
    amount = State()


def _is_admin(user: BotUser) -> bool:
    return (user.role or "") == "admin"


def _kind_label(kind: str) -> str:
    return "حجم (گیگ)" if kind == KIND_GB else "کاربر"


def _style_label(stored: str | None) -> str:
    if stored is None:
        return _STYLE_LABELS[None]
    return _STYLE_LABELS.get(stored, stored or "سفید")


def _packs_home_kb(plan_id: int) -> InlineKeyboardMarkup:
    pid = int(plan_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📦 بسته‌های حجم",
                    callback_data=f"adm:resx:k:{pid}:gb",
                )
            ],
            [
                InlineKeyboardButton(
                    text="👤 بسته‌های کاربر",
                    callback_data=f"adm:resx:k:{pid}:users",
                )
            ],
            [InlineKeyboardButton(text="◀️ بازگشت", callback_data=f"adm:resplan:view:{pid}")],
        ]
    )


def _kind_list_kb(
    plan: ResellerPlan, packs: list[ResellerPlanExtraPack], kind: str
) -> InlineKeyboardMarkup:
    pid = int(plan.id)
    rows: list[list[InlineKeyboardButton]] = []
    for pack in packs:
        style = resolve_extra_pack_button_style(pack.button_style, plan)
        unit = "گیگ" if kind == KIND_GB else "کاربر"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{pack.amount} {unit} · {_style_label(pack.button_style)}",
                    callback_data=f"adm:resx:v:{int(pack.id)}",
                    **telegram_style_kwargs(style),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="➕ افزودن بسته",
                callback_data=f"adm:resx:a:{pid}:{kind}",
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="◀️ بازگشت", callback_data=f"adm:resx:l:{pid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _pack_detail_kb(pack: ResellerPlanExtraPack) -> InlineKeyboardMarkup:
    pid = int(pack.plan_id)
    pk = int(pack.id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ مقدار", callback_data=f"adm:resx:e:{pk}")],
            [InlineKeyboardButton(text="🎨 رنگ دکمه", callback_data=f"adm:resx:c:{pk}")],
            [InlineKeyboardButton(text="🗑 حذف", callback_data=f"adm:resx:d:{pk}")],
            [
                InlineKeyboardButton(
                    text="◀️ بازگشت",
                    callback_data=f"adm:resx:k:{pid}:{pack.kind}",
                )
            ],
        ]
    )


def _color_picker_kb(pack: ResellerPlanExtraPack) -> InlineKeyboardMarkup:
    pk = int(pack.id)
    rows = []
    for code, value in _STYLE_CODES.items():
        label = _style_label(value)
        style = None if value is None else (value or None)
        # White is stored as ""; Telegram omits style.
        btn_style = value if value else None
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"adm:resx:cs:{pk}:{code}",
                    **telegram_style_kwargs(btn_style if code != "i" else None),
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="◀️ بازگشت", callback_data=f"adm:resx:v:{pk}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _require_plan(
    session: AsyncSession, plan_id: int
) -> ResellerPlan | None:
    return await session.get(ResellerPlan, int(plan_id))


@router.callback_query(F.data.startswith("adm:resx:l:"))
async def extrapacks_home(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
) -> None:
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    plan = await _require_plan(session, int(callback.data.rsplit(":", 1)[-1]))
    if plan is None:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    if not bool(plan.allow_buy_extra):
        await callback.answer("اول خرید حجم/کاربر اضافه را روشن کنید", show_alert=True)
        return
    await ensure_default_packs(session, plan)
    await session.commit()
    await callback.answer()
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"📦 بسته‌های پلن «{plan.name}»\nفقط روی همین پلن دیده می‌شوند.",
            reply_markup=_packs_home_kb(int(plan.id)),
        )


@router.callback_query(F.data.regexp(r"^adm:resx:k:\d+:(gb|users)$"))
async def extrapacks_kind(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
) -> None:
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    parts = (callback.data or "").split(":")
    plan = await _require_plan(session, int(parts[3]))
    kind = parts[4]
    if plan is None:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    packs = await list_packs(session, int(plan.id), kind=kind, active_only=True)
    await callback.answer()
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"بسته‌های {_kind_label(kind)} — «{plan.name}»",
            reply_markup=_kind_list_kb(plan, packs, kind),
        )


@router.callback_query(F.data.startswith("adm:resx:v:"))
async def extrapacks_view(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
) -> None:
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    pack = await session.get(ResellerPlanExtraPack, int(callback.data.rsplit(":", 1)[-1]))
    if pack is None:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    plan = await _require_plan(session, int(pack.plan_id))
    unit = "گیگ" if pack.kind == KIND_GB else "کاربر"
    await callback.answer()
    if callback.message:
        await safe_edit_text(
            callback.message,
            (
                f"بسته {_kind_label(pack.kind)}\n"
                f"مقدار: <b>{int(pack.amount)}</b> {unit}\n"
                f"رنگ: {_style_label(pack.button_style)}\n"
                f"پلن: {plan.name if plan else pack.plan_id}"
            ),
            reply_markup=_pack_detail_kb(pack),
        )


@router.callback_query(F.data.regexp(r"^adm:resx:a:\d+:(gb|users)$"))
async def extrapacks_add_ask(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
) -> None:
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    parts = (callback.data or "").split(":")
    plan_id, kind = int(parts[3]), parts[4]
    plan = await _require_plan(session, plan_id)
    if plan is None or not plan.allow_buy_extra:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    await state.set_state(ExtraPackStates.amount)
    await state.update_data(_resx_plan_id=plan_id, _resx_kind=kind, _resx_pack_id=None)
    await callback.answer()
    from app.bot.nav_input import ask_text

    await ask_text(
        callback,
        state,
        prompt=f"مقدار بسته {_kind_label(kind)} را وارد کنید:",
        cancel_code="adm_pln",
        return_to=f"adm:resx:k:{plan_id}:{kind}",
        fsm_state=ExtraPackStates.amount,
        edit=True,
    )


@router.callback_query(F.data.startswith("adm:resx:e:"))
async def extrapacks_edit_ask(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
) -> None:
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    pack = await session.get(ResellerPlanExtraPack, int(callback.data.rsplit(":", 1)[-1]))
    if pack is None:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    await state.set_state(ExtraPackStates.amount)
    await state.update_data(
        _resx_plan_id=int(pack.plan_id),
        _resx_kind=pack.kind,
        _resx_pack_id=int(pack.id),
    )
    await callback.answer()
    from app.bot.nav_input import ask_text

    await ask_text(
        callback,
        state,
        prompt=f"مقدار جدید بسته {_kind_label(pack.kind)}:",
        cancel_code="adm_pln",
        return_to=f"adm:resx:v:{int(pack.id)}",
        fsm_state=ExtraPackStates.amount,
        edit=True,
    )


@router.message(ExtraPackStates.amount)
async def extrapacks_amount_save(
    message: Message, session: AsyncSession, db_user: BotUser, state: FSMContext
) -> None:
    if not _is_admin(db_user):
        await message.answer(feedback.NOT_ADMIN)
        return
    data = await state.get_data()
    plan_id = int(data.get("_resx_plan_id") or 0)
    kind = str(data.get("_resx_kind") or "")
    pack_id = data.get("_resx_pack_id")
    plan = await _require_plan(session, plan_id)
    if plan is None or kind not in {KIND_GB, KIND_USERS}:
        await state.clear()
        await message.answer(feedback.NOT_FOUND)
        return
    try:
        from app.bot.handlers.admin_plans import parse_bot_int

        amount = parse_pack_amount(kind, parse_bot_int(str(message.text or "").strip()))
    except ValueError:
        await message.answer(feedback.ENTER_VALID_NUMBER)
        return
    existing = await list_packs(session, plan_id, kind=kind, active_only=False)
    if any(int(p.amount) == amount and int(p.id) != int(pack_id or 0) for p in existing):
        await message.answer("این مقدار از قبل در بسته‌های همین پلن هست.")
        return
    if pack_id:
        pack = await get_pack_for_plan(session, plan_id=plan_id, pack_id=int(pack_id))
        if pack is None:
            await state.clear()
            await message.answer(feedback.NOT_FOUND)
            return
        pack.amount = amount
    else:
        from app.services.reseller_plan_extra_packs import MAX_PACKS_PER_KIND

        active = [p for p in existing if p.is_active]
        if len(active) >= MAX_PACKS_PER_KIND:
            await message.answer(f"حداکثر {MAX_PACKS_PER_KIND} بسته برای هر نوع مجاز است.")
            return
        session.add(
            ResellerPlanExtraPack(
                plan_id=plan_id,
                kind=kind,
                amount=amount,
                button_style=None,
                sort_order=len(existing),
                is_active=True,
            )
        )
    await session.commit()
    await state.clear()
    packs = await list_packs(session, plan_id, kind=kind, active_only=True)
    await message.answer(
        feedback.UPDATED,
        reply_markup=_kind_list_kb(plan, packs, kind),
    )


@router.callback_query(F.data.startswith("adm:resx:c:"))
async def extrapacks_color_menu(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
) -> None:
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    pack = await session.get(ResellerPlanExtraPack, int(callback.data.rsplit(":", 1)[-1]))
    if pack is None:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"رنگ دکمه بسته {int(pack.amount)} {_kind_label(pack.kind)}:",
            reply_markup=_color_picker_kb(pack),
        )


@router.callback_query(F.data.startswith("adm:resx:cs:"))
async def extrapacks_color_set(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
) -> None:
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 5:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    pack = await session.get(ResellerPlanExtraPack, int(parts[3]))
    code = parts[4]
    if pack is None or code not in _STYLE_CODES:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    pack.button_style = _STYLE_CODES[code]
    await session.commit()
    await callback.answer(feedback.UPDATED)
    if callback.message:
        await safe_edit_text(
            callback.message,
            (
                f"بسته {_kind_label(pack.kind)}\n"
                f"مقدار: <b>{int(pack.amount)}</b>\n"
                f"رنگ: {_style_label(pack.button_style)}"
            ),
            reply_markup=_pack_detail_kb(pack),
        )


@router.callback_query(F.data.startswith("adm:resx:d:"))
async def extrapacks_del_ask(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
) -> None:
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    pack = await session.get(ResellerPlanExtraPack, int(callback.data.rsplit(":", 1)[-1]))
    if pack is None:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"بسته {int(pack.amount)} {_kind_label(pack.kind)} حذف شود؟",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✅ حذف",
                            callback_data=f"adm:resx:do:{int(pack.id)}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="انصراف",
                            callback_data=f"adm:resx:v:{int(pack.id)}",
                        )
                    ],
                ]
            ),
        )


@router.callback_query(F.data.startswith("adm:resx:do:"))
async def extrapacks_del_ok(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
) -> None:
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    pack = await session.get(ResellerPlanExtraPack, int(callback.data.rsplit(":", 1)[-1]))
    if pack is None:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    plan_id, kind = int(pack.plan_id), pack.kind
    await session.delete(pack)
    await session.commit()
    plan = await _require_plan(session, plan_id)
    packs = await list_packs(session, plan_id, kind=kind, active_only=True) if plan else []
    await callback.answer("حذف شد")
    if callback.message and plan is not None:
        await safe_edit_text(
            callback.message,
            f"بسته‌های {_kind_label(kind)} — «{plan.name}»",
            reply_markup=_kind_list_kb(plan, packs, kind),
        )
