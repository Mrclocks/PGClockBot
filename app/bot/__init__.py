from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware, Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import TelegramObject

from app.bot.chat_cleanup import ChatCleanupMiddleware, install_notice_watch
from app.bot.edit_mode import EditModeMiddleware
from app.bot.menu_keyboard_reopen import MenuKeyboardReopenMiddleware, install_menu_keyboard_watch
from app.bot.reply_keyboard_state import ReplyKeyboardStateMiddleware
from app.bot.screen_replay import install_replay_guard, set_dispatcher
from app.bot.middlewares import (
    DbSessionMiddleware,
    ErrorLogMiddleware,
    ForceJoinMiddleware,
    TermsEntryMiddleware,
    RateLimitMiddleware,
    UserMiddleware,
)
from app.config import get_settings
from app.services import bot_feedback_texts as feedback


class _BlockPlatformAdminOnResellerBot(BaseMiddleware):
    """Prevent main-panel admin tools from running on reseller-owned bots."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if data.get("is_reseller_bot"):
            return None
        return await handler(event, data)



class _RequireBotOwnerPrincipal(BaseMiddleware):
    """H4 — admin routers require explicit Owner Principal (not role alone).

    Shop bots are already blocked by ``_BlockPlatformAdminOnResellerBot``.
    Callback/message tampering by non-Owner candidates is denied here.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if data.get("is_reseller_bot"):
            return None
        from app.bot.auth import bot_admin_settings_in_flow, is_bot_owner_principal

        if await bot_admin_settings_in_flow(event, data):
            return await handler(event, data)

        ok = await is_bot_owner_principal(
            data.get("session"),
            data.get("db_user"),
            is_reseller_bot=bool(data.get("is_reseller_bot")),
        )
        if not ok:
            from aiogram.types import CallbackQuery, Message

            answer = getattr(event, "answer", None)
            if isinstance(event, CallbackQuery) or (
                callable(answer) and hasattr(event, "data")
            ):
                try:
                    await event.answer(feedback.SYSTEM_OWNER_REQUIRED, show_alert=True)
                except Exception:
                    pass
                return None
            if isinstance(event, Message):
                try:
                    await event.answer("دسترسی مالک سیستم لازم است.")
                except Exception:
                    pass
                return None
            return None
        return await handler(event, data)


def create_bot(token: str | None = None) -> Bot:
    settings = get_settings()
    bot = Bot(token=(token or settings.bot_token).strip(), default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    return install_notice_watch(install_menu_keyboard_watch(install_replay_guard(bot)))


def bot_routers() -> list[Router]:
    """Every bot router in the order the dispatcher tries them; the first matching handler wins."""
    # Imported here, as before, so importing ``app.bot`` does not load every handler module.
    from app.bot.handlers import (
        admin,
        admin_backup,
        admin_plans,
        admin_pg_nodes,
        admin_pg_users,
        admin_settings,
        button_colors,
        customer_linking,
        connection_status_admin,
        connection_status_admin_input,
        connection_status,
        guide_editor,
        loyalty,
        nav_hubs,
        marketing_tools,
        nav_marketing,
        payments,
        plan_catalog_manage,
        psp_check,
        purchase_contact,
        reply_nav,
        reseller,
        reseller_hub,
        reseller_package,
        reseller_plans,
        reseller_reps,
        reseller_settings,
        screen_editor,
        service_import,
        service_search,
        shop,
        stale_menu,
        start,
        terms,
        support,
        services,
        ticket_actions,
        wallet,
    )
    from app.bot import nav_input as nav_input_mod
    from app.bot import staff_cancel as _staff_cancel  # noqa: F401 — cancel registry
    from app.bot.handlers import guides

    return [
        # The menu editor's text input comes first: a typed button name such as «👍 کاربران» or
        # «🏡 منوی اصلی» must be saved, not read as that menu's label by the navigation below.
        # A tap on a visible menu button is passed on (see screen_editor._is_menu_tap).
        screen_editor.owner_router,
        screen_editor.shop_router,
        customer_linking.router,
        start.router,
        terms.router,
        connection_status.router,
        connection_status_admin.router,
        # Reply-keyboard nav early so menu labels win over FSM amount parsers
        reply_nav.router,
        nav_hubs.router,
        reseller_hub.router,
        nav_marketing.router,
        marketing_tools.router,
        nav_input_mod.router,
        connection_status_admin_input.router,
        purchase_contact.router,
        loyalty.router,
        shop.router,
        wallet.router,
        service_import.router,
        service_search.router,
        services.router,
        guides.router,
        support.router,
        ticket_actions.router,
        payments.router,
        psp_check.router,
        reseller.router,
        reseller_package.router,
        reseller_reps.router,
        reseller_plans.router,
        plan_catalog_manage.router,
        reseller_settings.router,
        admin_settings.router,
        button_colors.owner_router,
        button_colors.shop_router,
        guide_editor.owner_router,
        guide_editor.shop_router,
        admin_backup.router,
        admin_pg_users.router,
        admin_pg_nodes.router,
        admin_plans.router,
        admin.router,
        stale_menu.router,
    ]


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.middleware(ErrorLogMiddleware())
    dp.update.middleware(RateLimitMiddleware())
    dp.update.middleware(DbSessionMiddleware())
    dp.update.middleware(UserMiddleware())
    dp.update.middleware(ForceJoinMiddleware())
    dp.update.middleware(TermsEntryMiddleware())
    dp.update.middleware(EditModeMiddleware())
    dp.update.middleware(ChatCleanupMiddleware())
    # Notes typed text so the next main menu unfolds the reply keyboard again.
    dp.message.outer_middleware(ReplyKeyboardStateMiddleware())
    dp.message.outer_middleware(MenuKeyboardReopenMiddleware())

    for router in bot_routers():
        dp.include_router(router)
    set_dispatcher(dp)
    from app.bot.handlers import (
        admin,
        admin_backup,
        admin_plans,
        admin_pg_nodes,
        admin_pg_users,
        admin_settings,
        button_colors,
        connection_status_admin,
        connection_status_admin_input,
        guide_editor,
        screen_editor,
    )


    block = _BlockPlatformAdminOnResellerBot()
    owner_gate = _RequireBotOwnerPrincipal()
    for r in (
        admin.router,
        admin_backup.router,
        admin_settings.router,
        button_colors.owner_router,
        guide_editor.owner_router,
        screen_editor.owner_router,
        admin_plans.router,
        connection_status_admin.router,
        connection_status_admin_input.router,
    ):
        r.message.middleware(block)
        r.callback_query.middleware(block)
        r.message.middleware(owner_gate)
        r.callback_query.middleware(owner_gate)
    # Phase 4G — migrated PG families: shop-bot block only (Principal gate inside handlers).
    for r in (
        admin_pg_users.router,
        admin_pg_nodes.router,
    ):
        r.message.middleware(block)
        r.callback_query.middleware(block)
    return dp
