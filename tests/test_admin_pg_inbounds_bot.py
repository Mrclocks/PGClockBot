"""Read-only PG inbounds/admins in the bot: the page gate fails closed and output is escaped."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.bot.handlers import admin_pg_inbounds as view
from app.services import bot_feedback_texts as feedback
from app.services import bot_pg_page_authz as authz
from app.services.bot_pg_page_authz import BotPgPageGate

OWNER_STAFF = {"role": "admin", "org_principal_id": 1, "org_depth": 0, "org_parent_id": None, "org_status": "active",
               "pg_permissions": ["pg_inbounds", "pg_admins"], "pg_is_owner": True, "pg_capabilities_ok": True}


class PageGateTests(unittest.IsolatedAsyncioTestCase):
    async def gate(self, identity, *, page="pg_inbounds", owner_only=False, ready=True, allowed=True):
        with patch.object(authz, "resolve_bot_pg_family_identity", AsyncMock(return_value=identity)), \
                patch.object(authz, "principal_pg_authz_ready", MagicMock(return_value=ready)), \
                patch.object(authz, "authorize_pg_page", MagicMock(return_value=SimpleNamespace(allowed=allowed))), \
                patch.object(authz, "bot_pg_client_for_resolution", AsyncMock(return_value=(MagicMock(), True))):
            return await authz.authorize_bot_pg_page(
                MagicMock(), db_user=MagicMock(), page=page, owner_only=owner_only,
            )

    async def test_identity_failure_such_as_a_shop_bot_is_denied(self) -> None:
        gate = await self.gate("shop_bot_isolated")
        self.assertFalse(gate.allowed)
        self.assertEqual(gate.user_message, feedback.NOT_ADMIN)

    async def test_missing_capability_is_denied(self) -> None:
        gate = await self.gate(SimpleNamespace(staff=dict(OWNER_STAFF)), allowed=False)
        self.assertFalse(gate.allowed)

    async def test_owner_only_page_refuses_a_non_owner(self) -> None:
        gate = await self.gate(SimpleNamespace(staff={"role": "reseller", "bot_user_id": 5}), page="pg_admins", owner_only=True)
        self.assertFalse(gate.allowed)
        self.assertEqual(gate.user_message, feedback.SYSTEM_OWNER_REQUIRED)

    async def test_owner_gets_the_clients(self) -> None:
        gate = await self.gate(SimpleNamespace(staff=dict(OWNER_STAFF)), page="pg_admins", owner_only=True)
        self.assertTrue(gate.allowed)
        self.assertIsNotNone(gate.pg_client)

    async def test_client_construction_failure_is_logged_and_denied(self) -> None:
        with patch.object(authz, "resolve_bot_pg_family_identity", AsyncMock(return_value=SimpleNamespace(staff=dict(OWNER_STAFF)))), \
                patch.object(authz, "principal_pg_authz_ready", MagicMock(return_value=True)), \
                patch.object(authz, "authorize_pg_page", MagicMock(return_value=SimpleNamespace(allowed=True))), \
                patch.object(authz, "bot_pg_client_for_resolution", AsyncMock(side_effect=RuntimeError("no panel"))), \
                self.assertLogs("app.services.bot_pg_page_authz", level="WARNING") as logs:
            gate = await authz.authorize_bot_pg_page(MagicMock(), db_user=MagicMock(), page="pg_inbounds")
        self.assertFalse(gate.allowed)
        self.assertIn("pg_inbounds", logs.output[0])

    async def test_unready_staff_is_denied(self) -> None:
        gate = await self.gate(SimpleNamespace(staff=dict(OWNER_STAFF)), ready=False)
        self.assertFalse(gate.allowed)


class HubButtonTests(unittest.TestCase):
    def test_admins_button_is_shown_only_when_the_actors_features_include_it(self) -> None:
        from app.bot.nav_inline import pg_hub_keyboard

        def found(**kw) -> list[str]:
            return [b.callback_data for row in pg_hub_keyboard({}, **kw).inline_keyboard for b in row]

        self.assertNotIn("adm:pg:admins", found())
        self.assertNotIn("adm:pg:admins", found(features=frozenset({"pg_users"})))
        self.assertIn("adm:pg:admins", found(features=frozenset({"pg_users", "pg_admins"})))


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def press(self, handler, data: str, gate: BotPgPageGate):
        callback = AsyncMock()
        callback.data = data
        callback.message = AsyncMock()
        with patch.object(view, "authorize_bot_pg_page", AsyncMock(return_value=gate)), \
                patch.object(view, "safe_edit_text", AsyncMock()) as edit:
            await handler(callback, db_user=MagicMock(), session=MagicMock())
        return callback, edit

    async def test_denied_gate_never_reaches_the_panel(self) -> None:
        pg = MagicMock()
        gate = BotPgPageGate(allowed=False, user_message="no")
        for handler, data in ((view.show_inbounds, "adm:pg:inbounds"), (view.show_admins, "adm:pg:admins")):
            callback, edit = await self.press(handler, data, gate)
            callback.answer.assert_awaited_once_with("no", show_alert=True)
            edit.assert_not_awaited()
        pg.get_admins.assert_not_called()

    async def test_inbound_tags_accept_names_and_objects_and_are_escaped(self) -> None:
        pg = MagicMock()
        pg.get_inbounds = AsyncMock(return_value=["vless-tcp", {"tag": "<x>"}, {"name": "ws"}, {}])
        _, edit = await self.press(view.show_inbounds, "adm:pg:inbounds", BotPgPageGate(allowed=True, pg_client=pg))
        text = edit.await_args.args[1]
        self.assertIn("vless-tcp", text)
        self.assertIn("&lt;x&gt;", text)
        self.assertNotIn("<x>", text)

    async def test_admin_list_escapes_names_and_panel_errors_are_hidden(self) -> None:
        pg = MagicMock()
        pg.get_admins = AsyncMock(return_value=[{"username": "<root>", "is_sudo": True, "role": {"name": "owner"}}])
        _, edit = await self.press(view.show_admins, "adm:pg:admins", BotPgPageGate(allowed=True, pg_client=pg))
        self.assertIn("&lt;root&gt;", edit.await_args.args[1])
        pg.get_admins = AsyncMock(side_effect=RuntimeError("password=hunter2"))
        callback, _ = await self.press(view.show_admins, "adm:pg:admins", BotPgPageGate(allowed=True, pg_client=pg))
        self.assertNotIn("hunter2", callback.answer.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
