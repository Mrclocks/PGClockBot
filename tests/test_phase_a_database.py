"""Phase A — Alembic and engine-aware backup."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

PG_URL = os.environ.get(
    "PGCLOCK_TEST_PG_URL",
    "postgresql+asyncpg://pgclock:pgclock@127.0.0.1:5432/pgclock_mig",
)
PG_AVAILABLE = False
try:
    from sqlalchemy import create_engine, text

    from app.db.engine_url import to_sync_url

    _e = create_engine(to_sync_url(PG_URL))
    with _e.connect() as c:
        c.execute(text("SELECT 1"))
    _e.dispose()
    PG_AVAILABLE = True
except Exception:
    PG_AVAILABLE = False

requires_pg = pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL test DB unavailable")


def _seed_sqlite(db_path: Path) -> str:
    from sqlalchemy import create_engine, text

    from app.db import Base
    import app.db.models  # noqa: F401

    url = f"sqlite+aiosqlite:///{db_path}"
    eng = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO bot_users (id, telegram_id, role, wallet_balance, points_balance, referral_code, is_blocked) "
                "VALUES (1, 42, 'admin', 1500, 0, 'ref42', 0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO bot_users (id, telegram_id, role, wallet_balance, points_balance, referral_code, is_blocked) "
                "VALUES (2, 43, 'reseller', 200, 0, 'ref43', 0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO reseller_plans (id, name, price, "
                "can_approve_receipts, create_pg_admin, create_web_access, share_pg_panel_url, "
                "is_active, sort_order, billing_mode, plan_kind, duration_days, included_gb, "
                "included_users, addon_gb, addon_users, renew_pricing_mode, price_per_gb, "
                "allow_buy_extra, extra_gb_price, extra_user_price, renew_price, is_system) "
                "VALUES (1, 'Starter', 0, 0, 1, 1, 0, 1, 0, 'fixed', 'subscription', "
                "0, 0, 0, 0, 0, 'fixed', 0, 0, 0, 0, 0, 0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO plans (id, name, price, duration_days, is_active, is_trial, sort_order) "
                "VALUES (1, 'Demo', 10000, 30, 1, 0, 0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO reseller_profiles "
                "(id, user_id, can_approve_receipts, is_active, "
                "share_pg_panel_url, billing_mode, billing_balance, billing_watermark_bytes, "
                "payg_wallet_linked) "
                "VALUES (1, 2, 0, 1, 0, 'fixed', 777, 0, 0)"
            )
        )
        conn.execute(text("INSERT INTO settings (key, value) VALUES ('currency', 'Toman')"))
        conn.execute(
            text(
                "INSERT INTO wallet_transactions (user_id, amount, balance_after, reason) "
                "VALUES (1, 1500, 1500, 'seed')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO wallet_transactions (user_id, amount, balance_after, reason) "
                "VALUES (1, 100, 1600, 'referral:2')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO pg_staff_access (pg_username, web_username, web_password_hash, is_active) "
                "VALUES ('pgadmin1', 'staff1', '$2b$12$abc', 1)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO orders (user_id, plan_id, amount, discount_amount, quantity, status) "
                "VALUES (1, 1, 10000, 0, 1, 'pending')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO payments (order_id, user_id, amount, method, status, is_wallet_topup) "
                "VALUES (1, 1, 10000, 'card', 'pending', 0)"
            )
        )
        # referral partial unique index
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_wallet_referral_reason "
                "ON wallet_transactions (user_id, reason) WHERE reason LIKE 'referral:%'"
            )
        )
    eng.dispose()
    return url


def test_engine_url_helpers():
    from app.db.engine_url import normalize_async_url, parse_engine, to_sync_url

    assert normalize_async_url("postgresql://u:p@localhost/db").startswith("postgresql+asyncpg://")
    info = parse_engine("sqlite+aiosqlite:////tmp/x.db")
    assert info.is_sqlite and info.sqlite_path == Path("/tmp/x.db")
    sync = to_sync_url("postgresql+asyncpg://u:p@127.0.0.1:5432/db")
    assert sync.startswith("postgresql+psycopg://")


@requires_pg
def test_alembic_upgrade_and_downgrade_postgres():
    from app.db.alembic_runner import current_revision, downgrade_one, upgrade_head
    from app.db.engine_url import to_sync_url
    from sqlalchemy import create_engine, text

    # Reset public schema
    eng = create_engine(to_sync_url(PG_URL))
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        conn.execute(text("GRANT ALL ON SCHEMA public TO pgclock"))
        conn.execute(text("GRANT ALL ON SCHEMA public TO public"))
    eng.dispose()

    upgrade_head(PG_URL)
    assert current_revision(PG_URL) == "0001_baseline"

    eng = create_engine(to_sync_url(PG_URL))
    with eng.connect() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
            )
        }
        assert "bot_users" in tables
        assert "reseller_profiles" in tables
        assert "pg_staff_access" in tables
        assert "alembic_version" in tables
        idx = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE tablename='wallet_transactions'"
                )
            )
        }
        assert "uq_wallet_referral_reason" in idx
    eng.dispose()

    downgrade_one(PG_URL)
    assert current_revision(PG_URL) is None


def test_engine_aware_sqlite_backup_roundtrip(tmp_path, monkeypatch):
    from app.services import backup as backup_mod

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "backups").mkdir()
    db_path = data_dir / "bot.db"
    source = _seed_sqlite(db_path)
    (tmp_path / ".env").write_text(f'DATABASE_URL="{source}"\n', encoding="utf-8")
    (data_dir / "web_admin.json").write_text('{"username":"admin"}', encoding="utf-8")

    monkeypatch.setenv("DATABASE_URL", source)
    import app.config as config

    config.get_settings.cache_clear()
    monkeypatch.setattr(config, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(backup_mod, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(backup_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(backup_mod, "BACKUP_DIR", data_dir / "backups")

    created = backup_mod.create_backup(note="phase-a-test", include_env=True, created_by="test")
    assert created["ok"]
    assert created["db_engine"] == "sqlite"
    zip_path = Path(created["path"])
    ok, err, manifest = backup_mod.validate_backup_archive(zip_path)
    assert ok, err
    assert manifest["db_engine"] == "sqlite"

    # Mismatch: pretend live engine is postgresql
    monkeypatch.setattr(backup_mod, "current_db_engine", lambda: "postgresql")
    restored = backup_mod.restore_backup(
        zip_path, restore_env=False, safety_backup=False, restart=False, actor="test"
    )
    assert restored["ok"] is False
    assert "موتور" in restored["error"] or "engine" in restored["error"].lower() or "postgresql" in restored["error"]

    config.get_settings.cache_clear()


@requires_pg
def test_engine_aware_postgres_backup(tmp_path, monkeypatch):
    from app.db.alembic_runner import upgrade_head
    from app.db.engine_url import to_sync_url
    from app.services import backup as backup_mod
    from sqlalchemy import create_engine, text

    eng = create_engine(to_sync_url(PG_URL))
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        conn.execute(text("GRANT ALL ON SCHEMA public TO pgclock"))
    eng.dispose()
    upgrade_head(PG_URL)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "backups").mkdir()
    (tmp_path / ".env").write_text(f'DATABASE_URL="{PG_URL}"\n', encoding="utf-8")

    monkeypatch.setenv("DATABASE_URL", PG_URL)
    import app.config as config

    config.get_settings.cache_clear()
    monkeypatch.setattr(config, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(backup_mod, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(backup_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(backup_mod, "BACKUP_DIR", data_dir / "backups")

    created = backup_mod.create_backup(note="pg-bak", include_env=False, created_by="test")
    assert created["ok"]
    assert created["db_engine"] == "postgresql"
    ok, err, manifest = backup_mod.validate_backup_archive(Path(created["path"]))
    assert ok, err
    assert manifest["db_engine"] == "postgresql"
    assert any(f.get("path") == "data/postgres.dump" for f in manifest["files"])

    config.get_settings.cache_clear()
