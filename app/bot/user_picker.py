"""«انتخاب از مخاطبین»: staff pick a person from their own Telegram chats instead of typing an id.

Telegram sends the picked person's numeric id straight to the bot (``users_shared``), even for a
person who never started it and even without a username, so this is the exact way to name someone.
Only the id is trusted; a name or username in the share is display text.
"""

from __future__ import annotations

from aiogram.types import KeyboardButton, KeyboardButtonRequestUsers, Message, ReplyKeyboardMarkup

# Echoed back in ``users_shared.request_id``; distinguishes this picker from any other.
PICK_REQUEST_ID = 4101
PICK_LABEL = "👤 انتخاب از مخاطبین"
CANCEL_LABEL = "❌ انصراف"
PICK_HINT = "یا با دکمهٔ «👤 انتخاب از مخاطبین» پایین صفحه، یک نفر را از گفتگوهای تلگرام خودتان انتخاب کنید."


def picker_keyboard() -> ReplyKeyboardMarkup:
    """Reply keyboard with the picker and a cancel; it closes after one use."""
    request = KeyboardButtonRequestUsers(
        request_id=PICK_REQUEST_ID, user_is_bot=False, max_quantity=1, request_username=True, request_name=True
    )
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=PICK_LABEL, request_users=request)], [KeyboardButton(text=CANCEL_LABEL)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def shared_user_id(message: Message) -> int | None:
    """The id picked with this picker, or ``None`` for any other message or picker."""
    shared = message.users_shared
    if shared is None or shared.request_id != PICK_REQUEST_ID:
        return None
    ids = [user.user_id for user in (shared.users or [])] or list(shared.user_ids or [])
    return int(ids[0]) if len(ids) == 1 and int(ids[0]) > 0 else None
