"""Restoring .env keeps this server's own settings and warns about unreadable secrets."""

from __future__ import annotations

import sqlite3
import stat
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from app.services import backup as backup_mod
from app.services import backup_env
from app.services.secret_box import _hkdf_fernet_key, _legacy_fernet_key, decrypts_with_secret

from tests import test_backup_postgres_restore as pg_case

PG_URL = pg_case.PG_URL
requires_pg = pg_case.requires_pg
# Shared PostgreSQL fixture: a fresh schema plus isolated data and backup directories.
pg_install = pg_case.pg_install

OLD_SERVER_ENV = """# PGClockBot
BOT_TOKEN=111:old-token
WEB_SECRET="old-server-secret"
DATABASE_URL=postgresql+asyncpg://pgclock:OLDPASS@127.0.0.1:5432/pgclock
WEB_PORT=9000
export PUBLIC_BASE_URL=https://panel.example
"""
NEW_SERVER_ENV = """BOT_TOKEN=222:new-token
WEB_SECRET=new-server-secret
DATABASE_URL=postgresql+asyncpg://pgclock:NEWPASS@127.0.0.1:5432/pgclock
WEB_PORT=8443
WEB_HOST=0.0.0.0
DEPLOYED_CHANNEL=dev
"""


class TestMergeRestoredEnv:
    def test_server_keys_stay_and_the_rest_comes_from_the_backup(self):
        merged = backup_env.merge_restored_env(OLD_SERVER_ENV, NEW_SERVER_ENV)
        assert backup_env.env_value(merged, "DATABASE_URL").endswith("NEWPASS@127.0.0.1:5432/pgclock")
        assert backup_env.env_value(merged, "WEB_PORT") == "8443"
        assert backup_env.env_value(merged, "WEB_SECRET") == "old-server-secret"
        assert backup_env.env_value(merged, "BOT_TOKEN") == "111:old-token"
        assert backup_env.env_value(merged, "PUBLIC_BASE_URL") == "https://panel.example"
        assert "OLDPASS" not in merged

    def test_server_keys_missing_from_the_backup_are_added(self):
        merged = backup_env.merge_restored_env(OLD_SERVER_ENV, NEW_SERVER_ENV)
        assert backup_env.env_value(merged, "WEB_HOST") == "0.0.0.0"
        assert backup_env.env_value(merged, "DEPLOYED_CHANNEL") == "dev"

    def test_comments_and_order_of_the_backup_are_kept(self):
        merged = backup_env.merge_restored_env(OLD_SERVER_ENV, NEW_SERVER_ENV)
        assert merged.startswith("# PGClockBot\nBOT_TOKEN=111:old-token\n")

    def test_backup_key_is_kept_when_this_server_has_none(self):
        merged = backup_env.merge_restored_env(OLD_SERVER_ENV, "BOT_TOKEN=x\n")
        assert backup_env.env_value(merged, "WEB_PORT") == "9000"

    def test_quoted_values_are_read_without_quotes(self):
        assert backup_env.env_value('A="x y"\nB=\'z\'\n', "A") == "x y"
        assert backup_env.env_value('A="x y"\nB=\'z\'\n', "B") == "z"
        assert backup_env.env_value("A=1\nA=2\n", "A") == "2"

    def test_written_file_is_private_and_complete(self, tmp_path):
        target = tmp_path / ".env"
        backup_env.write_private_file(target, "A=1\n")
        assert target.read_text() == "A=1\n"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert [p.name for p in tmp_path.iterdir()] == [".env"]


class TestStoredSecretsReadable:
    def _db(self, tmp_path: Path, token: str | None) -> str:
        path = tmp_path / "secrets.db"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE reseller_profiles (pg_admin_password_enc TEXT, pg_api_key_enc TEXT)")
            if token is not None:
                conn.execute("INSERT INTO reseller_profiles VALUES (?, NULL)", (token,))
        return f"sqlite+aiosqlite:///{path}"

    def test_secret_of_this_server_reads_them(self, tmp_path):
        token = Fernet(_hkdf_fernet_key(b"kept")).encrypt(b"pg-pass").decode()
        assert backup_env.stored_secrets_readable(self._db(tmp_path, token), "kept") is True

    def test_another_server_secret_does_not(self, tmp_path):
        token = Fernet(_hkdf_fernet_key(b"old")).encrypt(b"pg-pass").decode()
        assert backup_env.stored_secrets_readable(self._db(tmp_path, token), "new") is False

    def test_nothing_stored_means_nothing_to_warn_about(self, tmp_path):
        assert backup_env.stored_secrets_readable(self._db(tmp_path, None), "new") is None

    def test_legacy_key_still_counts(self):
        token = Fernet(_legacy_fernet_key(b"kept")).encrypt(b"x").decode()
        assert decrypts_with_secret(token, "kept") is True
        assert decrypts_with_secret(token, "other") is False
        assert decrypts_with_secret("not-a-token", "kept") is False


@requires_pg
def test_restore_on_a_new_server_keeps_its_database_connection(pg_install, tmp_path):
    root = backup_mod.ROOT_DIR
    (root / ".env").write_text(OLD_SERVER_ENV, encoding="utf-8")
    created = backup_mod.create_backup(note="t", include_env=True, created_by="test")
    this_server = NEW_SERVER_ENV.replace(
        "postgresql+asyncpg://pgclock:NEWPASS@127.0.0.1:5432/pgclock", PG_URL
    )
    (root / ".env").write_text(this_server, encoding="utf-8")

    result = backup_mod.restore_backup(
        Path(created["path"]), restore_env=True, safety_backup=False, restart=False, actor="test"
    )

    assert result["ok"] is True, result
    restored = (root / ".env").read_text(encoding="utf-8")
    assert backup_env.env_value(restored, "DATABASE_URL") == PG_URL
    assert backup_env.env_value(restored, "WEB_SECRET") == "old-server-secret"
    assert stat.S_IMODE((root / ".env").stat().st_mode) == 0o600
    backups = list(root.glob(".env.bak.restore.*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == this_server


@requires_pg
def test_unreadable_secrets_are_reported_after_the_restart(pg_install):
    created = backup_mod.create_backup(note="t", created_by="test")
    with patch.object(backup_mod, "stored_secrets_readable", return_value=False):
        result = backup_mod.restore_backup(
            Path(created["path"]), restore_env=False, safety_backup=False, restart=False, actor="test"
        )
    assert result["ok"] is True
    assert result["warnings"] == [backup_env.UNREADABLE_SECRETS_WARNING]
    assert backup_mod.read_restore_notice() == [backup_env.UNREADABLE_SECRETS_WARNING]

    with patch.object(backup_mod, "stored_secrets_readable", return_value=True):
        result = backup_mod.restore_backup(
            Path(created["path"]), restore_env=False, safety_backup=False, restart=False, actor="test"
        )
    assert result["warnings"] == []
    assert backup_mod.read_restore_notice() == []


def test_old_notice_is_not_shown(tmp_path, monkeypatch):
    notice = tmp_path / "notice.json"
    notice.write_text('{"at": "2000-01-01T00:00:00+00:00", "warnings": ["x"]}', encoding="utf-8")
    monkeypatch.setattr(backup_mod, "RESTORE_NOTICE_FILE", notice)
    assert backup_mod.read_restore_notice() == []


@pytest.fixture(autouse=True)
def _isolated_notice(tmp_path, monkeypatch):
    monkeypatch.setattr(backup_mod, "RESTORE_NOTICE_FILE", tmp_path / "restore_notice.json")
