"""«افزودن سرویس قبلی»: allowed subscription paths and panel admins, link parsing, reasons, settings."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


from sqlalchemy import select

from app.db.models import ResellerProfile, ServiceImportAttempt, UserService
from app.services import service_import as si
from app.services import service_import_config as cfg
from app.services.service_import import (
    GENERIC_REFUSAL,
    INVALID_LINK,
    ImportOutcome,
    Reason,
    ServiceImportError,
    parse_subscription_link,
)
from app.services.users import set_settings_bulk
from tests.test_service_import import PLATFORM_ADMIN, SHOP_ADMIN, ImportCase
from tests.postgres_support import scoped_sync_engine


class ConfigCase(ImportCase):
    async def set_platform(self, **values: str) -> None:
        async with self.Session() as session:
            await set_settings_bulk(session, values)

    async def set_shop(self, shop_id: int = 5, **values: str) -> None:
        async with self.Session() as session:
            await set_settings_bulk(session, values, reseller_id=shop_id)

    async def reason_of(self, user_id: int, link: str, *, shop_id: int | None = None) -> Reason:
        with self.assertRaises(ServiceImportError) as caught:
            await self.run_import(user_id, link, shop_id=shop_id)
        return caught.exception.reason

    async def attempts(self) -> list[ServiceImportAttempt]:
        async with self.Session() as session:
            return list((await session.scalars(select(ServiceImportAttempt).order_by(ServiceImportAttempt.id))).all())


class SubscriptionPathTests(ConfigCase):
    async def test_the_panels_own_path_is_accepted_without_any_setting(self):
        self.assertEqual((await self.run_import(1, "https://p.example/sub/tok-plat-0000001")).outcome, ImportOutcome.ADDED)

    async def test_a_custom_panel_path_works_when_it_is_the_configured_one(self):
        self.platform_panel.sub_path = "/hamid"
        self.panel_settings.pg_subscription_path = "/hamid"
        self.assertEqual((await self.run_import(1, "https://p.example/hamid/tok-plat-0000001")).outcome, ImportOutcome.ADDED)

    async def test_a_listed_path_is_accepted_next_to_the_configured_one(self):
        await self.set_platform(service_import_paths="/hamid\n/other")
        self.platform_panel.sub_path = "/hamid"
        self.assertEqual((await self.run_import(1, "https://p.example/hamid/tok-plat-0000001")).outcome, ImportOutcome.ADDED)
        self.platform_panel.sub_path = "/sub"
        self.assertEqual((await self.run_import(2, "https://p.example/sub/tok-plat-0000002")).outcome, ImportOutcome.ADDED)

    async def test_a_path_that_is_not_listed_is_refused_before_the_panel_is_asked(self):
        self.platform_panel.sub_path = "/hamid"
        self.assertEqual(await self.reason_of(1, "https://p.example/hamid/tok-plat-0000001"), Reason.PATH_NOT_ALLOWED)
        self.assertEqual(self.platform_panel.info_calls, [])

    async def test_a_hostile_path_never_reaches_another_panel_route(self):
        for link in ("https://p.example/api/admins/tok-plat-0000001", "https://p.example/api/users/tok-plat-0000001"):
            self.assertEqual(await self.reason_of(1, link), Reason.PATH_NOT_ALLOWED)
        self.assertEqual(self.platform_panel.info_calls, [])

    async def test_a_bare_token_is_tried_on_every_allowed_path_and_only_not_found_moves_on(self):
        await self.set_platform(service_import_paths="/hamid")
        self.platform_panel.sub_path = "/hamid"
        self.assertEqual((await self.run_import(1, "tok-plat-0000001")).outcome, ImportOutcome.ADDED)
        self.assertEqual(self.platform_panel.info_calls, ["/sub/tok-plat-0000001", "/hamid/tok-plat-0000001"])

    async def test_a_panel_outage_on_the_first_path_does_not_try_the_next_one(self):
        await self.set_platform(service_import_paths="/hamid")
        self.platform_panel.fail_with = si.PasarGuardError("down", status_code=503)
        self.assertEqual(await self.reason_of(1, "tok-plat-0000001"), Reason.PANEL_ERROR)

    async def test_the_stored_link_uses_the_path_that_answered(self):
        await self.set_platform(service_import_paths="/hamid")
        self.platform_panel.sub_path = "/hamid"
        record = self.platform_users["tok-plat-0000001"]
        record["subscription_url"] = ""
        with patch.object(si, "absolutize_subscription_url", lambda value: f"https://p.example{value}"):
            result = await self.run_import(1, "tok-plat-0000001")
        self.assertEqual(result.service.subscription_url, "https://p.example/hamid/tok-plat-0000001")


class AllowedAdminTests(ConfigCase):
    async def test_the_admin_the_bot_is_signed_in_as_is_enough_even_if_the_username_setting_differs(self):
        self.panel_settings.pg_username = "someoneelse"
        self.assertEqual((await self.run_import(1, "tok-plat-0000001")).outcome, ImportOutcome.ADDED)

    async def test_a_service_of_an_unlisted_admin_is_refused_and_the_operator_learns_who_owns_it(self):
        self.platform_users["tok-plat-0000001"]["admin"] = {"username": "other-admin"}
        with self.assertRaises(ServiceImportError) as caught:
            await self.run_import(1, "tok-plat-0000001")
        self.assertEqual(caught.exception.reason, Reason.ADMIN_NOT_ALLOWED)
        self.assertEqual(caught.exception.detail, {"owner_admin": "other-admin"})
        self.assertEqual(str(caught.exception), GENERIC_REFUSAL)

    async def test_listing_the_admin_makes_the_service_importable(self):
        self.platform_users["tok-plat-0000001"]["admin"] = {"username": "Other-Admin"}
        await self.set_platform(service_import_admins="other-admin")
        self.assertEqual((await self.run_import(1, "tok-plat-0000001")).outcome, ImportOutcome.ADDED)

    async def test_a_shops_admin_can_never_be_imported_by_the_platform_even_when_listed(self):
        await self.set_platform(service_import_admins=SHOP_ADMIN)
        self.assertEqual(await self.reason_of(1, "tok-shop-0000001"), Reason.ADMIN_NOT_ALLOWED)

    async def test_a_shop_imports_only_from_its_own_admin_and_ignores_every_list(self):
        await self.set_shop(service_import_admins=PLATFORM_ADMIN, service_import_paths="/x")
        await self.set_platform(service_import_admins=PLATFORM_ADMIN)
        # The shop's scoped panel client cannot even read the platform's user.
        self.assertEqual(await self.reason_of(6, "tok-plat-0000001", shop_id=5), Reason.NOT_FOUND)
        self.assertEqual((await self.run_import(6, "tok-shop-0000001", shop_id=5)).outcome, ImportOutcome.ADDED)

    async def test_a_shop_without_a_panel_admin_cannot_import_anything(self):
        async with self.Session() as session:
            profile = await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == 5))
            profile.pg_admin_username = None
            await session.commit()
        self.assertEqual(await self.reason_of(6, "tok-shop-0000001", shop_id=5), Reason.IDENTITY_UNKNOWN)

    async def test_the_signed_in_admin_is_cached_briefly_and_refreshable(self) -> None:
        calls = 0
        original = self.platform_panel.get_current_admin

        async def counting() -> dict[str, object]:
            nonlocal calls
            calls += 1
            return await original()

        self.platform_panel.get_current_admin = counting
        clock = SimpleNamespace(monotonic=Mock(return_value=1000.0))
        with patch.object(si, "time", clock):
            await self.run_import(1, "tok-plat-0000001")
            await self.run_import(2, "tok-plat-0000002")
            self.assertEqual(calls, 1)
            si.clear_identity_cache()
            await self.run_import(1, "tok-plat-0000001")
            self.assertEqual(calls, 2)
            clock.monotonic.return_value += si.IDENTITY_TTL_SECONDS
            await self.run_import(1, "tok-plat-0000001")
            self.assertEqual(calls, 3)


class LinkParsingTests(ConfigCase):
    def test_a_link_inside_a_message_is_found_and_trailing_punctuation_is_dropped(self):
        for raw in (
            "https://p.example/sub/tok-plat-0000001",
            "لینک من: https://p.example/sub/tok-plat-0000001 ممنون",
            "(https://p.example/sub/tok-plat-0000001).",
            "https://p.example:8443/sub/tok-plat-0000001/",
            "https://p.example/sub/tok-plat-0000001/info",
        ):
            with self.subTest(raw=raw):
                link = parse_subscription_link(raw)
                self.assertEqual((link.prefix, link.token), ("/sub", "tok-plat-0000001"))

    def test_the_path_of_the_link_is_kept_and_a_bare_token_has_none(self):
        self.assertEqual(parse_subscription_link("https://p.example/hamid/tok-plat-0000001").prefix, "/hamid")
        self.assertIsNone(parse_subscription_link("tok-plat-0000001").prefix)

    def test_everything_else_is_not_a_link(self):
        for raw in ("", "   ", "سلام", "https://p.example/", "https://p.example/sub", "javascript:alert(1)", "a" * 3000,
                    "tok with spaces", "https://p.example/sub/short"):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_subscription_link(raw))

    async def test_a_malformed_message_gets_its_own_safe_message_and_is_logged_as_invalid(self):
        self.assertEqual(await self.refused(1, "سلام این لینک من است"), INVALID_LINK)
        rows = await self.attempts()
        self.assertEqual([(r.outcome, r.reason) for r in rows], [("invalid", "invalid_link")])
        self.assertEqual(self.platform_panel.info_calls, [])


class AttemptReasonTests(ConfigCase):
    async def test_every_refusal_keeps_its_reason_for_the_operator_and_a_success_none(self):
        await self.run_import(1, "tok-plat-0000001")
        await self.refused(2, "tok-plat-0000001")  # already attached to customer 1
        await self.refused(2, "tok-nope-0000000009")
        self.platform_users["tok-plat-0000002"]["admin"] = {"username": "other"}
        await self.refused(2, "tok-plat-0000002")
        rows = await self.attempts()
        self.assertEqual(
            [(r.outcome, r.reason) for r in rows],
            [("added", None), ("refused", "already_taken"), ("refused", "not_found"), ("refused", "admin_not_allowed")],
        )

    async def test_customers_never_see_the_reason_only_the_generic_message(self):
        self.platform_users["tok-plat-0000001"]["admin"] = {"username": "other"}
        for link in ("tok-plat-0000001", "tok-nope-0000000009", "https://p.example/api/x/tok-plat-0000002"):
            self.assertEqual(await self.refused(1, link), GENERIC_REFUSAL)

    async def test_an_attached_service_is_not_exposed_through_a_different_message(self):
        await self.run_import(1, "tok-plat-0000001")
        taken = await self.refused(2, "tok-plat-0000001")
        missing = await self.refused(2, "tok-nope-0000000009")
        self.assertEqual(taken, missing)
        async with self.Session() as session:
            self.assertEqual(len((await session.scalars(select(UserService))).all()), 1)


class SettingNormalizationTests(unittest.TestCase):
    def test_paths_are_canonical_distinct_and_hostile_entries_are_dropped(self):
        raw = "/sub/, hamid\n/Sub;/a/b  https://evil.example/x ../etc /q?x=1 /" + "/a" * 80
        self.assertEqual(cfg.normalize_paths(raw), ["/sub", "/hamid", "/Sub", "/a/b"])

    def test_admins_are_lower_case_distinct_and_valid(self):
        self.assertEqual(
            cfg.normalize_admins("Admin1, admin1\nbob.smith  bad name! -x ../y x" + "y" * 80),
            ["admin1", "bob.smith", "bad"],
        )

    def test_lists_are_capped(self):
        self.assertEqual(len(cfg.normalize_admins(",".join(f"a{i}" for i in range(50)))), cfg.MAX_LIST_ITEMS)
        self.assertEqual(len(cfg.normalize_paths(",".join(f"/p{i}" for i in range(50)))), cfg.MAX_LIST_ITEMS)

    def test_the_cap_is_clamped_and_garbage_falls_back_to_the_default(self):
        huge = str(cfg.MAX_PER_USER_LIMIT + 1)
        for raw, expected in (
            ("3", 3), ("۷", 7), ("0", 1), ("-4", 1), ("21", 21), ("999", 999), (huge, cfg.MAX_PER_USER_LIMIT),
            ("x", 5), ("", 5), (None, 5), ("2.5", 5), (True, 5),
            ("unlimited", None), ("نامحدود", None),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(cfg.normalize_max_per_user(raw), expected)

    def test_only_the_unlimited_value_reads_as_unlimited(self):
        self.assertTrue(cfg.read_settings({cfg.KEY_MAX_PER_USER: cfg.UNLIMITED_VALUE}).is_unlimited)
        self.assertFalse(cfg.read_settings({cfg.KEY_MAX_PER_USER: "0"}).is_unlimited)
        self.assertFalse(cfg.read_settings({cfg.KEY_MAX_PER_USER: "20"}).is_unlimited)
        self.assertFalse(cfg.read_settings({}).is_unlimited)

    def test_stored_values_are_normalised_by_key(self):
        self.assertEqual(cfg.normalize_setting_value(cfg.KEY_PATHS, "sub,/hamid/"), "/sub\n/hamid")
        self.assertEqual(cfg.normalize_setting_value(cfg.KEY_ADMINS, "A, b"), "a\nb")
        self.assertEqual(cfg.normalize_setting_value(cfg.KEY_MAX_PER_USER, "50"), "50")
        self.assertEqual(cfg.normalize_setting_value(cfg.KEY_MAX_PER_USER, "0"), "1")
        # The web field's «نامحدود» button posts an empty value.
        self.assertEqual(cfg.normalize_setting_value(cfg.KEY_MAX_PER_USER, ""), cfg.UNLIMITED_VALUE)
        self.assertEqual(cfg.normalize_setting_value(cfg.KEY_MAX_PER_USER, "نامحدود"), cfg.UNLIMITED_VALUE)
        self.assertEqual(cfg.normalize_setting_value("service_import_enabled", "1"), "1")

    def test_defaults_when_nothing_is_stored(self):
        config = cfg.read_settings({})
        self.assertEqual((config.enabled, config.max_per_user, config.extra_paths, config.extra_admins), (False, 5, (), ()))


class ReasonMigrationTests(unittest.TestCase):
    def test_the_reason_column_is_added_twice_safely_and_removed_without_losing_rows(self):
        import importlib.util
        from pathlib import Path

        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("reason_migration", root / "alembic/versions/0052_service_import_reason.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = scoped_sync_engine()
        with engine.begin() as conn:
            conn.execute(sa.text(
                "CREATE TABLE service_import_attempts (id INTEGER PRIMARY KEY, bot_user_id INTEGER NOT NULL, "
                "token_hash VARCHAR(64) NOT NULL, outcome VARCHAR(24) NOT NULL, created_at TIMESTAMPTZ)"
            ))
            conn.execute(sa.text("INSERT INTO service_import_attempts VALUES (1, 1, 'h', 'refused', NULL)"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                self.assertIn("reason", {c["name"] for c in sa.inspect(conn).get_columns("service_import_attempts")})
                conn.execute(sa.text("UPDATE service_import_attempts SET reason = 'not_found'"))
                migration.downgrade()
                self.assertNotIn("reason", {c["name"] for c in sa.inspect(conn).get_columns("service_import_attempts")})
                self.assertEqual(conn.execute(sa.text("SELECT COUNT(*) FROM service_import_attempts")).scalar(), 1)
                migration.upgrade()
        engine.dispose()

    def test_a_database_without_the_table_is_left_alone(self):
        import importlib.util
        from pathlib import Path

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("reason_migration2", root / "alembic/versions/0052_service_import_reason.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = scoped_sync_engine()
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            migration.downgrade()
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
