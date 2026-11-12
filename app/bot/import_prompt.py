"""Home-screen offer to add a service bought before: a short rich message under the welcome.

The welcome message is sent untouched with the menu keyboard. This note and its button follow it
as a separate message, so a rendering problem here can never affect the welcome or the keyboard.
"""

from __future__ import annotations

import logging

from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    InputRichBlockButtons,
    InputRichBlockParagraph,
    InputRichMessage,
    Message,
    RichMessageButton,
)

from app.bot.service_import_ui import add_existing_row, import_enabled

logger = logging.getLogger(__name__)

HOME_ADD_CALLBACK = "svc:addh"
HOME_ADD_NOTE = "برای اضافه کردن سرویس قبلی یا از طریق دکمه زیر یا از بخش «سرویس‌های من» اقدام کنید."


def should_offer_import(ui: dict | None, *, has_services: bool) -> bool:
    """Only a customer of this shop who has no service yet, while adding old services is on."""
    return import_enabled(ui) and not has_services


def build_import_prompt(ui: dict | None) -> InputRichMessage:
    source = add_existing_row(ui)[0]
    button = RichMessageButton(text=source.text, style=source.style, callback_data=HOME_ADD_CALLBACK)
    return InputRichMessage(
        is_rtl=True,
        blocks=[
            InputRichBlockParagraph(text=HOME_ADD_NOTE),
            InputRichBlockButtons(buttons=[button], align="center"),
        ],
    )


async def send_import_prompt(message: Message, ui: dict | None) -> Message | None:
    """Send the offer; a Telegram refusal only means the offer is skipped."""
    try:
        return await message.bot.send_rich_message(chat_id=message.chat.id, rich_message=build_import_prompt(ui))
    except TelegramAPIError:
        logger.warning("could not send the add-existing-service offer", exc_info=True)
        return None
