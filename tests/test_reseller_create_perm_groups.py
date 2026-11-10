"""The add-representative modal groups permissions like the representative plan, and keeps them all on."""

from __future__ import annotations

import os
import re
import unittest
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.services.resellers import FEATURE_PERMS, feature_perm_groups
from tests.test_message_variables_entry import OWNER, _page


def create_modal() -> str:
    html = _page(
        "resellers.html", "/resellers", OWNER,
        owner_view=True, tab="list", tabs=[{"id": "list", "href": "/resellers", "label": "لیست"}],
        show_reseller_apply="1", default_panel_url="", default_pg_panel_url="",
        custom_panel_url="", custom_pg_panel_url="", panel_url="", pg_panel_url="",
        using_custom_panel_url=False, using_custom_pg_panel_url=False,
        resellers=[], rows=[], items=[], feature_perms=FEATURE_PERMS, pg_roles=[],
        reseller_plans=[SimpleNamespace(id=1, name="p", billing_mode="fixed", price_per_gb=0)],
    )
    return html.split('id="modal-reseller-create"', 1)[1].split('id="modal-reseller-create-child"', 1)[0]


class ResellerCreatePermGroupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.modal = create_modal()

    def test_permissions_are_in_the_plan_groups(self) -> None:
        self.assertEqual(self.modal.count("<details class=\"ds-acc"), len(feature_perm_groups()))
        self.assertIn("data-ds-perms", self.modal)
        for group in feature_perm_groups():
            self.assertIn(group.title, self.modal)

    def test_every_permission_is_posted_once_and_starts_on(self) -> None:
        for key, _label in FEATURE_PERMS:
            inputs = re.findall(rf'<input[^>]*name="perm_{key}"[^>]*>', self.modal)
            self.assertEqual(len(inputs), 1, key)
            self.assertIn("checked", inputs[0], key)

    def test_pasarguard_switches_stay_in_the_form(self) -> None:
        self.assertIn('name="create_pg_admin"', self.modal)
        self.assertIn('name="share_pg_panel_url"', self.modal)


if __name__ == "__main__":
    unittest.main()
