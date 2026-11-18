"""The user edit and reseller edit modals open with the same identity header as the user card."""

from __future__ import annotations

import unittest
from types import SimpleNamespace


HOSTILE = '<img src=x onerror="alert(1)">'
IDENTITY = '<section class="edit-section edit-section--identity">'


def person(*, role: str, blocked: bool = False, name: str = "علی") -> SimpleNamespace:
    return SimpleNamespace(
        id=7, role=role, telegram_id=5501, username="ali_shop", full_name=name, first_name=name,
        wallet_balance=0, points=0, is_blocked=blocked, is_demo=False, created_at=None, color_tag=None,
    )


def render(template: str, **context) -> str:
    from app.api.app import templates

    request = SimpleNamespace(url=SimpleNamespace(path="/"), state=SimpleNamespace(), query_params={})
    return templates.env.get_template(template).render(request=request, csrf_token="t", **context)


def render_user_edit(user: SimpleNamespace) -> str:
    return render("_user_edit_body.html", user=user, can_manage_users=True, reseller_plans=[])


def render_reseller_edit(user: SimpleNamespace) -> str:
    profile = SimpleNamespace(
        id=1, user_id=user.id, wallet_balance=0, max_users=10, max_traffic_gb=100, billing_suspended=False,
        billing_suspended_at=None, billing_watermark_bytes=0, is_active=True, pg_admin_username="shop",
        billing_mode="fixed",
    )
    return render(
        "_reseller_edit_body.html", user=user, profile=profile, is_payg=False, topup_nonce="n" * 16,
        wallet_balance=0, billing_rate=1500, min_unsuspend=40_000, staff={"role": "admin"}, pg_limits=None,
    )


class IdentityHeaderTests(unittest.TestCase):
    def test_reseller_edit_starts_with_who_the_reseller_is(self) -> None:
        html = render_reseller_edit(person(role="reseller"))
        header = html.index(IDENTITY)
        self.assertLess(header, html.index("تگ ریسک"), "the header comes before any setting")
        self.assertIn('data-copy="@ali_shop"', html)
        self.assertIn('data-copy="5501"', html)
        self.assertIn('src="/users/7/photo"', html)
        self.assertIn(">نماینده</span>", html)
        self.assertIn(">فعال</span>", html)

    def test_a_blocked_reseller_is_marked_blocked(self) -> None:
        html = render_reseller_edit(person(role="reseller", blocked=True))
        self.assertIn('<span class="badge blocked">مسدود</span>', html)
        self.assertNotIn(">فعال</span>", html.split(IDENTITY)[1].split("</section>")[0])

    def test_user_edit_uses_the_same_header(self) -> None:
        html = render_user_edit(person(role="user"))
        self.assertEqual(html.count(IDENTITY), 1)
        self.assertIn('<h3 class="user-identity-name"><bdi>علی</bdi></h3>', html)
        self.assertIn('src="/users/7/photo"', html)

    def test_a_person_without_username_gets_only_the_id_chip(self) -> None:
        user = person(role="user")
        user.username = None
        html = render_user_edit(user)
        self.assertEqual(html.count('class="user-id-chip"'), 1)
        self.assertIn('data-copy="5501"', html)

    def test_a_hostile_username_is_escaped_in_the_copy_value(self) -> None:
        user = person(role="reseller")
        user.username = 'x"><script>'
        html = render_reseller_edit(user)
        self.assertNotIn('<script>', html)
        self.assertIn('data-copy="@x&#34;&gt;&lt;script&gt;"', html)

    def test_a_hostile_name_is_escaped_in_both_headers(self) -> None:
        for html in (render_reseller_edit(person(role="reseller", name=HOSTILE)),
                     render_user_edit(person(role="user", name=HOSTILE))):
            self.assertNotIn(HOSTILE, html)
            self.assertIn("&lt;img src=x", html)


class SharedCardModalTests(unittest.TestCase):
    def render_modals(self, **kwargs) -> str:
        from app.api.app import templates

        staff = {"role": "admin", "org_principal_id": 1, "org_depth": 0, "org_status": "active"}
        module = templates.env.get_template("_user_card_modals.html").make_module({"csrf_token": "t", "staff": staff})
        return str(module.user_card_modals(**kwargs))

    def test_the_resellers_list_gets_the_card_and_a_message_form_that_returns_there(self) -> None:
        html = self.render_modals(return_page="resellers")
        self.assertIn('id="user-card-body" class="edit-modal-body"', html)
        self.assertIn('<input type="hidden" name="return_page" value="resellers" />', html)
        self.assertNotIn('name="return_filter"', html)

    def test_without_the_message_right_only_the_card_is_rendered(self) -> None:
        html = self.render_modals(with_message=False)
        self.assertIn('id="modal-user-card"', html)
        self.assertNotIn('id="modal-user-message"', html)


if __name__ == "__main__":
    unittest.main()
