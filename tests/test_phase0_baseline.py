"""Phase 0 baseline — backup, version record, restore verification."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.postgres_support import scoped_database_url, scoped_sync_engine


@pytest.fixture()
def seeded_install(tmp_path, monkeypatch):
    from app.db.alembic_runner import upgrade_head

    url = scoped_database_url(tables=False)
    # Before ROOT_DIR is patched below: Alembic reads alembic.ini from the project root.
    upgrade_head(url)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    backups = data_dir / "backups"
    backups.mkdir()
    (data_dir / "uploads").mkdir()
    (data_dir / "web_admin.json").write_text(
        json.dumps({"username": "admin", "password_hash": "$2b$12$testhash"}),
        encoding="utf-8",
    )
    env_path = tmp_path / ".env"
    env_path.write_text(
        f'DATABASE_URL="{url}"\nWEB_SECRET="test-secret-phase0-xxxxxx"\n',
        encoding="utf-8",
    )

    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("WEB_SECRET", "test-secret-phase0-xxxxxx")

    import app.config as config

    config.get_settings.cache_clear()
    monkeypatch.setattr(config, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)

    import app.services.backup as backup
    import app.services.baseline as baseline

    monkeypatch.setattr(backup, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(backup, "DATA_DIR", data_dir)
    monkeypatch.setattr(backup, "BACKUP_DIR", backups)
    monkeypatch.setattr(baseline, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(baseline, "DATA_DIR", data_dir)
    monkeypatch.setattr(baseline, "BASELINE_DIR", data_dir / "baselines")

    # Schema at head plus one seed row, on this test's own database.
    from sqlalchemy import text

    eng = scoped_sync_engine()
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO bot_users (telegram_id, role, wallet_balance, points_balance, referral_code, is_blocked) "
                "VALUES (1001, 'admin', 500, 0, 'ref1001', false)"
            )
        )
        conn.execute(
            text("INSERT INTO settings (key, value) VALUES ('welcome_text', 'hello')")
        )
    eng.dispose()

    yield {"url": url, "data": data_dir, "root": tmp_path}
    config.get_settings.cache_clear()


def test_phase0_baseline_creates_artifacts_and_verifies_restore(seeded_install):
    from app.services.baseline import create_phase0_baseline, latest_baseline

    result = create_phase0_baseline(note="unit-test-phase0", verify_restore=True)
    assert result["ok"] is True
    root = Path(result["path"])
    assert (root / "BASELINE.json").is_file()
    assert list(root.glob("pgclock-backup-*.zip"))
    assert (root / "config" / "env").is_file()
    assert (root / "config" / "web_admin.json").is_file()

    record = result["record"]
    assert record["magic"] == "pgclock-phase0-baseline-v1"
    assert record["git"]["commit"]
    assert record["restore_verification"]["ok"] is True
    assert record["database_url_dialect"] == "postgresql"
    assert record["restore_verification"]["engine"] == "postgresql"
    assert record["restore_verification"]["postgres_dump_size"] > 0
    assert record["restore_verification"]["has_web_admin"] is True
    assert record["restore_verification"]["has_env"] is True

    latest = latest_baseline()
    assert latest is not None
    assert latest["baseline_id"] == result["baseline_id"]
