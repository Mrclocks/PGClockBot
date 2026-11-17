"""Restoring a SQLite backup, storing an uploaded archive and checking member hashes are safe."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import stat
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services import backup as backup_mod


def _wal_database(path: Path, value: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.execute("INSERT INTO t VALUES (?)", (value,))
    conn.commit()
    return conn


def _values(path: Path) -> list[str]:
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        return [row[0] for row in conn.execute("SELECT v FROM t")]


def test_a_wal_left_by_the_old_database_is_not_replayed_onto_the_restored_one(tmp_path: Path) -> None:
    live = tmp_path / "bot.db"
    conn = _wal_database(live, "old")
    # The service was stopped hard: its WAL still holds committed frames of the old database.
    stale_wal = tmp_path / "stale-wal"
    shutil.copy2(f"{live}-wal", stale_wal)
    conn.close()
    shutil.copy2(stale_wal, f"{live}-wal")

    restored = tmp_path / "restored.db"
    _wal_database(restored, "new").close()

    backup_mod._replace_sqlite_database(restored, live)

    assert _values(live) == ["new"]
    assert not Path(f"{live}-wal").exists() or Path(f"{live}-wal").stat().st_size == 0
    assert stat.S_IMODE(live.stat().st_mode) == 0o600


def _archive(path: Path, *, db_bytes: bytes = b"SQLite format 3\x00" + b"x" * 200, declared: bytes | None = None) -> bytes:
    manifest = {
        "magic": backup_mod.MAGIC,
        "db_engine": "sqlite",
        "include_env": True,
        "files": [{"path": backup_mod.SQLITE_DB_MEMBER, "sha256": hashlib.sha256(declared or db_bytes).hexdigest()}],
    }
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(backup_mod.MANIFEST_NAME, json.dumps(manifest))
        zf.writestr(backup_mod.SQLITE_DB_MEMBER, db_bytes)
        zf.writestr("env/.env", "BOT_TOKEN=1:secret\n")
    return path.read_bytes()


@pytest.fixture()
def backup_dir(tmp_path: Path):
    target = tmp_path / "backups"
    with patch.object(backup_mod, "BACKUP_DIR", target), patch.object(
        backup_mod, "read_restore_status", return_value={"state": "idle"}
    ):
        yield target


def test_an_uploaded_backup_and_its_details_are_readable_only_by_the_service(tmp_path: Path, backup_dir: Path) -> None:
    result = backup_mod.save_uploaded_backup(_archive(tmp_path / "in.zip"), filename="in.zip")

    assert result["ok"] is True
    archive = Path(result["path"])
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert stat.S_IMODE(archive.with_suffix(".json").stat().st_mode) == 0o600
    assert sorted(p.name for p in backup_dir.iterdir()) == sorted([archive.name, archive.with_suffix(".json").name])


def test_an_upload_whose_member_does_not_match_its_declared_hash_is_refused_and_removed(
    tmp_path: Path, backup_dir: Path
) -> None:
    content = _archive(tmp_path / "in.zip", declared=b"something else")

    result = backup_mod.save_uploaded_backup(content, filename="in.zip")

    assert result["ok"] is False
    assert "هش" in result["error"]
    assert list(backup_dir.glob("*.zip")) == []
