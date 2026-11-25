"""The «🌐 وب‌پنل» card: what the web panel settings offer, a button that opens them and «بازگشت».

The card replaces the screen it was opened from (the same bubble), and its back button leads
to that screen: the fixed parent is the settings hub, and the panel history turns it into the
screen the person actually tapped «وب‌پنل» on.
"""

from __future__ import annotations

import html

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot import keyboards as kb
from app.bot.nav_inline import present_inline_only
from app.services.formatting import format_message

ADMIN_SETTINGS_HUB = "nv:adm:settings"
SHOP_SETTINGS_HUB = "res:st:hub"
OPEN_LABEL = "🌐 باز کردن وب‌پنل"

ADMIN_SETTINGS_TITLE = "🌐 تنظیمات کامل وب‌پنل"
ADMIN_SETTINGS_BODY = "ظاهر ربات، رنگ دکمه‌ها، گزارش روزانه، لینک‌ها و متن‌های بلند را در وب‌پنل ویرایش کنید."
SHOP_SETTINGS_TITLE = "🌐 تنظیمات کامل فروشگاه در وب‌پنل"
SHOP_SETTINGS_BODY = (
    "ظاهر، رنگ، گزارش روزانه و متن‌های بلند را در وب‌پنل ویرایش کنید.\n"
    "فقط همین فروشگاه — بدون دسترسی به تنظیمات پلتفرم."
)
SHOP_PLANS_TITLE = "🌐 پلن‌ها در وب‌پنل"
SHOP_PLANS_BODY = (
    "در وب‌پنل از منوی «پلن‌ها» می‌توانید پلن تست، پلن دلخواه و اتصال دقیق "
    "به گروه‌ها/تمپلیت‌های مجاز پاسارگارد را مدیریت کنید."
)


def web_panel_card(
    *, title: str, body: str, url: str | None, ui: dict | None, back_callback: str
) -> tuple[str, InlineKeyboardMarkup]:
    """Card text and keyboard; the open button only when ``url`` is an absolute web address."""
    clean_url = (url or "").strip()
    lines = [body]
    rows: list[list[InlineKeyboardButton]] = []
    if clean_url:
        lines += ["", f"<code>{html.escape(clean_url)}</code>"]
    if clean_url.startswith(("https://", "http://")):
        rows.append([InlineKeyboardButton(text=OPEN_LABEL, url=clean_url)])
    rows.append([kb.back_button(ui, back_callback)])
    return format_message(title, "\n".join(lines)), InlineKeyboardMarkup(inline_keyboard=rows)


async def present_web_panel_card(
    message: Message,
    *,
    title: str,
    body: str,
    url: str | None,
    ui: dict | None,
    back_callback: str,
) -> None:
    """Show the card in place of the tapped screen, or as a new panel after a reply-keyboard tap."""
    text, markup = web_panel_card(title=title, body=body, url=url, ui=ui, back_callback=back_callback)
    await present_inline_only(message, text=text, inline=markup)
