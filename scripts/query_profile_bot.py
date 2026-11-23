#!/usr/bin/env python3
"""Count the SQL statements of every main-menu button and every admin-panel section of the real bot.

The bot runs in the chat simulator of the test suite (``tests/chat_sim.py``) on a throwaway
PostgreSQL database: ``/start``, every button of the customer and owner menus, every inline button
of the admin panel and every button of the admin sections it opens. Each tap is one unit of work.

The tour runs once for each ``--scale`` (default 20 and 200): that many extra customers, and as
many services, orders, tickets and wallet entries for the person touring
(``scripts/query_profile_data.py``). Both scales have data, so a screen that only shows a section
when there is something to show costs the same in both; a tap whose count still grows does work per
row.

    TEST_DATABASE_URL=postgresql+asyncpg://... python scripts/query_profile_bot.py OUT.jsonl [--scale 20 --scale 200]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
# As in tests/conftest.py: the app builds its engine from DATABASE_URL at import time, so it must
# name the test server before any app module loads.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "").strip()

from query_counter import QueryRecorder  # noqa: E402 - the paths are set just above
from query_profile_data import bulk_rows, person_rows  # noqa: E402 - the paths are set just above
from tests import postgres_support  # noqa: E402 - must follow the DATABASE_URL override

RESTART = "🔄 شروع مجدد"
ADMIN_PANEL = "🛠 پنل ادمین"
CUSTOMER_ID = 6101
OWNER_ID = 5101
PLAN_ID = 1
PERSON_FIRST_ID = 300_000
DEFAULT_SCALES = (20, 200)
# Inline buttons that leave the admin panel or switch the menus into edit mode.
SKIPPED_CALLBACKS = ("menu:home", "ed:")
SECTION_PREFIX = "nv:adm:"
# Buttons show live counts, «عملیات روزانه (۴۵)»; the count is dropped so both scales share a label.
_BADGE = re.compile(r"\s*\([0-9۰-۹,٬+]+\)")

Rows = list[dict[str, Any]]


class Tour:
    """One simulated chat whose taps are measured into ``rows``."""

    def __init__(self, sim: Any, recorder: QueryRecorder, rows: Rows, scale: int) -> None:
        self.sim, self.recorder, self.rows, self.scale = sim, recorder, rows, scale

    async def measure(self, label: str, action: Callable[[], Awaitable[None]]) -> None:
        with self.recorder.measure(_BADGE.sub("", label)) as tally:
            await action()
        self.rows.append({**tally.as_dict(), "scale": self.scale})

    def menu_labels(self) -> list[str]:
        return [label for row in self.sim.telegram.keyboard or [] for label in row if label != RESTART]

    def inline_buttons(self) -> list[tuple[str, str]]:
        """Buttons of the newest bot message that carries inline buttons."""
        for shown in reversed(self.sim.telegram.visible()):
            if shown.from_bot and shown.inline:
                return [(text, data) for row in shown.inline for text, data in row
                        if not data.startswith(SKIPPED_CALLBACKS)]
        return []

    async def tap_data(self, data: str) -> None:
        for shown in reversed(self.sim.telegram.visible()):
            if any(d == data for row in shown.inline for _, d in row):
                await self.sim._tap(shown, data)
                return
        raise LookupError(data)

    async def open_admin(self, section: str | None = None) -> None:
        await self.sim.type("/start")
        await self.sim.type(ADMIN_PANEL)
        if section:
            await self.tap_data(section)


async def _seed(sim: Any, *, telegram_id: int, scale: int) -> None:
    from sqlalchemy import select

    from app.db.models import BotUser, Plan

    async with sim.Session() as session:
        session.add(Plan(id=PLAN_ID, name="بسته اقتصادی", price=100000, duration_days=30, data_limit_gb=10,
                         is_active=True))
        await session.flush()
        session.add_all(bulk_rows(scale, plan_id=PLAN_ID))
        await session.commit()
    await sim.type("/start")  # registers the person touring
    async with sim.Session() as session:
        person = await session.scalar(select(BotUser.id).where(BotUser.telegram_id == telegram_id))
        session.add_all(person_rows(person, scale, plan_id=PLAN_ID, first_id=PERSON_FIRST_ID))
        await session.commit()


async def _menu(tour: Tour, who: str) -> None:
    await tour.measure(f"{who}: /start", lambda: tour.sim.type("/start"))
    for label in tour.menu_labels():
        await tour.measure(f"{who}: {label}", lambda label=label: tour.sim.type(label))
        await tour.sim.type("/start")


async def _admin(tour: Tour) -> None:
    await tour.open_admin()
    for text, data in tour.inline_buttons():
        await tour.open_admin()
        await tour.measure(f"admin: {text}", lambda data=data: tour.tap_data(data))
        if not data.startswith(SECTION_PREFIX):
            continue
        for child_text, child_data in tour.inline_buttons():
            await tour.open_admin(data)
            try:
                await tour.measure(f"admin: {text} › {child_text}",
                                   lambda child_data=child_data: tour.tap_data(child_data))
            except LookupError:
                continue


async def _run(*, owner: bool, scale: int, rows: Rows) -> None:
    from app.db.session import engine
    from tests.chat_sim import ChatSim

    telegram_id = OWNER_ID if owner else CUSTOMER_ID
    sim = ChatSim(telegram_id)
    await sim.start(owner=owner)
    recorder = QueryRecorder(sim.engine, engine)
    try:
        with patch("app.bot.middlewares._rate_limited", MagicMock(return_value=False)), \
                patch("app.bot.auth.platform_can_manage_representatives", AsyncMock(return_value=True)):
            await _seed(sim, telegram_id=telegram_id, scale=scale)
            tour = Tour(sim, recorder, rows, scale)
            await _menu(tour, "owner" if owner else "customer")
            if owner:
                await _admin(tour)
    finally:
        recorder.close()
        await sim.stop()


async def profile(scales: list[int]) -> Rows:
    rows: Rows = []
    for scale in scales:
        for owner in (False, True):
            # Each tour gets a database of its own, as each test of the menu tour does.
            scope = postgres_support.begin_scope()
            try:
                await _run(owner=owner, scale=scale, rows=rows)
            finally:
                postgres_support.end_scope(scope)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("out", type=Path)
    parser.add_argument("--scale", type=int, action="append", default=[],
                        help="extra rows for one tour (repeatable; default 20 and 200)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.CRITICAL)
    postgres_support.freeze_template_name()
    try:
        rows = asyncio.run(profile(args.scale or list(DEFAULT_SCALES)))
    finally:
        postgres_support.end_session()
    args.out.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    print(f"query-profile bot: {len(rows)} taps -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
