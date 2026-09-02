from __future__ import annotations

import logging
import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import event

from app.config import get_settings
from app.db import Base
from app.db.engine_url import normalize_async_url, parse_engine

# Register models on Base.metadata for Alembic / legacy paths
import app.db.models  # noqa: F401

log = logging.getLogger(__name__)

settings = get_settings()
_db_url = normalize_async_url(settings.database_url)
_engine_info = parse_engine(_db_url)
_engine_kwargs: dict = {"echo": False, "future": True}
if _engine_info.is_sqlite:
    _engine_kwargs["connect_args"] = {"timeout": 30}
elif _engine_info.is_postgresql:
    # Production-friendly pool defaults (override via env later if needed)
    _engine_kwargs.setdefault("pool_pre_ping", True)
engine = create_async_engine(_db_url, **_engine_kwargs)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

# Allow tests / migrator to force legacy create_all bootstrap
_ALLOW_CREATE_ALL = os.environ.get("PGCLOCK_ALLOW_CREATE_ALL", "").strip() in {
    "1",
    "true",
    "yes",
}


if _engine_info.is_sqlite:

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_on_connect(dbapi_conn, _connection_record) -> None:
        cursor = dbapi_conn.cursor()
        # These PRAGMAs must not run inside SQLAlchemy's transaction
        # (SQLite rejects journal_mode / synchronous mid-transaction).
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()
        # bot.db holds encrypted PasarGuard passwords, session data, etc.
        # DATA_DIR is already 0700, but pin the file itself too — belt and
        # suspenders against e.g. a misconfigured backup/export tool that
        # copies files with looser default permissions.
        try:
            if _engine_info.sqlite_path and _engine_info.sqlite_path.exists():
                os.chmod(_engine_info.sqlite_path, 0o600)
                for suffix in ("-wal", "-shm", "-journal"):
                    side = _engine_info.sqlite_path.with_name(_engine_info.sqlite_path.name + suffix)
                    if side.exists():
                        os.chmod(side, 0o600)
        except OSError:
            pass


def _tables_exist(sync_conn) -> bool:
    from sqlalchemy import inspect

    insp = inspect(sync_conn)
    return insp.has_table("bot_users") and insp.has_table("settings")


def _alembic_version_exists(sync_conn) -> bool:
    from sqlalchemy import inspect

    return inspect(sync_conn).has_table("alembic_version")


async def init_db() -> None:
    """Initialize schema via Alembic; legacy create_all only as controlled fallback."""
    from app.db.alembic_runner import stamp_head, upgrade_head

    async with engine.begin() as conn:
        has_tables = await conn.run_sync(lambda c: _tables_exist(c))
        has_alembic = await conn.run_sync(lambda c: _alembic_version_exists(c))

    if has_tables and not has_alembic:
        # Existing production DB created before Alembic — apply legacy additive
        # migrations once, then stamp head only if Phase 1–3 objects exist.
        log.warning(
            "Existing database without alembic_version — applying legacy "
            "compatibility migrator then stamping Alembic head"
        )
        async with engine.begin() as conn:
            await conn.run_sync(_migrate_sqlite_legacy)
            if _engine_info.is_sqlite:
                await conn.run_sync(_ensure_indexes)
            await conn.run_sync(_assert_ready_to_stamp_head)
        stamp_head(_db_url)
    else:
        # Fresh DB or already under Alembic — upgrade to head (no create_all).
        try:
            upgrade_head(_db_url)
        except Exception:
            if _ALLOW_CREATE_ALL or not has_tables:
                log.exception(
                    "Alembic upgrade failed; falling back to create_all "
                    "(set PGCLOCK_ALLOW_CREATE_ALL=1 to silence in labs)"
                )
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)
                    await conn.run_sync(_migrate_sqlite_legacy)
                    if _engine_info.is_sqlite:
                        await conn.run_sync(_ensure_indexes)
                try:
                    stamp_head(_db_url)
                except Exception:
                    log.exception("Failed to stamp Alembic head after create_all fallback")
            else:
                raise

    # Idempotent additive columns for DBs already stamped at an older Alembic
    # head (create_all in 0001 only runs once). Keeps ORM fields like
    # reseller_plans.billing_mode from 500'ing /plans before a new revision lands.
    async with engine.begin() as conn:
        await conn.run_sync(_migrate_sqlite_legacy)
        if _engine_info.is_sqlite:
            await conn.run_sync(_ensure_indexes)


# ---------------------------------------------------------------------------
# Legacy additive migrator (SQLite-era). Kept only to bring pre-Alembic DBs
# up to the baseline shape before stamping. New installs must use Alembic.
# ---------------------------------------------------------------------------


def _migrate_sqlite_legacy(sync_conn) -> None:
    from sqlalchemy import inspect, text as sql_text

    insp = inspect(sync_conn)
    if not insp.has_table("plans"):
        return
    cols = {c["name"] for c in insp.get_columns("plans")}
    if "pg_group_ids" not in cols:
        sync_conn.execute(sql_text("ALTER TABLE plans ADD COLUMN pg_group_ids VARCHAR(255)"))
    if "owner_reseller_id" not in cols:
        sync_conn.execute(sql_text("ALTER TABLE plans ADD COLUMN owner_reseller_id INTEGER"))
    if "pg_username_prefix" not in cols:
        sync_conn.execute(sql_text("ALTER TABLE plans ADD COLUMN pg_username_prefix VARCHAR(64)"))
    if "pg_username_suffix" not in cols:
        sync_conn.execute(sql_text("ALTER TABLE plans ADD COLUMN pg_username_suffix VARCHAR(64)"))
    if "pg_username_pattern" not in cols:
        sync_conn.execute(sql_text("ALTER TABLE plans ADD COLUMN pg_username_pattern VARCHAR(255)"))

    if insp.has_table("reseller_profiles"):
        rcols = {c["name"] for c in insp.get_columns("reseller_profiles")}
        alters = {
            "pg_role_id": "INTEGER",
            "web_username": "VARCHAR(128)",
            "web_password_hash": "VARCHAR(255)",
            "web_permissions": "TEXT",
            "bot_permissions": "TEXT",
            "plan_id": "INTEGER",
            "created_at": "DATETIME",
            "setup_token": "VARCHAR(64)",
            "setup_token_expires": "DATETIME",
            "setup_completed_at": "DATETIME",
            "bot_token": "TEXT",
            "bot_username": "VARCHAR(64)",
            "bot_telegram_id": "BIGINT",
            "share_pg_panel_url": "BOOLEAN DEFAULT 0",
            "bot_admin_ids": "TEXT",
            "pg_admin_password_enc": "TEXT",
            "billing_mode": "VARCHAR(16) DEFAULT 'fixed'",
            "billing_balance": "INTEGER DEFAULT 0",
            "billing_watermark_bytes": "BIGINT DEFAULT 0",
            "billing_low_warned_at": "DATETIME",
            "billing_suspended_at": "DATETIME",
            "billing_suspended_user_ids": "TEXT",
            "payg_wallet_linked": "BOOLEAN DEFAULT 0",
        }
        for col, typ in alters.items():
            if col not in rcols:
                sync_conn.execute(
                    sql_text(f"ALTER TABLE reseller_profiles ADD COLUMN {col} {typ}")
                )

    if insp.has_table("reseller_plans"):
        pcols = {c["name"] for c in insp.get_columns("reseller_plans")}
        if "share_pg_panel_url" not in pcols:
            sync_conn.execute(
                sql_text("ALTER TABLE reseller_plans ADD COLUMN share_pg_panel_url BOOLEAN DEFAULT 0")
            )
        if "billing_mode" not in pcols:
            sync_conn.execute(
                sql_text(
                    "ALTER TABLE reseller_plans ADD COLUMN billing_mode VARCHAR(16) DEFAULT 'fixed'"
                )
            )
        if "price_per_gb" not in pcols:
            sync_conn.execute(
                sql_text("ALTER TABLE reseller_plans ADD COLUMN price_per_gb INTEGER DEFAULT 0")
            )
        if "pg_group_ids" not in pcols:
            sync_conn.execute(
                sql_text("ALTER TABLE reseller_plans ADD COLUMN pg_group_ids VARCHAR(255)")
            )
        if "allow_buy_extra" not in pcols:
            sync_conn.execute(
                sql_text("ALTER TABLE reseller_plans ADD COLUMN allow_buy_extra BOOLEAN DEFAULT 0")
            )
        if "extra_gb_price" not in pcols:
            sync_conn.execute(
                sql_text("ALTER TABLE reseller_plans ADD COLUMN extra_gb_price INTEGER DEFAULT 0")
            )
        if "extra_user_price" not in pcols:
            sync_conn.execute(
                sql_text("ALTER TABLE reseller_plans ADD COLUMN extra_user_price INTEGER DEFAULT 0")
            )
        if "renew_price" not in pcols:
            sync_conn.execute(
                sql_text("ALTER TABLE reseller_plans ADD COLUMN renew_price INTEGER DEFAULT 0")
            )
        plan_kind_alters = {
            "plan_kind": "VARCHAR(32) DEFAULT 'subscription'",
            "duration_days": "INTEGER DEFAULT 0",
            "included_gb": "INTEGER DEFAULT 0",
            "included_users": "INTEGER DEFAULT 0",
            "addon_gb": "INTEGER DEFAULT 0",
            "addon_users": "INTEGER DEFAULT 0",
            "renew_pricing_mode": "VARCHAR(32) DEFAULT 'fixed'",
        }
        for col, typ in plan_kind_alters.items():
            if col not in pcols:
                sync_conn.execute(
                    sql_text(f"ALTER TABLE reseller_plans ADD COLUMN {col} {typ}")
                )

    if not insp.has_table("pg_admin_subscriptions"):
        sync_conn.execute(
            sql_text(
                """
                CREATE TABLE pg_admin_subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pg_username VARCHAR(128) NOT NULL UNIQUE,
                    plan_id INTEGER,
                    access_status VARCHAR(16) DEFAULT 'active' NOT NULL,
                    started_at DATETIME,
                    expires_at DATETIME,
                    base_gb INTEGER DEFAULT 0 NOT NULL,
                    base_users INTEGER DEFAULT 0 NOT NULL,
                    extra_gb_purchased INTEGER DEFAULT 0 NOT NULL,
                    extra_users_purchased INTEGER DEFAULT 0 NOT NULL,
                    expired_at DATETIME,
                    expiry_disabled_user_ids TEXT,
                    last_renewed_at DATETIME,
                    renew_generation INTEGER DEFAULT 0 NOT NULL,
                    warn_sent_at DATETIME,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        sync_conn.execute(
            sql_text(
                "CREATE INDEX IF NOT EXISTS ix_pg_admin_subscriptions_expires_at "
                "ON pg_admin_subscriptions (expires_at)"
            )
        )
        sync_conn.execute(
            sql_text(
                "CREATE INDEX IF NOT EXISTS ix_pg_admin_subscriptions_access_status "
                "ON pg_admin_subscriptions (access_status)"
            )
        )

    if not insp.has_table("org_principals"):
        sync_conn.execute(
            sql_text(
                """
                CREATE TABLE org_principals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    parent_id INTEGER REFERENCES org_principals(id),
                    depth INTEGER NOT NULL,
                    status VARCHAR(32) DEFAULT 'active' NOT NULL,
                    pg_username VARCHAR(128),
                    reseller_profile_id INTEGER REFERENCES reseller_profiles(id),
                    pg_staff_id INTEGER REFERENCES pg_staff_access(id),
                    bot_user_id INTEGER REFERENCES bot_users(id),
                    pg_password_enc TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        sync_conn.execute(
            sql_text(
                "CREATE INDEX IF NOT EXISTS ix_org_principals_parent_id "
                "ON org_principals (parent_id)"
            )
        )
        sync_conn.execute(
            sql_text(
                "CREATE INDEX IF NOT EXISTS ix_org_principals_depth "
                "ON org_principals (depth)"
            )
        )
        sync_conn.execute(
            sql_text(
                "CREATE INDEX IF NOT EXISTS ix_org_principals_status "
                "ON org_principals (status)"
            )
        )
        sync_conn.execute(
            sql_text(
                "CREATE INDEX IF NOT EXISTS ix_org_principals_pg_username "
                "ON org_principals (pg_username)"
            )
        )
        sync_conn.execute(
            sql_text(
                "CREATE INDEX IF NOT EXISTS ix_org_principals_bot_user_id "
                "ON org_principals (bot_user_id)"
            )
        )
        sync_conn.execute(
            sql_text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_org_principals_reseller_profile "
                "ON org_principals (reseller_profile_id) "
                "WHERE reseller_profile_id IS NOT NULL"
            )
        )
        sync_conn.execute(
            sql_text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_org_principals_pg_staff "
                "ON org_principals (pg_staff_id) "
                "WHERE pg_staff_id IS NOT NULL"
            )
        )
        sync_conn.execute(
            sql_text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_org_principals_bot_user_id "
                "ON org_principals (bot_user_id) "
                "WHERE bot_user_id IS NOT NULL"
            )
        )
        insp.clear_cache() if hasattr(insp, "clear_cache") else None

    if insp.has_table("org_principals"):
        ocols = {c["name"] for c in insp.get_columns("org_principals")}
        if "pg_password_enc" not in ocols:
            sync_conn.execute(
                sql_text("ALTER TABLE org_principals ADD COLUMN pg_password_enc TEXT")
            )
            insp.clear_cache() if hasattr(insp, "clear_cache") else None
        _ensure_org_principal_aux_tables(sync_conn)
        _seed_owner_if_no_depth0(sync_conn)
        # Phase 5B / 6A — unique Telegram bind (NULLs remain unbound).
        # Idempotent with Alembic 0017 (IF NOT EXISTS).
        sync_conn.execute(
            sql_text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_org_principals_bot_user_id "
                "ON org_principals (bot_user_id) "
                "WHERE bot_user_id IS NOT NULL"
            )
        )

    # Phase 1E — nullable owner_principal_id on prioritized business resources.
    # NULL means unresolved (never global). No automatic inventing backfill here.
    _owner_principal_tables = (
        ("bot_users", "ix_bot_users_owner_principal_id"),
        ("orders", "ix_orders_owner_principal_id"),
        ("tickets", "ix_tickets_owner_principal_id"),
        ("user_services", "ix_user_services_owner_principal_id"),
    )
    for _tbl, _idx in _owner_principal_tables:
        if not insp.has_table(_tbl):
            continue
        _cols = {c["name"] for c in insp.get_columns(_tbl)}
        if "owner_principal_id" in _cols:
            continue
        sync_conn.execute(
            sql_text(
                f"ALTER TABLE {_tbl} ADD COLUMN owner_principal_id "
                "INTEGER REFERENCES org_principals(id)"
            )
        )
        sync_conn.execute(
            sql_text(
                f"CREATE INDEX IF NOT EXISTS {_idx} ON {_tbl} (owner_principal_id)"
            )
        )

    if insp.has_table("pg_staff_access"):
        scols = {c["name"] for c in insp.get_columns("pg_staff_access")}
        if "pg_admin_password_enc" not in scols:
            sync_conn.execute(
                sql_text("ALTER TABLE pg_staff_access ADD COLUMN pg_admin_password_enc TEXT")
            )
        if "pg_role_id" not in scols:
            sync_conn.execute(
                sql_text("ALTER TABLE pg_staff_access ADD COLUMN pg_role_id INTEGER")
            )

    if insp.has_table("panel_tickets"):
        tcols = {c["name"] for c in insp.get_columns("panel_tickets")}
        if "owner_unread" not in tcols:
            sync_conn.execute(
                sql_text("ALTER TABLE panel_tickets ADD COLUMN owner_unread BOOLEAN DEFAULT 1")
            )

    if insp.has_table("panel_ticket_messages"):
        mcols = {c["name"] for c in insp.get_columns("panel_ticket_messages")}
        alters_msg = {
            "attachment_path": "VARCHAR(512)",
            "attachment_name": "VARCHAR(255)",
            "attachment_mime": "VARCHAR(128)",
        }
        for col, typ in alters_msg.items():
            if col not in mcols:
                sync_conn.execute(sql_text(f"ALTER TABLE panel_ticket_messages ADD COLUMN {col} {typ}"))

    if insp.has_table("orders"):
        ocols = {c["name"] for c in insp.get_columns("orders")}
        if "quantity" not in ocols:
            sync_conn.execute(
                sql_text("ALTER TABLE orders ADD COLUMN quantity INTEGER DEFAULT 1")
            )

    if insp.has_table("tickets"):
        ticols = {c["name"] for c in insp.get_columns("tickets")}
        if "reseller_id" not in ticols:
            sync_conn.execute(
                sql_text("ALTER TABLE tickets ADD COLUMN reseller_id INTEGER")
            )
            try:
                sync_conn.execute(
                    sql_text(
                        "UPDATE tickets SET reseller_id = ("
                        "SELECT bot_users.reseller_id FROM bot_users "
                        "WHERE bot_users.id = tickets.user_id"
                        ") WHERE reseller_id IS NULL"
                    )
                )
            except Exception:
                pass

    if insp.has_table("bot_users"):
        bucols = {c["name"] for c in insp.get_columns("bot_users")}
        if "points_balance" not in bucols:
            sync_conn.execute(
                sql_text(
                    "ALTER TABLE bot_users ADD COLUMN points_balance INTEGER DEFAULT 0"
                )
            )

    if insp.has_table("loyalty_rewards"):
        lrcols = {c["name"] for c in insp.get_columns("loyalty_rewards")}
        if "min_purchase_toman" not in lrcols:
            sync_conn.execute(
                sql_text(
                    "ALTER TABLE loyalty_rewards ADD COLUMN min_purchase_toman INTEGER DEFAULT 0"
                )
            )
        if "max_discount_toman" not in lrcols:
            sync_conn.execute(
                sql_text("ALTER TABLE loyalty_rewards ADD COLUMN max_discount_toman INTEGER")
            )
        if "expires_days" not in lrcols:
            sync_conn.execute(
                sql_text("ALTER TABLE loyalty_rewards ADD COLUMN expires_days INTEGER")
            )
    if insp.has_table("reward_redemptions"):
        rrcols = {c["name"] for c in insp.get_columns("reward_redemptions")}
        if "discount_code" not in rrcols:
            sync_conn.execute(
                sql_text(
                    "ALTER TABLE reward_redemptions ADD COLUMN discount_code VARCHAR(64)"
                )
            )

    if insp.has_table("user_services"):
        uscols = {c["name"] for c in insp.get_columns("user_services")}
        if "quota_expire_at" not in uscols:
            sync_conn.execute(
                sql_text(
                    "ALTER TABLE user_services ADD COLUMN quota_expire_at DATETIME"
                )
            )
        if "quota_data_limit_bytes" not in uscols:
            sync_conn.execute(
                sql_text(
                    "ALTER TABLE user_services ADD COLUMN quota_data_limit_bytes BIGINT"
                )
            )
        if "quota_synced_at" not in uscols:
            sync_conn.execute(
                sql_text(
                    "ALTER TABLE user_services ADD COLUMN quota_synced_at DATETIME"
                )
            )

    marker = None
    try:
        from app.config import DATA_DIR

        marker = DATA_DIR / ".migrated_core_reseller_perms_v1"
        if not marker.exists():
            _ensure_core_reseller_perms(sync_conn, "reseller_profiles")
            _ensure_core_reseller_perms(sync_conn, "reseller_plans")
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            marker.write_text("1\n", encoding="utf-8")
    except Exception:
        _ensure_core_reseller_perms(sync_conn, "reseller_profiles")
        _ensure_core_reseller_perms(sync_conn, "reseller_plans")


def _seed_owner_if_no_depth0(sync_conn) -> None:
    """Insert the first Owner only when no depth-0 row exists (any status).

    A disabled Owner must stay disabled. Never insert a replacement.
    Duplicate depth-0 rows are left for application fail-closed logic.
    """
    from sqlalchemy import text as sql_text

    row = sync_conn.execute(
        sql_text(
            "SELECT COUNT(1) FROM org_principals "
            "WHERE depth = 0 AND parent_id IS NULL"
        )
    ).scalar()
    if row:
        return
    sync_conn.execute(
        sql_text(
            "INSERT INTO org_principals "
            "(parent_id, depth, status, pg_username, reseller_profile_id, "
            "pg_staff_id, bot_user_id) "
            "VALUES (NULL, 0, 'active', NULL, NULL, NULL, NULL)"
        )
    )


def _ensure_org_principal_aux_tables(sync_conn) -> None:
    """Create missing Phase 2A/2B tables without touching existing rows."""
    from app.db.models import OrgPrincipalProvision, OrgPrincipalWebIdentity

    OrgPrincipalProvision.__table__.create(sync_conn, checkfirst=True)
    OrgPrincipalWebIdentity.__table__.create(sync_conn, checkfirst=True)


def _missing_alembic_head_schema(sync_conn) -> list[str]:
    """Return required Phase 1–3 objects that are absent (never invent data)."""
    from sqlalchemy import inspect

    insp = inspect(sync_conn)
    missing: list[str] = []
    if not insp.has_table("org_principals"):
        missing.append("org_principals")
        return missing
    cols = {c["name"] for c in insp.get_columns("org_principals")}
    if "pg_password_enc" not in cols:
        missing.append("org_principals.pg_password_enc")
    if not insp.has_table("org_principal_provisions"):
        missing.append("org_principal_provisions")
    if not insp.has_table("org_principal_web_identities"):
        missing.append("org_principal_web_identities")
    idx_names = {ix.get("name") for ix in insp.get_indexes("org_principals")}
    if "uq_org_principals_bot_user_id" not in idx_names:
        missing.append("uq_org_principals_bot_user_id")
    return missing


def _assert_ready_to_stamp_head(sync_conn) -> None:
    """Refuse to stamp Alembic head when the compatibility migrator is incomplete."""
    missing = _missing_alembic_head_schema(sync_conn)
    if not missing:
        return
    raise RuntimeError(
        "Pre-Alembic database is missing required schema after the "
        "compatibility migrator: "
        + ", ".join(missing)
        + ". Refusing to stamp Alembic head as current."
    )


# Backwards-compatible alias used by older tests / callers
_migrate_sqlite = _migrate_sqlite_legacy


def _ensure_core_reseller_perms(sync_conn, table: str) -> None:
    from sqlalchemy import text as sql_text

    try:
        rows = sync_conn.execute(
            sql_text(f"SELECT id, web_permissions, bot_permissions FROM {table}")
        ).fetchall()
    except Exception:
        return
    for row in rows:
        rid, web, bot = row[0], row[1], row[2]
        new_web = _append_core_reseller_perms_csv(web)
        new_bot = _append_core_reseller_perms_csv(bot if bot is not None else web)
        if new_web != (web or "") or new_bot != (bot or ""):
            sync_conn.execute(
                sql_text(
                    f"UPDATE {table} SET web_permissions = :w, bot_permissions = :b WHERE id = :id"
                ),
                {"w": new_web or None, "b": new_bot or None, "id": rid},
            )


def _append_core_reseller_perms_csv(raw: str | None) -> str:
    raw = (raw or "").strip()
    if not raw:
        return "dashboard,orders,payments,plans,shop_settings,stats,tickets"
    parts = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    for key in ("shop_settings", "plans"):
        if key not in parts:
            parts.append(key)
    seen: list[str] = []
    for p in parts:
        if p not in seen:
            seen.append(p)
    return ",".join(seen)


def _ensure_indexes(sync_conn) -> None:
    """Additive indexes for frequent dashboard / payment filters (legacy SQLite path)."""
    from sqlalchemy import text as sql_text

    statements = (
        "CREATE INDEX IF NOT EXISTS ix_orders_reseller_id ON orders (reseller_id)",
        "CREATE INDEX IF NOT EXISTS ix_orders_plan_id ON orders (plan_id)",
        "CREATE INDEX IF NOT EXISTS ix_orders_service_id ON orders (service_id)",
        "CREATE INDEX IF NOT EXISTS ix_payments_order_id ON payments (order_id)",
        "CREATE INDEX IF NOT EXISTS ix_discount_codes_code ON discount_codes (code)",
        # Every reseller-scoped user query (bot "my customers", dashboards,
        # tenant-isolation filters) hits this column — see security audit note
        # on multi-tenant scale.
        "CREATE INDEX IF NOT EXISTS ix_bot_users_reseller_id ON bot_users (reseller_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_wallet_referral_reason "
        "ON wallet_transactions (user_id, reason) WHERE reason LIKE 'referral:%'",
    )
    for stmt in statements:
        try:
            sync_conn.execute(sql_text(stmt))
        except Exception:
            pass
