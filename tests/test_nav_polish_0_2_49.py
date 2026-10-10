"""0.2.49 nav polish: apply CTA, ticket actions, cancel reopen, queue shortcuts, Back."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch


class ResellerApplyOnMainKbTests(unittest.TestCase):
    def test_apply_appears_when_enabled_in_menu_order(self):
        from app.bot import keyboards as kb

        ui = {
            "menu_order": "shop,wallet,support,reseller_apply,miniapp",
            "menu_layout": "compact",
            "show_reseller_apply": "1",
            "btn_shop": "خرید",
            "btn_wallet": "کیف",
            "btn_support": "پشتیبانی",
            "btn_reseller_apply": "🤝 درخواست نمایندگی",
            "btn_miniapp": "مینی‌اپ",
        }
        markup = kb.main_reply_keyboard(
            "user", has_services=False, ui=ui, show_reseller_apply=True
        )
        labels = [btn.text for row in markup.keyboard for btn in row]
        self.assertIn("🤝 درخواست نمایندگی", labels)

    def test_apply_hidden_when_flag_off(self):
        from app.bot import keyboards as kb

        ui = {
            "menu_order": "shop,reseller_apply",
            "menu_layout": "compact",
            "show_reseller_apply": "0",
            "btn_shop": "خرید",
            "btn_reseller_apply": "🤝 درخواست نمایندگی",
        }
        markup = kb.main_reply_keyboard(
            "user", has_services=False, ui=ui, show_reseller_apply=False
        )
        labels = [btn.text for row in markup.keyboard for btn in row]
        self.assertNotIn("🤝 درخواست نمایندگی", labels)


class QueueShortcutTests(unittest.TestCase):
    def test_only_nonzero_shortcuts(self):
        from app.bot.nav_inline import admin_groups_hub_keyboard, queue_shortcut_row

        row = queue_shortcut_row({}, payments=0, tickets=2, cancellations=0)
        self.assertEqual(len(row), 1)
        self.assertEqual(row[0].callback_data, "adm:tickets")
        self.assertIn("تیکت", row[0].text)

        row2 = queue_shortcut_row({}, payments=1, tickets=2, cancellations=5)
        self.assertEqual([b.callback_data for b in row2], [
            "adm:payments",
            "adm:tickets",
            "adm:cancellations",
        ])

        kb = admin_groups_hub_keyboard(
            {},
            pending_payments=0,
            pending_tickets=3,
            pending_cancellations=0,
        )
        first = kb.inline_keyboard[0]
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].callback_data, "adm:tickets")

    def test_empty_queue_no_shortcut_row(self):
        from app.bot.nav_inline import admin_groups_hub_keyboard

        kb = admin_groups_hub_keyboard({})
        # First row should be hub groups, not shortcuts
        cbs = [b.callback_data for b in kb.inline_keyboard[0]]
        self.assertTrue(all(c.startswith("nv:adm:") for c in cbs))


class TicketActionsMarkupTests(unittest.TestCase):
    def test_ticket_view_has_reply_close_back(self):
        from app.bot.handlers.admin import _admin_ticket_actions_markup

        markup = _admin_ticket_actions_markup(7, {})
        flat = [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]
        texts = [t for t, _ in flat]
        cbs = [c for _, c in flat]
        self.assertTrue(any("پاسخ" in t for t in texts))
        self.assertTrue(any("بستن" in t for t in texts))
        self.assertIn("adm:ticket:reply:7", cbs)
        self.assertIn("adm:ticket:close:7", cbs)
        self.assertIn("adm:tickets", cbs)  # back to list


class UserCardBackTests(unittest.TestCase):
    def test_user_actions_has_back_to_list(self):
        from app.bot.keyboards import admin_user_actions

        markup = admin_user_actions(42, is_blocked=False, role="user")
        cbs = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("adm:users:list:0", cbs)

    def test_resellers_list_wraps_inline_back(self):
        from pathlib import Path

        src = Path("app/bot/handlers/admin.py").read_text(encoding="utf-8")
        chunk = src.split("async def adm_resellers_list")[1].split(
            "RESELLER_SVCS_PAGE_SIZE"
        )[0]
        self.assertIn("with_inline_back", chunk)
        self.assertIn("nv:adm:resellers", chunk)


class TicketCancelReopenTests(unittest.IsolatedAsyncioTestCase):
    async def test_adm_ticket_reply_cancel_reopens_list(self):
        from app.bot.handlers.admin import adm_ticket_reply
        from app.bot.handlers.admin import AdminStates

        message = AsyncMock()
        message.text = "انصراف"
        message.answer = AsyncMock()
        session = AsyncMock()
        db_user = MagicMock()
        state = AsyncMock()
        state.get_state = AsyncMock(return_value=AdminStates.ticket_reply.state)
        state.get_data = AsyncMock(return_value={"ticket_id": 1})

        with (
            patch("app.bot.keyboards.is_cancel_text", return_value=True),
            patch(
                "app.bot.handlers.admin._answer_tickets_nav",
                AsyncMock(),
            ) as nav,
        ):
            await adm_ticket_reply(message, state, session, db_user)

        nav.assert_awaited()
        self.assertEqual(nav.await_args.kwargs.get("clear_state"), True)


if __name__ == "__main__":
    unittest.main()
