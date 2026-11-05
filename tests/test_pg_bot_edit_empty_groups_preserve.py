"""Bot group-edit with empty selection must not wipe the user's current groups."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery


class BotEditEmptyGroupsPreserveTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_selection_omits_group_ids_from_modify_payload(self):
        from app.bot.handlers.admin_pg_users import pg_edit_grpdone

        callback = AsyncMock(spec=CallbackQuery)
        callback.data = "adm:pg:edgrpdone"
        callback.message = AsyncMock()
        callback.answer = AsyncMock()
        state = AsyncMock(spec=FSMContext)
        state.get_data = AsyncMock(
            return_value={"pg_edit_uid": 42, "pg_selected_groups": []}
        )
        state.clear = AsyncMock()
        db_user = MagicMock()
        session = AsyncMock()

        pg_client = AsyncMock()
        # Successful fetch with no staff-visible groups → selection optional;
        # empty selection must omit group_ids (preserve panel-assigned groups).
        pg_client.get_groups_simple = AsyncMock(return_value=[])
        gate = MagicMock()
        gate.allowed = True
        gate.staff = {
            "role": "reseller",
            "pg_access": {"allowed_group_ids": []},
        }
        gate.pg_user = {
            "id": 42,
            "username": "cust1",
            "status": "active",
            "group_ids": [7, 9],
        }
        gate.pg_client = pg_client

        with (
            patch(
                "app.bot.handlers.admin_pg_users._pg_user_gate",
                new=AsyncMock(return_value=gate),
            ),
            patch(
                "app.bot.handlers.admin_pg_users._show_user_card",
                new=AsyncMock(),
            ),
        ):
            await pg_edit_grpdone(
                callback,
                state,
                db_user,
                session=session,
            )

        pg_client.modify_user_by_id.assert_awaited()
        uid, payload = pg_client.modify_user_by_id.await_args.args
        self.assertEqual(uid, 42)
        self.assertNotIn(
            "group_ids",
            payload,
            "empty bot group selection must omit group_ids so the panel keeps "
            "the customer's existing groups (assigned on the admin, not listed)",
        )


if __name__ == "__main__":
    unittest.main()
