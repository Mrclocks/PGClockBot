from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import TelegramObject

from app.bot.middlewares import (
    DbSessionMiddleware,
    ErrorLogMiddleware,
    ForceJoinMiddleware,
    TermsEntryMiddleware,
    RateLimitMiddleware,
    UserMiddleware,
)
from app.config import get_settings


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
                    await event.answer("دسترسی مالک سیستم لازم است", show_alert=True)
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
    return Bot(
        token=(token or settings.bot_token).strip(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.middleware(ErrorLogMiddleware())
    dp.update.middleware(RateLimitMiddleware())
    dp.update.middleware(DbSessionMiddleware())
    dp.update.middleware(UserMiddleware())
    dp.update.middleware(ForceJoinMiddleware())
    dp.update.middleware(TermsEntryMiddleware())

    from app.bot.handlers import (
        admin,
        admin_backup,
        admin_plans,
        admin_pg_nodes,
        admin_pg_users,
        admin_settings,
        loyalty,
        nav_hubs,
        payments,
        plan_catalog_manage,
        purchase_contact,
        reply_nav,
        reseller,
        reseller_plans,
        reseller_reps,
        reseller_settings,
        shop,
        start,
        terms,
        support,
        services,
        ticket_actions,
        wallet,
    )

    dp.include_router(start.router)
    dp.include_router(terms.router)
    # Reply-keyboard nav early so menu labels win over FSM amount parsers
    dp.include_router(reply_nav.router)
    dp.include_router(nav_hubs.router)
    dp.include_router(purchase_contact.router)
    dp.include_router(loyalty.router)
    dp.include_router(shop.router)
    dp.include_router(wallet.router)
    dp.include_router(services.router)
    from app.bot.handlers import guides

    dp.include_router(guides.router)
    dp.include_router(support.router)
    dp.include_router(ticket_actions.router)
    dp.include_router(payments.router)
    dp.include_router(reseller.router)
    dp.include_router(reseller_reps.router)
    dp.include_router(reseller_plans.router)
    dp.include_router(plan_catalog_manage.router)
    dp.include_router(reseller_settings.router)
    dp.include_router(admin_settings.router)
    dp.include_router(admin_backup.router)
    dp.include_router(admin_pg_users.router)
    dp.include_router(admin_pg_nodes.router)
    dp.include_router(admin_plans.router)
    dp.include_router(admin.router)

    block = _BlockPlatformAdminOnResellerBot()
    owner_gate = _RequireBotOwnerPrincipal()
    for r in (
        admin.router,
        admin_backup.router,
        admin_settings.router,
        admin_plans.router,
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
