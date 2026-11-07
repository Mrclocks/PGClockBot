"""Bot settings tabs are grouped without changing which tabs a role holds."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.services.resellers import RESELLER_SETTINGS_TABS
from app.services.settings_tab_groups import (
    FALLBACK_GROUP_KEY,
    group_settings_tabs,
)
from app.services.users import SETTINGS_TABS

TEMPLATES = Path(__file__).resolve().parents[1] / "app/web/templates"


def _flat_keys(groups) -> list[str]:
    return [tab.key for group in groups for tab in group.tabs]


class GroupSettingsTabsTests(unittest.TestCase):
    def test_owner_tabs_are_all_present_exactly_once(self):
        groups = group_settings_tabs(SETTINGS_TABS, "welcome")
        keys = _flat_keys(groups)
        self.assertEqual(sorted(keys), sorted(key for key, _ in SETTINGS_TABS))
        self.assertEqual(len(keys), len(set(keys)))

    def test_shop_owner_tabs_are_all_present_exactly_once(self):
        groups = group_settings_tabs(RESELLER_SETTINGS_TABS, "welcome")
        keys = _flat_keys(groups)
        self.assertEqual(sorted(keys), sorted(key for key, _ in RESELLER_SETTINGS_TABS))
        self.assertEqual(len(keys), len(set(keys)))

    def test_no_group_is_ever_empty_and_the_fallback_is_unused_today(self):
        for tabs in (SETTINGS_TABS, RESELLER_SETTINGS_TABS):
            groups = group_settings_tabs(tabs, "welcome")
            self.assertTrue(all(group.tabs for group in groups))
            self.assertNotIn(FALLBACK_GROUP_KEY, [group.key for group in groups])

    def test_a_role_never_gains_a_tab_it_was_not_given(self):
        subset = [("welcome", "خوش‌آمد"), ("limits", "محدودیت")]
        groups = group_settings_tabs(subset, "welcome")
        self.assertEqual(sorted(_flat_keys(groups)), ["limits", "welcome"])
        self.assertEqual(len(groups), 2)

    def test_unknown_tab_lands_in_the_fallback_group(self):
        tabs = [*SETTINGS_TABS, ("brand_new", "تب تازه")]
        groups = group_settings_tabs(tabs, "brand_new")
        self.assertEqual(groups[-1].key, FALLBACK_GROUP_KEY)
        self.assertEqual([tab.key for tab in groups[-1].tabs], ["brand_new"])
        self.assertTrue(groups[-1].is_active)

    def test_exactly_one_group_is_active_for_a_known_tab(self):
        groups = group_settings_tabs(SETTINGS_TABS, "daily_report")
        active = [group.key for group in groups if group.is_active]
        self.assertEqual(active, ["alerts"])

    def test_unknown_active_tab_activates_no_group(self):
        groups = group_settings_tabs(SETTINGS_TABS, "nope")
        self.assertFalse(any(group.is_active for group in groups))

    def test_labels_come_from_the_tab_list_the_role_received(self):
        groups = group_settings_tabs(RESELLER_SETTINGS_TABS, "bot")
        labels = {tab.key: tab.label for group in groups for tab in group.tabs}
        self.assertEqual(labels["bot"], "ربات اختصاصی")

    def test_empty_input_gives_no_groups(self):
        self.assertEqual(group_settings_tabs([], "welcome"), ())


def _render(template: str, macro: str, *args) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    source = (
        "{% from \"" + template + "\" import " + macro + " %}"
        "{{ " + macro + "(" + ", ".join(f"a{i}" for i in range(len(args))) + ") }}"
    )
    return env.from_string(source).render(**{f"a{i}": value for i, value in enumerate(args)})


class SettingsTabsTemplateTests(unittest.TestCase):
    def test_owner_page_renders_groups_and_only_the_active_groups_tabs(self):
        groups = group_settings_tabs(SETTINGS_TABS, "colors")
        html = _render("_settings_tabs.html", "settings_tabs", "colors", SETTINGS_TABS, groups)
        self.assertEqual(html.count("settings-tabs-groups"), 1)
        self.assertEqual(html.count('class="active"'), 2)
        self.assertIn('href="/settings?tab=messages" class="active"', html)
        self.assertIn('href="/settings?tab=colors" class="active"', html)
        self.assertNotIn('href="/settings?tab=naming" class="active"', html)

    def test_shop_page_links_stay_on_the_shop_settings_base(self):
        groups = group_settings_tabs(RESELLER_SETTINGS_TABS, "welcome")
        html = _render(
            "_shop_settings_tabs.html",
            "shop_settings_tabs",
            "welcome",
            RESELLER_SETTINGS_TABS,
            "/shop-settings",
            "/shop-settings",
            groups,
        )
        self.assertIn('href="/shop-settings?tab=messages"', html)
        self.assertNotIn('href="/settings?tab=', html)

    def test_flat_strip_still_renders_without_groups(self):
        html = _render("_settings_tabs.html", "settings_tabs", "menu", SETTINGS_TABS)
        self.assertNotIn("settings-tabs-groups", html)
        self.assertEqual(html.count("<a "), len(SETTINGS_TABS))

    def test_hostile_labels_are_escaped(self):
        tabs = [("welcome", "<script>alert(1)</script>"), ("menu", "x")]
        groups = group_settings_tabs(tabs, "welcome")
        html = _render("_settings_tabs.html", "settings_tabs", "welcome", tabs, groups)
        self.assertNotIn("<script>", html)


if __name__ == "__main__":
    unittest.main()


class RegroupedLayoutTests(unittest.TestCase):
    def test_owner_tabs_are_grouped_by_what_they_change(self):
        groups = group_settings_tabs(SETTINGS_TABS, "welcome")
        layout = {g.label: [t.key for t in g.tabs] for g in groups}
        self.assertEqual(list(layout), ["ظاهر ربات", "متن‌ها", "فروش و سرویس", "هشدار و گزارش"])
        self.assertEqual(layout["متن‌ها"], ["messages", "buttons", "colors", "screens"])
        self.assertIn("links", layout["ظاهر ربات"])
        self.assertNotIn("bot", [k for keys in layout.values() for k in keys])

    def test_a_reseller_keeps_their_own_bot_in_its_own_group(self):
        groups = group_settings_tabs(RESELLER_SETTINGS_TABS, "bot")
        own = [g for g in groups if [t.key for t in g.tabs] == ["bot"]]
        self.assertEqual([(g.label, g.is_active) for g in own], [("ربات اختصاصی", True)])

    def test_no_label_is_shared_with_the_sidebar_action_centre(self):
        labels = {g.label for g in group_settings_tabs(SETTINGS_TABS, "welcome")}
        self.assertNotIn("اعلان‌ها", labels)
