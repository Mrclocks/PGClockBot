"""Repeated linking migration preserves populated proof and consent history."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa

import test_customer_identity_migration as identity

connection = identity.connection
SPEC = importlib.util.spec_from_file_location("linking_migration", Path(__file__).resolve().parents[1] / "alembic/versions/0076_customer_telegram_links.py")
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


def test_repeat_upgrade_repair_and_retained_consent_downgrade(connection) -> None:
    identity.prepare(connection)
    identity.MIGRATION.upgrade()
    MIGRATION.upgrade()
    MIGRATION.upgrade()
    customers = identity.reflected(connection, "customers")
    connection.execute(customers.insert(), [{"id": 1, "public_id": "cus_web1"}, {"id": 2, "public_id": "cus_web2"}])
    challenges = identity.reflected(connection, "customer_link_challenges")
    now = datetime.now(timezone.utc)
    connection.execute(challenges.insert(), [{"id": f"lnk_{n}", "integration_id": "intg_synthetic", "customer_id": n,
        "initiating_credential_selector": "a"*32, "creation_key_digest": str(n)*64, "token_digest": str(n)*64,
        "expires_at": now+timedelta(minutes=10), "candidate_bot_user_id": 1, "telegram_confirmed_at": now} for n in (1, 2)])
    links = identity.reflected(connection, "customer_telegram_links")
    connection.execute(links.insert(), {"id": 1, "integration_id": "intg_synthetic", "customer_id": 1,
                                      "bot_user_id": 1, "challenge_id": "lnk_1"})
    with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
        connection.execute(links.insert(), {"id": 2, "integration_id": "intg_synthetic", "customer_id": 2,
                                          "bot_user_id": 1, "challenge_id": "lnk_2"})
    connection.execute(links.update().values(ended_at=now, version=2))
    connection.execute(links.insert(), {"id": 2, "integration_id": "intg_synthetic", "customer_id": 2,
                                      "bot_user_id": 1, "challenge_id": "lnk_2"})
    before = connection.execute(links.select().order_by(links.c.id)).all()
    sa.Index("ix_customer_telegram_links_bot_user_id", links.c.bot_user_id, links.c.id).drop(connection)
    MIGRATION.upgrade()
    MIGRATION.downgrade()
    MIGRATION.downgrade()
    MIGRATION.upgrade()
    assert before == connection.execute(links.select().order_by(links.c.id)).all()
    assert connection.scalar(sa.text("SELECT wallet_balance FROM bot_users WHERE id=1")) == 321
    assert connection.scalar(sa.text("SELECT value FROM settings WHERE key='integration_api_enabled'")) == "0"
