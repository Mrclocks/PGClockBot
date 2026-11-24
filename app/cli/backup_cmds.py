"""pgclock backup / restore — thin wrappers over app.services.backup."""

from __future__ import annotations

from app.cli.context import CliContext, require_confirm
from app.cli.output import CliError, header, human_size, info, kv, ok, terminal_text, warn


def _bind_paths(ctx: CliContext) -> None:
    """Point backup module at the resolved install data dir."""
    import app.config as config
    from app.services import backup as backup_mod

    config.get_settings.cache_clear()
    # Do not rewrite ROOT_DIR (templates/code); only data + backup paths.
    config.DATA_DIR = ctx.data_dir
    backup_mod.DATA_DIR = ctx.data_dir
    backup_mod.BACKUP_DIR = ctx.data_dir / "backups"
    backup_mod.ROOT_DIR = ctx.root
    backup_mod.RESTORE_STATUS_FILE = ctx.data_dir / "backup_restore.json"


def cmd_backup(ctx: CliContext, *, note: str = "", include_env: bool = False) -> int:
    header("backup")
    _bind_paths(ctx)
    from app.services.backup import create_backup

    result = create_backup(
        note=note or "cli",
        include_env=include_env,
        created_by="cli",
    )
    if not result.get("ok"):
        raise CliError("Backup failed")
    ok(f"created {result.get('filename')}")
    kv("id", result.get("id"))
    kv("path", result.get("path"))
    kv("engine", result.get("db_engine"))
    kv("size", human_size(result.get("size")))
    kv("sha256", (result.get("sha256") or "")[:16] + "…")
    return 0


def cmd_restore(
    ctx: CliContext,
    *,
    backup_id: str,
    yes: bool = False,
    restore_env: bool = False,
    restart: bool = True,
) -> int:
    header("restore")
    if not backup_id:
        raise CliError("Usage: pgclock restore <backup_id> [--yes] [--env] [--no-restart]")
    _bind_paths(ctx)
    from app.services.backup import get_backup_path, list_backups, restore_backup

    path = get_backup_path(backup_id)
    if path is None:
        # Show available
        items = list_backups()[:10]
        warn("Backup not found. Recent backups:")
        for it in items:
            info(f"{it.get('id')}  {human_size(it.get('size'))}  {it.get('created_at')}")
        raise CliError(f"Unknown backup id: {backup_id}")

    require_confirm(
        f"Restore from {path.name}? This overwrites live data.",
        yes=yes,
    )
    result = restore_backup(
        path,
        restore_env=restore_env,
        safety_backup=True,
        restart=restart,
        actor="cli",
    )
    if not result.get("ok"):
        detail = result.get("error")
        raise CliError(f"Restore failed: {terminal_text(detail)}" if detail else "Restore failed")
    # The service's own message is Persian (it is shown in the web panel too).
    ok("restore complete")
    kv("safety_id", result.get("safety_id"))
    kv("restart", result.get("restart_scheduled"))
    return 0


def cmd_backup_list(ctx: CliContext) -> int:
    header("backups")
    _bind_paths(ctx)
    from app.services.backup import list_backups

    items = list_backups()
    if not items:
        info("No backups in data/backups/")
        return 0
    for it in items:
        print(
            f"  {it.get('id')}  {human_size(it.get('size')):>8}  "
            f"engine={it.get('app_version')}  {it.get('created_at')}"
        )
    # Also print db_engine from sidecar when present
    return 0
