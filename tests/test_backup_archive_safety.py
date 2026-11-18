"""Storing an uploaded archive, checking member hashes and refusing non-PostgreSQL archives are safe."""

from __future__ import annotations

import hashlib
import json
import stat
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services import backup as backup_mod

DUMP_BYTES = b"PGDMP" + b"x" * 200
# The member an old SQLite install wrote instead of a PostgreSQL dump.
LEGACY_DB_MEMBER = "data/bot.db"


def _archive(
    path: Path,
    *,
    member: str = backup_mod.POSTGRES_DUMP_MEMBER,
    engine: str = backup_mod.DB_ENGINE,
    declared: bytes | None = None,
) -> bytes:
    manifest = {
        "magic": backup_mod.MAGIC,
        "db_engine": engine,
        "include_env": True,
        "files": [{"path": member, "sha256": hashlib.sha256(declared or DUMP_BYTES).hexdigest()}],
    }
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(backup_mod.MANIFEST_NAME, json.dumps(manifest))
        zf.writestr(member, DUMP_BYTES)
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


@pytest.mark.parametrize(
    ("member", "engine"),
    [(LEGACY_DB_MEMBER, "sqlite"), (LEGACY_DB_MEMBER, ""), (backup_mod.POSTGRES_DUMP_MEMBER, "sqlite")],
)
def test_an_archive_without_a_postgres_dump_is_refused(
    tmp_path: Path, backup_dir: Path, member: str, engine: str
) -> None:
    result = backup_mod.save_uploaded_backup(_archive(tmp_path / "in.zip", member=member, engine=engine), filename="in.zip")

    assert result["ok"] is False
    assert result["error"] == backup_mod.NOT_POSTGRES_BACKUP_MESSAGE
    assert list(backup_dir.glob("*.zip")) == []


def test_the_settings_page_names_the_database_without_its_password() -> None:
    url = "postgresql+asyncpg://pgclock:s3cret-pass@db.internal:5432/pgclock_prod"
    with patch.object(backup_mod, "get_settings", return_value=SimpleNamespace(database_url=url)):
        label = backup_mod.database_label()

    assert label == "PostgreSQL · pgclock_prod@db.internal"
    assert "s3cret-pass" not in label


def test_an_unreadable_database_url_is_not_echoed() -> None:
    with patch.object(backup_mod, "get_settings", return_value=SimpleNamespace(database_url="::not a url s3cret")):
        label = backup_mod.database_label()

    assert "s3cret" not in label
