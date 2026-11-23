"""The reseller package in the user edit modal: right under the role, shown only once «نماینده» is picked."""

from __future__ import annotations

import re
import unittest
from types import SimpleNamespace



def render(role: str) -> str:
    from app.api.app import templates

    user = SimpleNamespace(
        id=40, role=role, telegram_id=1, username="ali", first_name="علی", wallet_balance=0, points=0,
        is_blocked=False, created_at=None,
    )
    plans = [SimpleNamespace(id=3, name="پایه", billing_mode="fixed")]
    return templates.env.get_template("_user_edit_body.html").render(
        user=user, can_manage_users=True, reseller_plans=plans, csrf_token="t"
    )


class RolePlanFieldTests(unittest.TestCase):
    def test_the_package_sits_between_the_role_and_the_apply_button(self):
        html = render("user")
        role = html.find('name="role"')
        plan = html.find('name="reseller_plan_id"')
        apply = html.find(">اعمال نقش</button>")
        self.assertTrue(0 <= role < plan < apply)

    def test_the_package_is_hidden_until_the_role_is_reseller(self):
        html = render("user")
        role_id = re.search(r'<select name="role" id="([^"]+)"', html).group(1)
        field = re.search(r'<label class="form-field" data-ds-show-when="([^"]+)"( hidden)?>پلن نمایندگی', html)
        self.assertIsNotNone(field)
        self.assertEqual(field.group(1), f"#{role_id}:reseller")
        self.assertEqual(field.group(2), " hidden")

    def test_a_reseller_gets_no_package_field_here(self):
        self.assertNotIn('name="reseller_plan_id"', render("reseller"))


if __name__ == "__main__":
    unittest.main()
