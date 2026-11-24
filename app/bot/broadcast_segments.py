"""«🎯 مخاطبان هدفمند» in the bot: pick a segment for a group message (the platform owner or a shop).

The owner's screen is handled here; a shop's is handled in handlers/shop_broadcast, which resolves the shop.

The hub of each group message offers its broad audiences directly; this screen lists the narrower segments
(broadcast_audience), each with its default day window. A tap continues the existing flow with that segment as
the audience: ``adm:broadcast:aud:<key>`` for the owner, ``shp:bc:aud:<key>`` for a shop. The flows check the
key again against the sender's tenant, so a forged key for another tenant is refused there.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.services.users import get_all_settings
from app.bot.auth import require_bot_owner_handler
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser
from app.services.broadcast_audience import Segment, segments_for

router = Router(name="broadcast_segments")

PLATFORM_LIST = "adm:bc:seg"
SHOP_LIST = "shp:bc:seg"
PLATFORM_PICK = "adm:broadcast:aud:"
SHOP_PICK = "shp:bc:aud:"
PLATFORM_HUB = "nv:adm:broadcast"
SHOP_HUB = "shp:bc:hub"
TARGETED_LABEL = "🎯 مخاطبان هدفمند"
TITLE = "🎯 مخاطبان هدفمند\nیک گروه را انتخاب کنید؛ بازهٔ روزها پیش‌فرض است و در وب‌پنل قابل تغییر است."
# Broad audiences stay on the hub itself.
HUB_SEGMENTS = frozenset({"all", "users", "resellers", "admins"})
SEGMENT_EMOJI: dict[str, str] = {
    "active": "✅",
    "expiring": "⏳",
    "expired": "⌛",
    "no_service": "➖",
    "buyers": "🛒",
    "loyal": "⭐",
    "never_bought": "🆕",
    "trial": "🎁",
    "inactive": "💤",
    "new": "👋",
}


def segment_label(segment: Segment) -> str:
    days = f" · {segment.days} روز" if segment.days else ""
    return f"{SEGMENT_EMOJI.get(segment.key, '•')} {segment.label}{days}"


def targeted_keyboard(
    *, shop_id: int | None, pick_prefix: str, back: str, ui: dict[str, str]
) -> InlineKeyboardMarkup:
    """The narrower segments of this tenant, two to a row, then «بازگشت» to the hub."""
    buttons = [
        InlineKeyboardButton(text=segment_label(s), callback_data=f"{pick_prefix}{s.key}")
        for s in segments_for(shop_id)
        if s.key not in HUB_SEGMENTS
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([kb.back_button(ui, back)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == PLATFORM_LIST)
@require_bot_owner_handler
async def platform_segments(callback: CallbackQuery, session: AsyncSession, db_user: BotUser) -> None:
    await callback.answer()
    if callback.message:
        markup = targeted_keyboard(
            shop_id=None, pick_prefix=PLATFORM_PICK, back=PLATFORM_HUB, ui=await get_all_settings(session)
        )
        await safe_edit_text(callback.message, TITLE, reply_markup=markup)
