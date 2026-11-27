"""What the web «توقف و انتقال» and «لغو سرویس» settings sections need, and how their values are kept valid.

The sections are the customer's own actions on a service. The page shows the shop's plans for each
action's plan list and the bounds of each number; saving clamps every number to the same bounds
the services read with, so a value typed past a limit is stored as the limit, and replaces a choice
that is not offered with its default.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.plan_scope import plan_choices
from app.services.service_cancellation_settings import CANCEL_BOUNDS, CHOICES as CANCEL_CHOICES, normalize_choices
from app.services.service_freeze_settings import FREEZE_BOUNDS
from app.services.service_transfer_settings import TRANSFER_BOUNDS
from app.services.service_link_reset_settings import LINK_RESET_BOUNDS

NUMBER_BOUNDS: dict[str, tuple[int, int]] = {**FREEZE_BOUNDS, **TRANSFER_BOUNDS, **LINK_RESET_BOUNDS, **CANCEL_BOUNDS}
# Settings tabs rendered with service_actions_context.
SERVICE_ACTION_TABS = frozenset({"service_actions", "service_cancel"})


async def service_actions_context(session: AsyncSession, shop_id: int | None) -> dict[str, Any]:
    """Template values: the shop's own plans, the bounds of each number field and the offered choices."""
    return {
        "plan_scope_choices": await plan_choices(session, shop_id),
        "service_action_bounds": NUMBER_BOUNDS,
        "service_action_choices": CANCEL_CHOICES,
    }


def clamp_service_action_numbers(payload: MutableMapping[str, str], bounds: Mapping[str, tuple[int, int]] = NUMBER_BOUNDS) -> None:
    """Keep each submitted number inside its bounds; a value that is not a whole number is not saved."""
    for key, (low, high) in bounds.items():
        if key not in payload:
            continue
        raw = str(payload[key]).strip()
        if not raw.lstrip("-").isdigit():
            payload.pop(key)
            continue
        payload[key] = str(min(max(int(raw), low), high))


def normalize_service_action_values(payload: MutableMapping[str, str]) -> None:
    """Clamp the sections' numbers and keep their choices to the offered ones, before saving."""
    clamp_service_action_numbers(payload)
    normalize_choices(payload)
