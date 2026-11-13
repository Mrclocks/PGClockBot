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

from app.bot.service_import_ui import import_enabled

logger = logging.getLogger(__name__)

HOME_ADD_CALLBACK = "svc:addh"
HOME_HIDE_CALLBACK = "svc:addx"
ADD_LABEL = "اتصال اشتراک قبلی"
HIDE_LABEL = "نمایش نده"
HIDE_DONE = "این پیام دیگر نمایش داده نمی‌شود."
HOME_ADD_NOTE = "برای اضافه کردن سرویس قبلی یا از طریق دکمه زیر یا از بخش «سرویس‌های من» اقدام کنید."


def should_offer_import(ui: dict | None, *, has_services: bool, dismissed: bool) -> bool:
    """A customer of this shop with no service who did not ask to stop, while adding old services is on."""
    return import_enabled(ui) and not has_services and not dismissed


def build_import_prompt() -> InputRichMessage:
    buttons = [
        RichMessageButton(text=ADD_LABEL, style="primary", callback_data=HOME_ADD_CALLBACK),
        RichMessageButton(text=HIDE_LABEL, style="danger", callback_data=HOME_HIDE_CALLBACK),
    ]
    return InputRichMessage(
        is_rtl=True,
        blocks=[
            InputRichBlockParagraph(text=HOME_ADD_NOTE),
            InputRichBlockButtons(buttons=buttons, align="center"),
        ],
    )


async def send_import_prompt(message: Message) -> Message | None:
    """Send the offer; a Telegram refusal only means the offer is skipped."""
    try:
        return await message.bot.send_rich_message(chat_id=message.chat.id, rich_message=build_import_prompt())
    except TelegramAPIError:
        logger.warning("could not send the add-existing-service offer", exc_info=True)
        return None
