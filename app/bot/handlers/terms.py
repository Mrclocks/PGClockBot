"""Terms / rules acceptance callbacks (entry + purchase resume)."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.handlers.start import render_home
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser
from app.services.terms import (
    GATES,
    GateId,
    TermsPrompt,
    needs_purchase_gate,
    record_acceptance,
    shop_scope_id,
)
from app.services.users import get_all_settings

logger = logging.getLogger(__name__)
router = Router(name="terms")

# Must match shop / reseller buy callback_data exactly.
_RESUME_EXACT = frozenset({"shop:custom:buy", "shop:wholesale:buy"})
_RESUME_PREFIXES = ("shop:buy:", "resapply:buy:")


def _resume_allowed(data: str | None) -> bool:
    if not data:
        return False
    if data in _RESUME_EXACT:
        return True
    return any(data.startswith(p) for p in _RESUME_PREFIXES)


async def show_terms_prompt(
    target,
    prompt: TermsPrompt,
    ui: dict,
    *,
    edit: bool = False,
) -> None:
    """Send or edit a terms prompt; preserve premium emoji via entities when packed."""
    markup = kb.terms_inline_keyboard(prompt, ui)
    kwargs: dict = {"reply_markup": markup}
    if prompt.entities:
        kwargs["entities"] = prompt.entities
        kwargs["parse_mode"] = None
    message = getattr(target, "message", None)
    if edit and message is not None:
        ok = await safe_edit_text(message, prompt.text, **kwargs)
        if ok:
            return
        await message.answer(prompt.text, **kwargs)
        return
    msg = message or target
    await msg.answer(prompt.text, **kwargs)


async def prompt_terms_if_needed(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    ui: dict,
    gate: GateId,
    *,
    reseller_owner_id: int | None = None,
) -> bool:
    """If gate terms required, show prompt and stash resume callback. True = blocked."""
    prompt = await needs_purchase_gate(
        session,
        db_user,
        ui,
        gate,
        reseller_owner_id=reseller_owner_id,
    )
    if prompt is None:
        return False
    await state.update_data(terms_resume=callback.data, terms_gate=gate)
    await callback.answer()
    await show_terms_prompt(callback, prompt, ui, edit=True)
    return True


@router.callback_query(F.data.startswith("terms:ok:"))
async def terms_accept(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    gate = (callback.data or "").split(":")[-1]
    if gate not in GATES:
        await callback.answer("نامعتبر", show_alert=True)
        return
    ui = await get_all_settings(session)
    await record_acceptance(
        session,
        bot_user_id=int(db_user.id),
        shop_owner_id=shop_scope_id(reseller_owner_id=reseller_owner_id),
        gate=gate,  # type: ignore[arg-type]
        ui=ui,
    )
    data = await state.get_data()
    resume = data.get("terms_resume")
    await state.update_data(terms_resume=None, terms_gate=None)

    if gate == "entry":
        await callback.answer("پذیرفته شد ✅")
        if callback.message:
            await render_home(
                callback.message,
                session,
                db_user,
                seed_reply_kb=True,
                is_reseller_bot=is_reseller_bot,
                reseller_owner_id=reseller_owner_id,
                ui=ui,
            )
        return

    if not _resume_allowed(resume if isinstance(resume, str) else None):
        await callback.answer("پذیرفته شد ✅ — دوباره خرید را بزنید", show_alert=True)
        return

    await callback.answer("پذیرفته شد ✅")
    object.__setattr__(callback, "data", resume)
    try:
        if resume.startswith("shop:buy:") or resume in _RESUME_EXACT:
            from app.bot.handlers import shop as shop_handlers

            if resume.startswith("shop:buy:"):
                await shop_handlers.shop_buy(callback, session, db_user, state)
            elif resume == "shop:custom:buy":
                await shop_handlers.custom_buy(callback, session, db_user, state)
            elif resume == "shop:wholesale:buy":
                await shop_handlers.wholesale_buy(callback, session, db_user, state)
        elif resume.startswith("resapply:buy:"):
            from app.bot.handlers import reseller as reseller_handlers

            await reseller_handlers.resapply_buy(
                callback,
                session,
                db_user,
                state,
                is_reseller_bot=is_reseller_bot,
                reseller_owner_id=reseller_owner_id,
            )
    except Exception:
        logger.exception("terms resume failed gate=%s resume=%s", gate, resume)
        if callback.message:
            await callback.message.answer("پذیرفته شد — لطفاً دوباره خرید را انتخاب کنید.")
