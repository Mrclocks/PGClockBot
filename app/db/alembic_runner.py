"""Alembic runner — upgrade/downgrade/stamp without requiring CLI cwd."""

from __future__ import annotations

import logging

from alembic import command
from alembic.config import Config

from app.config import ROOT_DIR

log = logging.getLogger(__name__)

ALEMBIC_INI = ROOT_DIR / "alembic.ini"
ALEMBIC_DIR = ROOT_DIR / "alembic"


def alembic_config(database_url: str | None = None) -> Config:
    if not ALEMBIC_INI.is_file():
        raise FileNotFoundError(f"Missing {ALEMBIC_INI}")
    cfg = Config(str(ALEMBIC_INI))
    # The app already configured logging; alembic/env.py must not reset it.
    cfg.attributes["configure_logging"] = False
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    if database_url:
        from app.db.engine_url import to_sync_url

        cfg.set_main_option("sqlalchemy.url", to_sync_url(database_url))
    return cfg


def _sync_engine(url: str):
    from sqlalchemy import create_engine

    # psycopg3 honors connect_timeout; prevents Alembic hanging forever on bad PG.
    connect_args: dict = {}
    if "postgresql" in (url or ""):
        connect_args["connect_timeout"] = 15
    return create_engine(url, connect_args=connect_args) if connect_args else create_engine(url)


def current_revision(database_url: str | None = None) -> str | None:
    cfg = alembic_config(database_url)
    from alembic.runtime.migration import MigrationContext

    url = cfg.get_main_option("sqlalchemy.url")
    engine = _sync_engine(url)
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            return ctx.get_current_revision()
    finally:
        engine.dispose()


def upgrade_head(database_url: str | None = None) -> None:
    cfg = alembic_config(database_url)
    log.info("Alembic upgrade head")
    # Force connect timeout into the URL env Alembic/SQLAlchemy will use.
    url = cfg.get_main_option("sqlalchemy.url") or ""
    if url.startswith("postgresql") and "connect_timeout=" not in url:
        sep = "&" if "?" in url else "?"
        cfg.set_main_option("sqlalchemy.url", f"{url}{sep}connect_timeout=15")
    command.upgrade(cfg, "head")


def downgrade_one(database_url: str | None = None) -> None:
    cfg = alembic_config(database_url)
    command.downgrade(cfg, "-1")


def stamp_head(database_url: str | None = None) -> None:
    cfg = alembic_config(database_url)
    command.stamp(cfg, "head")


def heads() -> list[str]:
    cfg = alembic_config()
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(cfg)
    return list(script.get_heads())
