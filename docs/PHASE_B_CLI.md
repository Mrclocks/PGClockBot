# Phase B — Global CLI (`pgclock`)

**Scope:** Global operations CLI only. Database behavior unchanged from Phase A.  
**Out of scope:** permissions, identity sync, PasarGuard admin logic, node management.

---

## Installation

From the project root (requires sudo once):

```bash
sudo bash scripts/install_global_cli.sh
# or explicitly:
sudo bash scripts/install_global_cli.sh /path/to/PGClockBot
```

This writes:

| Path | Purpose |
|------|---------|
| `/usr/local/bin/pgclock` | Global command |
| `/usr/local/lib/pgclockbot/install_root` | Absolute install path |
| `/usr/local/lib/pgclockbot/ctl` | (via install helper) constrained systemctl verbs |

`bash pgclock.sh install` / systemd install also refreshes the global CLI.

Without sudo, from the repo:

```bash
export PGCLOCK_HOME=/path/to/PGClockBot
./scripts/pgclock status
# or
.venv/bin/python -m app.cli status
```

---

## Commands

```bash
pgclock status
pgclock start
pgclock stop
pgclock restart
pgclock logs              # last 100 lines
pgclock logs -f           # follow
pgclock logs -n 200
pgclock health
pgclock backup [--note TEXT] [--no-env]
pgclock backup --list
pgclock restore <backup_id> [--yes] [--env] [--no-restart]
pgclock migrate                    # alembic upgrade head
pgclock doctor
```

Optional: `pgclock --root /path/to/install status`

---

## Examples

```bash
pgclock doctor
pgclock status
pgclock backup --note "pre-update"
pgclock restore 20260804-120030-4745d007 --yes --no-restart
pgclock migrate
pgclock restart
pgclock health
```

---

## Tests

```bash
.venv/bin/python -m pytest tests/test_phase_b_cli.py -q
```

---

## Final verification (Phase B complete)

```text
Fresh shell (env -i) from random dir → pgclock status OK (resolves install_root)
Service detect: status shows pgclockbot (active|inactive); start/stop/restart OK
  (verified with systemctl shim — this agent host has no systemd PID 1)
Health against live panel → /health {"ok": true}
Failure messages: unknown cmd exit 2; bad restore id / migrate usage exit 1 with clear text
backup.py unchanged in Phase B (diff vs Phase A tip = 0); CLI wraps create_backup/restore_backup
```
