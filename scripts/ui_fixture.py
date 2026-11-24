#!/usr/bin/env python3
"""Prepare a checkout of PGClockBot to serve the web panel with the same sample data every time.

Used by ``scripts/ui_regression.sh`` to screenshot two versions of the panel under identical
conditions. Run it with the checkout under test first on PYTHONPATH and its directory as the
working directory: the app's DATA_DIR and ``.env`` are that checkout's, never this repository's.

It writes the checkout's ``.env`` (fake bot token, the local fake panel, fixed secrets), the web
admin login and the setup-complete flag, upgrades the given empty database to head and inserts a
fixed set of users, shops, plans, orders, payments, services and tickets with fixed timestamps.
Nothing here is a real credential; the database must be a throwaway one.

Usage (from scripts/ui_regression.sh):
    DATABASE_URL=postgresql+asyncpg://... PYTHONPATH=<checkout> python scripts/ui_fixture.py <checkout> <port>
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ADMIN_USER = "uiadmin"
ADMIN_PASSWORD = "Ui-Regression-Only-9f3c2b"
ADMIN_TELEGRAM_ID = 700000001
# Shaped like a Telegram token so setup counts as complete; Telegram rejects it, so the bot stays offline.
FAKE_BOT_TOKEN = "700000999:AAFakeTokenForUiRegressionOnly0000000"
# scripts/ui_fake_panel.py answers here; see there why a refused port is not good enough.
DEFAULT_PANEL_URL = "http://127.0.0.1:9319"
FIXED_SECRET = "ui-regression-fixed-secret-0123456789abcdef"
EPOCH = datetime(2026, 1, 10, 9, 30, tzinfo=timezone.utc)


def write_env(checkout: Path, port: int, database_url: str) -> None:
    lines = {
        "BOT_TOKEN": FAKE_BOT_TOKEN,
        "BOT_USERNAME": "ui_regression_bot",
        "ADMIN_IDS": str(ADMIN_TELEGRAM_ID),
        "DATABASE_URL": database_url,
        "PG_BASE_URL": os.environ.get("UI_FAKE_PANEL_URL", DEFAULT_PANEL_URL),
        "PG_USERNAME": "ui-regression",
        "PG_PASSWORD": "ui-regression",
        "WEB_HOST": "127.0.0.1",
        "WEB_PORT": str(port),
        "WEB_SECRET": FIXED_SECRET,
        "WEBHOOK_SECRET_TOKEN": FIXED_SECRET,
        "WEBHOOK_URL": "",
        "PUBLIC_BASE_URL": "",
        "UPDATE_CHANNEL": "main",
    }
    (checkout / ".env").write_text("".join(f'{key}="{value}"\n' for key, value in lines.items()), encoding="utf-8")


def at(days: int, hours: int = 0) -> datetime:
    return EPOCH + timedelta(days=days, hours=hours)


def sample_rows() -> list[object]:
    from app.db.models import (
        BotUser,
        Order,
        Payment,
        Plan,
        ResellerProfile,
        Ticket,
        TicketMessage,
        UserService,
    )

    gb = 1024**3
    rows: list[object] = [
        BotUser(id=1, telegram_id=ADMIN_TELEGRAM_ID, username="owner", full_name="مدیر نمونه", role="admin",
                referral_code="UIADMIN1", created_at=at(-60)),
        BotUser(id=2, telegram_id=700000002, username="shop_one", full_name="فروشگاه نمونه", role="reseller",
                referral_code="UISHOP01", created_at=at(-50)),
    ]
    rows += [
        BotUser(id=10 + n, telegram_id=700000100 + n, username=f"customer{n}", full_name=f"مشتری {n}",
                role="user", referral_code=f"UICUST{n:02d}", wallet_balance=n * 25_000,
                reseller_id=2 if n % 3 == 0 else None, created_at=at(-40 + n))
        for n in range(1, 9)
    ]
    rows.append(ResellerProfile(id=1, user_id=2, is_active=True, allow_pg_panel_access=False,
                                allow_volume_oversell=False, external_allocated_bytes=0))
    plan_flags = dict(carry_remaining_quota=False, carry_remaining_volume=False, carry_remaining_time=False,
                      allow_buy_extra=False, extra_gb_price=0, extra_month_price=0)
    rows += [
        Plan(id=1, name="پلن یک‌ماهه ۳۰ گیگ", price=150_000, duration_days=30, data_limit_gb=30, is_active=True,
             sort_order=1, **plan_flags),
        Plan(id=2, name="پلن سه‌ماهه ۱۰۰ گیگ", price=390_000, duration_days=90, data_limit_gb=100, is_active=True,
             sort_order=2, **plan_flags),
        Plan(id=3, name="پلن غیرفعال", price=90_000, duration_days=30, data_limit_gb=10, is_active=False,
             sort_order=3, **plan_flags),
    ]
    statuses = ["delivered", "awaiting_approval", "rejected", "delivered", "cancelled", "paid"]
    for n, status in enumerate(statuses, start=1):
        user_id = 10 + n
        rows.append(Order(id=n, user_id=user_id, plan_id=1 + n % 2, amount=150_000 * (1 + n % 2), status=status,
                          created_at=at(-20 + n), updated_at=at(-20 + n, 1)))
        payment_status = {"delivered": "approved", "paid": "approved", "rejected": "rejected"}.get(status, "pending")
        rows.append(Payment(id=n, order_id=n, user_id=user_id, amount=150_000 * (1 + n % 2), method="card",
                            status=payment_status, created_at=at(-20 + n)))
    for n in range(1, 5):
        rows.append(UserService(id=n, bot_user_id=10 + n, plan_id=1, pg_user_id=500 + n, pg_username=f"svc_user_{n}",
                                shop_needs_review=False, created_at=at(-18 + n),
                                quota_data_limit_bytes=30 * gb, quota_expire_at=at(20 + n),
                                shop_id=2 if n == 3 else None))
    rows.append(Ticket(id=1, user_id=11, subject="مشکل اتصال", status="open", created_at=at(-3), updated_at=at(-3, 2)))
    rows.append(TicketMessage(id=1, ticket_id=1, sender_id=11, body="سلام، سرویسم وصل نمی‌شود.", created_at=at(-3)))
    return rows


async def seed() -> None:
    from sqlalchemy import text

    from app.db.session import SessionLocal, init_db
    from app.services.users import ensure_default_settings

    await init_db()
    async with SessionLocal() as session:
        await ensure_default_settings(session)
        session.add_all(sample_rows())
        await session.commit()
        # Rows were inserted with fixed ids: move each sequence past them so the app can add more.
        for table in ("bot_users", "reseller_profiles", "plans", "orders", "payments", "user_services", "tickets",
                      "ticket_messages"):
            await session.execute(text(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), (SELECT max(id) FROM {table}))"
            ))
        await session.commit()


def main() -> int:
    checkout, port = Path(sys.argv[1]).resolve(), int(sys.argv[2])
    database_url = os.environ["DATABASE_URL"]
    write_env(checkout, port, database_url)
    os.chdir(checkout)
    from app.services.setup_wizard import mark_setup_complete
    from app.services.web_auth import save_web_admin

    save_web_admin(ADMIN_USER, ADMIN_PASSWORD)
    mark_setup_complete()
    asyncio.run(seed())
    print(f"ui fixture ready in {checkout} (user {ADMIN_USER})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
