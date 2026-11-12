"""Open a notice's screen in a panel under the notice, and close that panel again."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

from app.bot.notice_panels import notice_panels, panel_key
from app.bot.screen_replay import ReplayAnswer, replay_screen, watching_replay
from app.bot.tg_utils import safe_edit_text
from app.services import bot_feedback_texts as feedback
from app.services.notice_links import NOTICE_CLOSE_CALLBACK, NOTICE_OPEN_PREFIX, opened_screen

logger = logging.getLogger(__name__)

router = Router(name="notice_panels")

OPENING_TEXT = "⏳ در حال باز کردن…"
CLOSED_TEXT = "بسته شد."


def _refusal(answers: list[ReplayAnswer]) -> ReplayAnswer | None:
    """The alert a screen raised instead of opening (no access, not found)."""
    return next((a for a in answers if a.show_alert and a.text), None)


def _toast(answers: list[ReplayAnswer]) -> str | None:
    return next((a.text for a in answers if a.text), None)


async def _remove_panel(bot: Bot, panel: Message) -> None:
    notice_panels.close(panel_key(bot, panel))
    try:
        await bot.delete_message(chat_id=panel.chat.id, message_id=panel.message_id)
    except TelegramAPIError:
        # Older than Telegram's delete window: leave a closed panel instead of a live one.
        logger.debug("notice panel not deleted message=%s", panel.message_id, exc_info=True)
        await safe_edit_text(panel, CLOSED_TEXT)


@router.callback_query(F.data.startswith(NOTICE_OPEN_PREFIX))
async def open_notice_screen(callback: CallbackQuery, bot: Bot) -> None:
    screen = opened_screen(callback.data)
    notice = callback.message
    if screen is None or not isinstance(notice, Message):
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    try:
        panel = await notice.reply(OPENING_TEXT, allow_sending_without_reply=True)
    except TelegramAPIError:
        logger.warning("notice panel not sent screen=%s", screen, exc_info=True)
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    notice_panels.open(panel_key(bot, panel), screen)
    with watching_replay() as replay:
        handled = await replay_screen(bot, user=callback.from_user, chat_id=panel.chat.id, data=screen, message=panel)
    refusal = _refusal(replay.answers)
    # A gate may refuse without a word (a platform screen on a shop bot), so only an edited panel counts.
    if handled and refusal is None and int(panel.message_id) in replay.edited:
        await callback.answer(_toast(replay.answers))
        return
    await _remove_panel(bot, panel)
    await callback.answer(refusal.text if refusal else feedback.INVALID, show_alert=True)


@router.callback_query(F.data == NOTICE_CLOSE_CALLBACK)
async def close_notice_panel(callback: CallbackQuery, bot: Bot) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await _remove_panel(bot, callback.message)
