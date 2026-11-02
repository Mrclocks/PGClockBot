"""Gift-code page: same access rule as the code routes, strict shop scoping, safe output."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.gift_code_pages import legacy_gift_modal_redirect, register_gift_code_pages
from app.services.gift_codes import gift_code_scope

OWNER = {
    "role": "admin",
    "permissions": [],
    "org_principal_id": 1,
    "org_depth": 0,
    "org_parent_id": None,
    "org_status": "active",
}


def _reseller(permissions: list[str], shop_id: int | None = 5) -> dict:
    return {"role": "reseller", "permissions": permissions, "bot_user_id": shop_id}


class GiftCodeScopeTests(unittest.TestCase):
    def test_owner_manages_the_platform_shop(self):
        self.assertIsNone(gift_code_scope(OWNER))

    def test_shop_staff_with_gift_codes_get_their_own_shop(self):
        self.assertEqual(gift_code_scope(_reseller(["gift_codes"])), 5)
        self.assertEqual(gift_code_scope(_reseller(["gift_codes"], shop_id=9)), 9)

    def test_orders_or_plans_alone_no_longer_reach_gift_codes(self):
        for perms in (["plans"], ["orders"], ["plans", "orders"]):
            with self.subTest(perms=perms), self.assertRaises(PermissionError):
                gift_code_scope(_reseller(perms))

    def test_missing_permission_is_refused(self):
        with self.assertRaises(PermissionError):
            gift_code_scope(_reseller(["tickets", "loyalty"]))

    def test_staff_without_a_shop_never_reaches_any_codes(self):
        for staff in (
            _reseller(["gift_codes"], shop_id=None),
            _reseller(["gift_codes"], shop_id=0),
            {"role": "admin", "permissions": []},
            {"role": "pg_staff", "permissions": ["gift_codes", "plans", "orders"]},
        ):
            with self.subTest(staff=staff), self.assertRaises(PermissionError):
                gift_code_scope(staff)

    def test_forged_owner_flags_grant_nothing(self):
        staff = _reseller(["tickets"])
        staff.update({"is_owner": True, "web_owner": True})
        with self.assertRaises(PermissionError):
            gift_code_scope(staff)


class LegacyRedirectTests(unittest.TestCase):
    def test_old_modal_link_moves_to_the_page_and_keeps_flash(self):
        self.assertEqual(legacy_gift_modal_redirect({"gifts": "1"}), "/gift-codes")
        self.assertEqual(
            legacy_gift_modal_redirect({"gifts": "1", "ok": "x", "other": "y"}),
            "/gift-codes?ok=x",
        )

    def test_plain_plans_page_is_not_redirected(self):
        self.assertIsNone(legacy_gift_modal_redirect({}))
        self.assertIsNone(legacy_gift_modal_redirect({"gifts": "0"}))

    def test_redirect_never_leaves_the_panel(self):
        target = legacy_gift_modal_redirect({"gifts": "1", "ok": "//evil.test"})
        self.assertTrue(target.startswith("/gift-codes"))


class GiftCodeHarness(unittest.IsolatedAsyncioTestCase):
    """Real page route over a temp database with codes for the platform and two shops."""

    async def asyncSetUp(self):
        from app.db import Base
        from app.db.models import ChargeCode
        import app.db.models  # noqa: F401

        self._tmpdir = tempfile.TemporaryDirectory()
        engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 'g.db'}")
        self.engine = engine
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all(
                [
                    ChargeCode(code="PLATFORMCODE", amount=1000, reseller_id=None, note="<script>x</script>"),
                    ChargeCode(code="SHOPFIVECODE", amount=2000, reseller_id=5),
                    ChargeCode(code="SHOPSIXCODE", amount=3000, reseller_id=6),
                ]
            )
            await session.commit()

        from app.api.app import render

        self.staff: dict = {}

        async def get_db():
            async with self.Session() as session:
                yield session

        app = FastAPI()
        register_gift_code_pages(app, render=render, require_staff=lambda: self.staff, get_db=get_db)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _get(self, staff: dict, url: str = "/gift-codes"):
        self.staff.clear()
        self.staff.update(staff)
        return await self.client.get(url)


class GiftCodePageTests(GiftCodeHarness):

    async def test_owner_sees_only_platform_codes_and_notes_are_escaped(self):
        response = await self._get(OWNER)
        self.assertEqual(response.status_code, 200)
        self.assertIn("PLATFORMCODE", response.text)
        self.assertNotIn("SHOPFIVECODE", response.text)
        self.assertNotIn("SHOPSIXCODE", response.text)
        self.assertNotIn("<script>x</script>", response.text)
        self.assertIn("&lt;script&gt;x&lt;/script&gt;", response.text)

    async def test_the_title_has_no_caption_under_it(self):
        response = await self._get(OWNER)
        self.assertNotIn("page-sub", response.text)

    async def test_shop_staff_see_only_their_own_shop(self):
        response = await self._get(_reseller(["gift_codes"], shop_id=5))
        self.assertIn("SHOPFIVECODE", response.text)
        self.assertNotIn("SHOPSIXCODE", response.text)
        self.assertNotIn("PLATFORMCODE", response.text)
        other = await self._get(_reseller(["gift_codes"], shop_id=6))
        self.assertIn("SHOPSIXCODE", other.text)
        self.assertNotIn("SHOPFIVECODE", other.text)

    async def test_staff_without_permission_or_shop_are_sent_home(self):
        for staff in (
            _reseller(["tickets"]),
            _reseller(["gift_codes"], shop_id=None),
            {"role": "pg_staff", "permissions": ["gift_codes"]},
            {"role": "admin", "permissions": []},
        ):
            with self.subTest(staff=staff):
                response = await self._get(staff)
                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.headers["location"], "/home")

    async def test_forms_carry_a_csrf_token_and_post_to_the_existing_routes(self):
        response = await self._get(_reseller(["gift_codes"], shop_id=5))
        self.assertIn('name="csrf_token"', response.text)
        self.assertIn('action="/plans/gift-codes"', response.text)
        self.assertIn('/plans/gift-codes/', response.text)
        self.assertIn('/static/gift-codes.js', response.text)

    async def test_flash_messages_are_shown_and_escaped(self):
        done = await self._get(OWNER, "/gift-codes?ok=1")
        self.assertIn("انجام شد", done.text)
        failed = await self._get(OWNER, "/gift-codes?err=%3Cb%3Ebad%3C%2Fb%3E")
        self.assertNotIn("<b>bad</b>", failed.text)

    async def test_sidebar_entry_is_active_on_the_page(self):
        response = await self._get(_reseller(["gift_codes"], shop_id=5))
        entry = response.text.split("بازاریابی و وفاداری</span>", 1)[0].rsplit("<a ", 1)[1]
        self.assertIn("active", entry)

    async def test_tab_strip_marks_gift_codes_active(self):
        response = await self._get(_reseller(["gift_codes", "loyalty"], shop_id=5))
        self.assertIn('href="/gift-codes" class="active" aria-current="page"', response.text)


class GiftCodeRedirectTests(GiftCodeHarness):
    """The create, edit and toggle routes now return to the gift-code page."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        from app.api.app import render
        from app.api.ux20_pages import register_ux20_pages

        async def get_db():
            async with self.Session() as session:
                yield session

        app = FastAPI()
        register_ux20_pages(
            app, render=render, require_staff=lambda: self.staff, require_admin=lambda: self.staff, get_db=get_db
        )
        self.post_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )

    async def asyncTearDown(self):
        await self.post_client.aclose()
        await super().asyncTearDown()

    async def test_create_and_toggle_return_to_the_gift_code_page(self):
        self.staff.update(OWNER)
        created = await self.post_client.post("/plans/gift-codes", data={"kind": "wallet", "amount": "1000", "code": "NEWCODE1"})
        self.assertTrue(created.headers["location"].startswith("/gift-codes?ok="))
        failed = await self.post_client.post("/plans/gift-codes", data={"kind": "discount", "percent": "101"})
        self.assertTrue(failed.headers["location"].startswith("/gift-codes?err="))
        toggled = await self.post_client.post("/plans/gift-codes/1/toggle")
        self.assertEqual(toggled.headers["location"], "/gift-codes?ok=1")


if __name__ == "__main__":
    unittest.main()
