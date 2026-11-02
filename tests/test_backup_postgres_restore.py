"""PostgreSQL backups restore all or nothing, and refuse what this install cannot run.

Tests marked ``requires_pg`` use a disposable PostgreSQL database
(``PGCLOCK_TEST_PG_URL``) and are skipped when it is not reachable.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services import backup as backup_mod
from app.services import backup_postgres as pg_backup

PG_URL = os.environ.get(
    "PGCLOCK_TEST_PG_URL",
    "postgresql+asyncpg://pgclock:pgclock@127.0.0.1:5432/pgclock_mig",
)


def _pg_available() -> bool:
    try:
        from sqlalchemy import create_engine, text

        from app.db.engine_url import to_sync_url

        engine = create_engine(to_sync_url(PG_URL), connect_args={"connect_timeout": 3})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except Exception:  # noqa: BLE001 - any failure means the optional database is absent
        return False
    return pg_backup.find_tool("pg_dump") is not None and pg_backup.find_tool("pg_restore") is not None


requires_pg = pytest.mark.skipif(not _pg_available(), reason="PostgreSQL test DB unavailable")


def _sync_engine():
    from sqlalchemy import create_engine

    from app.db.engine_url import to_sync_url

    return create_engine(to_sync_url(PG_URL))


def _scalar(sql: str):
    from sqlalchemy import text

    engine = _sync_engine()
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql)).scalar()
    finally:
        engine.dispose()


def _execute(*statements: str) -> None:
    from sqlalchemy import text

    engine = _sync_engine()
    try:
        with engine.begin() as conn:
            for sql in statements:
                conn.execute(text(sql))
    finally:
        engine.dispose()


@pytest.fixture
def pg_install(tmp_path, monkeypatch):
    """A fresh schema at head plus isolated data/backup directories."""
    from app.db.alembic_runner import upgrade_head
    import app.config as config

    _execute("DROP SCHEMA public CASCADE", "CREATE SCHEMA public", "GRANT ALL ON SCHEMA public TO pgclock")
    monkeypatch.setenv("DATABASE_URL", PG_URL)
    config.get_settings.cache_clear()
    upgrade_head(PG_URL)
    data_dir = tmp_path / "data"
    (data_dir / "backups").mkdir(parents=True)
    (tmp_path / ".env").write_text(f'DATABASE_URL="{PG_URL}"\n', encoding="utf-8")
    monkeypatch.setattr(backup_mod, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(backup_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(backup_mod, "BACKUP_DIR", data_dir / "backups")
    monkeypatch.setattr(backup_mod, "RESTORE_STATUS_FILE", data_dir / "backup_restore.json")
    _execute("INSERT INTO settings (key, value) VALUES ('backup_probe', 'snapshot')")
    yield data_dir
    config.get_settings.cache_clear()


def _probe() -> str | None:
    return _scalar("SELECT value FROM settings WHERE key = 'backup_probe'")


def _restore(path: Path, **kwargs):
    return backup_mod.restore_backup(path, restore_env=False, restart=False, actor="test", **kwargs)


@requires_pg
def test_restore_brings_back_the_snapshot_and_records_the_schema(pg_install):
    created = backup_mod.create_backup(note="t", created_by="test")
    manifest = backup_mod.read_manifest(Path(created["path"]))
    assert manifest["alembic_revision"] == _scalar("SELECT version_num FROM alembic_version")
    _execute("UPDATE settings SET value = 'changed' WHERE key = 'backup_probe'")

    result = _restore(Path(created["path"]), safety_backup=True)

    assert result["ok"] is True, result
    assert _probe() == "snapshot"
    assert result["safety_id"]
    assert backup_mod.read_restore_status()["state"] == "done"


@requires_pg
def test_failing_restore_leaves_the_database_unchanged(pg_install):
    created = backup_mod.create_backup(note="t", created_by="test")
    # A live object depending on a backed-up table makes the clean step fail midway.
    _execute(
        "UPDATE settings SET value = 'live' WHERE key = 'backup_probe'",
        "CREATE VIEW settings_probe AS SELECT key FROM settings",
    )

    result = _restore(Path(created["path"]), safety_backup=False)

    assert result["ok"] is False
    assert result["error"] == pg_backup.RESTORE_FAILED_MESSAGE
    assert _probe() == "live"
    assert _scalar("SELECT count(*) FROM settings") >= 1
    status = backup_mod.read_restore_status()
    assert status["state"] == "error"
    assert "pg_restore" not in status["message"]


@requires_pg
def test_unreadable_dump_is_refused_before_any_change(pg_install):
    created = backup_mod.create_backup(note="t", created_by="test")
    broken = _with_replaced_dump(Path(created["path"]), os.urandom(4096))
    _execute("UPDATE settings SET value = 'live' WHERE key = 'backup_probe'")
    before = {p.name for p in backup_mod.BACKUP_DIR.glob("*.zip")}

    result = _restore(broken, safety_backup=True)

    assert result["ok"] is False
    assert result["error"] == pg_backup.UNREADABLE_MESSAGE
    assert _probe() == "live"
    assert {p.name for p in backup_mod.BACKUP_DIR.glob("*.zip")} == before, "no safety backup for a bad archive"


@requires_pg
def test_session_left_open_on_a_table_does_not_block_the_restore(pg_install):
    from sqlalchemy import text

    created = backup_mod.create_backup(note="t", created_by="test")
    _execute("UPDATE settings SET value = 'live' WHERE key = 'backup_probe'")
    engine = _sync_engine()
    holder = engine.connect()
    holder.begin()
    holder.execute(text("SELECT * FROM settings LIMIT 1"))
    try:
        with patch.object(pg_backup, "RESTORE_TIMEOUT_SEC", 60):
            result = _restore(Path(created["path"]), safety_backup=False)
    finally:
        holder.invalidate()
        engine.dispose()

    assert result["ok"] is True, result
    assert _probe() == "snapshot"


@requires_pg
def test_backup_from_a_newer_schema_is_refused(pg_install):
    created = backup_mod.create_backup(note="t", created_by="test")
    newer = _with_manifest(Path(created["path"]), alembic_revision="9999_from_the_future", app_version="99.0.0")
    _execute("UPDATE settings SET value = 'live' WHERE key = 'backup_probe'")

    result = _restore(newer, safety_backup=True)

    assert result["ok"] is False
    assert "99.0.0" in result["error"]
    assert _probe() == "live"


def _with_replaced_dump(path: Path, payload: bytes) -> Path:
    import hashlib
    import json
    import zipfile

    out = path.with_name(path.stem + "-broken.zip")
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(out, "w") as dst:
        manifest = json.loads(src.read(backup_mod.MANIFEST_NAME))
        for entry in manifest["files"]:
            if entry["path"] == backup_mod.POSTGRES_DUMP_MEMBER:
                entry["sha256"] = hashlib.sha256(payload).hexdigest()
        for item in src.infolist():
            if item.filename == backup_mod.MANIFEST_NAME:
                dst.writestr(item, json.dumps(manifest))
            elif item.filename == backup_mod.POSTGRES_DUMP_MEMBER:
                dst.writestr(item, payload)
            else:
                dst.writestr(item, src.read(item))
    return out


def _with_manifest(path: Path, **changes) -> Path:
    import json
    import zipfile

    out = path.with_name(path.stem + "-edited.zip")
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(out, "w") as dst:
        manifest = json.loads(src.read(backup_mod.MANIFEST_NAME))
        manifest.update(changes)
        for item in src.infolist():
            data = json.dumps(manifest).encode() if item.filename == backup_mod.MANIFEST_NAME else src.read(item)
            dst.writestr(item, data)
    return out


class TestVersionGuard:
    def test_known_revision_is_accepted(self):
        from app.db.alembic_runner import heads

        backup_mod.ensure_backup_not_newer({"alembic_revision": heads()[0], "app_version": "0.0.1"})

    def test_unknown_revision_is_refused(self):
        with pytest.raises(backup_mod.BackupRestoreRefused):
            backup_mod.ensure_backup_not_newer({"alembic_revision": "9999_unknown", "app_version": "1.0"})

    def test_old_archive_without_revision_uses_the_app_version(self):
        backup_mod.ensure_backup_not_newer({"app_version": "0.0.1"})
        with pytest.raises(backup_mod.BackupRestoreRefused):
            backup_mod.ensure_backup_not_newer({"app_version": "999.0.0"})

    def test_archive_without_any_version_is_allowed(self):
        backup_mod.ensure_backup_not_newer({})


class TestSafeMessages:
    def test_tool_output_maps_to_persian_causes(self):
        assert pg_backup._failure_message("pg_restore: error: unsupported version (1.16) in file header", "x") == pg_backup.VERSION_MESSAGE
        assert pg_backup._failure_message("ERROR:  canceling statement due to lock timeout", "x") == pg_backup.BUSY_MESSAGE
        assert pg_backup._failure_message("could not connect to 10.0.0.5", "default") == "default"
        assert pg_backup._failure_message("input file does not appear to be a valid archive", "x") == "x"

    def test_unexpected_errors_never_reach_the_admin(self):
        assert backup_mod._restore_error_text(OSError("/srv/pgclock/data: denied")) == backup_mod.RESTORE_FAILED_MESSAGE
        refused = backup_mod.BackupRestoreRefused("روشن")
        assert backup_mod._restore_error_text(refused) == "روشن"

    def test_password_is_passed_by_environment_not_argv(self, monkeypatch):
        import app.config as config

        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:s3cret@db.local:6543/shop")
        config.get_settings.cache_clear()
        try:
            args, env = pg_backup._connection()
        finally:
            config.get_settings.cache_clear()
        assert "s3cret" not in " ".join(args)
        assert env["PGPASSWORD"] == "s3cret"
        assert args[args.index("--port") + 1] == "6543"
