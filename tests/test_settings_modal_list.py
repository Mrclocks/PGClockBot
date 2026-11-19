"""Settings modals open on a list of the sections the role may open, and a named section opens directly
(DESIGN_SYSTEM.md §3)."""

from __future__ import annotations

import re
import unittest


from starlette.requests import Request

from app.api.app import templates


def _request(query: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "query_string": query.encode(), "headers": []})


def _render(name: str, query: str = "", **ctx: object) -> str:
    return templates.env.get_template(name).render(request=_request(query), csrf_token="t", **ctx)


def rows(html: str) -> list[str]:
    return re.findall(r'data-modal-list-open="([a-z]+)"', html)


def shown(html: str) -> list[str]:
    """Section panels rendered visible."""
    return re.findall(r'data-modal-tab-panel="([a-z]+)" (?!hidden)', html)


def heading(html: str, heading_id: str) -> str:
    return re.search(rf'id="{heading_id}">([^<]*)<', html).group(1)


def list_is_hidden(html: str) -> bool:
    return bool(re.search(r"data-modal-list-rows hidden", html))


def back_is_shown(html: str) -> bool:
    return "data-modal-list-back aria-label" in html


class FinanceSettingsModalTests(unittest.TestCase):
    def finance(self, open_settings: str = "", **perms: bool) -> str:
        ctx = {"can_billing_settings": False, "can_pending_orders": False, **perms}
        return _render("_modal_finance_settings.html", open_finance_settings=open_settings, values={}, **ctx)

    def test_a_role_with_every_section_opens_on_the_list(self) -> None:
        html = self.finance(can_billing_settings=True, can_pending_orders=True)
        self.assertEqual(rows(html), ["payment", "billing", "cleanup"])
        self.assertEqual(shown(html), [])
        self.assertFalse(list_is_hidden(html))
        self.assertFalse(back_is_shown(html))
        self.assertEqual(heading(html, "finance-settings-title"), "تنظیمات مالی")

    def test_a_named_section_opens_directly_with_its_title_and_way_back(self) -> None:
        html = self.finance("cleanup", can_billing_settings=True, can_pending_orders=True)
        self.assertEqual(shown(html), ["cleanup"])
        self.assertTrue(list_is_hidden(html))
        self.assertTrue(back_is_shown(html))
        self.assertEqual(heading(html, "finance-settings-title"), "پاکسازی")

    def test_a_section_the_role_lacks_has_no_row_and_cannot_be_opened(self) -> None:
        html = self.finance("billing", can_pending_orders=True)
        self.assertEqual(rows(html), ["payment", "cleanup"])
        self.assertNotIn('data-modal-tab-panel="billing"', html)
        self.assertEqual(shown(html), [])

    def test_a_single_section_shows_without_a_list(self) -> None:
        html = self.finance()
        self.assertEqual(rows(html), [])
        self.assertEqual(shown(html), ["payment"])
        self.assertFalse(back_is_shown(html))
        self.assertEqual(heading(html, "finance-settings-title"), "تنظیمات مالی")


class LoyaltySettingsModalTests(unittest.TestCase):
    def loyalty(self, query: str, loy_tab: str, referral: bool = False) -> str:
        return _render("_modal_loyalty_settings.html", query, open_loyalty_settings=True,
                       loyalty_settings_tab=loy_tab, referral_tab_groups=["referral"] if referral else [],
                       reward_type_labels={}, wheel_prize_type_labels={}, event_labels={}, submenu_labels={},
                       referral_groups={"referral": []}, values={})

    def test_the_generic_opener_shows_the_list(self) -> None:
        html = self.loyalty("settings=1", "club")
        self.assertEqual(rows(html), ["club", "rules", "rewards", "wheel", "tiers"])
        self.assertEqual(shown(html), [])

    def test_a_saved_section_reopens_on_that_section(self) -> None:
        self.assertEqual(shown(self.loyalty("settings=rules", "rules")), ["rules"])
        self.assertEqual(shown(self.loyalty("settings=club&saved=1", "club")), ["club"])

    def test_referral_without_the_permission_stays_on_the_list_without_its_row(self) -> None:
        html = self.loyalty("settings=referral", "club")
        self.assertNotIn("referral", rows(html))
        self.assertEqual(shown(html), [])
        self.assertIn("referral", rows(self.loyalty("settings=1", "club", referral=True)))


class SupportSettingsModalTests(unittest.TestCase):
    def supports(self, query: str) -> str:
        return _render("_modal_supports_settings.html", query, open_supports_modal=True)

    def test_supports_opens_on_the_list_and_a_named_section_directly(self) -> None:
        self.assertEqual(shown(self.supports("supports=1")), [])
        self.assertEqual(shown(self.supports("supports=1&stab=text")), ["text"])
        self.assertEqual(shown(self.supports("supports=1&stab=contacts&saved=1")), ["contacts"])

    def test_an_unknown_section_name_falls_back_to_the_list(self) -> None:
        html = self.supports('supports=1&stab="><script>')
        self.assertEqual(shown(html), [])
        self.assertNotIn("<script>", html)


if __name__ == "__main__":
    unittest.main()
