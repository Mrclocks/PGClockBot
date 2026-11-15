"""Visible PG groups make selection mandatory; empty visible list keeps it optional."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from app.services.plans_catalog import (
    resolve_user_write_group_ids,
    staff_must_select_groups,
)


class StaffGroupSelectionRequiredTests(unittest.TestCase):
    def test_empty_visible_list_is_optional(self):
        self.assertFalse(staff_must_select_groups([]))
        self.assertFalse(staff_must_select_groups(None))
        self.assertIsNone(resolve_user_write_group_ids([], visible_groups=[]))
        self.assertIsNone(resolve_user_write_group_ids(None, visible_groups=[]))

    def test_nonempty_visible_list_requires_selection(self):
        visible = [{"id": 1, "name": "g1"}]
        self.assertTrue(staff_must_select_groups(visible))
        with self.assertRaises(ValueError) as ctx:
            resolve_user_write_group_ids([], visible_groups=visible)
        self.assertIn("حداقل یک گروه", str(ctx.exception))
        self.assertEqual(
            resolve_user_write_group_ids([1, 2], visible_groups=visible),
            [1, 2],
        )


class BotMandatoryWhenVisibleTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_grpdone_rejects_empty_when_groups_visible(self):
        from app.bot.handlers.admin_pg_users import pg_create_grpdone

        callback = AsyncMock(spec=CallbackQuery)
        callback.data = "adm:pg:grpdone"
        callback.answer = AsyncMock()
        state = AsyncMock(spec=FSMContext)
        state.get_data = AsyncMock(return_value={"pg_selected_groups": []})
        state.update_data = AsyncMock()

        pg_client = AsyncMock()
        pg_client.get_groups_simple = AsyncMock(
            return_value=[{"id": 3, "name": "seen"}]
        )
        gate = MagicMock()
        gate.allowed = True
        gate.staff = {
            "role": "reseller",
            "pg_username": "res1",
            "pg_access": {"allowed_group_ids": [3], "allowed_template_ids": []},
        }
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
        ):
            await pg_create_grpdone(callback, state, MagicMock(), session=AsyncMock())

        begin.assert_not_awaited()
        alert = callback.answer.await_args
        self.assertTrue(alert.kwargs.get("show_alert"))
        self.assertIn("حداقل یک گروه", alert.args[0])

    async def test_edit_grpdone_rejects_empty_when_groups_visible(self):
        from app.bot.handlers.admin_pg_users import pg_edit_grpdone

        callback = AsyncMock(spec=CallbackQuery)
        callback.data = "adm:pg:edgrpdone"
        callback.answer = AsyncMock()
        state = AsyncMock(spec=FSMContext)
        state.get_data = AsyncMock(
            return_value={"pg_edit_uid": 42, "pg_selected_groups": []}
        )

        pg_client = AsyncMock()
        pg_client.get_groups_simple = AsyncMock(
            return_value=[{"id": 7, "name": "seen"}]
        )
        pg_client.modify_user_by_id = AsyncMock()
        gate = MagicMock()
        gate.allowed = True
        gate.staff = {
            "role": "reseller",
            "pg_username": "res1",
            "pg_access": {"allowed_group_ids": [7], "allowed_template_ids": []},
        }
        gate.pg_user = {"id": 42, "username": "cust", "group_ids": [7], "status": "active"}
        gate.pg_client = pg_client

        with patch(
            "app.bot.handlers.admin_pg_users._pg_user_gate",
            new=AsyncMock(return_value=gate),
        ):
            await pg_edit_grpdone(callback, state, MagicMock(), session=AsyncMock())

        pg_client.modify_user_by_id.assert_not_awaited()
        alert = callback.answer.await_args
        self.assertTrue(alert.kwargs.get("show_alert"))
        self.assertIn("حداقل یک گروه", alert.args[0])


if __name__ == "__main__":
    unittest.main()
