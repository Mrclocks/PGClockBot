"""Quick renew on the users list names the service it renews instead of guessing the most urgent one."""

from __future__ import annotations

import re
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock


def _service(sid: int, plan_name: str, *, has_alert: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        id=sid, label=f"svc{sid}", plan_name=plan_name, pg_username=f"svc{sid}", volume_text="10 GB",
        expire_text="5 روز", expire_tone="ok", days_left=5, has_alert=has_alert,
    )


def _row(services: list[SimpleNamespace]) -> SimpleNamespace:
    user = SimpleNamespace(
        id=7, telegram_id=107, full_name="مشتری", username="cust", is_blocked=False, is_demo=False, role="user",
    )
    return SimpleNamespace(
        user=user, services=services, service_count=len(services), has_alert=False,
        volume_text="10 GB", expire_text="5 روز", expire_tone="ok", days_left=5, wallet_balance=0,
    )


def _render(rows: list[SimpleNamespace], *, can_pg_update: bool = True) -> str:
    from app.api.app import templates

    request = MagicMock()
    request.url.path = "/users"
    request.query_params = {}
    ctx = {
        "request": request,
        "staff": {"role": "admin", "permissions": ["users"]},
        "q": "",
        "flt": "all",
        "rows": rows,
        "can_pg_update": can_pg_update,
        "can_user_ops": True,
        "filter_href": lambda **_kw: "/users",
    }
    return templates.env.get_template("users.html").render(**ctx)


def _renew_forms(html: str) -> list[str]:
    return re.findall(r'<form method="post" action="/users/7/quick-renew".*?</form>', html, re.S)


def _service_id(form: str) -> str:
    match = re.search(r'name="service_id" value="(\d+)"', form)
    assert match, "renew form sends no service_id"
    return match.group(1)


class UsersServiceRenewTests(unittest.TestCase):
    def test_each_service_of_a_multi_service_user_has_its_own_renew(self) -> None:
        html = _render([_row([_service(11, "ماهانه"), _service(12, "سه‌ماهه")])])
        forms = _renew_forms(html)
        # One per service in the phone list, plus the row's own form that follows the service picker.
        self.assertEqual(sorted(_service_id(f) for f in forms), ["11", "11", "12"])
        per_service = [f for f in forms if "data-users-row-renew" not in f]
        self.assertEqual(sorted(_service_id(f) for f in per_service), ["11", "12"])
        self.assertTrue(any("«سه‌ماهه»" in f and _service_id(f) == "12" for f in per_service))

    def test_the_row_renew_starts_on_the_service_the_picker_shows(self) -> None:
        html = _render([_row([_service(11, "ماهانه"), _service(12, "سه‌ماهه", has_alert=True)])])
        row_form = next(f for f in _renew_forms(html) if "data-users-row-renew" in f)
        self.assertEqual(_service_id(row_form), "12")
        self.assertIn('class="li-hide"', row_form)

    def test_a_single_service_user_renews_that_service(self) -> None:
        forms = _renew_forms(_render([_row([_service(11, "ماهانه")])]))
        self.assertEqual([_service_id(f) for f in forms], ["11"])

    def test_no_renew_without_the_panel_update_permission(self) -> None:
        html = _render([_row([_service(11, "ماهانه"), _service(12, "سه‌ماهه")])], can_pg_update=False)
        self.assertEqual(_renew_forms(html), [])

    def test_service_names_are_escaped_in_the_question(self) -> None:
        html = _render([_row([_service(11, '<img src=x onerror="a()">'), _service(12, "سه‌ماهه")])])
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img src=x", html)


if __name__ == "__main__":
    unittest.main()
