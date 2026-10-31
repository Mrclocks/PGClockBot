"""Shared pieces of the «افزودن سرویس قبلی» entry point in the customer's service list."""

from __future__ import annotations

from collections.abc import Mapping

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.keyboards import _ikb, _t, back_button
from app.services.service_import import SETTING_KEY
from app.services.users import current_shop_reseller_id, on

ADD_CALLBACK = "svc:add"
EMPTY_LIST_HINT = "اگر سرویسی بیرون از ربات گرفته‌اید، با لینک اشتراک آن را اضافه کنید."


def import_enabled(ui: Mapping[str, str] | None) -> bool:
    return on((ui or {}).get(SETTING_KEY, "0"))


def add_existing_row(ui: dict | None) -> list[InlineKeyboardButton]:
    return [
        _ikb(
            _t(ui, "btn_svc_add_existing"),
            callback_data=ADD_CALLBACK,
            ui=ui,
            label_key="btn_svc_add_existing",
            style_id="svc_add_existing",
        )
    ]


def empty_services_keyboard(ui: dict | None) -> InlineKeyboardMarkup:
    """Customer without services: buy a new one, connect one bought before, or go back home."""
    buy = [_ikb(_t(ui, "btn_shop"), callback_data="shop:list", ui=ui, style_id="shop", style_fallback="primary", label_key="btn_shop")]
    return InlineKeyboardMarkup(inline_keyboard=[buy, add_existing_row(ui), [back_button(ui, "menu:home")]])


async def announce_new_request(message, session, user, result) -> None:
    """Tell the admins about a freshly queued request (once; repeats of the same link stay quiet)."""
    if not result.new_request or result.request_id is None:
        return
    from app.services.service_import_notify import notify_service_import_request

    await notify_service_import_request(
        message.bot, session, user=user,
        panel_username=str(result.info.get("username") or ""), request_id=result.request_id,
        shop_id=current_shop_reseller_id(),
    )
