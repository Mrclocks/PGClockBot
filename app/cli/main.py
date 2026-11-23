"""pgclock — global CLI entrypoint."""

from __future__ import annotations

import argparse
import sys

from app.cli.output import CliError, err


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pgclock",
        description="PGClockBot global operations CLI",
    )
    p.add_argument(
        "--root",
        default="",
        help="Install directory (default: PGCLOCK_HOME or /usr/local/lib/pgclockbot/install_root)",
    )
    p.add_argument("--json", action="store_true", help="Reserved for machine output")

    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Install + service + health overview")
    sub.add_parser("start", help="Start systemd service")
    sub.add_parser("stop", help="Stop systemd service")
    sub.add_parser("restart", help="Restart systemd service")

    logs = sub.add_parser("logs", help="Show service logs")
    logs.add_argument("-f", "--follow", action="store_true", help="Follow journalctl")
    logs.add_argument("-n", "--lines", type=int, default=100, help="Number of lines")

    health = sub.add_parser("health", help="Probe local /health")
    health.add_argument("--detail", action="store_true", help="Hint for authenticated detail")

    bak = sub.add_parser("backup", help="Create a full backup archive")
    bak.add_argument("--note", default="cli", help="Backup note")
    bak.add_argument(
        "--with-env",
        action="store_true",
        help="Include .env (tokens/secrets) — opt-in; default excludes .env",
    )
    bak.add_argument(
        "--no-env",
        action="store_true",
        help="Deprecated alias: exclude .env (already the default)",
    )
    bak.add_argument("--list", action="store_true", help="List backups only")

    rst = sub.add_parser("restore", help="Restore from a backup id")
    rst.add_argument("backup_id", help="Backup id from data/backups/")
    rst.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")
    rst.add_argument("--env", action="store_true", help="Also restore .env")
    rst.add_argument("--no-restart", action="store_true", help="Do not schedule service restart")

    sub.add_parser("migrate", help="Apply Alembic migrations")

    rev = sub.add_parser("shop-review", help="List or assign services whose selling shop is unknown")
    rev.add_argument("action", choices=("list", "assign"))
    rev.add_argument("--service", type=int, default=0, help="Service id (assign)")
    rev.add_argument("--shop", default="", help="Shop owner's user id, or 'platform' (assign)")

    doc = sub.add_parser("doctor", help="Run read-only diagnostics (OK/WARN/FAIL)")
    doc.add_argument(
        "--json",
        action="store_true",
        dest="doctor_json",
        help="Machine-readable JSON report",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    import os

    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)

    # build_context chdirs into the install root; always restore caller cwd
    # (keeps pytest source-guard suites and nested CLI invocations sane).
    prev_cwd = os.getcwd()
    try:
        from app.cli.context import build_context

        ctx = build_context(root=args.root or None, json_mode=bool(args.json))

        cmd = args.command
        if cmd == "status":
            from app.cli.service_cmds import cmd_status

            return cmd_status(ctx)
        if cmd == "start":
            from app.cli.service_cmds import cmd_start

            return cmd_start(ctx)
        if cmd == "stop":
            from app.cli.service_cmds import cmd_stop

            return cmd_stop(ctx)
        if cmd == "restart":
            from app.cli.service_cmds import cmd_restart

            return cmd_restart(ctx)
        if cmd == "logs":
            from app.cli.service_cmds import cmd_logs

            return cmd_logs(ctx, follow=bool(args.follow), lines=int(args.lines))
        if cmd == "health":
            from app.cli.service_cmds import cmd_health

            return cmd_health(ctx, detail=bool(args.detail))
        if cmd == "backup":
            from app.cli.backup_cmds import cmd_backup, cmd_backup_list

            if args.list:
                return cmd_backup_list(ctx)
            return cmd_backup(
                ctx,
                note=args.note,
                include_env=bool(args.with_env) and not bool(args.no_env),
            )
        if cmd == "restore":
            from app.cli.backup_cmds import cmd_restore

            return cmd_restore(
                ctx,
                backup_id=args.backup_id,
                yes=bool(args.yes),
                restore_env=bool(args.env),
                restart=not bool(args.no_restart),
            )
        if cmd == "migrate":
            from app.cli.migrate_cmds import cmd_migrate

            return cmd_migrate(ctx)
        if cmd == "shop-review":
            from app.cli.shop_review_cmds import cmd_shop_review_assign, cmd_shop_review_list

            if args.action == "list":
                return cmd_shop_review_list(ctx)
            if not args.service or not args.shop:
                raise CliError("assign needs --service and --shop")
            return cmd_shop_review_assign(ctx, service_id=args.service, shop=args.shop)
        if cmd == "doctor":
            from app.cli.doctor_cmds import cmd_doctor

            if getattr(args, "doctor_json", False):
                ctx.json_mode = True
            return cmd_doctor(ctx)

        parser.error(f"unknown command: {cmd}")
        return 2
    except CliError as e:
        err(str(e))
        return int(e.code)
    except KeyboardInterrupt:
        err("Interrupted")
        return 130
    except Exception as e:
        err(f"Unexpected error: {e}")
        return 1
    finally:
        try:
            os.chdir(prev_cwd)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
