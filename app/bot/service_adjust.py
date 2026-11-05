"""Staged day/GB changes on a service screen, applied once per confirm.

Staff stage a change with the +/- buttons, then confirm. Telegram can deliver two
confirm taps at the same time, so the staged change is taken out of the FSM under
a per-service lock before the panel call. A second tap finds nothing staged.
"""

from __future__ import annotations

import asyncio
import weakref
from dataclasses import dataclass

from aiogram.fsm.context import FSMContext

NOTHING_STAGED = "تغییری برای اعمال نیست؛ ممکن است همین حالا اعمال شده باشد."
UNKNOWN_ADJUST_OUTCOME = (
    "وضعیت نامشخص است — ممکن است اعمال شده باشد. دوباره نزنید؛ "
    "جزئیات سرویس را باز کنید و وضعیت را ببینید."
)

# Locks live only while a confirm holds them, so the map does not grow with services.
# They guard one process: every bot runs in the app process today. Running bots in
# several processes would need a database claim on the service instead.
_service_locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()


@dataclass(frozen=True, slots=True)
class StagedAdjustment:
    days: int
    gb: float

    @property
    def is_empty(self) -> bool:
        return self.days == 0 and self.gb == 0


def service_adjust_lock(service_id: int) -> asyncio.Lock:
    """One confirm at a time per service in this process."""
    lock = _service_locks.get(int(service_id))
    if lock is None:
        lock = asyncio.Lock()
        _service_locks[int(service_id)] = lock
    return lock


def read_staged(data: dict, key: str) -> StagedAdjustment:
    stored = data.get(key) if isinstance(data.get(key), dict) else {}
    return StagedAdjustment(days=int(stored.get("days") or 0), gb=float(stored.get("gb") or 0))


async def take_staged(state: FSMContext, key: str) -> StagedAdjustment:
    """Return the staged change and clear it, so it cannot be applied twice."""
    staged = read_staged(await state.get_data(), key)
    await state.update_data(**{key: {"days": 0, "gb": 0.0}})
    return staged


async def restore_staged(state: FSMContext, key: str, staged: StagedAdjustment) -> None:
    """Put a change back after the panel refused it, so staff can fix and retry."""
    await state.update_data(**{key: {"days": staged.days, "gb": staged.gb}})
