"""Representative → Sub-Representative create on the Representative's own bot.

Parent is always the authenticated shop Principal. Client parent/depth/ids
are ignored. PG ``admins.create`` is required; Sub-Representatives are denied.
"""

from __future__ import annotations

import uuid

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import REPLY_ACTION_RES_ADD_REP, back_button
from app.bot.reseller_hub_groups import back_callback_for
from app.bot.screen_replay import replay_screen
from app.db.models import BotUser
from app.services.bot_principal_identity import resolve_bot_principal_bridge
from app.services.principal_child_provisioning import (
    ChildProvisionError,
    Level2ProvisionRequest,
    provision_level2_child,
)
from app.services.representative_unification import (
    RepresentativeUnifyError,
    assert_staff_can_manage_representatives,
)
from app.services import bot_feedback_texts as feedback
import logging

logger = logging.getLogger(__name__)

router = Router(name="reseller_reps")

_USERNAME_KEY = "rrep_username"
_PASSWORD_KEY = "rrep_password"


class ResellerRepStates(StatesGroup):
    username = State()
    password = State()


async def _actor_staff(
    session: AsyncSession,
    db_user: BotUser,
    *,
    is_reseller_bot: bool,
    reseller_owner_id: int | None,
) -> dict | None:
    if not is_reseller_bot:
        return None
    resolution = await resolve_bot_principal_bridge(
        session,
        db_user=db_user,
        is_reseller_bot=True,
        reseller_owner_id=reseller_owner_id,
    )
    return None if resolution is None else resolution.staff


async def start_add_representative(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    staff = await _actor_staff(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    try:
        assert_staff_can_manage_representatives(staff)
    except RepresentativeUnifyError:
        await message.answer("ساخت نماینده برای این حساب فعال نیست.")
        return
    await state.set_state(ResellerRepStates.username)
    await message.answer(
        "➕ افزودن نماینده\n\nنام کاربری پاسارگارد نمایندهٔ جدید را بفرستید.\n"
        "نمایندهٔ جدید زیرمجموعهٔ همین حساب ساخته می‌شود."
    )


@router.message(ResellerRepStates.username, F.text)
async def rrep_username(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    staff = await _actor_staff(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    try:
        assert_staff_can_manage_representatives(staff)
    except RepresentativeUnifyError:
        await state.clear()
        await message.answer("ساخت نماینده برای این حساب فعال نیست.")
        return
    username = (message.text or "").strip()
    if not username or "parent_id" in username.lower() or len(username) > 64:
        await message.answer("نام کاربری نامعتبر است. دوباره بفرستید.")
        return
    await state.update_data(**{_USERNAME_KEY: username})
    await state.set_state(ResellerRepStates.password)
    await message.answer("رمز پاسارگارد نماینده را بفرستید.")


@router.message(ResellerRepStates.password, F.text)
async def rrep_password(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    staff = await _actor_staff(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    try:
        assert_staff_can_manage_representatives(staff)
    except RepresentativeUnifyError:
        await state.clear()
        await message.answer("ساخت نماینده برای این حساب فعال نیست.")
        return
    password = message.text or ""
    data = await state.get_data()
    username = str(data.get(_USERNAME_KEY) or "").strip()
    await state.update_data(**{_PASSWORD_KEY: password})
    if not username:
        await state.clear()
        await message.answer("اطلاعات این مرحله از بین رفته است. ساخت نماینده را از اول شروع کنید.")
        return
    roles = await _parent_roles(session, staff)
    if not roles:
        await state.clear()
        await message.answer("نقشی در پاسارگارد برای نمایندهٔ جدید پیدا نشد. از ادمین بخواهید یک نقش بسازد.")
        return
    rows = [
        [
            InlineKeyboardButton(
                text=str(role.get("name") or role["id"])[:40],
                callback_data=f"rrep:role:{int(role['id'])}",
            )
        ]
        for role in roles[:12]
    ]
    rows.append([InlineKeyboardButton(text="انصراف", callback_data="rrep:cancel")])
    await message.answer(
        "نقش پاسارگارد را انتخاب کنید. نام نقش، سطح سازمان نیست.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def _parent_roles(session: AsyncSession, staff: dict) -> list[dict]:
    from app.services.pasarguard import get_pg_for_principal

    try:
        pid = int(staff.get("org_principal_id") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid <= 0:
        return []
    try:
        pg = await get_pg_for_principal(session, principal_id=pid)
        raw = await pg.get_admin_roles()
    except Exception:
        return []
    out: list[dict] = []
    for role in raw or []:
        if not isinstance(role, dict) or role.get("is_owner"):
            continue
        try:
            rid = int(role.get("id"))
        except (TypeError, ValueError):
            continue
        if rid <= 0:
            continue
        out.append({"id": rid, "name": str(role.get("name") or rid)})
    return out


@router.callback_query(F.data == "rrep:cancel")
async def rrep_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await callback.answer(feedback.CANCELLED_SHORT)
    except Exception:
        logger.debug("rrep_cancel: ignored failure", exc_info=True)
    if not callback.message:
        return
    # One step back: the customers group the flow was started from.
    reopened = await replay_screen(
        callback.bot,
        user=callback.from_user,
        chat_id=int(callback.message.chat.id),
        data=back_callback_for(REPLY_ACTION_RES_ADD_REP),
        message=callback.message,
    )
    if not reopened:
        await callback.message.answer("ساخت نماینده لغو شد.")


@router.callback_query(F.data.startswith("rrep:role:"))
async def rrep_role(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    staff = await _actor_staff(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    try:
        assert_staff_can_manage_representatives(staff)
    except RepresentativeUnifyError:
        await state.clear()
        try:
            await callback.answer(feedback.NO_ACCESS, show_alert=True)
        except Exception:
            logger.debug("rrep_role: ignored failure", exc_info=True)
        return
    raw = (callback.data or "").split(":")
    try:
        role_id = int(raw[-1])
    except (TypeError, ValueError, IndexError):
        try:
            await callback.answer("نقش نامعتبر", show_alert=True)
        except Exception:
            logger.debug("rrep_role: ignored failure", exc_info=True)
        return
    data = await state.get_data()
    username = str(data.get(_USERNAME_KEY) or "").strip()
    password = str(data.get(_PASSWORD_KEY) or "")
    await state.clear()
    if not username or not password:
        try:
            await callback.answer("این مرحله منقضی شده است؛ از اول شروع کنید", show_alert=True)
        except Exception:
            logger.debug("rrep_role: ignored failure", exc_info=True)
        return
    try:
        await provision_level2_child(
            session,
            staff,
            Level2ProvisionRequest(
                pg_username=username,
                pg_password=password,
                pg_role_id=role_id,
                idempotency_key=f"bot-child-{uuid.uuid4()}",
                parent_id=None,
                depth=None,
            ),
        )
        await session.commit()
    except ChildProvisionError as exc:
        try:
            await session.rollback()
        except Exception:
            logger.debug("rrep_role: ignored failure", exc_info=True)
        try:
            await callback.answer(exc.message[:180], show_alert=True)
        except Exception:
            logger.debug("rrep_role: ignored failure", exc_info=True)
        if callback.message:
            await callback.message.answer(exc.message)
        return
    except Exception:
        try:
            await session.rollback()
        except Exception:
            logger.debug("rrep_role: ignored failure", exc_info=True)
        try:
            await callback.answer("ساخت نماینده انجام نشد", show_alert=True)
        except Exception:
            logger.debug("rrep_role: ignored failure", exc_info=True)
        return
    try:
        await callback.answer("نماینده ساخته شد")
    except Exception:
        logger.debug("rrep_role: ignored failure", exc_info=True)
    if callback.message:
        await callback.message.answer(
            "نماینده ساخته شد.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[back_button(None, back_callback_for(REPLY_ACTION_RES_ADD_REP))]]
            ),
        )
