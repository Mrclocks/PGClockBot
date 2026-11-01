"""A customer's staff note and risk tag: who may write them.

Shop staff need the ``customer_notes`` key and may only write the people their shop
attributed; the platform owner keeps writing platform customers.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.customer_marks import (
    CUSTOMER_NOTES_PERMISSION,
    has_customer_notes_permission,
    may_edit_private_marks,
)
from app.services.resellers import DEFAULT_FEATURE_PERMS, FEATURE_PERMS, normalize_feature_perms, parse_perms

SHOP_A = 1001
SHOP_B = 1002

OWNER = {
    "role": "admin",
    "id": 1,
    "username": "root",
    "org_principal_id": 1,
    "org_depth": 0,
    "org_parent_id": None,
    "org_status": "active",
}


def _reseller(shop_id: int, perms: list[str]) -> dict:
    return {"role": "reseller", "bot_user_id": shop_id, "id": shop_id, "username": f"shop{shop_id}", "permissions": perms}


def _route(app: FastAPI, path: str):
    for route in app.routes:
        if getattr(route, "path", None) == path and "POST" in (getattr(route, "methods", None) or set()):
            return route
    raise AssertionError(f"route not found: {path}")


class CustomerNotesKeyTests(unittest.TestCase):
    def test_key_is_a_real_shop_permission(self):
        self.assertIn(CUSTOMER_NOTES_PERMISSION, {key for key, _ in FEATURE_PERMS})
        self.assertIn(CUSTOMER_NOTES_PERMISSION, parse_perms(normalize_feature_perms(CUSTOMER_NOTES_PERMISSION)))

    def test_new_shops_get_the_key_by_default(self):
        self.assertIn(CUSTOMER_NOTES_PERMISSION, parse_perms(DEFAULT_FEATURE_PERMS))

    def test_who_has_the_permission(self):
        self.assertTrue(has_customer_notes_permission(OWNER))
        self.assertTrue(has_customer_notes_permission(_reseller(SHOP_A, [CUSTOMER_NOTES_PERMISSION])))
        self.assertFalse(has_customer_notes_permission(_reseller(SHOP_A, ["dashboard", "orders"])))
        self.assertFalse(has_customer_notes_permission({"role": "pg_staff", "permissions": []}))
        self.assertFalse(has_customer_notes_permission(None))
        # A bare legacy admin label without the owner principal is not the owner.
        self.assertFalse(has_customer_notes_permission({"role": "pg_staff", "permissions": ["users"]}))


class CustomerNotesRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        import app.db.models  # noqa: F401

        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 'notes.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

        from app.api.ux20_pages import register_ux20_pages

        app = FastAPI()
        register_ux20_pages(
            app, render=lambda *a, **k: None, require_staff=lambda: None, require_admin=lambda: None, get_db=lambda: None
        )
        self.note_route = _route(app, "/users/{user_id}/staff-note")
        self.tag_route = _route(app, "/users/{user_id}/color-tag")

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _customer(self, session, *, telegram_id: int, reseller_id: int | None):
        from app.db.models import BotUser

        for shop in (SHOP_A, SHOP_B):
            if await session.get(BotUser, shop) is None:
                session.add(BotUser(id=shop, telegram_id=shop * 10, referral_code=f"shop{shop}", role="reseller"))
        user = BotUser(telegram_id=telegram_id, referral_code=f"c{telegram_id}", reseller_id=reseller_id)
        session.add(user)
        await session.commit()
        return user

    async def _note(self, session, user, staff, text="late payer"):
        return await self.note_route.endpoint(user_id=user.id, note=text, risk_manual="", staff=staff, session=session)

    async def _tag(self, session, user, staff, tag="red"):
        return await self.tag_route.endpoint(user_id=user.id, color_tag=tag, staff=staff, session=session)

    async def test_shop_with_key_writes_note_and_tag_of_its_own_customer(self):
        async with self.Session() as session:
            user = await self._customer(session, telegram_id=5001, reseller_id=SHOP_A)
            staff = _reseller(SHOP_A, [CUSTOMER_NOTES_PERMISSION])
            await self._note(session, user, staff, "<b>late</b> payer")
            await self._tag(session, user, staff)
            await session.refresh(user)
            self.assertEqual(user.staff_note, "<b>late</b> payer")
            self.assertEqual(user.color_tag, "red")
            self.assertTrue(may_edit_private_marks(staff, user))

    async def test_shop_without_key_cannot_write(self):
        async with self.Session() as session:
            user = await self._customer(session, telegram_id=5002, reseller_id=SHOP_A)
            staff = _reseller(SHOP_A, ["dashboard", "orders", "plans"])
            await self._note(session, user, staff)
            await self._tag(session, user, staff)
            await session.refresh(user)
            self.assertIsNone(user.staff_note)
            self.assertIsNone(user.color_tag)
            self.assertFalse(may_edit_private_marks(staff, user))

    async def test_shop_cannot_write_another_shops_customer(self):
        async with self.Session() as session:
            user = await self._customer(session, telegram_id=5003, reseller_id=SHOP_B)
            staff = _reseller(SHOP_A, [CUSTOMER_NOTES_PERMISSION])
            await self._note(session, user, staff)
            await self._tag(session, user, staff)
            await session.refresh(user)
            self.assertIsNone(user.staff_note)
            self.assertIsNone(user.color_tag)
            self.assertFalse(may_edit_private_marks(staff, user))

    async def test_shop_cannot_write_a_platform_customer(self):
        async with self.Session() as session:
            user = await self._customer(session, telegram_id=5004, reseller_id=None)
            staff = _reseller(SHOP_A, [CUSTOMER_NOTES_PERMISSION])
            await self._note(session, user, staff)
            await session.refresh(user)
            self.assertIsNone(user.staff_note)

    async def test_owner_writes_platform_customer_but_not_a_shops(self):
        async with self.Session() as session:
            platform_user = await self._customer(session, telegram_id=5005, reseller_id=None)
            shop_user = await self._customer(session, telegram_id=5006, reseller_id=SHOP_A)
            await self._note(session, platform_user, OWNER, "vip")
            await self._note(session, shop_user, OWNER, "owner note")
            await session.refresh(platform_user)
            await session.refresh(shop_user)
            self.assertEqual(platform_user.staff_note, "vip")
            self.assertIsNone(shop_user.staff_note)


class CustomerNotesTemplateTests(unittest.TestCase):
    """The edit page offers the note and tag forms only to staff who may save them."""

    def _render(self, *, can_edit: bool) -> str:
        from types import SimpleNamespace

        from app.api.app import templates
        from app.services.color_tags import color_tag_meta, color_tags_for_ui

        user = SimpleNamespace(
            id=7, telegram_id=7007, username="cust", full_name="Customer", is_blocked=False, is_demo=False,
            role="user", color_tag="yellow", staff_note="<script>x</script>", wallet_balance=0,
        )
        return templates.env.get_template("_user_edit_body.html").render(
            user=user, staff={}, show_private_marks=True, can_edit_private_marks=can_edit, can_manage_users=False,
            display_wallet=0, services=[], wallet_txs=[], plans=[], color_tags=color_tags_for_ui(),
            color_tag_meta=color_tag_meta, format_toman=str, wallet_nonce="n",
        )

    def test_forms_hidden_without_permission(self):
        html = self._render(can_edit=False)
        self.assertNotIn("/users/7/staff-note", html)
        self.assertNotIn("/users/7/color-tag", html)

    def test_forms_shown_with_permission_and_note_escaped(self):
        html = self._render(can_edit=True)
        self.assertIn("/users/7/staff-note", html)
        self.assertIn("/users/7/color-tag", html)
        self.assertNotIn("<script>x</script>", html)
        self.assertIn("&lt;script&gt;x&lt;/script&gt;", html)


if __name__ == "__main__":
    unittest.main()
