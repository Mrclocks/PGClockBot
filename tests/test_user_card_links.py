"""A customer's name in any panel table opens the user card, for staff who may open it, with one hint above the table."""

from __future__ import annotations

import unittest
from typing import Any


OWNER = {"role": "admin", "org_principal_id": 1, "org_depth": 0, "org_status": "active"}
SHOP_USERS = {"role": "reseller", "bot_user_id": 10, "permissions": ["dashboard"]}
SHOP_FINANCE_ONLY = {"role": "reseller", "bot_user_id": 10, "permissions": ["finance"]}
HINT = "با کلیک روی نام هر کاربر اطلاعات نمایش داده می‌شود"


def macros(staff: dict[str, Any] | None) -> Any:
    from app.api.app import templates

    context: dict[str, Any] = {"csrf_token": "t"}
    if staff is not None:
        context["staff"] = staff
    return templates.env.get_template("_user_card_modals.html").make_module(context)


class UserCardLinkTests(unittest.TestCase):
    def test_a_name_opens_the_card_from_its_page(self) -> None:
        html = str(macros(OWNER).user_card_link(42, "Aboli", "finance"))
        self.assertIn('class="cell-user-link"', html)
        self.assertIn('data-modal-open="modal-user-card"', html)
        self.assertIn('data-modal-load="/users/42/card?from=finance"', html)
        self.assertIn('data-modal-load-target="user-card-body"', html)

    def test_shop_staff_with_the_users_right_get_the_link(self) -> None:
        self.assertIn("<button", str(macros(SHOP_USERS).user_card_link(42, "Aboli", "tickets")))

    def test_staff_without_the_users_right_see_plain_text(self) -> None:
        html = str(macros(SHOP_FINANCE_ONLY).user_card_link(42, "Aboli", "finance"))
        self.assertEqual(html, '<span dir="auto">Aboli</span>')

    def test_a_row_without_a_customer_stays_text(self) -> None:
        self.assertNotIn("<button", str(macros(OWNER).user_card_link(None, "—", "finance")))

    def test_a_list_title_keeps_the_bold_name_style(self) -> None:
        html = str(macros(OWNER).user_card_link(42, "Aboli", "users", title=True))
        self.assertIn('class="cell-name-text cell-name-link"', html)
        self.assertIn('<span class="cell-name-text" dir="auto">Aboli</span>', str(
            macros(SHOP_FINANCE_ONLY).user_card_link(42, "Aboli", "users", title=True)))

    def test_a_hostile_name_stays_text(self) -> None:
        html = str(macros(OWNER).user_card_link(42, '<img src=x onerror="alert(1)">', "finance"))
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img src=x", html)

    def test_the_user_id_is_rendered_as_a_number(self) -> None:
        html = str(macros(OWNER).user_card_link("7/../../x", "Aboli", "finance"))
        self.assertNotIn("../", html)


class UserCardRowContextTests(unittest.TestCase):
    def test_the_row_label_travels_with_the_link(self) -> None:
        html = str(macros(OWNER).user_card_link(42, "Aboli", "finance", context="پرداخت ۱۲"))
        self.assertIn('data-card-context="پرداخت ۱۲"', html)

    def test_no_label_attribute_without_a_context(self) -> None:
        self.assertNotIn("data-card-context", str(macros(OWNER).user_card_link(42, "Aboli", "finance")))

    def test_a_hostile_label_is_escaped(self) -> None:
        html = str(macros(OWNER).user_card_link(42, "Aboli", "cleanup", context='سرویس x"><script>'))
        self.assertNotIn("<script>", html)
        self.assertIn('data-card-context="سرویس x&#34;&gt;&lt;script&gt;"', html)

    def test_the_modals_load_the_row_actions_script(self) -> None:
        self.assertIn('src="/static/user-card-actions.js', str(macros(OWNER).user_card_modals(return_page="finance")))


class UserCardButtonTests(unittest.TestCase):
    def test_a_row_button_opens_the_card_instead_of_the_users_list(self) -> None:
        html = str(macros(OWNER).user_card_button(42, "مشاهده کاربر", "cleanup"))
        self.assertIn('data-modal-load="/users/42/card?from=cleanup"', html)
        self.assertNotIn("/users?uid=", html)

    def test_no_button_without_the_users_right(self) -> None:
        self.assertEqual(str(macros(SHOP_FINANCE_ONLY).user_card_button(42, "مشاهده کاربر", "cleanup")).strip(), "")


class UserCardHintTests(unittest.TestCase):
    def test_the_hint_has_the_alert_icon_and_the_sentence(self) -> None:
        html = str(macros(OWNER).user_card_hint())
        self.assertIn('class="user-card-hint"', html)
        self.assertIn('class="user-card-hint-ico"', html)
        self.assertIn(HINT, html)

    def test_no_hint_and_no_modal_without_the_users_right(self) -> None:
        module = macros(SHOP_FINANCE_ONLY)
        self.assertEqual(str(module.user_card_hint()).strip(), "")
        self.assertEqual(str(module.user_card_modals(return_page="finance")).strip(), "")

    def test_the_modals_carry_the_page_and_load_the_message_script(self) -> None:
        html = str(macros(OWNER).user_card_modals(return_page="finance"))
        self.assertIn('id="modal-user-card"', html)
        self.assertIn('<input type="hidden" name="return_page" value="finance" />', html)
        self.assertIn('src="/static/user-message.js', html)


class ApplicationsPageTests(unittest.TestCase):
    def test_an_applicant_name_opens_the_card_with_its_request_label(self) -> None:
        from types import SimpleNamespace

        from app.api.app import templates

        applicant = SimpleNamespace(id=7, full_name="Aboli", username="ab", telegram_id=55)
        application = SimpleNamespace(
            id=5, user=applicant, plan=SimpleNamespace(name="پایه"), amount=1000, status="awaiting_approval",
            payment_method="card",
        )
        request = SimpleNamespace(url=SimpleNamespace(path="/resellers/applications"), state=SimpleNamespace(),
                                  query_params={})
        html = templates.env.get_template("reseller_applications.html").render(
            request=request, staff=OWNER, csrf_token="t", tabs=[], tab="pending", apps=[application],
        )
        self.assertIn('data-modal-load="/users/7/card?from=applications"', html)
        self.assertIn('data-card-context="درخواست ۵"', html)
        self.assertIn('id="modal-user-card"', html)
        self.assertIn(HINT, html)


class UserCardPageTests(unittest.TestCase):
    def test_every_card_page_returns_to_a_panel_path(self) -> None:
        from app.services.user_card import USER_CARD_PAGES, user_card_page

        for key, path in USER_CARD_PAGES.items():
            self.assertTrue(path.startswith("/") and not path.startswith("//"), key)
            self.assertEqual(user_card_page(key), key)
        self.assertEqual(user_card_page("//evil.example"), "users")
        self.assertEqual(user_card_page(None), "users")


if __name__ == "__main__":
    unittest.main()
