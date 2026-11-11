"""A stand-in FSM state that already holds the reason an admin typed for one specific callback."""

from __future__ import annotations

from typing import Any

from app.bot.handlers.action_reason import KEY_DONE_FOR, KEY_REASON

DEFAULT_REASON = "درخواست مشتری"


class ReasonedState:
    """Minimal async state: ``take_reason`` finds the reason for ``callback_data`` and nothing else."""

    def __init__(self, callback_data: str | None, reason: str = DEFAULT_REASON) -> None:
        self.data: dict[str, Any] = {KEY_REASON: reason, KEY_DONE_FOR: callback_data}

    async def get_data(self) -> dict[str, Any]:
        return dict(self.data)

    async def update_data(self, **values: Any) -> dict[str, Any]:
        self.data.update(values)
        return dict(self.data)

    async def set_data(self, data: dict[str, Any]) -> None:
        self.data = dict(data)

    async def set_state(self, state: Any = None) -> None:
        return None

    async def clear(self) -> None:
        self.data.clear()
