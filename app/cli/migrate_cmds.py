"""pgclock migrate — Alembic upgrade of the configured database."""

from __future__ import annotations

from app.cli.context import CliContext, database_url
from app.cli.output import header, kv, ok


def cmd_migrate(ctx: CliContext) -> int:
    """Run ``alembic upgrade head`` on the configured DATABASE_URL."""
    header("migrate")
    url = database_url(ctx.root)
    kv("database_url", _redact(url))
    from app.db.alembic_runner import current_revision, upgrade_head

    before = current_revision(url)
    kv("revision_before", before or "(none)")
    upgrade_head(url)
    after = current_revision(url)
    ok(f"upgraded to {after}")
    return 0


def _redact(url: str) -> str:
    if "://" not in url or "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "@" not in rest:
        return url
    creds, host = rest.rsplit("@", 1)
    if ":" in creds:
        user = creds.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host}"
    return f"{scheme}://***@{host}"
