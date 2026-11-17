"""Whether a customer may be offered a free trial, which trial plans it would start, and where
the «دریافت تست» button is shown (the home offer, the main keyboard, or both)."""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Plan, TrialClaim
from app.services.orders import list_active_plans
from app.services.service_scope import list_customer_services
from app.services.users import DEFAULT_SETTINGS, current_shop_reseller_id, get_setting, on, set_settings_bulk

# Menu-order key of the trial button on the main keyboard; the menu editor places it like any item.
TRIAL_MENU_KEY = "trial"
TRIAL_LABEL_KEY = "btn_trial"
TRIAL_PLACEMENT_KEY = "trial_button_placement"

PLACEMENT_OFFER = "offer"
PLACEMENT_KEYBOARD = "keyboard"
PLACEMENT_BOTH = "both"
DEFAULT_PLACEMENT = PLACEMENT_OFFER
# Display order of the choices, with their Persian names.
PLACEMENT_LABELS: dict[str, str] = {
    PLACEMENT_OFFER: "پیام ورودی",
    PLACEMENT_KEYBOARD: "کیبورد اصلی",
    PLACEMENT_BOTH: "هر دو",
}
INVALID_PLACEMENT = "محل نمایش دکمهٔ تست نامعتبر است."
# A shop never offers «درخواست نمایندگی», so the platform default order must not leak into one.
_PLATFORM_ONLY_MENU_KEYS = frozenset({"reseller_apply", "miniapp"})


def trial_placement(ui: Mapping[str, str] | None) -> str:
    """The saved placement; anything unknown reads as the default, the home offer."""
    raw = str((ui or {}).get(TRIAL_PLACEMENT_KEY) or "").strip()
    return raw if raw in PLACEMENT_LABELS else DEFAULT_PLACEMENT


def parse_trial_placement(raw: object) -> str | None:
    """A placement sent by a settings form: None when the field was not sent at all."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(INVALID_PLACEMENT)
    value = raw.strip()
    if not value:
        return None
    if value not in PLACEMENT_LABELS:
        raise ValueError(INVALID_PLACEMENT)
    return value


def next_trial_placement(current: str) -> str:
    """The choice after ``current``, for a button that cycles through them."""
    order = list(PLACEMENT_LABELS)
    index = order.index(current) if current in order else -1
    return order[(index + 1) % len(order)]


def shows_trial_in_offer(ui: Mapping[str, str] | None) -> bool:
    return trial_placement(ui) in {PLACEMENT_OFFER, PLACEMENT_BOTH}


def shows_trial_on_keyboard(ui: Mapping[str, str] | None) -> bool:
    return trial_placement(ui) in {PLACEMENT_KEYBOARD, PLACEMENT_BOTH}


def menu_order_with_trial(raw_order: str, *, for_shop: bool) -> str | None:
    """``raw_order`` with the trial button added first; None when it is already in the menu."""
    order = [part.strip() for part in (raw_order or "").split(",") if part.strip()]
    if TRIAL_MENU_KEY in order:
        return None
    if for_shop:
        order = [key for key in order if key not in _PLATFORM_ONLY_MENU_KEYS]
    return ",".join([TRIAL_MENU_KEY, *order])


async def save_trial_placement(session: AsyncSession, placement: str, *, reseller_id: int | None) -> None:
    """Store the placement of one shop (``None`` is the platform).

    Choosing the keyboard puts the button in that shop's menu when it is not there yet, so it
    shows up at once; its position is then the menu editor's, like every other menu item.
    """
    if placement not in PLACEMENT_LABELS:
        raise ValueError(INVALID_PLACEMENT)
    payload = {TRIAL_PLACEMENT_KEY: placement}
    if placement in {PLACEMENT_KEYBOARD, PLACEMENT_BOTH}:
        current_order = await get_setting(
            session, "menu_order", DEFAULT_SETTINGS["menu_order"], reseller_id=reseller_id
        )
        new_order = menu_order_with_trial(current_order, for_shop=reseller_id is not None)
        if new_order is not None:
            payload["menu_order"] = new_order
    await set_settings_bulk(session, payload, reseller_id=reseller_id)


def _trial_shop_key() -> str:
    """Same key `create_order` uses for the one-trial-per-shop claim."""
    shop_id = current_shop_reseller_id()
    return str(int(shop_id)) if shop_id is not None else "platform"


async def has_claimed_trial(session: AsyncSession, user_id: int) -> bool:
    row = await session.scalar(
        select(TrialClaim.id)
        .where(TrialClaim.user_id == int(user_id), TrialClaim.shop_key == _trial_shop_key())
        .limit(1)
    )
    return row is not None


async def offerable_trial_plans(session: AsyncSession, ui: Mapping[str, str] | None, user_id: int) -> list[Plan]:
    """Active trial plans of this shop, empty when trials are off or this customer already took theirs."""
    if not on((ui or {}).get("trial_enabled")):
        return []
    if await has_claimed_trial(session, user_id):
        return []
    plans = await list_active_plans(session, include_trial=True)
    return [plan for plan in plans if plan.is_trial]


async def customer_trial_plans(session: AsyncSession, ui: Mapping[str, str] | None, user_id: int) -> list[Plan]:
    """Trial plans a customer of the current shop may start now: none once they own a service here."""
    if not on((ui or {}).get("trial_enabled")):
        return []
    if await list_customer_services(session, int(user_id), current_shop_reseller_id(), limit=1):
        return []
    return await offerable_trial_plans(session, ui, user_id)
