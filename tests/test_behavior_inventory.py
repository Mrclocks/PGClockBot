"""The behaviour inventory tells a pure code move from a change in what the app answers."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aiogram import F
from aiogram.fsm.state import State, StatesGroup

from scripts.behavior_inventory import compare, describe


def _write(directory: str, name: str, data: dict) -> Path:
    path = Path(directory) / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


ROUTE = {"path": "/plans", "kind": "APIRoute", "methods": ["GET"], "handler": "app.api.app.plans_page",
         "dependencies": ["require_perm"]}
HANDLER = {"router": "dispatcher/plans", "event": "callback_query", "handler": "app.bot.handlers.admin.plans",
           "filters": [describe(F.data.startswith("adm:plans"))]}


def _states_class_in(module: str) -> type[StatesGroup]:
    class AdminStates(StatesGroup):
        add_plan_name = State()

    AdminStates.__module__ = module
    return AdminStates


class DescribeTests(unittest.TestCase):
    def test_a_state_is_described_by_its_stored_name_wherever_its_class_lives(self) -> None:
        before, after = _states_class_in("app.bot.handlers.admin"), _states_class_in("app.bot.handlers.admin_states")
        self.assertEqual(describe(before.add_plan_name), describe(after.add_plan_name))
        self.assertEqual(describe(before.add_plan_name), {"state": "AdminStates:add_plan_name"})
        self.assertEqual(describe(before), describe(after))

    def test_filters_are_described_by_what_they_match_not_by_memory_address(self) -> None:
        one, same, other = (describe(F.data.startswith(prefix)) for prefix in ("adm:", "adm:", "res:"))
        self.assertEqual(one, same)
        self.assertNotEqual(one, other)
        self.assertNotIn("0x", json.dumps(one))
        self.assertIn("adm:", json.dumps(one))


class CompareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.before = _write(self.tmp.name, "before.json", {"web": [ROUTE], "bot": [HANDLER]})

    def test_identical_recordings_pass(self) -> None:
        after = _write(self.tmp.name, "after.json", {"web": [ROUTE], "bot": [HANDLER]})
        self.assertEqual(compare(self.before, after, ignore_modules=False), 0)

    def test_a_lost_permission_or_changed_filter_fails(self) -> None:
        for web, bot in (
            ([{**ROUTE, "dependencies": []}], [HANDLER]),
            ([ROUTE], [{**HANDLER, "filters": [describe(F.data.startswith("adm:plan"))]}]),
            ([], [HANDLER]),
        ):
            with self.subTest(web=web, bot=bot):
                after = _write(self.tmp.name, "after.json", {"web": web, "bot": bot})
                self.assertEqual(compare(self.before, after, ignore_modules=True), 1)

    def test_a_handler_moved_to_another_module_passes_only_when_moves_are_allowed(self) -> None:
        moved = {**HANDLER, "handler": "app.bot.handlers.admin_plans.plans"}
        after = _write(self.tmp.name, "after.json", {"web": [ROUTE], "bot": [moved]})
        self.assertEqual(compare(self.before, after, ignore_modules=False), 1)
        self.assertEqual(compare(self.before, after, ignore_modules=True), 0)


if __name__ == "__main__":
    unittest.main()
