"""Phase A — Alembic on PostgreSQL and PostgreSQL backups."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.db.engine_url import to_sync_url
from tests.postgres_support import scoped_database_url


def test_engine_url_helpers():
    from app.db.engine_url import normalize_async_url, parse_engine, to_sync_url

    assert normalize_async_url("postgresql://u:p@localhost/db").startswith("postgresql+asyncpg://")
    info = parse_engine("postgresql://u:p@db.local:6543/shop")
    assert info.dialect == "postgresql" and info.pg_dsn == "postgresql://u:p@db.local:6543/shop"
    with pytest.raises(ValueError) as refused:
        parse_engine("mysql://root:s3cret@db/shop")
    assert "s3cret" not in str(refused.value)
    sync = to_sync_url("postgresql+asyncpg://u:p@127.0.0.1:5432/db")
    assert sync.startswith("postgresql+psycopg://")


def test_alembic_upgrade_and_downgrade_postgres():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from app.db.alembic_runner import ALEMBIC_DIR, ALEMBIC_INI, current_revision, downgrade_one, upgrade_head

    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    head = ScriptDirectory.from_config(config).get_current_head()
    url = scoped_database_url(tables=False)

    upgrade_head(url)
    assert current_revision(url) == head

    eng = create_engine(to_sync_url(url))
    try:
        with eng.connect() as conn:
            tables = set(conn.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname='public'")))
            assert {"bot_users", "reseller_profiles", "pg_staff_access", "alembic_version"} <= tables
            indexes = set(conn.scalars(text("SELECT indexname FROM pg_indexes WHERE tablename='wallet_transactions'")))
            assert "uq_wallet_referral_reason" in indexes
    finally:
        eng.dispose()

    downgrade_one(url)
    assert current_revision(url) not in (None, head)


def test_engine_aware_postgres_backup(tmp_path, monkeypatch):
    from app.db.alembic_runner import upgrade_head
    from app.services import backup as backup_mod

    url = scoped_database_url(tables=False)
    upgrade_head(url)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "backups").mkdir()
    (tmp_path / ".env").write_text(f'DATABASE_URL="{url}"\n', encoding="utf-8")

    monkeypatch.setenv("DATABASE_URL", url)
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
