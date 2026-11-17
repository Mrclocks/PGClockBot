"""«دریافت تست» on the main keyboard: starts the same trial purchase as the home offer.

A keyboard tap has no callback to hand to the shop, so the purchase runs as a replayed
«shop:buy:<id>» tap on a message of its own. Every gate of the shop flow (terms, contact,
account age, one trial per customer and shop) runs unchanged; an alert it raises is shown in
that message instead of the toast a real tap would get.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Mapping
from functools import lru_cache

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.handlers.home_trial import TRIAL_PREPARING, TRIAL_TITLE, TRIAL_UNAVAILABLE
from app.bot.nav_chrome import heal_main_reply
from app.bot.screen_replay import ReplayLog, replay_screen, watching_replay
from app.bot.tg_utils import safe_edit_text
from app.bot.verbatim_input import NotVerbatimTextInput
from app.db.models import BotUser, Plan
from app.services.formatting import format_message
from app.services.reply_label_aliases import ALIAS_SETTING_KEY, label_variants, register_aliases_on_mapping
from app.services.trial_offer import TRIAL_LABEL_KEY, TRIAL_MENU_KEY, customer_trial_plans, shows_trial_on_keyboard
from app.services.users import DEFAULT_SETTINGS, get_all_settings

logger = logging.getLogger(__name__)

router = Router(name="trial_menu")

TRIAL_NOT_STARTED = "شروع سرویس تست ممکن نشد؛ دوباره تلاش کنید."
# Distinct (label, aliases) pairs kept; one per shop that renamed the button is plenty.
_LABEL_CACHE_SIZE = 256


def trial_menu_labels(ui: Mapping[str, str]) -> frozenset[str]:
    """Texts a tap on the trial button may carry: its label now and the labels it had before."""
    raw = ui.get(TRIAL_LABEL_KEY) or DEFAULT_SETTINGS.get(TRIAL_LABEL_KEY, "")
    return _labels_for(str(raw), str(ui.get(ALIAS_SETTING_KEY) or ""))


@lru_cache(maxsize=_LABEL_CACHE_SIZE)
def _labels_for(raw_label: str, stored_aliases: str) -> frozenset[str]:
    # The filter runs on every private text message. Aliases are only added for actions already in the
    # mapping (here «trial» alone), so the label and the stored aliases fully decide the result.
    mapping = {label: TRIAL_MENU_KEY for label in label_variants(raw_label)}
    register_aliases_on_mapping(mapping, {TRIAL_LABEL_KEY: raw_label, ALIAS_SETTING_KEY: stored_aliases})
    return frozenset(mapping)


class TrialMenuLabel(BaseFilter):
    async def __call__(self, message: Message, session: AsyncSession) -> bool:
        text = (message.text or "").strip()
        if not text:
            return False
        return text in trial_menu_labels(await get_all_settings(session))


def _refusal(log: ReplayLog) -> str | None:
    return next((answer.text for answer in log.answers if answer.show_alert and answer.text), None)


async def _start_purchase(message: Message, session: AsyncSession, plan: Plan) -> None:
    """Run the shop purchase of ``plan`` on a message of its own and show why it stopped, if it did."""
    if message.from_user is None or message.bot is None:
        return
    screen = await message.answer(format_message(TRIAL_TITLE, TRIAL_PREPARING))
    # The replayed purchase opens its own session and writes the order; end this read first.
    await session.commit()
    with watching_replay() as log:
        handled = await replay_screen(
            message.bot,
            user=message.from_user,
            chat_id=message.chat.id,
            data=f"shop:buy:{int(plan.id)}",
            message=screen,
        )
    if log.was_edited(screen.chat.id, screen.message_id):
        return
    refusal = _refusal(log)
    if refusal is None and handled:
        # The purchase answered with a message of its own (a contact or terms step).
        await _delete_quietly(screen)
        return
    await safe_edit_text(screen, html.escape(refusal or TRIAL_NOT_STARTED))


async def _delete_quietly(screen: Message) -> None:
    try:
        await screen.delete()
    except TelegramAPIError:
        logger.debug("trial preparing message already gone", exc_info=True)


# A settings prompt that stores text as typed (renaming this very button) keeps the label as its answer.
@router.message(F.chat.type == "private", NotVerbatimTextInput(), TrialMenuLabel())
async def keyboard_get_trial(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    await state.set_state(None)
    ui = await get_all_settings(session)
    plans = await customer_trial_plans(session, ui, int(db_user.id)) if shows_trial_on_keyboard(ui) else []
    if not plans:
        # A stale keyboard: say so and send the current menu, which no longer has the button.
        await heal_main_reply(
            message,
            session,
            db_user,
            text=TRIAL_UNAVAILABLE,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
            ui=ui,
        )
        return
    if len(plans) > 1:
        await message.answer(
            format_message("پلن تست", "پلن تست را انتخاب کنید:"),
            reply_markup=kb.plans_keyboard(plans, ui, back_callback="menu:home", kind="trial"),
        )
        return
    await _start_purchase(message, session, plans[0])
