"""Phase 6A — production-readiness fixes (Owner bootstrap, index, secrets)."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import Base
import app.db.models  # noqa: F401
from app.db.models import BotUser, OrgPrincipal, ResellerProfile, Role
from app.services.org_principals import (
    OrgPrincipalError,
    bind_reseller_profile_principal,
    ensure_owner_principal,
)
from tests.postgres_support import scoped_async_engine, scoped_database_url, scoped_sync_engine


def _plans_ddl(conn) -> None:
    # The legacy-migrator tests below still run on SQLite until that migrator is removed.
    if conn.dialect.name == "sqlite":
        id_column, true, false = "id INTEGER PRIMARY KEY AUTOINCREMENT", "1", "0"
    else:
        id_column, true, false = "id SERIAL PRIMARY KEY", "true", "false"
    conn.execute(
        text(
            f"""
            CREATE TABLE plans (
                {id_column},
                name VARCHAR(128),
                price INTEGER,
                duration_days INTEGER DEFAULT 30,
                is_active BOOLEAN DEFAULT {true},
                is_trial BOOLEAN DEFAULT {false},
                sort_order INTEGER DEFAULT 0
            )
            """
        )
    )


class OwnerBootstrapTests(unittest.IsolatedAsyncioTestCase):
    def test_active_owner_not_duplicated(self) -> None:
        from app.db.session import _migrate_sqlite_legacy, _seed_owner_if_no_depth0

        eng = create_engine("sqlite:///:memory:")  # SQLite-only legacy migrator; removed with it
        with eng.begin() as conn:
            _plans_ddl(conn)
            conn.execute(
                text(
                    "CREATE TABLE org_principals ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "parent_id INTEGER, depth INTEGER NOT NULL, "
                    "status VARCHAR(32) DEFAULT 'active' NOT NULL, "
                    "pg_username VARCHAR(128), pg_password_enc TEXT, "
                    "reseller_profile_id INTEGER, pg_staff_id INTEGER, "
                    "bot_user_id INTEGER)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO org_principals "
                    "(parent_id, depth, status) VALUES (NULL, 0, 'active')"
                )
            )
            _seed_owner_if_no_depth0(conn)
            _migrate_sqlite_legacy(conn)
            n = conn.execute(
                text("SELECT COUNT(1) FROM org_principals WHERE depth = 0")
            ).scalar()
            self.assertEqual(n, 1)
            status = conn.execute(
                text("SELECT status FROM org_principals WHERE depth = 0")
            ).scalar()
            self.assertEqual(status, "active")
        eng.dispose()

    def test_disabled_owner_not_replaced(self) -> None:
        from app.db.session import _migrate_sqlite_legacy, _seed_owner_if_no_depth0

        eng = create_engine("sqlite:///:memory:")  # SQLite-only legacy migrator; removed with it
        with eng.begin() as conn:
            _plans_ddl(conn)
            conn.execute(
                text(
                    "CREATE TABLE org_principals ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "parent_id INTEGER, depth INTEGER NOT NULL, "
                    "status VARCHAR(32) DEFAULT 'active' NOT NULL, "
                    "pg_username VARCHAR(128), pg_password_enc TEXT, "
                    "reseller_profile_id INTEGER, pg_staff_id INTEGER, "
                    "bot_user_id INTEGER)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO org_principals "
                    "(parent_id, depth, status) VALUES (NULL, 0, 'disabled')"
                )
            )
            _seed_owner_if_no_depth0(conn)
            _migrate_sqlite_legacy(conn)
            rows = conn.execute(
                text("SELECT id, status FROM org_principals WHERE depth = 0")
            ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][1], "disabled")
        eng.dispose()

    def test_zero_owner_rows_seeds_exactly_one(self) -> None:
        from app.db.session import _migrate_sqlite_legacy

        eng = create_engine("sqlite:///:memory:")  # SQLite-only legacy migrator; removed with it
        with eng.begin() as conn:
            _plans_ddl(conn)
            _migrate_sqlite_legacy(conn)
            rows = conn.execute(
                text(
                    "SELECT depth, parent_id, status FROM org_principals "
                    "WHERE depth = 0"
                )
            ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertIsNone(rows[0][1])
            self.assertEqual(rows[0][2], "active")
        eng.dispose()

    async def test_duplicate_depth0_still_fail_closed(self) -> None:
        engine = scoped_async_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with Session() as session:
            session.add(
                OrgPrincipal(parent_id=None, depth=0, status="active")
            )
            await session.commit()
            session.add(
                OrgPrincipal(parent_id=None, depth=0, status="active")
            )
            with self.assertRaises(IntegrityError):
                await session.flush()
            await session.rollback()
            n = (
                await session.execute(
                    select(OrgPrincipal).where(OrgPrincipal.depth == 0)
                )
            ).scalars().all()
            self.assertEqual(len(n), 1)
            n[0].status = "disabled"
            await session.commit()
            with self.assertRaises(OrgPrincipalError):
                await ensure_owner_principal(session)
        await engine.dispose()


class BotUserIdUniqueIndexTests(unittest.TestCase):
    def test_alembic_head_is_single_and_descends_from_0018(self) -> None:
        # The exact head migration moves forward over time (0019, 0020, ...);
        # what this test actually guards against is a *branched* migration
        # history (multiple heads) and the org-principal unique-index
        # migration (0018) being dropped or reordered out of the chain.
        from app.db.alembic_runner import alembic_config, heads
        from alembic.script import ScriptDirectory

        current_heads = heads()
        self.assertEqual(len(current_heads), 1, f"expected a single alembic head, got {current_heads}")

        script = ScriptDirectory.from_config(alembic_config())
        ancestor_revisions = {
            rev.revision for rev in script.iterate_revisions(current_heads[0], None)
        }
        self.assertIn("0018_org_principal_single_owner", ancestor_revisions)

    def test_migration_creates_partial_unique_index(self) -> None:
        from app.db.alembic_runner import upgrade_head

        upgrade_head(scoped_database_url(tables=False))
        sync_eng = scoped_sync_engine()
        try:
            insp = inspect(sync_eng)
            names = {ix["name"] for ix in insp.get_indexes("org_principals")}
            self.assertIn("uq_org_principals_bot_user_id", names)
            with sync_eng.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO org_principals (parent_id, depth, status, bot_user_id) "
                        "VALUES (NULL, 0, 'active', NULL)"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO org_principals (parent_id, depth, status, bot_user_id) "
                        "VALUES (NULL, 1, 'active', NULL)"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO bot_users (telegram_id, role, wallet_balance, "
                        "points_balance, referral_code, is_blocked) "
                        "VALUES (9001, 'user', 0, 0, 'r9001', false)"
                    )
                )
                uid = conn.execute(text("SELECT id FROM bot_users WHERE telegram_id=9001")).scalar()
                conn.execute(
                    text(
                        "INSERT INTO org_principals (parent_id, depth, status, bot_user_id) "
                        "VALUES (1, 1, 'active', :uid)"
                    ),
                    {"uid": uid},
                )
                with self.assertRaises(IntegrityError), conn.begin_nested():
                    conn.execute(
                        text(
                            "INSERT INTO org_principals (parent_id, depth, status, bot_user_id) "
                            "VALUES (1, 1, 'active', :uid)"
                        ),
                        {"uid": uid},
                    )
        finally:
            sync_eng.dispose()

    def test_duplicate_non_null_fails_index_create(self) -> None:
        index_sql = (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_org_principals_bot_user_id "
            "ON org_principals (bot_user_id) "
            "WHERE bot_user_id IS NOT NULL"
        )

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "dup.db"
            url = f"sqlite:///{db_path}"
            eng = create_engine(url)
            try:
                Base.metadata.create_all(eng)
                with eng.begin() as conn:
                    conn.execute(text("DROP INDEX IF EXISTS uq_org_principals_bot_user_id"))
                    conn.execute(
                        text(
                            "INSERT INTO bot_users (telegram_id, role, wallet_balance, "
                            "points_balance, referral_code, is_blocked) "
                            "VALUES (8001, 'user', 0, 0, 'r8001', 0)"
                        )
                    )
                    uid = conn.execute(
                        text("SELECT id FROM bot_users WHERE telegram_id=8001")
                    ).scalar()
                    conn.execute(
                        text(
                            "INSERT INTO org_principals (parent_id, depth, status, bot_user_id) "
                            "VALUES (NULL, 0, 'active', NULL)"
                        )
                    )
                    conn.execute(
                        text(
                            "INSERT INTO org_principals (parent_id, depth, status, bot_user_id) "
                            "VALUES (1, 1, 'active', :uid)"
                        ),
                        {"uid": uid},
                    )
                    conn.execute(
                        text(
                            "INSERT INTO org_principals (parent_id, depth, status, bot_user_id) "
                            "VALUES (1, 1, 'active', :uid)"
                        ),
                        {"uid": uid},
                    )
                    with self.assertRaises(IntegrityError), conn.begin_nested():
                        conn.execute(text(index_sql))
            finally:
                eng.dispose()


class PreAlembicStampTests(unittest.TestCase):
    def test_incomplete_schema_refuses_stamp(self) -> None:
        from app.db.session import _assert_ready_to_stamp_head, _missing_alembic_head_schema

        eng = scoped_sync_engine()
        with eng.begin() as conn:
            _plans_ddl(conn)
            missing = _missing_alembic_head_schema(conn)
            self.assertIn("org_principals", missing)
            with self.assertRaises(RuntimeError) as ctx:
                _assert_ready_to_stamp_head(conn)
            self.assertIn("Refusing to stamp", str(ctx.exception))
            self.assertIn("org_principals", str(ctx.exception))
        eng.dispose()

    def test_legacy_migrator_makes_schema_stampable(self) -> None:
        from app.db.session import (
            _assert_ready_to_stamp_head,
            _migrate_sqlite_legacy,
            _missing_alembic_head_schema,
        )

        eng = create_engine("sqlite:///:memory:")  # SQLite-only legacy migrator; removed with it
        with eng.begin() as conn:
            _plans_ddl(conn)
            _migrate_sqlite_legacy(conn)
            missing = _missing_alembic_head_schema(conn)
            self.assertEqual(missing, [])
            _assert_ready_to_stamp_head(conn)
            cols = {c["name"] for c in inspect(conn).get_columns("org_principals")}
            self.assertIn("pg_password_enc", cols)
        eng.dispose()


class WebSecretTests(unittest.TestCase):
    def test_external_secret_allowed_when_env_unwritable(self) -> None:
        from app.services import setup_wizard

        token = "external-container-secret-aaaa-bbbb"
        with (
            patch.dict(os.environ, {"WEB_SECRET": token}, clear=False),
            patch.object(setup_wizard, "update_env_keys") as writer,
        ):
            got = setup_wizard.ensure_web_secret()
        self.assertEqual(got, token)
        writer.assert_not_called()

    def test_generated_unpersisted_secret_fails_closed(self) -> None:
        from app.services import setup_wizard

        distinctive = "generated-must-not-leak-zzzzzzzz"
        with (
            patch.object(setup_wizard, "_existing_valid_web_secret", return_value=""),
            patch.object(setup_wizard.secrets, "token_hex", return_value=distinctive),
            patch.object(
                setup_wizard, "update_env_keys", side_effect=OSError("read-only")
            ),
        ):
            with self.assertRaises(setup_wizard.WebSecretPersistenceError) as ctx:
                setup_wizard.ensure_web_secret()
        msg = str(ctx.exception)
        self.assertNotIn(distinctive, msg)
        self.assertNotIn("change-me", msg)
        self.assertIn("WEB_SECRET", msg)

    def test_placeholder_never_accepted(self) -> None:
        from app.services import setup_wizard

        self.assertFalse(setup_wizard._is_usable_web_secret("change-me"))
        self.assertFalse(setup_wizard._is_usable_web_secret(""))
        self.assertTrue(setup_wizard._is_usable_web_secret("a" * 32))


class AdapterCredentialPathTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_adapter_bind_does_not_copy_ciphertext(self) -> None:
        from app.services.pasarguard import PasarGuardError, get_pg_for_principal

        async with self.Session() as session:
            await ensure_owner_principal(session)
            user = BotUser(
                telegram_id=7101,
                role=Role.RESELLER.value,
                referral_code="adp1",
            )
            session.add(user)
            await session.flush()
            profile = ResellerProfile(
                user_id=user.id,
                is_active=True,
                web_username="shop_adp",
                web_password_hash="h" * 24,
                setup_completed_at=datetime.now(timezone.utc),
                pg_admin_username="pg_shop_adp",
                pg_admin_password_enc="gAAAA-not-copied-ciphertext",
            )
            session.add(profile)
            await session.flush()
            principal = await bind_reseller_profile_principal(session, profile)
            await session.commit()
            self.assertIsNotNone(principal)
            assert principal is not None
            self.assertEqual(int(principal.depth), 1)
            self.assertTrue(profile.pg_admin_password_enc)
            self.assertFalse((principal.pg_password_enc or "").strip())
            with self.assertRaises(PasarGuardError):
                await get_pg_for_principal(session, principal_id=int(principal.id))
