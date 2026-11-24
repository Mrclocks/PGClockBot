"""A restore stays all or nothing when the client tools are newer than the database server.

A dump made by pg_dump 17 or later sets ``transaction_timeout``, which a version 16 server rejects;
pg_restore then fails on every backup. In that case restore_database turns the dump into SQL,
leaves that setting out and runs the rest through psql in one transaction that commits only when
the whole dump was read. These tests force that path on this machine's own client.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from app.services import backup_postgres as pg_backup
from tests import test_backup_postgres_restore as restore_tests
from tests.test_backup_postgres_restore import _probe, _scalar, pg_install  # noqa: F401 - pytest fixture, used by name

NEWER_SETTING = b"SET transaction_timeout = 0;\n"


@pytest.fixture
def newer_client(monkeypatch):
    """Make the installed pg_restore look one major version newer than the server."""
    calls = []

    def newer(tool: str) -> int | None:
        server = pg_backup.server_major_version()
        calls.append(tool)
        return None if server is None else server + 1

    monkeypatch.setattr(pg_backup, "client_major_version", newer)
    return calls


@pytest.mark.usefixtures("pg_install")
def test_restore_through_psql_brings_back_the_snapshot(newer_client):
    restore_tests.test_restore_brings_back_the_snapshot_and_records_the_schema(None)
    assert newer_client, "the restore took the newer-client path"


@pytest.mark.usefixtures("pg_install")
def test_restore_through_psql_that_fails_leaves_the_database_unchanged(newer_client):
    restore_tests.test_failing_restore_leaves_the_database_unchanged(None)


@pytest.mark.usefixtures("pg_install")
def test_restore_through_psql_is_not_blocked_by_an_open_session(newer_client):
    restore_tests.test_session_left_open_on_a_table_does_not_block_the_restore(None)


def _fake_pg_restore(tmp_path: Path, sql: bytes, exit_code: int) -> str:
    """A stand-in for pg_restore that prints ``sql`` and exits with ``exit_code``."""
    body = tmp_path / "sql.out"
    body.write_bytes(sql)
    script = tmp_path / "pg_restore"
    script.write_text(f"#!/bin/sh\ncat '{body}'\nexit {exit_code}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


def _restore_with(tmp_path: Path, sql: bytes, exit_code: int) -> None:
    target, env = pg_backup._connection()
    server = pg_backup.server_major_version()
    pg_backup._restore_through_psql(_fake_pg_restore(tmp_path, sql, exit_code), tmp_path / "unused.dump", target, env,
                                    server_major=server)


@pytest.mark.usefixtures("pg_install")
def test_a_dump_that_breaks_off_part_way_changes_nothing(tmp_path):
    sql = b"UPDATE settings SET value = 'half' WHERE key = 'backup_probe';\nCREATE TABLE half_restored (id int);\n"
    with pytest.raises(pg_backup.BackupDatabaseError):
        _restore_with(tmp_path, sql, exit_code=1)
    assert _probe() == "snapshot"
    assert _scalar("SELECT to_regclass('public.half_restored')") is None


@pytest.mark.usefixtures("pg_install")
def test_the_newer_client_setting_is_left_out_and_the_rest_commits(tmp_path):
    sql = NEWER_SETTING + b"SET transaction_timeout TO 0;\nUPDATE settings SET value = 'restored' WHERE key = 'backup_probe';\n"
    _restore_with(tmp_path, sql, exit_code=0)
    assert _probe() == "restored"


@pytest.mark.usefixtures("pg_install")
def test_an_sql_error_rolls_back_everything_before_it(tmp_path):
    sql = b"UPDATE settings SET value = 'first' WHERE key = 'backup_probe';\nSELECT no_such_function();\n"
    with pytest.raises(pg_backup.BackupDatabaseError):
        _restore_with(tmp_path, sql, exit_code=0)
    assert _probe() == "snapshot"


def test_client_version_is_read_from_the_tool(tmp_path):
    script = tmp_path / "pg_restore"
    script.write_text("#!/bin/sh\necho 'pg_restore (PostgreSQL) 17.2 (Ubuntu 17.2-1.pgdg24.04+1)'\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    assert pg_backup.client_major_version(str(script)) == 17
    assert pg_backup.client_major_version(str(tmp_path / "missing")) is None
