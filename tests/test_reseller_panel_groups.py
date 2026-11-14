"""Shop panel in four groups: entries keep their buttons, every screen backs up to its group."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.bot.handlers import reseller, reseller_hub, reseller_reps
from app.bot.nav_inline import reseller_group_hub_keyboard, reseller_manage_hub_keyboard
from app.bot.reply_keyboards import _reseller_submenu_entries
from app.bot.reseller_hub_groups import RESELLER_GROUPS, back_callback_for
from app.services import bot_feedback_texts as feedback
from app.services.admin_counters import PendingCounts
from tests.test_bot_ui_keyboard_snapshots import sample_reseller_profile

ENTRIES = [
    ("res_dash", "🏠 خانه نماینده"),
    ("res_users", "👥 مشتریان من"),
    ("res_plans", "💎 پلن‌های فروش"),
    ("res_payments", "🧾 رسیدها"),
]


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


class PanelLayoutTests(unittest.TestCase):
    def test_the_home_lists_only_groups_that_have_an_entry(self) -> None:
        with patch("app.bot.reply_keyboards._reseller_submenu_entries", return_value=ENTRIES):
            markup = reseller_manage_hub_keyboard(MagicMock(), {})
        self.assertEqual(_callbacks(markup), ["nv:res:grp:daily", "nv:res:grp:customers", "nv:res:grp:shop", "menu:home"])

    def test_a_group_keeps_the_entries_buttons_and_backs_up_to_the_home(self) -> None:
        with patch("app.bot.reply_keyboards._reseller_submenu_entries", return_value=ENTRIES):
            markup = reseller_group_hub_keyboard("daily", MagicMock(), {})
        self.assertEqual(_callbacks(markup), ["nv:res:ra:res_dash", "nv:res:ra:res_payments", "nv:res:home"])

    def test_a_forged_group_shows_nothing_but_back(self) -> None:
        with patch("app.bot.reply_keyboards._reseller_submenu_entries", return_value=ENTRIES):
            self.assertEqual(_callbacks(reseller_group_hub_keyboard("admin", MagicMock(), {})), ["nv:res:home"])

    def test_every_panel_entry_has_a_group(self) -> None:
        grouped = {action for group in RESELLER_GROUPS for action in group.actions}
        for mode in ("fixed", "payg"):
            with self.subTest(mode=mode), patch(
                "app.bot.reply_keyboards._reseller_capacity_entries",
                return_value=[("res_addon_packs", "📦"), ("res_renew", "🔄")],
            ):
                entries = _reseller_submenu_entries(
                    sample_reseller_profile(billing_mode=mode), can_add_representative=True, ui={}
                )
                self.assertEqual({key for key, _text in entries} - grouped, set())


class ScreenBackTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_shop_summary_has_a_way_back_to_its_group(self) -> None:
        callback = MagicMock(answer=AsyncMock(), message=MagicMock())
        summary = {"users": 1, "orders": 2, "pending": 0, "tickets": 0}
        with (
            patch.object(reseller, "_actor", AsyncMock(return_value=(5, MagicMock()))),
            patch.object(reseller, "has_bot_perm", return_value=True),
            patch.object(reseller, "reseller_shop_summary", AsyncMock(return_value=summary)),
            patch.object(reseller, "safe_edit_text", AsyncMock()) as shown,
        ):
            await reseller.res_dash(callback, MagicMock(), MagicMock(), is_reseller_bot=True, reseller_owner_id=5)
        self.assertEqual(_callbacks(shown.await_args.kwargs["reply_markup"]), ["nv:res:grp:daily"])

    def test_screens_outside_the_groups_return_to_the_home(self) -> None:
        self.assertEqual(back_callback_for("res_cancellations"), "nv:res:grp:daily")
        self.assertEqual(back_callback_for("res_creds"), "nv:res:home")


class GroupHandlerTests(unittest.IsolatedAsyncioTestCase):
    def _callback(self, data: str) -> MagicMock:
        callback = MagicMock(data=data, answer=AsyncMock())
        callback.message = MagicMock(answer=AsyncMock())
        return callback

    async def test_a_group_opens_in_the_panel_message(self) -> None:
        hub = reseller_hub.ResellerHub(
            profile=MagicMock(),
            ui={},
            can_add_representative=False,
            show_owner_creds=False,
            queue=PendingCounts(),
        )
        callback = self._callback("nv:res:grp:shop")
        with (
            patch.object(reseller_hub, "load_reseller_hub", AsyncMock(return_value=hub)),
            patch("app.bot.reply_keyboards._reseller_submenu_entries", return_value=ENTRIES),
            patch.object(reseller_hub.nav, "set_nav_level", AsyncMock()),
            patch.object(reseller_hub, "present_nav_panel", AsyncMock()) as present,
        ):
            await reseller_hub.nv_res_group(callback, MagicMock(), MagicMock(), AsyncMock(), is_reseller_bot=True, reseller_owner_id=5)
        self.assertIs(present.await_args.args[0], callback.message)
        self.assertEqual(_callbacks(present.await_args.kwargs["inline"]), ["nv:res:ra:res_plans", "nv:res:home"])

    async def test_without_shop_access_no_group_opens(self) -> None:
        callback = self._callback("nv:res:grp:daily")
        with (
            patch.object(reseller_hub, "load_reseller_actor", AsyncMock(return_value=(None, None))),
            patch.object(reseller_hub, "present_nav_panel", AsyncMock()) as present,
        ):
            await reseller_hub.nv_res_group(callback, MagicMock(), MagicMock(), AsyncMock(), is_reseller_bot=True, reseller_owner_id=5)
        present.assert_not_awaited()
        callback.message.answer.assert_awaited_once_with(feedback.RESELLER_ACCESS_NOT_FOUND)

    async def test_the_platform_bot_shows_its_own_shop_owner_screen(self) -> None:
        callback = self._callback("nv:res:grp:account")
        with patch("app.bot.handlers.reply_nav.open_reseller_home", AsyncMock()) as home:
            await reseller_hub.nv_res_group(callback, MagicMock(), MagicMock(), AsyncMock(), is_reseller_bot=False)
        self.assertFalse(home.await_args.kwargs["is_reseller_bot"])


class AddRepresentativeCancelTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_returns_to_the_customers_group(self) -> None:
        callback = MagicMock(answer=AsyncMock(), from_user=SimpleNamespace(id=9))
        callback.message = MagicMock(chat=SimpleNamespace(id=9), answer=AsyncMock())
        with patch.object(reseller_reps, "replay_screen", AsyncMock(return_value=True)) as replay:
            await reseller_reps.rrep_cancel(callback, AsyncMock())
        self.assertEqual(replay.await_args.kwargs["data"], "nv:res:grp:customers")
        callback.message.answer.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
