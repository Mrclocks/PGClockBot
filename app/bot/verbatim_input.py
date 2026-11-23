"""Prompts whose typed answer is stored as-is, so menu labels must never interrupt them.

An admin renaming «درخواست نمایندگی» or «🔄 شروع مجدد» from the settings types exactly that
label. Navigation routers run before the settings routers, so without this guard the typed
value opens the matching menu (or restarts the bot) instead of being saved. During these
prompts the person leaves with the inline cancel button, «انصراف» or /start.
"""

from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

# State names are ``StatesGroup:state`` strings so this module imports no handler module.
VERBATIM_TEXT_INPUT_STATES: frozenset[str] = frozenset(
    {
        # Owner bot settings: values (button labels, texts), support, trial name.
        "SettingsStates:edit_value",
        "SettingsStates:support_title",
        "SettingsStates:support_telegram",
        "SettingsStates:trial_name",
        # Command center search: a typed «خرید» searches for the shop instead of opening it.
        "CommandCenterStates:query",
        # Shop bot settings.
        "ResellerSettingsStates:edit_value",
        "ResellerSettingsStates:support_title",
        "ResellerSettingsStates:support_telegram",
        "ResellerSettingsStates:bot_token",
        # Guide editors: titles, bodies and link labels.
        "OwnerGuideInput:title",
        "OwnerGuideInput:body",
        "OwnerGuideInput:new_title",
        "OwnerGuideInput:new_body",
        "OwnerGuideInput:link_url",
        "OwnerGuideInput:link_label",
        "ShopGuideInput:title",
        "ShopGuideInput:body",
        "ShopGuideInput:new_title",
        "ShopGuideInput:new_body",
        "ShopGuideInput:link_url",
        "ShopGuideInput:link_label",
        # Payment destination lists: card holder names, gateway names and links, wallets.
        "OwnerDestinationInput:value",
        "ShopDestinationInput:value",
        # Loyalty and plan catalog texts.
        "LoyaltyManageStates:edit_referral_text",
        "PlanCatalogStates:cat_name",
        "PlanCatalogStates:cat_edit_name",
        "PlanCatalogStates:cat_edit_desc",
        "PlanCatalogStates:addon_name",
    }
)


def is_verbatim_text_input(state_name: str | None) -> bool:
    return state_name in VERBATIM_TEXT_INPUT_STATES


class NotVerbatimTextInput(BaseFilter):
    """Pass unless the chat is answering a prompt from ``VERBATIM_TEXT_INPUT_STATES``."""

    async def __call__(self, message: Message, state: FSMContext) -> bool:
        return not is_verbatim_text_input(await state.get_state())
