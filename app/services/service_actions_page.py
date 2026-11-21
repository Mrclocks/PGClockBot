"""What the web «توقف و انتقال» settings section needs, and how its numbers are kept in range.

The section is the customer's own actions on a service. The page shows the shop's plans for each
action's plan list and the bounds of each number; saving clamps every number to the same bounds
the services read with, so a value typed past a limit is stored as the limit.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.plan_scope import plan_choices
from app.services.service_freeze_settings import FREEZE_BOUNDS

NUMBER_BOUNDS: dict[str, tuple[int, int]] = {**FREEZE_BOUNDS}


async def service_actions_context(session: AsyncSession, shop_id: int | None) -> dict[str, Any]:
    """Template values: the shop's own plans and the bounds of each number field."""
    return {"plan_scope_choices": await plan_choices(session, shop_id), "service_action_bounds": NUMBER_BOUNDS}


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
