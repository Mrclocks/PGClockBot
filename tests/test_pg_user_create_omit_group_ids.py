"""Create payload omits group_ids when staff did not choose any."""

from __future__ import annotations

import unittest

from app.services.pasarguard import build_user_create_payload
from app.services.plans_catalog import groups_allowed_for_staff


class CreateOmitGroupIdsTests(unittest.TestCase):
    def test_none_omits_group_ids_key(self):
        p = build_user_create_payload(username="u1", group_ids=None)
        self.assertNotIn("group_ids", p)
        self.assertEqual(p["username"], "u1")

    def test_empty_list_omits_group_ids_key(self):
        p = build_user_create_payload(username="u2", group_ids=[])
        self.assertNotIn("group_ids", p)

    def test_chosen_groups_are_included(self):
        p = build_user_create_payload(username="u3", group_ids=[3, 5])
        self.assertEqual(p["group_ids"], [3, 5])

    def test_allow_list_still_rejects_foreign_groups(self):
        staff = {
            "role": "reseller",
            "pg_access": {"allowed_group_ids": [3], "allowed_template_ids": [1]},
        }
        self.assertTrue(groups_allowed_for_staff(staff, [3]))
        self.assertFalse(groups_allowed_for_staff(staff, [9]))


if __name__ == "__main__":
    unittest.main()
