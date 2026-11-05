"""Staff with panel-assigned groups but an empty selectable list can still create/edit."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from app.services.pasarguard import build_user_create_payload, build_user_modify_payload
from app.services.plans_catalog import groups_allowed_for_staff


def _staff_no_visible_groups() -> dict:
    return {
        "role": "reseller",
        "pg_username": "res1",
        "pg_access": {"allowed_group_ids": [], "allowed_template_ids": []},
    }


class StaffWithoutVisibleGroupsTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_create_skips_group_picker_when_list_empty(self):
        from app.bot.handlers.admin_pg_users import pg_create_custom_groups

        callback = AsyncMock(spec=CallbackQuery)
        callback.data = "adm:pg:create:custom"
        callback.message = AsyncMock()
        callback.answer = AsyncMock()
        state = AsyncMock(spec=FSMContext)
        state.clear = AsyncMock()
        state.update_data = AsyncMock()
        state.set_state = AsyncMock()
        state.get_data = AsyncMock(return_value={})

        pg_client = AsyncMock()
        pg_client.get_groups_simple = AsyncMock(return_value=[])
        gate = MagicMock()
        gate.allowed = True
        gate.staff = _staff_no_visible_groups()
        gate.pg_client = pg_client

        with (
            patch(
                "app.bot.handlers.admin_pg_users._pg_user_gate",
                new=AsyncMock(return_value=gate),
            ),
            patch(
                "app.bot.handlers.admin_pg_users._show_create_group_picker",
                new=AsyncMock(),
            ) as picker,
            patch(
                "app.bot.handlers.admin_pg_users._begin_custom_create_username",
                new=AsyncMock(),
            ) as begin,
        ):
            await pg_create_custom_groups(callback, state, MagicMock(), session=AsyncMock())

        begin.assert_awaited()
        picker.assert_not_awaited()

    async def test_edit_groups_start_refuses_blank_picker(self):
        from app.bot.handlers.admin_pg_users import pg_edit_groups_start

        callback = AsyncMock(spec=CallbackQuery)
        callback.data = "adm:pg:u:42:ed:grps"
        callback.answer = AsyncMock()
        state = AsyncMock(spec=FSMContext)
        state.get_data = AsyncMock(return_value={})
        state.update_data = AsyncMock()

        pg_client = AsyncMock()
        pg_client.get_groups_simple = AsyncMock(return_value=[])
        gate = MagicMock()
        gate.allowed = True
        gate.staff = _staff_no_visible_groups()
        gate.pg_user = {"id": 42, "username": "cust", "group_ids": [7]}
        gate.pg_client = pg_client

        with (
            patch(
                "app.bot.handlers.admin_pg_users._pg_user_gate",
                new=AsyncMock(return_value=gate),
            ),
            patch(
                "app.bot.handlers.admin_pg_users._show_create_group_picker",
                new=AsyncMock(),
            ) as picker,
        ):
            await pg_edit_groups_start(callback, state, MagicMock(), session=AsyncMock())

        picker.assert_not_awaited()
        callback.answer.assert_awaited()
        alert = callback.answer.await_args
        self.assertTrue(alert.kwargs.get("show_alert"))

    def test_create_and_edit_payloads_preserve_panel_groups(self):
        create = build_user_create_payload(username="n1", group_ids=None)
        self.assertNotIn("group_ids", create)

        modify = build_user_modify_payload(
            username="n1",
            data_limit=1024,
            group_ids=None,
            status="active",
        )
        self.assertNotIn("group_ids", modify)
        self.assertEqual(modify["data_limit"], 1024)

    def test_chosen_group_outside_allow_list_still_denied(self):
        staff = {
            "role": "reseller",
            "pg_access": {"allowed_group_ids": [2], "allowed_template_ids": [1]},
        }
        self.assertFalse(groups_allowed_for_staff(staff, [9]))
        self.assertTrue(groups_allowed_for_staff(staff, [2]))

    async def test_group_fetch_failure_does_not_cache_or_skip_to_create(self):
        from app.bot.handlers.admin_pg_users import (
            _load_staff_groups,
            pg_create_custom_groups,
        )

        callback = AsyncMock(spec=CallbackQuery)
        callback.data = "adm:pg:create:custom"
        callback.message = AsyncMock()
        callback.answer = AsyncMock()
        state = AsyncMock(spec=FSMContext)
        state.clear = AsyncMock()
        state.update_data = AsyncMock()
        state.set_state = AsyncMock()
        state.get_data = AsyncMock(return_value={})

        pg_client = AsyncMock()
        pg_client.get_groups_simple = AsyncMock(side_effect=TimeoutError("panel down"))
        gate = MagicMock()
        gate.allowed = True
        gate.staff = _staff_no_visible_groups()
        gate.pg_client = pg_client

        with (
            patch(
                "app.bot.handlers.admin_pg_users._pg_user_gate",
                new=AsyncMock(return_value=gate),
            ),
            patch(
                "app.bot.handlers.admin_pg_users._begin_custom_create_username",
                new=AsyncMock(),
            ) as begin,
            patch(
                "app.bot.handlers.admin_pg_users._show_create_group_picker",
                new=AsyncMock(),
            ) as picker,
        ):
            await pg_create_custom_groups(callback, state, MagicMock(), session=AsyncMock())

        begin.assert_not_awaited()
        picker.assert_not_awaited()
        alert = callback.answer.await_args
        self.assertTrue(alert.kwargs.get("show_alert"))
        # Failure must not write an empty success cache.
        for call in state.update_data.await_args_list:
            kwargs = call.kwargs
            self.assertNotEqual(kwargs.get("pg_groups_cache_ok"), True)

        groups, ok = await _load_staff_groups(pg_client, gate.staff, state)
        self.assertFalse(ok)
        self.assertEqual(groups, [])

    async def test_edit_groups_fetch_failure_stops_with_alert(self):
        from app.bot.handlers.admin_pg_users import pg_edit_groups_start

        callback = AsyncMock(spec=CallbackQuery)
        callback.data = "adm:pg:u:42:ed:grps"
        callback.answer = AsyncMock()
        state = AsyncMock(spec=FSMContext)
        state.get_data = AsyncMock(return_value={})
        state.update_data = AsyncMock()

        pg_client = AsyncMock()
        pg_client.get_groups_simple = AsyncMock(side_effect=TimeoutError("panel down"))
        gate = MagicMock()
        gate.allowed = True
        gate.staff = _staff_no_visible_groups()
        gate.pg_user = {"id": 42, "username": "cust", "group_ids": [7]}
        gate.pg_client = pg_client

        with (
            patch(
                "app.bot.handlers.admin_pg_users._pg_user_gate",
                new=AsyncMock(return_value=gate),
            ),
            patch(
                "app.bot.handlers.admin_pg_users._show_create_group_picker",
                new=AsyncMock(),
            ) as picker,
        ):
            await pg_edit_groups_start(callback, state, MagicMock(), session=AsyncMock())

        picker.assert_not_awaited()
        alert = callback.answer.await_args
        self.assertTrue(alert.kwargs.get("show_alert"))
        # Distinct from the empty-list message.
        self.assertIn("ناموفق", alert.args[0])


if __name__ == "__main__":
    unittest.main()
