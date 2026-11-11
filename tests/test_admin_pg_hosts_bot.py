"""PasarGuard hosts in the bot: the shared object gate decides, the handlers never mutate when it denies."""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.bot.handlers import admin_pg_hosts as hosts
from app.services.bot_pg_object_authz import BotPgObjectGate


def callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def allowed(pg_client=None, pg_object=None) -> BotPgObjectGate:
    return BotPgObjectGate(allowed=True, reason="ok", user_message="", pg_client=pg_client, pg_object=pg_object, kind="hosts")


DENIED = BotPgObjectGate(allowed=False, reason="resource_out_of_scope", user_message="اجازه این عمل را ندارید", kind="hosts")
HOST = {"id": 7, "remark": "<edge>", "address": ["a.example", "b.example"], "port": 443, "inbound_tag": "vless-tcp", "is_disabled": False}


class AdminPgHostsBotTests(unittest.IsolatedAsyncioTestCase):
    async def press(self, data: str, gate: BotPgObjectGate, handler, **extra):
        callback = AsyncMock()
        callback.data = data
        callback.message = AsyncMock()
        with patch.object(hosts, "authorize_bot_pg_object_op", AsyncMock(return_value=gate)), \
                patch.object(hosts, "list_scoped_pg_objects", AsyncMock(return_value=extra.get("listed", []))), \
                patch.object(hosts, "safe_edit_text", AsyncMock()) as edit:
            await handler(callback, db_user=MagicMock(), session=MagicMock())
        return callback, edit

    async def test_denied_gate_blocks_every_step_without_touching_the_panel(self) -> None:
        pg = MagicMock()
        for data, handler in (
            ("adm:pg:hosts", hosts.show_hosts), ("adm:pg:h:7", hosts.show_host), ("adm:pg:htog:7", hosts.toggle_host),
            ("adm:pg:hdelask:7", hosts.ask_delete_host), ("adm:pg:hdel:7", hosts.delete_host),
        ):
            callback, edit = await self.press(data, DENIED, handler)
            callback.answer.assert_awaited_once_with(DENIED.user_message, show_alert=True)
            edit.assert_not_awaited()
        pg.set_host_disabled.assert_not_called()

    async def test_list_shows_only_scoped_hosts(self) -> None:
        _, edit = await self.press("adm:pg:hosts", allowed(), hosts.show_hosts, listed=[HOST])
        self.assertIn("adm:pg:h:7", callbacks(edit.await_args.kwargs["reply_markup"]))

    async def test_card_escapes_panel_text(self) -> None:
        _, edit = await self.press("adm:pg:h:7", allowed(pg_object=HOST), hosts.show_host)
        text = edit.await_args.args[1]
        self.assertIn("&lt;edge&gt;", text)
        self.assertNotIn("<edge>", text)
        self.assertIn("adm:pg:htog:7", callbacks(edit.await_args.kwargs["reply_markup"]))

    async def test_toggle_flips_the_current_state_through_the_scoped_client(self) -> None:
        pg = MagicMock()
        pg.set_host_disabled = AsyncMock()
        pg.get_host = AsyncMock(return_value={**HOST, "is_disabled": True})
        _, edit = await self.press("adm:pg:htog:7", allowed(pg, HOST), hosts.toggle_host)
        pg.set_host_disabled.assert_awaited_once_with(7, True)
        self.assertIn("غیرفعال", edit.await_args.args[1])

    async def test_delete_asks_first_and_panel_error_is_not_echoed(self) -> None:
        pg = MagicMock()
        pg.delete_host = AsyncMock(side_effect=RuntimeError("secret internal detail token=abc"))
        _, edit = await self.press("adm:pg:hdelask:7", allowed(pg, HOST), hosts.ask_delete_host)
        pg.delete_host.assert_not_awaited()
        self.assertIn("adm:pg:hdel:7", callbacks(edit.await_args.kwargs["reply_markup"]))
        callback, _ = await self.press("adm:pg:hdel:7", allowed(pg, HOST), hosts.delete_host)
        shown = callback.answer.await_args.args[0]
        self.assertNotIn("abc", shown)


if __name__ == "__main__":
    unittest.main()
