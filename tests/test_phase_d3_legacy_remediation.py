"""Phase D3 — legacy remediation: status flags, confirm-align, inventory, no conversion."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.pg_staff_access import (
    classify_staff_cohort,
    staff_needs_remediation,
    staff_remediation_flags,
    staff_username_aligned,
)


_OK = "AaBb12!secret"


def _row(
    *,
    pg: str = "staff1",
    web: str | None = None,
    active: bool = True,
    enc: str | None = "encblob",
    rid: int = 1,
):
    return SimpleNamespace(
        id=rid,
        pg_username=pg,
        web_username=web if web is not None else pg,
        is_active=active,
        pg_admin_password_enc=enc,
        pg_role_id=None,
        note=None,
    )


class CohortHelperTests(unittest.TestCase):
    def test_healthy_l3(self):
        row = _row()
        with patch(
            "app.services.pg_staff_access.staff_has_stored_pg_password",
            return_value=True,
        ):
            self.assertTrue(staff_username_aligned(row))
            self.assertFalse(staff_needs_remediation(row))
            self.assertEqual(classify_staff_cohort(row), "L3")
            flags = staff_remediation_flags(row)
            self.assertTrue(flags["credentials_ready"])
            self.assertTrue(flags["username_aligned"])
            self.assertFalse(flags["needs_remediation"])

    def test_null_enc_aligned_l1(self):
        row = _row(enc=None)
        with patch(
            "app.services.pg_staff_access.staff_has_stored_pg_password",
            return_value=False,
        ):
            self.assertEqual(classify_staff_cohort(row), "L1")
            flags = staff_remediation_flags(row)
            self.assertFalse(flags["credentials_ready"])
            self.assertTrue(flags["username_aligned"])
            self.assertTrue(flags["needs_remediation"])

    def test_null_enc_mismatch_l2(self):
        row = _row(web="otherweb", enc=None)
        with patch(
            "app.services.pg_staff_access.staff_has_stored_pg_password",
            return_value=False,
        ):
            self.assertFalse(staff_username_aligned(row))
            self.assertEqual(classify_staff_cohort(row), "L2")
            self.assertTrue(staff_needs_remediation(row))

    def test_enc_mismatch_l4(self):
        row = _row(web="legacyweb")
        with patch(
            "app.services.pg_staff_access.staff_has_stored_pg_password",
            return_value=True,
        ):
            self.assertEqual(classify_staff_cohort(row), "L4")
            flags = staff_remediation_flags(row)
            self.assertTrue(flags["credentials_ready"])
            self.assertFalse(flags["username_aligned"])
            self.assertTrue(flags["needs_remediation"])

    def test_inactive_l5(self):
        row = _row(active=False)
        with patch(
            "app.services.pg_staff_access.staff_has_stored_pg_password",
            return_value=True,
        ):
            self.assertEqual(classify_staff_cohort(row), "L5")


class StatusMapEnrichmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_pg_staff_status_includes_flags(self):
        from app.services import pg_staff_access as psa

        staff = _row(web="staff1")
        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
        )
        with (
            patch.object(psa, "access_map_by_pg", new=AsyncMock(return_value={"staff1": staff})),
            patch.object(psa, "load_web_admin", return_value={"username": "owner"}),
            patch(
                "app.services.pg_staff_access.staff_has_stored_pg_password",
                return_value=False,
            ),
        ):
            out = await psa.web_access_status_map(session, ["staff1"])
        st = out["staff1"]
        self.assertEqual(st["source"], "pg_staff")
        self.assertFalse(st["credentials_ready"])
        self.assertTrue(st["username_aligned"])
        self.assertTrue(st["needs_remediation"])


class ConfirmAlignTests(unittest.IsolatedAsyncioTestCase):
    async def test_mismatch_without_confirm_errors(self):
        from app.services import pg_staff_access as psa

        existing = _row(web="oldweb", pg="staff1")
        session = AsyncMock()
        with (
            patch.object(psa, "access_by_pg_username", new=AsyncMock(return_value=existing)),
            patch.object(psa, "reseller_by_pg_username", new=AsyncMock(return_value=None)),
            patch(
                "app.services.pg_staff_access.staff_has_stored_pg_password",
                return_value=True,
            ),
        ):
            row, err = await psa.update_web_access(
                session,
                pg_username="staff1",
                web_username="staff1",
                password="",
                confirm_align=False,
            )
        self.assertIsNone(row)
        self.assertIsNotNone(err)
        self.assertIn("هم‌ترازسازی", err or "")
        self.assertIn("تغییر خودکار", err or "")

    async def test_mismatch_with_confirm_updates_username(self):
        from app.services import pg_staff_access as psa

        existing = _row(web="oldweb", pg="staff1")
        session = AsyncMock()
        session.commit = AsyncMock()
        session.refresh = AsyncMock()
        with (
            patch.object(psa, "access_by_pg_username", new=AsyncMock(return_value=existing)),
            patch.object(psa, "reseller_by_pg_username", new=AsyncMock(return_value=None)),
            patch.object(psa, "_username_taken", new=AsyncMock(return_value=None)),
            patch.object(psa, "resolve_pg_role_id_for_admin", new=AsyncMock(return_value=None)),
            patch(
                "app.services.pg_staff_access.staff_has_stored_pg_password",
                return_value=True,
            ),
        ):
            row, err = await psa.update_web_access(
                session,
                pg_username="staff1",
                web_username="staff1",
                password="",
                confirm_align=True,
            )
        self.assertIsNone(err)
        self.assertIsNotNone(row)
        self.assertEqual(existing.web_username, "staff1")

    async def test_aligned_update_no_confirm_needed(self):
        from app.services import pg_staff_access as psa

        existing = _row(web="staff1", pg="staff1")
        session = AsyncMock()
        session.commit = AsyncMock()
        session.refresh = AsyncMock()
        with (
            patch.object(psa, "access_by_pg_username", new=AsyncMock(return_value=existing)),
            patch.object(psa, "reseller_by_pg_username", new=AsyncMock(return_value=None)),
            patch.object(psa, "_username_taken", new=AsyncMock(return_value=None)),
            patch.object(psa, "resolve_pg_role_id_for_admin", new=AsyncMock(return_value=None)),
            patch(
                "app.services.pg_staff_access.staff_has_stored_pg_password",
                return_value=True,
            ),
        ):
            row, err = await psa.update_web_access(
                session,
                pg_username="staff1",
                web_username="staff1",
                password="",
                confirm_align=False,
            )
        self.assertIsNone(err)
        self.assertIsNotNone(row)


class NoConversionContracts(unittest.TestCase):
    def test_staff_route_no_provision(self):
        src = Path("app/api/pg_pages.py").read_text(encoding="utf-8")
        fn = src[
            src.find("async def pg_admins_web_access_staff") : src.find(
                "async def pg_admins_web_access_reseller"
            )
        ]
        self.assertIn("confirm_align", fn)
        self.assertIn("update_web_access", fn)
        self.assertNotIn("provision_existing_pg_admin", fn)

    def test_no_owner_fallback_in_staff_pg(self):
        src = Path("app/api/pg_pages.py").read_text(encoding="utf-8")
        fn = src[src.find("async def _staff_pg") : src.find("async def _assert_owned_user")]
        self.assertIn("get_pg_for_staff", fn)


class StaffPgAsOwnerContractTests(unittest.IsolatedAsyncioTestCase):
    """Behavioral replacement for the old brittle source-string assertion:
    pg_staff must never get ``as_owner=True`` out of ``_staff_pg``, regardless
    of the exact wording of the admin branch (which legitimately changed for
    Hybrid Owner support — admin's ``as_owner`` now reflects the real
    ``pg_is_owner`` flag instead of being hardcoded ``True``).
    """

    async def test_pg_staff_never_gets_as_owner_true(self):
        from app.api.pg_pages import _staff_pg

        with patch(
            "app.services.pasarguard.get_pg_for_staff",
            new=AsyncMock(return_value=MagicMock()),
        ):
            _client, as_owner = await _staff_pg(
                AsyncMock(),
                {
                    "role": "pg_staff",
                    "pg_admin_username": "s1",
                    "pg_staff_id": 9,
                    "pg_is_owner": True,
                },
            )
        self.assertFalse(as_owner)

    def test_admins_template_badges_and_checkbox(self):
        tpl = Path("app/web/templates/pg_admins.html").read_text(encoding="utf-8")
        self.assertIn("اعتبارنامه پاسارگارد ذخیره‌شده", tpl)
        self.assertIn("نیاز به ذخیره رمز یا کلید API", tpl)
        self.assertIn("نام کاربری ناهماهنگ", tpl)
        self.assertIn('name="confirm_align"', tpl)
        self.assertIn("هم‌ترازسازی نام کاربری با پاسارگارد", tpl)
        self.assertIn('name="pg_api_key"', tpl)

    def test_home_cta_markers(self):
        tpl = Path("app/web/templates/pg_home.html").read_text(encoding="utf-8")
        self.assertIn("staff_remediation", tpl)
        self.assertIn('href="/security"', tpl)

    def test_inventory_script_read_only(self):
        src = Path("scripts/list_pg_staff_remediation.py").read_text(encoding="utf-8")
        self.assertIn("inventory_staff_remediation", src)
        self.assertIn("Never prints passwords", src)
        self.assertNotIn("update_web_access", src)
        self.assertNotIn("grant_web_access", src)
        self.assertNotIn("provision_existing", src)


class InventoryHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_inventory_shapes_rows(self):
        from app.services import pg_staff_access as psa

        rows = [
            _row(pg="a", web="a", rid=1),
            _row(pg="b", web="other", rid=2, enc=None),
        ]
        with (
            patch.object(psa, "list_access_rows", new=AsyncMock(return_value=rows)),
            patch(
                "app.services.pg_staff_access.staff_has_stored_pg_password",
                side_effect=lambda r: bool(getattr(r, "pg_admin_password_enc", None)),
            ),
        ):
            out = await psa.inventory_staff_remediation(AsyncMock())
        self.assertEqual(len(out), 2)
        by_pg = {r["pg_username"]: r for r in out}
        self.assertEqual(by_pg["a"]["cohort"], "L3")
        self.assertFalse(by_pg["a"]["needs_remediation"])
        self.assertEqual(by_pg["b"]["cohort"], "L2")
        self.assertTrue(by_pg["b"]["needs_remediation"])
        # No secret fields
        for r in out:
            self.assertNotIn("pg_admin_password_enc", r)
            self.assertNotIn("web_password_hash", r)


if __name__ == "__main__":
    unittest.main()
