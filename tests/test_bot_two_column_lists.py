"""Long lists of fixed bot-settings entries use two columns; short or value-dependent lists do not."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from aiogram.types import InlineKeyboardButton

from app.bot import keyboards as kb
from app.bot.handlers import admin_plans, admin_settings, reseller_settings


def _buttons(*labels: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=label, callback_data=f"cb:{i}") for i, label in enumerate(labels)]


def _shape(rows) -> list[int]:
    return [len(row) for row in rows]


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _callback():
    message = AsyncMock()
    return SimpleNamespace(message=message, answer=AsyncMock(), data="x")


def _markup(callback) -> object:
    return callback.message.edit_text.await_args.kwargs["reply_markup"]


class PackLongListTests(unittest.TestCase):
    def test_up_to_six_entries_stay_one_per_row(self):
        for count in range(0, 7):
            rows = kb.pack_long_list(_buttons(*["کوتاه"] * count))
            self.assertEqual(_shape(rows), [1] * count)

    def test_seven_short_entries_go_two_column_and_the_odd_one_stays_alone(self):
        rows = kb.pack_long_list(_buttons(*["کوتاه"] * 7))
        self.assertEqual(_shape(rows), [2, 2, 2, 1])

    def test_a_long_label_takes_a_full_row_and_order_is_kept(self):
        long_label = "یک برچسب خیلی خیلی طولانی که بریده می‌شود"
        buttons = _buttons("الف", "ب", long_label, "ج", "د", "ه", "و", "ز")
        rows = kb.pack_long_list(buttons)
        self.assertEqual(_shape(rows), [2, 1, 2, 2, 1])
        self.assertEqual([b.callback_data for row in rows for b in row], [b.callback_data for b in buttons])
        self.assertEqual(rows[1][0].text, long_label)

    def test_the_label_limit_is_the_shared_short_label_constant(self):
        at_limit = "ا" * kb.SHORT_LABEL_MAX
        over_limit = "ا" * (kb.SHORT_LABEL_MAX + 1)
        rows = kb.pack_long_list(_buttons(*[at_limit] * 7))
        self.assertEqual(_shape(rows), [2, 2, 2, 1])
        rows = kb.pack_long_list(_buttons(*[over_limit] * 7))
        self.assertEqual(_shape(rows), [1] * 7)

    def test_no_button_is_lost_or_repeated(self):
        buttons = _buttons(*[("ا" * n) for n in (3, 30, 5, 5, 25, 4, 4, 4, 22, 2)])
        rows = kb.pack_long_list(buttons)
        self.assertEqual(sorted(b.callback_data for row in rows for b in row), sorted(b.callback_data for b in buttons))
        self.assertTrue(all(len(row) <= 2 for row in rows))


class AdminSettingsListsTests(unittest.IsolatedAsyncioTestCase):
    async def _section(self, sec_id: str):
        callback = _callback()
        await admin_settings._render_section(callback, None, sec_id)
        return _markup(callback)

    async def test_the_admin_button_groups_are_two_column_with_a_full_width_back_row(self):
        from app.bot.handlers.settings_labels import labels_audience_panel
        from app.services.settings_button_labels import label_subs_for

        subs = admin_settings.SECTIONS["labels"]["subs"]
        _text, markup = labels_audience_panel("adm", "admin", subs)
        entries = [r for r in markup.inline_keyboard[:-1]]
        self.assertGreater(len(entries), 0)
        self.assertTrue(any(len(row) == 2 for row in entries))
        self.assertTrue(all(len(row) <= 2 for row in markup.inline_keyboard))
        self.assertEqual(markup.inline_keyboard[-1][0].callback_data, "adm:st:sec:labels")
        expected = [f"adm:st:sub:labels:{sub[0]}" for sub in label_subs_for("admin", subs)]
        self.assertEqual(sorted(c for c in _callbacks(markup) if c.startswith("adm:st:sub:")), sorted(expected))

    async def test_a_section_with_six_entries_or_fewer_keeps_one_per_row(self):
        for sec_id, sec in admin_settings.SECTIONS.items():
            subs = sec.get("subs") or []
            if not subs or len(subs) >= kb.LONG_LIST_MIN_ITEMS or sec.get("kind"):
                continue
            markup = await self._section(sec_id)
            self.assertTrue(all(len(row) == 1 for row in markup.inline_keyboard), sec_id)

    async def test_the_field_list_of_a_long_subsection_is_two_column_and_loses_nothing(self):
        fields = next(sub[2] for sub in admin_settings.SECTIONS["labels"]["subs"] if sub[0] == "btn_main")
        self.assertGreaterEqual(len(fields), kb.LONG_LIST_MIN_ITEMS)
        callback = _callback()
        with patch.object(admin_settings, "get_all_settings", AsyncMock(return_value={})):
            await admin_settings._render_fields(
                callback, None, title="t", fields=fields, back_cb="adm:st:sec:shop"
            )
        markup = _markup(callback)
        self.assertTrue(any(len(row) == 2 for row in markup.inline_keyboard))
        got = [c for c in _callbacks(markup) if c != "adm:st:sec:shop"]
        self.assertEqual(sorted(got), sorted(f"adm:st:{'tog' if kind == 'toggle' else 'edit'}:{key}" for key, _, kind in fields))
        self.assertEqual(markup.inline_keyboard[-1][0].callback_data, "adm:st:sec:shop")

    async def test_notification_toggles_are_two_column_and_each_keeps_its_state_mark(self):
        callback = _callback()
        first_key = admin_settings.NOTIFY_PREFS[0][0]
        with patch.object(admin_settings, "get_all_settings", AsyncMock(return_value={first_key: "1"})):
            await admin_settings._render_notify(callback, None)
        markup = _markup(callback)
        self.assertTrue(any(len(row) == 2 for row in markup.inline_keyboard))
        marks = {b.callback_data: b.text for row in markup.inline_keyboard for b in row}
        self.assertTrue(marks[f"adm:st:tog:{first_key}"].startswith("✅"))
        # Every switch, the «گزارش روزانه» screen and «بازگشت».
        self.assertEqual(len(marks), len(admin_settings.PLATFORM_NOTIFY_PREFS) + 2)


class ResellerSettingsListsTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_customer_button_groups_are_two_column_for_a_shop_owner_too(self):
        from app.bot.handlers.settings_labels import labels_audience_panel
        from app.services.settings_button_labels import label_subs_for

        subs = reseller_settings.SECTIONS["labels"]["subs"]
        self.assertGreaterEqual(len(label_subs_for("user", subs)), kb.LONG_LIST_MIN_ITEMS)
        _text, markup = labels_audience_panel("res", "user", subs)
        self.assertTrue(any(len(row) == 2 for row in markup.inline_keyboard))
        self.assertEqual(len(markup.inline_keyboard[-1]), 1)
        self.assertEqual(markup.inline_keyboard[-1][0].callback_data, "res:st:sec:labels")

    async def test_editable_values_stay_one_per_row_because_their_width_follows_the_value(self):
        callback = _callback()
        with (
            patch.object(reseller_settings, "get_setting", AsyncMock(return_value="x")),
            patch.object(reseller_settings, "safe_edit_text", AsyncMock()) as edit,
        ):
            await reseller_settings._render_sub(callback, None, "shop", "btn_main", 5)
        markup = edit.await_args.kwargs["reply_markup"]
        self.assertTrue(all(len(row) == 1 for row in markup.inline_keyboard))

    async def test_shop_notification_toggles_use_two_columns_when_there_are_many(self):
        catalog = [(f"k{i}", f"اعلان {i}", "", True) for i in range(8)]
        callback = _callback()
        with (
            patch.object(reseller_settings, "get_reseller_profile", AsyncMock(return_value=None)),
            patch("app.services.authz.resolve_shop_permissions_from_profile", return_value=[]),
            patch("app.services.notifications.shop_notify_catalog", return_value=catalog),
            patch("app.services.notifications.get_shop_notify_prefs", AsyncMock(return_value={"k0": True})),
            patch.object(reseller_settings, "safe_edit_text", AsyncMock()) as edit,
        ):
            await reseller_settings._render_notify(callback, None, 5)
        markup = edit.await_args.kwargs["reply_markup"]
        self.assertEqual(_shape(markup.inline_keyboard), [2, 2, 2, 2, 1])
        self.assertTrue(markup.inline_keyboard[0][0].text.startswith("✅"))

    async def test_an_empty_notification_catalog_still_shows_the_explanation_row(self):
        callback = _callback()
        with (
            patch.object(reseller_settings, "get_reseller_profile", AsyncMock(return_value=None)),
            patch("app.services.authz.resolve_shop_permissions_from_profile", return_value=[]),
            patch("app.services.notifications.shop_notify_catalog", return_value=[]),
            patch("app.services.notifications.get_shop_notify_prefs", AsyncMock(return_value={})),
            patch.object(reseller_settings, "safe_edit_text", AsyncMock()) as edit,
        ):
            await reseller_settings._render_notify(callback, None, 5)
        markup = edit.await_args.kwargs["reply_markup"]
        self.assertEqual(_shape(markup.inline_keyboard), [1, 1])


class ResellerPlanPermissionRowsTests(unittest.TestCase):
    def test_the_permission_toggles_are_two_column_and_keep_every_permission_once(self):
        rows = admin_plans._perm_rows(7, {admin_plans.FEATURE_PERMS[0][0]})
        keys = [b.callback_data for row in rows[:-1] for b in row]
        self.assertEqual(sorted(keys), sorted(f"adm:resplan:togperm:7:{key}" for key, _ in admin_plans.FEATURE_PERMS))
        self.assertTrue(any(len(row) == 2 for row in rows[:-1]))
        self.assertEqual(rows[-1][0].callback_data, "adm:resplan:view:7")
        self.assertEqual(len(rows[-1]), 1)

    def test_the_active_state_is_shown_per_permission(self):
        active = {admin_plans.FEATURE_PERMS[1][0]}
        rows = admin_plans._perm_rows(7, active)
        marks = {b.callback_data.rsplit(":", 1)[-1]: b.text[0] for row in rows[:-1] for b in row}
        self.assertEqual(marks[admin_plans.FEATURE_PERMS[1][0]], "✅")
        self.assertEqual(marks[admin_plans.FEATURE_PERMS[0][0]], "⬜")


if __name__ == "__main__":
    unittest.main()
