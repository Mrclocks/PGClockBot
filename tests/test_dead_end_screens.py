"""Every bot screen offers a way back: no screen leaves the person without a button."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.handlers import admin_backup, admin_pg_users, loyalty, reply_nav, service_import, services, ticket_actions
from app.services.service_import import RATE_LIMITED, ServiceImportError


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


class PgUsersListTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_users_list_and_its_error_go_back_to_the_pasarguard_hub(self) -> None:
        target = MagicMock()
        users = [{"id": 1, "username": "a"}]
        for listed in (AsyncMock(return_value=(users, 1)), AsyncMock(side_effect=RuntimeError("down"))):
            with (
                self.subTest(error=listed.side_effect is not None),
                patch("app.services.bot_pg_user_authz.list_scoped_pg_users", listed),
                patch.object(admin_pg_users, "safe_edit_text", AsyncMock()) as shown,
            ):
                await admin_pg_users._render_users_list(target, gate=MagicMock())
                self.assertEqual(_callbacks(shown.await_args.kwargs["reply_markup"])[-1], "nv:adm:pg")


class ResultScreenTests(unittest.TestCase):
    def test_a_service_result_backs_up_to_its_card(self) -> None:
        self.assertEqual(_callbacks(services.service_back_markup(7)), ["svc:view:7"])

    def test_the_backup_panel_keeps_its_actions_files_and_a_way_back(self) -> None:
        data = _callbacks(admin_backup.backup_hub_markup([], {}))
        self.assertEqual(data[-1], "nv:adm:system")
        self.assertEqual(data.count("nv:adm:system"), 1)

    def test_club_screens_back_up_to_the_club(self) -> None:
        self.assertEqual(_callbacks(loyalty._ref_actions_keyboard())[-1], loyalty.CLUB_HOME)
        for can_spin in (True, False):
            self.assertEqual(_callbacks(loyalty._wheel_spin_keyboard(can_spin=can_spin))[-1], loyalty.CLUB_HOME)

    def test_a_denied_reply_action_still_has_a_button(self) -> None:
        message = MagicMock()
        self.assertEqual(_callbacks(reply_nav._SoftCallback(message, "x")._denial_markup), ["menu:home"])
        soft = reply_nav._SoftCallback(message, "x", back_callback="nv:adm:home")
        self.assertEqual(_callbacks(soft._denial_markup), ["nv:adm:home"])


class FlowEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_cancel_request_backs_up_to_the_service(self) -> None:
        message = MagicMock(text="دلیل")
        state = AsyncMock(get_data=AsyncMock(return_value={"cancellation_service_id": 7}))
        with (
            patch.object(services, "request_cancellation", AsyncMock(return_value={"id": 3, "status": "pending", "label": "در انتظار بررسی"})),
            patch.object(services, "get_all_settings", AsyncMock(return_value={})),
            patch("app.bot.nav_input.finish_text_step", AsyncMock()) as shown,
        ):
            await services.svc_cancel_reason(message, MagicMock(), SimpleNamespace(id=1), state)
        self.assertEqual(_callbacks(shown.await_args.kwargs["inline"]), ["svc:view:7"])

    async def test_a_rate_limited_import_backs_up_to_the_services(self) -> None:
        message = MagicMock(text="https://x/sub/abc")
        with (
            patch.object(service_import, "get_all_settings", AsyncMock(return_value={})),
            patch.object(service_import, "delete_user_message", AsyncMock()),
            patch.object(service_import, "import_enabled", return_value=True),
            patch.object(service_import, "import_existing_service", AsyncMock(side_effect=ServiceImportError(RATE_LIMITED))),
            patch.object(service_import, "finish_text_step", AsyncMock()) as shown,
        ):
            await service_import.svc_import_link(message, MagicMock(), SimpleNamespace(id=1), AsyncMock())
        self.assertEqual(_callbacks(shown.await_args.kwargs["inline"]), ["svc:list"])

    async def test_closing_a_ticket_drops_only_its_own_buttons(self) -> None:
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="r", callback_data="tkt:reply:5")],
            [InlineKeyboardButton(text="o", callback_data="tkt:reply:15")],
            [InlineKeyboardButton(text="b", callback_data="support:list")],
        ])
        message = MagicMock(reply_markup=markup, edit_reply_markup=AsyncMock())
        await ticket_actions._drop_ticket_buttons(MagicMock(message=message), 5)
        kept = message.edit_reply_markup.await_args.kwargs["reply_markup"]
        self.assertEqual(_callbacks(kept), ["tkt:reply:15", "support:list"])


if __name__ == "__main__":
    unittest.main()
