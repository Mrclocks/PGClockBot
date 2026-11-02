"""Shop staff action log: written with the change, read only by the shop it belongs to."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

import httpx
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.staff_audit import (
    PAGE_SIZE,
    StaffActor,
    actor_from_staff,
    add_staff_action,
    list_staff_actions,
    staff_action_views,
    staff_audit_scope,
)

ROOT = Path(__file__).resolve().parents[1]
SHOP_A = 71
SHOP_B = 72
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
    return {"role": "reseller", "bot_user_id": shop_id, "id": shop_id, "username": f"shop{shop_id}", "permissions": perms or []}


class StaffAuditScopeTests(unittest.TestCase):
    def test_owner_reads_the_platform_and_a_shop_owner_their_shop(self):
        self.assertIsNone(staff_audit_scope(OWNER))
        self.assertEqual(staff_audit_scope(_reseller(SHOP_A)), SHOP_A)

    def test_everyone_else_is_refused(self):
        for staff in (
            {"role": "pg_staff", "permissions": ["orders"], "bot_user_id": SHOP_A},
            {"role": "admin", "permissions": []},
            _reseller(0),
            {"role": "principal", "bot_user_id": SHOP_A},  # no reseller profile bound
            {"role": "reseller", "bot_user_id": "x"},
        ):
            with self.subTest(staff=staff), self.assertRaises(PermissionError):
                staff_audit_scope(staff)

    def test_actor_kind_follows_the_session(self):
        self.assertEqual(actor_from_staff(OWNER, shop_id=None).kind, "platform_owner")
        actor = actor_from_staff(_reseller(SHOP_A), shop_id=SHOP_A)
        self.assertEqual((actor.kind, actor.shop_id, actor.user_id, actor.label), ("shop_owner", SHOP_A, SHOP_A, f"shop{SHOP_A}"))


class AuditDbHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        import app.db.models  # noqa: F401

        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 'audit.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmpdir.cleanup()


class StaffAuditStoreTests(AuditDbHarness):
    def _actor(self, shop_id: int | None) -> StaffActor:
        return StaffActor(shop_id=shop_id, kind="shop_owner", user_id=shop_id, label="x")

    async def test_unknown_action_target_or_amount_is_refused(self):
        async with self.Session() as session:
            actor = self._actor(SHOP_A)
            with self.assertRaises(ValueError):
                add_staff_action(session, actor, action="drop_tables", target_type="gift_code", target_id=1)
            with self.assertRaises(ValueError):
                add_staff_action(session, actor, action="gift_code_edit", target_type="users", target_id=1)
            for amount in (True, 1.5, "10"):
                with self.subTest(amount=amount), self.assertRaises(ValueError):
                    add_staff_action(
                        session, actor, action="gift_code_edit", target_type="gift_code", target_id=1, amount=amount
                    )

    async def test_each_shop_reads_only_its_own_records(self):
        async with self.Session() as session:
            for shop_id in (SHOP_A, SHOP_B, None):
                add_staff_action(session, self._actor(shop_id), action="gift_code_edit", target_type="gift_code", target_id=1)
            await session.commit()
            a = await list_staff_actions(session, shop_id=SHOP_A)
            b = await list_staff_actions(session, shop_id=SHOP_B)
            platform = await list_staff_actions(session, shop_id=None)
        self.assertEqual([r.shop_id for r in a], [SHOP_A])
        self.assertEqual([r.shop_id for r in b], [SHOP_B])
        self.assertEqual([r.shop_id for r in platform], [None])

    async def test_keyset_pages_are_bounded_and_do_not_overlap(self):
        async with self.Session() as session:
            for _ in range(PAGE_SIZE + 5):
                add_staff_action(session, self._actor(SHOP_A), action="gift_code_toggle", target_type="gift_code", target_id=1)
            await session.commit()
            first = await list_staff_actions(session, shop_id=SHOP_A, limit=10_000)
            second = await list_staff_actions(session, shop_id=SHOP_A, before_id=first[-1].id)
        self.assertEqual(len(first), PAGE_SIZE)
        self.assertEqual(len(second), 5)
        self.assertFalse({r.id for r in first} & {r.id for r in second})
        self.assertEqual([r.id for r in first], sorted((r.id for r in first), reverse=True))

    async def test_a_repeated_request_key_is_written_once(self):
        from sqlalchemy.exc import IntegrityError

        async with self.Session() as session:
            add_staff_action(session, self._actor(SHOP_A), action="gift_code_edit", target_type="gift_code", target_id=1, request_key="k1")
            await session.commit()
            add_staff_action(session, self._actor(SHOP_A), action="gift_code_edit", target_type="gift_code", target_id=1, request_key="k1")
            with self.assertRaises(IntegrityError):
                await session.commit()

    def test_view_labels_and_time_zone(self):
        from datetime import datetime, timezone

        from app.db.models import ShopStaffAction

        row = ShopStaffAction(
            id=5, shop_id=SHOP_A, actor_kind="shop_owner", actor_user_id=SHOP_A, actor_label="ali",
            action="gift_code_create", target_type="gift_code", target_id=9, amount=5000, reason=None,
            created_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        )
        view = staff_action_views([row])[0]
        self.assertEqual(view.when, "2026-01-01 15:30")
        self.assertEqual((view.action, view.target, view.actor_kind), ("ساخت کد هدیه", "کد هدیه #9", "صاحب فروشگاه"))


class GiftCodeAuditTests(AuditDbHarness):
    """Gift-code changes write their record in the same commit, and only for the acting shop."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.staff: dict = _reseller(SHOP_A, ["gift_codes"])

        async def get_db():
            async with self.Session() as session:
                yield session

        from app.api.bulk_pages import register_bulk_pages
        from app.api.ux20_pages import register_ux20_pages

        app = FastAPI()
        register_ux20_pages(
            app, render=lambda *a, **k: None, require_staff=lambda: self.staff,
            require_admin=lambda: self.staff, get_db=get_db,
        )
        register_bulk_pages(
            app, require_perm=lambda _p: (lambda: self.staff), require_admin=lambda: self.staff,
            get_db=get_db, require_staff=lambda: self.staff,
        )
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        await super().asyncTearDown()

    async def _records(self) -> list:
        from app.db.models import ShopStaffAction

        async with self.Session() as session:
            return list((await session.scalars(select(ShopStaffAction).order_by(ShopStaffAction.id))).all())

    async def _code_id(self, code: str) -> int:
        from app.db.models import ChargeCode

        async with self.Session() as session:
            return int((await session.execute(select(ChargeCode.id).where(ChargeCode.code == code))).scalar_one())

    async def test_create_edit_toggle_and_bulk_are_recorded_for_the_shop(self):
        await self.client.post("/plans/gift-codes", data={"kind": "wallet", "amount": "5000", "code": "AUDIT1"})
        code_id = await self._code_id("AUDIT1")
        await self.client.post(f"/plans/gift-codes/{code_id}/edit", data={"kind": "wallet", "amount": "6000"})
        await self.client.post(f"/plans/gift-codes/{code_id}/toggle")
        await self.client.post("/plans/gift-codes/bulk-action", data={"action": "toggle", "ids": [str(code_id)]})
        records = await self._records()
        self.assertEqual(
            [r.action for r in records], ["gift_code_create", "gift_code_edit", "gift_code_toggle", "gift_code_toggle"]
        )
        self.assertEqual({r.shop_id for r in records}, {SHOP_A})
        self.assertEqual({r.target_id for r in records}, {code_id})
        self.assertEqual(records[0].amount, 5000)

    async def test_a_failed_change_leaves_no_record(self):
        await self.client.post("/plans/gift-codes", data={"kind": "wallet", "amount": "5000", "code": "DUP1"})
        await self.client.post("/plans/gift-codes", data={"kind": "wallet", "amount": "5000", "code": "DUP1"})
        code_id = await self._code_id("DUP1")
        await self.client.post(f"/plans/gift-codes/{code_id}/edit", data={"kind": "discount", "percent": "10"})
        self.assertEqual([r.action for r in await self._records()], ["gift_code_create"])

    async def test_refused_staff_write_no_record(self):
        from app.db.models import ChargeCode

        async with self.Session() as session:
            row = ChargeCode(code="OTHER1", amount=1000, reseller_id=SHOP_B, is_active=True)
            session.add(row)
            await session.commit()
            other_id = int(row.id)
        await self.client.post(f"/plans/gift-codes/{other_id}/toggle")
        await self.client.post("/plans/gift-codes/bulk-action", data={"action": "toggle", "ids": [str(other_id)]})
        self.staff = _reseller(SHOP_A, ["orders"])
        await self.client.post("/plans/gift-codes", data={"kind": "wallet", "amount": "5000", "code": "NOPE1"})
        self.assertEqual(await self._records(), [])


class StaffAuditPageTests(AuditDbHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from app.api.app import templates
        from app.api.staff_audit_pages import register_staff_audit_pages

        self.staff: dict = _reseller(SHOP_A)

        async def get_db():
            async with self.Session() as session:
                yield session

        def render(request, name, ctx):
            ctx = {"request": request, "path": request.url.path, "csrf_token": "t", **ctx}
            return HTMLResponse(templates.env.get_template(name).render(**ctx))

        app = FastAPI()
        register_staff_audit_pages(app, render=render, require_staff=lambda: self.staff, get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        await super().asyncTearDown()

    async def test_page_shows_only_the_shop_records_and_escapes_text(self):
        async with self.Session() as session:
            mine = StaffActor(shop_id=SHOP_A, kind="shop_owner", user_id=SHOP_A, label="<b>ali</b>")
            theirs = StaffActor(shop_id=SHOP_B, kind="shop_owner", user_id=SHOP_B, label="OTHERSHOP")
            add_staff_action(session, mine, action="gift_code_edit", target_type="gift_code", target_id=3, reason="<script>x</script>")
            add_staff_action(session, theirs, action="gift_code_edit", target_type="gift_code", target_id=4)
            await session.commit()
        response = await self.client.get("/staff-actions?before=not-a-number")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("OTHERSHOP", response.text)
        self.assertNotIn("<script>x</script>", response.text)
        self.assertNotIn("<b>ali</b>", response.text)
        self.assertIn("&lt;b&gt;ali&lt;/b&gt;", response.text)

    async def test_staff_without_a_shop_are_sent_home(self):
        self.staff = {"role": "pg_staff", "permissions": ["orders"]}
        response = await self.client.get("/staff-actions")
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/home"))


class StaffAuditMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location("staff_actions_migration", ROOT / "alembic/versions/0078_shop_staff_actions.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            conn.execute(sa.text("CREATE TABLE bot_users (id INTEGER PRIMARY KEY)"))
            conn.execute(sa.text("INSERT INTO bot_users VALUES (1)"))
            migration.upgrade()
            migration.upgrade()
            indexes = {i["name"] for i in sa.inspect(conn).get_indexes("shop_staff_actions")}
            self.assertEqual(indexes, {"ix_shop_staff_actions_shop_id_id", "uq_shop_staff_actions_request_key"})
            conn.execute(sa.text(
                "INSERT INTO shop_staff_actions (shop_id, actor_kind, actor_label, action, target_type) "
                "VALUES (1, 'shop_owner', 'a', 'gift_code_edit', 'gift_code')"
            ))
            migration.downgrade()
            migration.downgrade()
            self.assertNotIn("shop_staff_actions", sa.inspect(conn).get_table_names())
            self.assertEqual(conn.execute(sa.text("SELECT COUNT(*) FROM bot_users")).scalar(), 1)
            migration.upgrade()
            self.assertIn("shop_staff_actions", sa.inspect(conn).get_table_names())
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
