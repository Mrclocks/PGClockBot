"""Adding an existing panel service: ownership scope, uniqueness, generic refusals, rate limit."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import BotUser, ResellerProfile, ServiceImportAttempt, UserService, Order, OrderStatus
from app.services import service_import as si
from app.services import service_import_config as si_config
from app.services.pasarguard import PasarGuardError, subscription_path_from_url
from app.services.service_import import (
    GENERIC_REFUSAL,
    ImportOutcome,
    ServiceImportError,
    import_existing_service,
    is_external_service,
    parse_subscription_token,
)

PLATFORM_ADMIN = "platformadmin"
SHOP_ADMIN = "shopadmin"
TOKEN = "tok-AAAAAAAAAAAA"


class FakePanel:
    """Authenticated panel client: unknown tokens 404, a scoped client only sees its own users.

    Subscription links answer only on ``sub_path`` (the panel's real subscription prefix) and the
    client is signed in as ``signed_in_as``, like a password or an API-key login.
    """

    def __init__(self, users: dict[str, dict], *, visible_admin: str | None = None, signed_in_as: str = "platformadmin"):
        self.users = users
        self.visible_admin = visible_admin
        self.fail_with: Exception | None = None
        self.sub_path = "/sub"
        self.signed_in_as = signed_in_as
        self.uses_api_key = False
        self.base_url = f"http://panel-{id(self)}.test"
        self.info_calls: list[str] = []

    async def subscription_info(self, token: str, *, subscription_url: str | None = None) -> dict:
        if self.fail_with:
            raise self.fail_with
        prefix = subscription_path_from_url(subscription_url) if subscription_url else "/sub"
        self.info_calls.append(f"{prefix}/{token}")
        user = self.users.get(token)
        if not user or prefix != self.sub_path:
            raise PasarGuardError("not found", status_code=404)
        return {"username": user["username"]}

    async def get_current_admin(self) -> dict:
        if self.fail_with:
            raise self.fail_with
        return {"username": self.signed_in_as}

    async def get_user_by_username(self, username: str) -> dict:
        for user in self.users.values():
            if user["username"] == username:
                if self.visible_admin and user["admin"]["username"] != self.visible_admin:
                    raise PasarGuardError("forbidden", status_code=403)
                return dict(user)
        raise PasarGuardError("not found", status_code=404)


def _panel_user(pid: int, name: str, admin: str) -> dict:
    return {"id": pid, "username": name, "admin": {"username": admin}, "subscription_url": f"https://p.example/sub/{name}"}


class ImportCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        import app.db.models  # noqa: F401

        from app.services.users import clear_settings_cache

        clear_settings_cache()  # settings are cached per process; every test has its own database
        si.clear_identity_cache()
        self.addCleanup(si.clear_identity_cache)
        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 'i.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=1, telegram_id=101, referral_code="u1", role="user"),
                BotUser(id=2, telegram_id=102, referral_code="u2", role="user"),
                BotUser(id=5, telegram_id=105, referral_code="shop5", role="reseller"),
                BotUser(id=6, telegram_id=106, referral_code="u6", role="user", reseller_id=5),
                BotUser(id=7, telegram_id=107, referral_code="u7", role="user", reseller_id=5),
                BotUser(id=8, telegram_id=108, referral_code="u8", role="user", is_blocked=True),
                ResellerProfile(user_id=5, pg_admin_username=SHOP_ADMIN),
                # The platform order behind the "order:1" services these tests create for user 1.
                Order(id=1, user_id=1, reseller_id=None, amount=1000, status=OrderStatus.DELIVERED.value),
            ])
            await session.commit()
        self.platform_users = {
            "tok-plat-0000001": _panel_user(11, "plat-one", PLATFORM_ADMIN),
            "tok-plat-0000002": _panel_user(12, "plat-two", PLATFORM_ADMIN),
            "tok-shop-0000001": _panel_user(21, "shop-one", SHOP_ADMIN),
        }
        self.platform_panel = FakePanel(self.platform_users)  # owner credentials see every user
        self.shop_panel = FakePanel(self.platform_users, visible_admin=SHOP_ADMIN)
        self.panel_settings = SimpleNamespace(pg_username=PLATFORM_ADMIN, pg_subscription_path="/sub")
        for module, target, value in (
            (si, "get_pg", lambda: self.platform_panel),
            (si, "get_settings", lambda: self.panel_settings),
            (si_config, "get_settings", lambda: self.panel_settings),
        ):
            patcher = patch.object(module, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        async def for_reseller(_session, _shop_id):
            return self.shop_panel

        patcher = patch.object(si, "get_pg_for_reseller", for_reseller)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def run_import(self, user_id: int, link: str, *, shop_id: int | None = None, renewable: bool = True):
        async with self.Session() as session:
            user = await session.get(BotUser, user_id)
            return await import_existing_service(session, user=user, raw_link=link, shop_id=shop_id, renewable=renewable)

    async def refused(self, user_id: int, link: str, *, shop_id: int | None = None) -> str:
        with self.assertRaises(ServiceImportError) as caught:
            await self.run_import(user_id, link, shop_id=shop_id)
        return str(caught.exception)

    async def rows(self, model=UserService):
        async with self.Session() as session:
            return list((await session.scalars(select(model))).all())


class ScopeTests(ImportCase):
    async def test_a_platform_customer_adds_a_platform_service(self):
        result = await self.run_import(1, "https://p.example/sub/tok-plat-0000001")
        self.assertEqual(result.outcome, ImportOutcome.ADDED)
        row = (await self.rows())[0]
        self.assertEqual((row.bot_user_id, row.pg_user_id, row.pg_username, row.remark), (1, 11, "plat-one", "imported"))
        self.assertEqual(row.subscription_token, "tok-plat-0000001")
        self.assertIsNone(row.plan_id)

    async def test_a_bare_token_works_too(self):
        self.assertEqual((await self.run_import(1, "tok-plat-0000002")).outcome, ImportOutcome.ADDED)

    async def test_a_shops_service_cannot_be_added_from_the_platform_bot(self):
        self.assertEqual(await self.refused(1, "tok-shop-0000001"), GENERIC_REFUSAL)
        self.assertEqual(await self.rows(), [])

    async def test_a_platform_service_cannot_be_added_from_a_shop_bot(self):
        self.assertEqual(await self.refused(6, "tok-plat-0000001", shop_id=5), GENERIC_REFUSAL)
        self.assertEqual(await self.rows(), [])

    async def test_a_shop_customer_adds_a_service_of_the_shop_admin(self):
        result = await self.run_import(6, "tok-shop-0000001", shop_id=5)
        self.assertEqual(result.service.bot_user_id, 6)

    async def test_even_with_owner_credentials_the_admin_must_match(self):
        # A shop whose scoped client could see the user is still refused when the owner differs.
        self.shop_panel.visible_admin = None
        self.assertEqual(await self.refused(6, "tok-plat-0000001", shop_id=5), GENERIC_REFUSAL)

    async def test_a_customer_of_another_shop_cannot_use_this_shop_context(self):
        self.assertEqual(await self.refused(1, "tok-shop-0000001", shop_id=5), GENERIC_REFUSAL)
        self.assertEqual(await self.refused(6, "tok-plat-0000001", shop_id=None), GENERIC_REFUSAL)

    async def test_a_blocked_customer_is_refused(self):
        self.assertEqual(await self.refused(8, "tok-plat-0000001"), GENERIC_REFUSAL)

    async def test_the_admin_is_read_from_the_panel_so_a_blank_username_setting_still_works(self):
        self.panel_settings.pg_username = ""  # e.g. an API-key login
        self.assertEqual((await self.run_import(1, "tok-plat-0000001")).outcome, ImportOutcome.ADDED)

    async def test_when_nobody_can_tell_who_the_bot_is_the_import_fails_closed(self):
        self.panel_settings.pg_username = ""
        self.platform_panel.fail_with = PasarGuardError("down", status_code=500)
        with self.assertRaises(ServiceImportError) as caught:
            await self.run_import(1, "tok-plat-0000001")
        self.assertEqual(caught.exception.reason, si.Reason.IDENTITY_UNKNOWN)
        self.assertEqual(str(caught.exception), si.PANEL_UNAVAILABLE)

    async def test_mismatching_or_malformed_panel_data_is_refused(self):
        self.platform_users["tok-plat-0000001"]["id"] = True
        self.assertEqual(await self.refused(1, "tok-plat-0000001"), GENERIC_REFUSAL)
        self.platform_users["tok-plat-0000001"]["id"] = 0
        self.assertEqual(await self.refused(1, "tok-plat-0000001"), GENERIC_REFUSAL)
        self.platform_users["tok-plat-0000001"].pop("admin")
        self.platform_users["tok-plat-0000001"]["id"] = 11
        self.assertEqual(await self.refused(1, "tok-plat-0000001"), GENERIC_REFUSAL)
        self.assertEqual(await self.rows(), [])


class OwnershipTests(ImportCase):
    async def test_a_service_of_another_customer_is_refused_with_the_same_message_as_a_missing_one(self):
        await self.run_import(1, "tok-plat-0000001")
        taken = await self.refused(2, "tok-plat-0000001")
        missing = await self.refused(2, "tok-plat-nothere-1")
        self.assertEqual(taken, missing)
        self.assertEqual(len(await self.rows()), 1)

    async def test_adding_the_same_service_twice_does_not_duplicate_it(self):
        first = await self.run_import(1, "tok-plat-0000001")
        second = await self.run_import(1, "tok-plat-0000001")
        self.assertEqual(second.outcome, ImportOutcome.ALREADY_YOURS)
        self.assertEqual(first.service.id, second.service.id)
        self.assertEqual(len(await self.rows()), 1)

    async def test_a_service_sold_by_the_bot_to_someone_else_cannot_be_imported(self):
        async with self.Session() as session:
            session.add(UserService(bot_user_id=2, pg_user_id=12, pg_username="plat-two", remark="order:5"))
            await session.commit()
        self.assertEqual(await self.refused(1, "tok-plat-0000002"), GENERIC_REFUSAL)

    async def test_my_own_view_only_link_is_upgraded_but_anothers_is_not(self):
        async with self.Session() as session:
            session.add(UserService(bot_user_id=1, pg_user_id=11, pg_username="plat-one", subscription_token="tok-plat-0000001", remark="linked"))
            session.add(UserService(bot_user_id=2, pg_user_id=12, pg_username="plat-two", subscription_token="tok-plat-0000002", remark="linked"))
            await session.commit()
        result = await self.run_import(1, "tok-plat-0000001")
        self.assertEqual(result.outcome, ImportOutcome.UPGRADED)
        self.assertEqual(result.service.remark, "imported")
        self.assertEqual(await self.refused(1, "tok-plat-0000002"), GENERIC_REFUSAL)

    async def test_a_stale_check_cannot_let_two_customers_win(self):
        await self.run_import(1, "tok-plat-0000001")

        async def stale(_session, _pg_user_id, _token):
            return []

        with patch.object(si, "existing_rows", stale):
            self.assertEqual(await self.refused(2, "tok-plat-0000001"), GENERIC_REFUSAL)
        rows = await self.rows()
        self.assertEqual([r.bot_user_id for r in rows], [1])

    async def test_the_database_itself_allows_one_imported_row_per_panel_user(self):
        async with self.Session() as session:
            session.add(UserService(bot_user_id=1, pg_user_id=99, pg_username="a", remark="imported"))
            await session.commit()
            session.add(UserService(bot_user_id=2, pg_user_id=99, pg_username="a", remark="imported"))
            with self.assertRaises(IntegrityError):
                await session.commit()
            await session.rollback()
            session.add(UserService(bot_user_id=2, pg_user_id=99, pg_username="a", remark="order:1"))
            await session.commit()

    async def _set(self, **values: str) -> None:
        from app.services.users import set_settings_bulk

        async with self.Session() as session:
            await set_settings_bulk(session, values)

    async def _seed_imported(self, count: int) -> None:
        async with self.Session() as session:
            session.add_all(
                UserService(bot_user_id=1, pg_user_id=500 + i, pg_username=f"x{i}", remark="imported")
                for i in range(count)
            )
            await session.commit()

    async def test_the_default_cap_is_five_services_per_customer(self):
        await self._seed_imported(5)
        self.assertEqual(await self.refused(1, "tok-plat-0000001"), si.LIMIT_REACHED)

    async def test_the_cap_comes_from_the_setting(self):
        await self._set(service_import_max_per_user="2")
        await self._seed_imported(2)
        self.assertEqual(await self.refused(1, "tok-plat-0000001"), si.LIMIT_REACHED)
        await self._set(service_import_max_per_user="3")
        self.assertEqual((await self.run_import(1, "tok-plat-0000001")).outcome, ImportOutcome.ADDED)

    async def test_a_zero_cap_lets_a_customer_add_past_the_old_ceiling_of_twenty(self):
        await self._set(service_import_max_per_user="0")
        await self._seed_imported(25)
        self.assertEqual((await self.run_import(1, "tok-plat-0000001")).outcome, ImportOutcome.ADDED)


class RateLimitTests(ImportCase):
    async def test_repeated_failures_block_even_a_valid_link_until_the_window_passes(self):
        for _ in range(si.MAX_FAILED_ATTEMPTS):
            await self.refused(1, "tok-plat-nothere-1")
        self.assertEqual(await self.refused(1, "tok-plat-0000001"), si.RATE_LIMITED)
        self.assertEqual(await self.rows(), [])
        async with self.Session() as session:
            await session.execute(update(ServiceImportAttempt).values(created_at=datetime.now(timezone.utc) - timedelta(hours=2)))
            await session.commit()
        self.assertEqual((await self.run_import(1, "tok-plat-0000001")).outcome, ImportOutcome.ADDED)

    async def test_limits_are_per_customer(self):
        for _ in range(si.MAX_FAILED_ATTEMPTS):
            await self.refused(1, "tok-plat-nothere-1")
        self.assertEqual((await self.run_import(2, "tok-plat-0000001")).outcome, ImportOutcome.ADDED)

    async def test_panel_outages_and_successes_do_not_burn_the_budget(self):
        self.platform_panel.fail_with = PasarGuardError("boom", status_code=502)
        for _ in range(si.MAX_FAILED_ATTEMPTS + 2):
            self.assertEqual(await self.refused(1, "tok-plat-0000001"), si.PANEL_UNAVAILABLE)
        self.platform_panel.fail_with = httpx.ConnectError("down")
        self.assertEqual(await self.refused(1, "tok-plat-0000001"), si.PANEL_UNAVAILABLE)
        self.platform_panel.fail_with = None
        self.assertEqual((await self.run_import(1, "tok-plat-0000001")).outcome, ImportOutcome.ADDED)

    async def test_unparsable_input_counts_as_a_failure(self):
        for _ in range(si.MAX_FAILED_ATTEMPTS):
            await self.refused(1, "not a link")
        self.assertEqual(await self.refused(1, "tok-plat-0000001"), si.RATE_LIMITED)

    async def test_the_log_keeps_a_hash_and_never_the_token(self):
        await self.run_import(1, "tok-plat-0000001")
        await self.refused(1, "tok-plat-nothere-1")
        attempts = await self.rows(ServiceImportAttempt)
        self.assertEqual(sorted(a.outcome for a in attempts), ["added", "refused"])
        for attempt in attempts:
            self.assertEqual(len(attempt.token_hash), 64)
            self.assertNotIn("tok-plat", attempt.token_hash)

    async def test_old_attempts_are_pruned_when_a_new_one_is_logged(self):
        await self.refused(1, "tok-plat-nothere-1")
        async with self.Session() as session:
            await session.execute(update(ServiceImportAttempt).values(created_at=datetime.now(timezone.utc) - timedelta(days=30)))
            await session.commit()
        await self.refused(1, "tok-plat-nothere-2")
        self.assertEqual(len(await self.rows(ServiceImportAttempt)), 1)


class TokenParsingTests(unittest.TestCase):
    def test_links_and_bare_tokens(self):
        self.assertEqual(parse_subscription_token("https://panel.example/sub/abc12345XYZ"), "abc12345XYZ")
        self.assertEqual(parse_subscription_token("  https://panel.example/sub/abc12345XYZ/info "), "abc12345XYZ")
        self.assertEqual(parse_subscription_token("abc12345XYZ"), "abc12345XYZ")

    def test_everything_else_is_rejected(self):
        for junk in (None, "", "   ", "short", "has space inside", "javascript:alert(1)", "../../etc/passwd",
                     "tok;DROP TABLE x", "a" * 3000, "https://panel.example/", "https://panel.example/sub/"):
            with self.subTest(junk=junk):
                self.assertIsNone(parse_subscription_token(junk))


class ExternalServiceTests(unittest.TestCase):
    def test_only_linked_and_imported_services_are_external(self):
        for remark, expected in (("linked", True), ("imported", True), ("order:5", False), (None, False), ("", False)):
            self.assertEqual(is_external_service(SimpleNamespace(remark=remark)), expected, remark)
        self.assertFalse(is_external_service(None))


class MigrationTests(unittest.TestCase):
    def test_migration_is_repeatable_and_reversible(self):
        import importlib.util

        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("import_migration", root / "alembic/versions/0051_service_import.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE bot_users (id INTEGER PRIMARY KEY)"))
            conn.execute(sa.text("CREATE TABLE user_services (id INTEGER PRIMARY KEY, pg_user_id INTEGER, remark TEXT)"))
            conn.execute(sa.text("INSERT INTO user_services VALUES (1, 5, 'order:1'), (2, 5, 'linked'), (3, 5, 'linked')"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                insp = sa.inspect(conn)
                self.assertIn("service_import_attempts", insp.get_table_names())
                self.assertIn("uq_user_services_imported_pg_user", {i["name"] for i in insp.get_indexes("user_services")})
                conn.execute(sa.text("INSERT INTO user_services VALUES (4, 5, 'imported')"))
                with self.assertRaises(sa.exc.IntegrityError):
                    conn.execute(sa.text("INSERT INTO user_services VALUES (5, 5, 'imported')"))
                migration.downgrade()
                self.assertNotIn("service_import_attempts", sa.inspect(conn).get_table_names())
                self.assertEqual(conn.execute(sa.text("SELECT COUNT(*) FROM user_services")).scalar(), 4)
                migration.upgrade()
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
