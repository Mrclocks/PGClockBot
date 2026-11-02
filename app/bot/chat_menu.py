"""Telegram chat menu button — Mini App «Open» or cleared default."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import MenuButtonDefault, MenuButtonWebApp, WebAppInfo

log = logging.getLogger("pgclock.bot")


async def clear_telegram_menu_button(bot: Bot) -> None:
    """Remove BotFather-style Menu / Open button beside the message composer.

    Keeps /start and /help working when typed; they are just not listed in the
    Telegram side menu.
    """
    try:
        await bot.delete_my_commands()
    except Exception:
        log.debug("delete_my_commands failed", exc_info=True)
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
    except Exception:
        log.debug("set_chat_menu_button(default) failed", exc_info=True)
    try:
        await bot.delete_chat_menu_button()
    except Exception:
        # Some Bot API builds only support set_chat_menu_button
        log.debug("delete_chat_menu_button failed", exc_info=True)


async def sync_telegram_menu_button(
    bot: Bot,
    *,
    allow_miniapp: bool = True,
    label: str | None = None,
    url: str | None = None,
) -> str:
    """Set global Menu Button to Mini App «Open» when enabled, else clear it.

    Returns ``webapp`` | ``cleared`` | ``skipped``.
    The platform bot opens the platform Mini App. A shop bot passes its own Mini App
    ``url`` (``/miniapp/s/<key>/``, verified with that bot's token) only while the shop's
    Mini App is on, and ``allow_miniapp=False`` otherwise.
    """
    from app.config import get_settings

    settings = get_settings()
    url = ((url if url is not None else settings.miniapp_url) or "").strip() if allow_miniapp else ""
    if allow_miniapp and settings.miniapp_enabled and url.startswith("https://"):
        text = (label or "Open").strip()[:16] or "Open"
        try:
            await bot.delete_my_commands()
        except Exception:
            log.debug("delete_my_commands failed", exc_info=True)
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text=text,
                    web_app=WebAppInfo(url=url),
                )
            )
            log.info("Telegram menu button → Mini App (%s)", url)
            return "webapp"
        except Exception:
            log.warning("set_chat_menu_button(web_app) failed", exc_info=True)
            await clear_telegram_menu_button(bot)
            return "cleared"
    await clear_telegram_menu_button(bot)
    return "cleared"
