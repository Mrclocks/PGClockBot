"""Phase B — global pgclock CLI."""

from __future__ import annotations


import pytest


@pytest.fixture()
def install_tree(tmp_path, monkeypatch):
    root = tmp_path / "PGClockBot"
    root.mkdir()
    (root / "run.py").write_text("print('ok')\n", encoding="utf-8")
    (root / "app").mkdir()
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (root / "VERSION").write_text("3.8.3\n", encoding="utf-8")
    venv_bin = root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (venv_bin / "python").chmod(0o755)
    (root / "data").mkdir()
    (root / ".env").write_text(
        'WEB_PORT="9019"\nWEB_SECRET="cli-test-secret-xxxxxxxxxxxxxxxx"\n'
        f'DATABASE_URL="sqlite+aiosqlite:///{root / "data" / "bot.db"}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PGCLOCK_HOME", str(root))
    return root


def test_resolve_install_root_from_env(install_tree):
    from app.cli.install_root import resolve_install_root

    root = resolve_install_root()
    assert root == install_tree.resolve()


def test_cli_help_lists_required_commands():
    from app.cli.main import build_parser

    parser = build_parser()
    help_txt = parser.format_help()
    for cmd in (
        "status",
        "start",
        "stop",
        "restart",
        "logs",
        "health",
        "backup",
        "restore",
        "migrate",
        "doctor",
    ):
        assert cmd in help_txt


def test_main_status_and_doctor(install_tree, monkeypatch):
    # Avoid real DB/alembic in doctor by stubbing connectivity path lightly —
    # doctor should still run and report missing DB file as a warning/fail.
    from app.cli.main import main

    # status should not require live health
    code = main(["--root", str(install_tree), "status"])
    assert code == 0

    code = main(["--root", str(install_tree), "doctor"])
    # doctor may return 1 if DB missing — acceptable; must not crash
    assert code in (0, 1)


def test_migrate_default_calls_upgrade(install_tree, monkeypatch):
    calls = {}

    def fake_upgrade(url=None):
        calls["upgrade"] = url

    def fake_current(url=None):
        return calls.get("after") or "0001_baseline"

    monkeypatch.setattr("app.db.alembic_runner.upgrade_head", fake_upgrade)
    monkeypatch.setattr("app.db.alembic_runner.current_revision", fake_current)


    # Patch where migrate_cmds imports from
    import app.cli.migrate_cmds as mig

    monkeypatch.setattr(mig, "cmd_migrate", mig.cmd_migrate)
    monkeypatch.setattr("app.db.alembic_runner.upgrade_head", fake_upgrade)
    monkeypatch.setattr("app.db.alembic_runner.current_revision", lambda url=None: "0001_baseline")

    # Direct unit call is clearer
    from app.cli.context import CliContext
    from app.cli.migrate_cmds import cmd_migrate

    ctx = CliContext(root=install_tree)
    # Re-patch inside module namespace used by cmd_migrate
    import app.db.alembic_runner as ar

    monkeypatch.setattr(ar, "upgrade_head", fake_upgrade)
    monkeypatch.setattr(ar, "current_revision", lambda url=None: "0001_baseline")
    assert cmd_migrate(ctx) == 0
    assert "upgrade" in calls


def test_backup_list_empty(install_tree, monkeypatch):
    from app.cli.context import CliContext
    from app.cli.backup_cmds import cmd_backup_list

    ctx = CliContext(root=install_tree)
    assert cmd_backup_list(ctx) == 0


def test_restore_requires_confirmation(install_tree, monkeypatch):
    from app.cli.context import CliContext
    from app.cli.backup_cmds import cmd_restore
    from app.cli.output import CliError

    ctx = CliContext(root=install_tree)

    def fake_get(_id):
        return install_tree / "data" / "backups" / "pgclock-backup-x.zip"

    monkeypatch.setattr("app.services.backup.get_backup_path", fake_get)
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "n")
    with pytest.raises(CliError) as ei:
        cmd_restore(ctx, backup_id="x", yes=False)
    assert ei.value.code == 2
