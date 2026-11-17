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
from app.services.button_styles import style_or_none

logger = logging.getLogger(__name__)

HOME_ADD_CALLBACK = "svc:addh"
# Offers sent earlier still carry a «نمایش نده» button, so its handler and answer stay.
HOME_HIDE_CALLBACK = "svc:addx"
HOME_TRIAL_CALLBACK = "svc:trialh"
ADD_LABEL = "اتصال اشتراک قبلی"
TRIAL_LABEL = "دریافت تست"
HIDE_DONE = "این پیام دیگر نمایش داده نمی‌شود."
HOME_ADD_NOTE = "برای اضافه کردن سرویس قبلی یا از طریق دکمه زیر یا از بخش «سرویس‌های من» اقدام کنید."
HOME_BOTH_NOTE = "سرویس قبلی خود را اضافه کنید یا یک تست رایگان بگیرید."
HOME_TRIAL_NOTE = "برای شروع می‌توانید یک تست رایگان بگیرید."


def should_offer_import(
    ui: dict | None, *, has_services: bool, dismissed: bool, trial_available: bool = False
) -> bool:
    """A customer of this shop who did not ask to stop, while adding old services (only without a
    service here) or a free trial (``trial_available`` already applies the shop's rules) can be offered."""
    if dismissed:
        return False
    return (import_enabled(ui) and not has_services) or trial_available


def _offer_note(*, with_import: bool, with_trial: bool) -> str:
    if with_import and with_trial:
        return HOME_BOTH_NOTE
    return HOME_ADD_NOTE if with_import else HOME_TRIAL_NOTE


def build_import_prompt(
    ui: dict | None, *, trial_available: bool = False, has_services: bool = False
) -> InputRichMessage:
    # A customer who owns a service here only reaches this offer for the trial.
    with_import = import_enabled(ui) and not has_services
    buttons: list[RichMessageButton] = []
    if with_import:
        # The colour is the admin's «افزودن سرویس قبلی» button colour, like the same button elsewhere.
        add_style = add_existing_row(ui)[0].style
        buttons.append(RichMessageButton(text=ADD_LABEL, style=add_style, callback_data=HOME_ADD_CALLBACK))
    if trial_available:
        trial_style = style_or_none(ui, "shop_kind_trial", fallback="primary")
        buttons.append(RichMessageButton(text=TRIAL_LABEL, style=trial_style, callback_data=HOME_TRIAL_CALLBACK))
    return InputRichMessage(
        is_rtl=True,
        blocks=[
            InputRichBlockParagraph(text=_offer_note(with_import=with_import, with_trial=trial_available)),
            InputRichBlockButtons(buttons=buttons, align="center"),
        ],
    )


async def send_import_prompt(
    message: Message, ui: dict | None, *, trial_available: bool = False, has_services: bool = False
) -> Message | None:
    """Send the offer; a Telegram refusal only means the offer is skipped."""
    try:
        return await message.bot.send_rich_message(
            chat_id=message.chat.id,
            rich_message=build_import_prompt(ui, trial_available=trial_available, has_services=has_services),
        )
    except TelegramAPIError:
        logger.warning("could not send the add-existing-service offer", exc_info=True)
        return None
