"""Guard: product installer must never scaffold SQLite for fresh installs."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_pgclock_install_requires_postgresql_scaffold():
    src = (ROOT / "pgclock.sh").read_text(encoding="utf-8")
    assert "ensure_postgresql" in src
    assert "scripts/setup_postgres.sh" in src
    # Old zero-config SQLite fallback must stay gone.
    assert "sqlite+aiosqlite:///{Path.cwd()" not in src
    assert 'or f"sqlite+aiosqlite:///' not in src
    assert "SQLite for zero-config labs" not in src
    # v11.0.12: install postgresql-common before server (pg_lsclusters).
    assert "postgresql-common" in src
    assert "pg_lsclusters" in src
    setup = (ROOT / "scripts" / "setup_postgres.sh").read_text(encoding="utf-8")
    assert "_cluster_from_etc" in setup


def test_setup_postgres_emits_url_file():
    src = (ROOT / "scripts" / "setup_postgres.sh").read_text(encoding="utf-8")
    assert "PGCLOCK_EMIT_URL_FILE" in src
    assert "postgresql+asyncpg://" in src
    # Password must be URL-encoded; TCP auth verified after role create.
    assert "urllib.parse.quote" in src
    assert "PGPASSWORD=" in src
    assert "127.0.0.1" in src
    # Socket-ready alone is not enough — force TCP listen + hba password rules.
    assert "listen_addresses" in src
    assert "_ensure_tcp_listener_and_hba" in src
    assert "scram-sha-256" in src
    assert "systemctl restart postgresql" in src or "pg_ctlcluster" in src
    # Root fix: hex password applied via DO/EXECUTE — never psql -v / :'var' for secrets.
    assert "SET password_encryption" in src
    assert "EXECUTE format('ALTER ROLE %I WITH LOGIN PASSWORD %L'" in src
    assert "PASSWORD '${DB_PASS}'" not in src
    assert "-v db_pass=" not in src
    assert ":'db_pass'" not in src
    # v11.0.5: never store md5 verifiers under scram-first HBA; trust is last resort.
    assert 'password_encryption = \'md5\'' not in src
    assert 'password_encryption = "md5"' not in src
    assert "_enable_trust_fallback" in src
    assert 'mode=scram' in src or "mode=${mode}" in src
    # v11.1.0: pin ALL admin/TCP/restart ops to one resolved cluster+port.
    assert "_resolve_target_cluster" in src
    assert "CLUSTER_SPEC" in src
    assert "--cluster" in src
    assert "unset PGHOST" in src
    # v11.0.11: never emit Unix-socket DATABASE_URL (asyncpg Errno 2 on VPS).
    assert 'DATABASE_URL="postgresql+asyncpg://${DB_USER}:${DB_PASS_ENC}@/${DB_NAME}?host=' not in src
    assert 'AUTH_MODE="socket"' not in src
    assert "Never emit Unix-socket URLs" in src
    assert "@127.0.0.1:${CLUSTER_PORT}/${DB_NAME}" in src
    sh = (ROOT / "pgclock.sh").read_text(encoding="utf-8")
    assert "rewrite_socket_database_url" in sh
    # Env URL must be verified over TCP (no silent skip).
    assert "will verify TCP" in sh
    assert "-u PGHOST" in sh


def test_setup_postgres_cluster_pin_guards():
    """Regression: socket OK + TCP fail from hitting two clusters."""
    src = (ROOT / "scripts" / "setup_postgres.sh").read_text(encoding="utf-8")
    assert "_resolve_target_cluster" in src
    assert "Could not restart PostgreSQL cluster" in src or "could not restart PostgreSQL cluster" in src
    assert "hostnossl" in src
    assert "pg_reload_conf" in src
    # Restart must not silently succeed when pg_ctlcluster fails.
    assert "return 0" in src  # still has success paths
    # But the failure path must exist (no bare trailing return 0 after failed restart).
    assert 'echo "ERROR: could not restart PostgreSQL cluster' in src


def test_install_waits_for_panel_health():
    src = (ROOT / "pgclock.sh").read_text(encoding="utf-8")
    assert 'Panel health' in src or "Panel health" in src
    assert "/health" in src
    assert "journalctl -u" in src


def test_install_global_cli_avoids_dev_stdin():
    src = (ROOT / "scripts" / "install_global_cli.sh").read_text(encoding="utf-8")
    # Must not feed /dev/stdin to `install` (fails under some sudo/pipe setups).
    assert "install -m 755 /dev/stdin" not in src
    assert "mktemp" in src
    assert 'install -m 755 "${tmp_bin}"' in src


def test_readme_documents_auto_postgres_install():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "PostgreSQL automatically" in readme
    assert "PGCLOCK_DATABASE_URL" in readme


def test_alembic_revision_ids_documented_over_32():
    """Guard: long revision ids need the PG version_num widen in alembic/env.py."""
    import re

    env = (ROOT / "alembic" / "env.py").read_text(encoding="utf-8")
    assert "_ensure_alembic_version_num_width" in env
    assert "VARCHAR({_ALEMBIC_VERSION_NUM_LEN})" in env

    lengths: list[int] = []
    for path in (ROOT / "alembic" / "versions").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        m = re.search(
            r'^revision\s*(?::\s*str)?\s*=\s*["\']([0-9A-Za-z_]+)["\']',
            text,
            re.M,
        )
        assert m, f"missing revision in {path.name}"
        lengths.append(len(m.group(1)))
    assert max(lengths) > 32, "expected at least one revision id longer than VARCHAR(32)"
    assert max(lengths) <= 128


def test_uninstall_wipes_local_postgres_and_le_cert():
    """Menu uninstall must drop local db/role + panel LE cert, not leave alembic ghosts."""
    src = (ROOT / "pgclock.sh").read_text(encoding="utf-8")
    assert "wipe_local_postgres_for_uninstall" in src
    assert "wipe_letsencrypt_for_uninstall" in src
    assert "DROP DATABASE IF EXISTS" in src
    assert "DROP ROLE" in src
    assert "pg_terminate_backend" in src
    assert "certbot delete" in src
    assert "local PostgreSQL database + role" in src
    # Old safe-default that left the DB behind must stay gone.
    assert "database are NOT dropped" not in src
    assert "are NOT dropped" not in src
    # Remote URLs must not be wiped blindly.
    assert "leaving that database alone" in src
    # Call order: stop service → wipe LE/DB → delete folder
    uninstall = src.split("cmd_uninstall() {", 1)[1].split("\ncmd_", 1)[0]
    assert uninstall.index("wipe_letsencrypt_for_uninstall") < uninstall.index(
        "wipe_local_postgres_for_uninstall"
    )
    assert uninstall.index("wipe_local_postgres_for_uninstall") < uninstall.index(
        "rm -rf \"$root\""
    )
