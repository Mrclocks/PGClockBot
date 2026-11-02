"""A shop owner's «اشتراک من» and «نمایندگان من» sit in the bot section of the sidebar."""

from __future__ import annotations

import re
import unittest
from types import SimpleNamespace

from app.api.app import templates


def sidebar(**ctx) -> dict[str, list[str]]:
    """Item labels per sidebar section, in order."""
    request = SimpleNamespace(query_params={}, url=SimpleNamespace(path="/home"), state=SimpleNamespace())
    html = templates.env.get_template("base.html").render(request=request, path="/home", app_version="t", **ctx)
    sections = {}
    for section_id, name in (("nav-panel-system", "web"), ("nav-panel-shop", "bot")):
        part = html.split(f'id="{section_id}"')[1].split('<div class="nav-section')[0].split("</nav>")[0]
        sections[name] = re.findall(r'class="nav-item[^"]*"[^>]*>\s*<svg[^>]*>.*?</svg>\s*<span>([^<]+)</span>', part, re.S)
    return sections


RESELLER = {"role": "reseller", "permissions": ["dashboard", "shop_settings"]}


class ResellerSidebarTests(unittest.TestCase):
    def test_subscription_is_second_and_representatives_follow_customers_in_the_bot_section(self) -> None:
        nav = sidebar(staff=RESELLER, identity={"kind": "reseller", "can_add_representative": True})
        self.assertEqual(nav["bot"][:4], ["نمای کلی", "اشتراک من", "مشتریان", "نمایندگان من"])
        self.assertNotIn("اشتراک من", nav["web"])
        self.assertNotIn("نمایندگان من", nav["web"])

    def test_representatives_need_the_right_to_add_them(self) -> None:
        nav = sidebar(staff=RESELLER, identity={"kind": "reseller", "can_add_representative": False})
        self.assertNotIn("نمایندگان من", nav["bot"] + nav["web"])
        self.assertIn("اشتراک من", nav["bot"])

    def test_the_owner_has_no_package_of_their_own(self) -> None:
        nav = sidebar(staff={"role": "admin", "permissions": []}, identity={"kind": "owner", "can_manage_representatives": True},
                      is_owner=True)
        self.assertNotIn("اشتراک من", nav["bot"] + nav["web"])
        self.assertIn("نمایندگان", nav["bot"])


if __name__ == "__main__":
    unittest.main()
