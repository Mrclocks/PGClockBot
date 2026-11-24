# Phase A — Database Foundation

**Version:** builds on 3.8.3  
**Scope:** PostgreSQL + Alembic + backup/restore  
**Out of scope:** global CLI, identity sync, permissions parity, node ops

**Runtime model:** one PostgreSQL `DATABASE_URL` per process, auto-provisioned by
`pgclock.sh install`. PostgreSQL is the only supported database; the app refuses to
start without it. There is no dual-write / dual-runtime path.

---

## Phase 0 — Baseline (required before cutover)

```bash
# Ensure schema exists (creates/stamps Alembic on current DATABASE_URL)
.venv/bin/python -m scripts.alembic_upgrade

# Create production baseline: ZIP backup (pg_dump) + configs + git/version + restore verify
.venv/bin/python -m scripts.phase0_baseline --note "pre-phase-a"
```

Artifacts land in `data/baselines/phase0-<timestamp>/`:

| File | Purpose |
|------|---------|
| `BASELINE.json` | Git commit, version, hashes, restore verification |
| `pgclock-backup-*.zip` | Full archive |
| `config/env` | `.env` snapshot |
| `config/web_admin.json` | Owner credentials (if present) |

Restore verification is a **sandbox dry-run** (live data is not overwritten).

---

## Phase A — PostgreSQL production setup

**Default path:** `bash pgclock.sh install` installs PostgreSQL packages, starts
the service, creates role/database `pgclock`, and writes `DATABASE_URL` into `.env`.

Optional override (managed/remote Postgres):

```bash
export PGCLOCK_DATABASE_URL="postgresql+asyncpg://user:SECRET@host:5432/db"
bash pgclock.sh install
```

Manual provision (if you are not using the installer):

```bash
sudo bash scripts/setup_postgres.sh pgclock pgclock
# prints DATABASE_URL=postgresql+asyncpg://...
.venv/bin/python -m scripts.alembic_upgrade
```

Recommended `.env`:

```env
DATABASE_URL="postgresql+asyncpg://pgclock:SECRET@127.0.0.1:5432/pgclock"
```

---

## Alembic

```bash
.venv/bin/python -m scripts.alembic_upgrade          # upgrade head
.venv/bin/python -m scripts.alembic_upgrade --current
.venv/bin/python -m scripts.alembic_upgrade --downgrade
```

Baseline revision: `0001_baseline` (full schema from `app.db.models` + `uq_wallet_referral_reason`).

`init_db()` on startup:

1. If tables exist but `alembic_version` missing → check the head schema is present → `stamp head`
2. Else → `alembic upgrade head`
3. `create_all` only as emergency fallback (empty DB / `PGCLOCK_ALLOW_CREATE_ALL=1`)

---

## Backup / restore

| Engine | Archive member | Tool |
|--------|----------------|------|
| PostgreSQL | `data/postgres.dump` | `pg_dump -Fc` / `pg_restore` |

The manifest records `db_engine: postgresql`. Restore **refuses** an archive without a PostgreSQL dump.

---

## Rollback

1. Stop service  
2. Restore the database from a backup ZIP (`pgclock restore <backup_id>` or the panel's backup page)  
3. Start service  

Alembic downgrade: `.venv/bin/python -m scripts.alembic_upgrade --downgrade` (drops baseline schema — destructive).
