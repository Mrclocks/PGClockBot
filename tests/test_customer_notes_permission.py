"""A customer's private staff note and risk tag: each shop keeps and reads only its own.

Shop staff need the ``customer_notes`` key and may mark any customer of their own shop; the
platform owner marks platform customers. A person who buys from several shops is never shown
another shop's (or the platform's) note or tag.
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.customer_marks import (
    CUSTOMER_NOTES_PERMISSION,
    color_filter_clause,
    has_customer_notes_permission,
    marks_for,
    marks_for_staff,
    may_edit_private_marks,
)
from app.services.resellers import DEFAULT_FEATURE_PERMS, FEATURE_PERMS, normalize_feature_perms, parse_perms

ROOT = Path(__file__).resolve().parents[1]
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


def _reseller(shop_id: int, perms: list[str] | None = None) -> dict:
    return {
        "role": "reseller", "bot_user_id": shop_id, "id": shop_id, "username": f"shop{shop_id}",
        "permissions": [CUSTOMER_NOTES_PERMISSION] if perms is None else perms,
    }


def _route(app: FastAPI, path: str):
    for route in app.routes:
        if getattr(route, "path", None) == path and "POST" in (getattr(route, "methods", None) or set()):
            return route
    raise AssertionError(f"route not found: {path}")


class CustomerNotesKeyTests(unittest.TestCase):
    def test_key_is_a_real_shop_permission_and_a_default(self):
        self.assertIn(CUSTOMER_NOTES_PERMISSION, {key for key, _ in FEATURE_PERMS})
        self.assertIn(CUSTOMER_NOTES_PERMISSION, parse_perms(normalize_feature_perms(CUSTOMER_NOTES_PERMISSION)))
        self.assertIn(CUSTOMER_NOTES_PERMISSION, parse_perms(DEFAULT_FEATURE_PERMS))

    def test_who_has_the_permission(self):
        self.assertTrue(has_customer_notes_permission(OWNER))
        self.assertTrue(has_customer_notes_permission(_reseller(SHOP_A)))
        self.assertFalse(has_customer_notes_permission(_reseller(SHOP_A, ["dashboard", "orders"])))
        self.assertFalse(has_customer_notes_permission({"role": "pg_staff", "permissions": ["users"]}))
        self.assertFalse(has_customer_notes_permission(None))


class MarksHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        from app.db.models import BotUser, BotUserShop

        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 'notes.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            for shop in (SHOP_A, SHOP_B):
                session.add(BotUser(id=shop, telegram_id=shop * 10, referral_code=f"shop{shop}", role="reseller"))
            session.add_all([
                BotUser(id=11, telegram_id=110, referral_code="c11", reseller_id=SHOP_A),  # A only
                BotUser(id=12, telegram_id=120, referral_code="c12", reseller_id=SHOP_B),  # B, member of A and platform
                BotUser(id=13, telegram_id=130, referral_code="c13", reseller_id=None),    # platform only
            ])
            await session.flush()
            session.add_all([BotUserShop(bot_user_id=12, shop_id=SHOP_A), BotUserShop(bot_user_id=12, shop_id=None)])
            await session.commit()

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

    async def _write(self, staff: dict, user_id: int, *, note: str | None = None, tag: str | None = None) -> None:
        async with self.Session() as session:
            if note is not None:
                await self.note_route.endpoint(user_id=user_id, note=note, risk_manual="", staff=staff, session=session)
            if tag is not None:
                await self.tag_route.endpoint(user_id=user_id, color_tag=tag, staff=staff, session=session)

    async def _marks(self, shop_id: int | None, user_id: int):
        async with self.Session() as session:
            return (await marks_for(session, shop_id, [user_id]))[user_id]


class PerShopMarksTests(MarksHarness):
    async def test_each_shop_keeps_its_own_marks_on_a_shared_customer(self):
        from app.db.models import BotUser

        await self._write(_reseller(SHOP_A), 12, note="<b>A says late</b>", tag="red")
        await self._write(_reseller(SHOP_B), 12, note="B says fine", tag="yellow")
        await self._write(OWNER, 12, note="platform note", tag="orange")
        a, b, platform = await self._marks(SHOP_A, 12), await self._marks(SHOP_B, 12), await self._marks(None, 12)
        self.assertEqual((a.note, a.tag), ("<b>A says late</b>", "red"))
        self.assertEqual((b.note, b.tag), ("B says fine", "yellow"))
        self.assertEqual((platform.note, platform.tag), ("platform note", "orange"))
        async with self.Session() as session:
            person = await session.get(BotUser, 12)
            self.assertEqual((person.staff_note, person.color_tag), ("platform note", "orange"))

    async def test_a_shop_never_writes_a_stranger(self):
        await self._write(_reseller(SHOP_B), 11, note="nope", tag="red")
        await self._write(_reseller(SHOP_A), 13, note="nope", tag="red")
        for shop_id, user_id in ((SHOP_B, 11), (SHOP_A, 13), (None, 13), (None, 11)):
            marks = await self._marks(shop_id, user_id)
            self.assertEqual((marks.note, marks.tag), ("", "green"))

    async def test_without_the_key_nothing_is_written(self):
        await self._write(_reseller(SHOP_A, ["dashboard", "orders"]), 11, note="nope", tag="red")
        marks = await self._marks(SHOP_A, 11)
        self.assertEqual((marks.note, marks.tag), ("", "green"))

    async def test_who_may_edit_and_what_a_viewer_reads(self):
        from app.db.models import BotUser

        await self._write(_reseller(SHOP_A), 12, note="A only")
        async with self.Session() as session:
            shared = await session.get(BotUser, 12)
            self.assertTrue(await may_edit_private_marks(session, _reseller(SHOP_A), shared))
            self.assertTrue(await may_edit_private_marks(session, _reseller(SHOP_B), shared))
            self.assertFalse(await may_edit_private_marks(session, _reseller(SHOP_B), await session.get(BotUser, 11)))
            self.assertFalse(await may_edit_private_marks(session, _reseller(SHOP_A, ["orders"]), shared))
            self.assertEqual((await marks_for_staff(session, _reseller(SHOP_A), shared)).note, "A only")
            self.assertEqual((await marks_for_staff(session, _reseller(SHOP_B), shared)).note, "")
            self.assertEqual((await marks_for_staff(session, {"role": "pg_staff"}, shared)).note, "")

    async def test_list_filter_reads_this_shops_tags_only(self):
        from app.db.models import BotUser
        from app.services.users_ops import scoped_users_where

        await self._write(_reseller(SHOP_A), 11, tag="red")
        await self._write(_reseller(SHOP_B), 12, tag="red")

        async def ids(shop_id, color):
            async with self.Session() as session:
                query = select(BotUser.id).where(scoped_users_where(shop_id), color_filter_clause(shop_id, color))
                return set((await session.scalars(query)).all())

        self.assertEqual(await ids(SHOP_A, "red"), {11})
        self.assertEqual(await ids(SHOP_A, "green"), {12})
        self.assertEqual(await ids(SHOP_B, "red"), {12})

    async def test_shop_bot_customer_list_shows_this_shops_tag(self):
        from app.services.color_tags import tag_mark

        await self._write(_reseller(SHOP_B), 12, tag="red")
        self.assertEqual(tag_mark((await self._marks(SHOP_A, 12)).tag), tag_mark("green"))
        self.assertEqual(tag_mark((await self._marks(SHOP_B, 12)).tag), tag_mark("red"))

    async def test_a_shop_created_account_keeps_its_tag_on_the_membership(self):
        from app.services.bot_user_admin import admin_create_bot_user

        async with self.Session() as session:
            user = await admin_create_bot_user(
                session, telegram_id=777_001, staff=_reseller(SHOP_A), reseller_id=SHOP_A, color_tag="orange"
            )
            self.assertEqual(user.color_tag, "green")
        self.assertEqual((await self._marks(SHOP_A, int(user.id))).tag, "orange")


class CustomerNotesTemplateTests(unittest.TestCase):
    """The edit page offers the note and tag forms only to staff who may save them."""

    def _render(self, *, can_edit: bool) -> str:
        from types import SimpleNamespace

        from app.api.app import templates
        from app.services.color_tags import color_tag_meta, color_tags_for_ui
        from app.services.customer_marks import CustomerMarks

        user = SimpleNamespace(
            id=7, telegram_id=7007, username="cust", full_name="Customer", is_blocked=False, is_demo=False,
            role="user", color_tag="red", staff_note="GLOBAL NOTE", wallet_balance=0,
        )
        return templates.env.get_template("_user_edit_body.html").render(
            user=user, staff={}, marks=CustomerMarks(note="<script>x</script>", tag="yellow"),
            show_private_marks=True, can_edit_private_marks=can_edit, can_manage_users=False,
            display_wallet=0, services=[], wallet_txs=[], plans=[], color_tags=color_tags_for_ui(),
            color_tag_meta=color_tag_meta, format_toman=str, wallet_nonce="n",
        )

    def test_forms_hidden_without_permission(self):
        html = self._render(can_edit=False)
        self.assertNotIn("/users/7/staff-note", html)
        self.assertNotIn("/users/7/color-tag", html)

    def test_the_viewers_own_marks_are_shown_escaped_never_the_person_record(self):
        html = self._render(can_edit=True)
        self.assertIn("/users/7/staff-note", html)
        self.assertNotIn("<script>x</script>", html)
        self.assertIn("&lt;script&gt;x&lt;/script&gt;", html)
        self.assertNotIn("GLOBAL NOTE", html)


class MarksMigrationTests(unittest.TestCase):
    def test_shop_marks_move_to_memberships_and_back(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location("marks", ROOT / "alembic/versions/0083_shop_customer_marks.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            conn.execute(sa.text(
                "CREATE TABLE bot_users (id INTEGER PRIMARY KEY, reseller_id INTEGER, staff_note TEXT, color_tag VARCHAR(16))"
            ))
            conn.execute(sa.text(
                "CREATE TABLE bot_user_shops (id INTEGER PRIMARY KEY, bot_user_id INTEGER NOT NULL, shop_id INTEGER, created_at TIMESTAMP)"
            ))
            conn.execute(sa.text(
                "CREATE UNIQUE INDEX uq_bot_user_shops_shop ON bot_user_shops (bot_user_id, shop_id) WHERE shop_id IS NOT NULL"
            ))
            conn.execute(sa.text(
                "INSERT INTO bot_users VALUES (1, 5, 'shop note', 'red'), (2, NULL, 'platform note', 'orange'), (3, 5, NULL, 'green')"
            ))
            conn.execute(sa.text("INSERT INTO bot_user_shops (bot_user_id, shop_id) VALUES (3, 5)"))
            migration.upgrade()
            migration.upgrade()
            person = conn.execute(sa.text("SELECT staff_note, color_tag FROM bot_users WHERE id = 1")).one()
            self.assertEqual(tuple(person), (None, "green"))
            platform = conn.execute(sa.text("SELECT staff_note, color_tag FROM bot_users WHERE id = 2")).one()
            self.assertEqual(tuple(platform), ("platform note", "orange"))
            moved = conn.execute(sa.text(
                "SELECT staff_note, color_tag FROM bot_user_shops WHERE bot_user_id = 1 AND shop_id = 5"
            )).one()
            self.assertEqual(tuple(moved), ("shop note", "red"))
            self.assertEqual(conn.execute(sa.text("SELECT COUNT(*) FROM bot_user_shops")).scalar(), 2)
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
            back = conn.execute(sa.text("SELECT staff_note, color_tag FROM bot_users WHERE id = 1")).one()
            self.assertEqual(tuple(back), ("shop note", "red"))
            self.assertNotIn("staff_note", {c["name"] for c in sa.inspect(conn).get_columns("bot_user_shops")})
            self.assertEqual(conn.execute(sa.text("SELECT COUNT(*) FROM bot_user_shops")).scalar(), 2)
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
