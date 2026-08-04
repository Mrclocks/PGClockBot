# PGClock Stable Upgrade — Architecture Migration Plan

**Version under analysis:** `3.8.3`  
**Branch:** `cursor/architecture-migration-plan-b96b`  
**Status:** Phase 1 analysis only — **no implementation in this revision**  
**Audience:** Owner / reviewers approving the controlled architecture migration

---

## 0. Executive verdict

PGClock is a working multi-tenant Telegram + web panel for PasarGuard, already hardened through several audit cycles. It is **not** yet a production-grade multi-admin / multi-reseller platform for thousands of users.

The blocking foundation issue is the database lifecycle:

| Area | Current state | Required state |
|------|---------------|----------------|
| Primary DB | SQLite by default (`data/bot.db`) | PostgreSQL default for production |
| Schema management | `Base.metadata.create_all` + manual `ALTER TABLE` | Alembic versioned migrations |
| Backup / restore | SQLite file ZIP only | Engine-aware (`pg_dump` / `pg_restore` + SQLite path retained for labs) |
| Ops CLI | Interactive `bash pgclock.sh` menu | Global `pgclock` command surface |
| Identity | Split web / PG passwords & policies | Unified identity + synchronized policy |
| Nodes | List + reconnect only | Full operational node control |
| Permissions | Strong reseller isolation; incomplete PG parity | Web never weaker than PasarGuard |

**Recommendation:** Approve this plan, then implement **phase by phase** on feature branches. Do not rewrite the application in one PR.

---

## 1. Current architecture report

### 1.1 System map (as implemented)

```text
┌─────────────────────────────────────────────────────────────────┐
│                     Host (Ubuntu + systemd)                      │
│  Unit: pgclockbot.service → .venv/bin/python run.py              │
│  Optional helper: /usr/local/lib/pgclockbot/ctl (+ sudoers)      │
└───────────────────────────────┬─────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────┐
│ app/main.py                                                      │
│  • FastAPI (Jinja web panel) on WEB_HOST:WEB_PORT                │
│  • aiogram bot (polling or webhook)                              │
│  • APScheduler jobs                                              │
│  • Reseller bot manager (extra Telegram bots)                    │
└───────┬─────────────────┬──────────────────┬────────────────────┘
        │                 │                  │
        ▼                 ▼                  ▼
   app/db/*          PasarGuard API     Telegram API
   SQLAlchemy async  (owner + reseller  (main + shop bots)
   SQLite default    clients)
```

| Layer | Components | Entry |
|-------|------------|-------|
| Process | FastAPI + aiogram + APScheduler + reseller bots | `run.py` → `app/main.py` |
| Web | Jinja templates, cookie sessions, Origin/Referer CSRF | `app/api/app.py`, `*_pages.py` |
| Bot | Reply menus, shop/wallet/support/admin/reseller | `app/bot/handlers/*` |
| PasarGuard | HTTP client, quotas, provision gates | `app/services/pasarguard.py`, `pg_quota.py`, `provision_gate.py` |
| Database | SQLAlchemy 2 async ORM | `app/db/models.py`, `app/db/session.py` |
| AuthN | Owner JSON + bcrypt; reseller/pg_staff DB hashes; bot roles | `web_auth.py`, `bot/auth.py` |
| AuthZ | Shared permission keys + PG role map + shop scope | `pg_access.py`, `shop_scope.py`, `reseller_access.py` |
| Billing | Fixed commission + PAYG | `billing.py`, scheduler |
| Backup | ZIP + SHA256 manifest | `backup.py` (SQLite-centric) |
| Ops | `pgclock.sh` interactive installer/manager | repo-local only |

### 1.2 Identity hierarchy (actual)

1. **Owner / platform web admin** — `data/web_admin.json` (bcrypt). Full panel. Uses owner PG credentials / optional `PG_ACCESS_TOKEN`.
2. **Bot `ADMIN_IDS`** — Telegram admin on main bot; not automatically web Owner.
3. **Reseller** — `ResellerProfile` with shop scope, optional linked PG admin (`pg_admin_username` + encrypted password), web hash, billing.
4. **pg_staff** — `PgStaffAccess` with web credentials only; **no stored PG password**; PG mutations use owner client + local ACL/quota guards + `set_owner`.
5. **End users** — `BotUser` / shop customers; services mapped to PasarGuard users.

There is **no first-class Sub-admin model**. “Sub-admin” today is either pg_staff or a reseller-backed PG admin.

### 1.3 Database inventory (preserve through migration)

| Table | Model | Domain |
|-------|-------|--------|
| `bot_users` | `BotUser` | Users, roles, wallet, referral, reseller link |
| `plans` | `Plan` | Shop plans + PG template/group naming |
| `orders` | `Order` | Orders |
| `payments` | `Payment` | Payments / receipts / topups |
| `user_services` | `UserService` | Delivered PG services |
| `tickets` / `ticket_messages` | Ticket* | Shop support |
| `panel_tickets` / `panel_ticket_messages` | PanelTicket* | Panel support |
| `discount_codes` | `DiscountCode` | Discounts |
| `broadcast_logs` | `BroadcastLog` | Broadcasts |
| `reseller_profiles` | `ResellerProfile` | Resellers, web/PG creds, bots, billing |
| `reseller_billing_transactions` | `ResellerBillingTransaction` | Billing ledger (idempotent) |
| `reseller_billing_rates` | `ResellerBillingRate` | PAYG rates |
| `reseller_settings` | `ResellerSetting` | Per-shop settings |
| `reseller_plans` | `ResellerPlan` | Reseller packages |
| `reseller_applications` | `ResellerApplication` | Applications |
| `settings` | `Setting` | Global settings KV |
| `wallet_transactions` | `WalletTransaction` | Wallet ledger |
| `pg_staff_access` | `PgStaffAccess` | pg_staff web access |
| `trial_claims` | `TrialClaim` | One trial per user/shop |

**Out-of-DB state that must also be preserved:**

- `data/web_admin.json` — Owner credentials  
- `.env` — secrets and `DATABASE_URL`  
- `data/uploads/**`, `data/private/**`, setup flags, webhook secret files  
- Filesystem migration marker: `data/.migrated_core_reseller_perms_v1` (anti-pattern)

### 1.4 How schema is managed today

```26:37:app/db/session.py
async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_sqlite)
        if _db_url.startswith("sqlite"):
            ...
            await conn.run_sync(_ensure_indexes)
```

- Production schema = `create_all` + additive `_migrate_sqlite()` `ALTER TABLE`s.
- **No Alembic.** No revision history. No downgrade path.
- Referral partial unique index (`uq_wallet_referral_reason`) is created **only on SQLite**.
- Installer always writes SQLite `DATABASE_URL` (`pgclock.sh`).
- Drivers already present: `aiosqlite` + `asyncpg`.

### 1.5 PasarGuard integration (current)

Supported client surfaces include users, templates, groups, hosts, admins, and nodes (list/simple/realtime/reconnect).

**Admin creation path gaps:**

- Role `role_id` may be dropped on PG rejection (silent weaker admin).
- Web password policy is not always applied before PG admin create.
- pg_staff cannot authenticate to PG as itself → owner-client + local mirrors.

**Quota coverage:**

- Strong for user max / traffic / expiry on create-modify paths.
- Weak or missing for resource counts (nodes, hosts, groups, templates, inbounds) beyond limited/disabled write gates.

**Node management:** list + reconnect only (web + bot). No restart, health probe, sync, diagnostics, logs, or create/edit/delete.

### 1.6 CLI / deployment (current)

| Capability | Today |
|------------|-------|
| Install / update / uninstall | `bash pgclock.sh …` |
| Status | Top-level `status` |
| Start / stop / restart / logs | Interactive **service menu only** |
| Health | Embedded in status / web menu |
| Backup / restore | Web + Telegram admin — **not CLI** |
| Migrate | Implicit on startup |
| Doctor | Missing |
| Global `pgclock` binary | **Does not exist** |

Service: systemd unit `pgclockbot` with `Restart=always`. Fallback: `nohup` to `/tmp/pgclock-panel.log`.

### 1.7 Test posture

~99 test modules, largely version-tagged regression / UI / security fixtures. Strong coverage for recent audit fixes; **no** dedicated PostgreSQL migration / Alembic / CLI / node-ops suites.

---

## 2. Problems found

### P0 — Must fix for stable production

1. **SQLite is the production default** despite multi-admin / multi-reseller ambitions. Concurrent writes, backup, and scale are limited.
2. **No Alembic** — schema evolution is ad hoc; rollback is impossible; fresh vs upgraded DBs diverge (missing FKs/uniques/indexes after manual ALTER).
3. **Backup/restore is SQLite-file only** — unsafe/wrong if `DATABASE_URL` is PostgreSQL.
4. **pg_staff owner-client pattern** — local ACL must be perfect or it is privilege escalation; incomplete resource quotas increase risk.
5. **Identity split** — web vs PG passwords/policies can diverge; conversion paths can leave reseller without usable `pg_admin_password_enc`.

### P1 — Architecture blockers for stated goals

6. **Incomplete PasarGuard admin limitation parity** (HWID/device/resource limits, granular CRUD permissions).
7. **Incomplete node operations** — reconnect is not an ops platform.
8. **No global CLI** — operators must enter the repo and use menus.
9. **Filesystem migration markers** outside DB state.
10. **Installer ignores PostgreSQL** — always scaffolds SQLite.

### P2 — Hardening / UX debt (from 3.8.3 audit + this review)

11. CSP `'unsafe-inline'`; PG gate fail-open; in-process rate limits; plaintext reseller `bot_token`.
12. Silent role_id fallback on admin create; uneven password validation on `/pg/admins`.
13. Hosts/nodes visibility not fully ownership-scoped for non-owner roles.
14. False/ambiguous success surfaces and missing progress for long ops (backup/restore/migrate/sync).
15. Public health endpoint is `{ok: true}` only — insufficient for `pgclock health` / doctor.

---

## 3. Proposed architecture

### 3.1 Target platform shape

```text
                    ┌──────────────────────────┐
                    │   Global CLI: pgclock    │
                    │ status|start|stop|…|doctor│
                    └────────────┬─────────────┘
                                 │
              systemd / service_control / alembic / backup
                                 │
┌────────────────────────────────▼────────────────────────────────┐
│                     PGClock Application Runtime                  │
│  FastAPI panel │ Telegram bots │ Scheduler │ Reseller bots       │
└───────┬───────────────────┬───────────────────┬─────────────────┘
        │                   │                   │
        ▼                   ▼                   ▼
┌───────────────┐   ┌───────────────┐   ┌───────────────────────┐
│ PostgreSQL    │   │ PasarGuard    │   │ Secure secret store   │
│ (primary)     │   │ Unified Identity│ │ WEB_SECRET / Fernet   │
│ Alembic-managed│  │ sync adapter  │   │ (no plaintext bots)   │
└───────────────┘   └───────────────┘   └───────────────────────┘
```

### 3.2 Database foundation (Phase A)

- **Production default:** `postgresql+asyncpg://…`
- **Dev/lab:** SQLite retained via explicit `DATABASE_URL`
- Keep SQLAlchemy async models; **do not** invent a second ORM
- Alembic:
  - baseline revision = current models
  - all future DDL via revisions
  - remove production dependence on `create_all` and `_migrate_sqlite`
- Engine-aware backup:
  - PostgreSQL → `pg_dump` custom format + app files ZIP
  - SQLite → existing online backup path
- Validation suite for row counts / critical FK integrity / wallet & billing balances after migrate

### 3.3 Unified identity (Phase D)

One account model across Web + PasarGuard:

| Concern | Target |
|---------|--------|
| Username | Same identifier where PG-linked |
| Password | Same policy (length, upper, lower, special, digits as required by PG) |
| Status | Active/disabled synced |
| Permissions | Panel never grants more than PG role |
| Limitations | Users/traffic/expiry/HWID/device + resource limits enforced locally **and** reflected from PG |
| Storage | bcrypt for web; encrypted PG password only when needed for non-owner client; prefer PG-native auth for pg_staff |

**Target create flow (admins):**

1. Create PasarGuard account with role + limits  
2. Enforce PG password policy locally before API call  
3. Create Web Panel permissions / staff or reseller binding  
4. Store credentials securely  
5. Verify round-trip sync (login web + PG action as that identity)

### 3.4 Permission model (Phase C + F)

- Owner isolation, Admin isolation, Reseller shop isolation, pg_staff scoped access — **non-negotiable**
- Never introduce Owner token fallback for reseller mutations
- Prefer authenticating as the acting PG admin; if impossible, fail closed rather than silently escalate
- Web UI must expose all relevant PG limitations when creating Owner/Admin/Sub-admin/Reseller/pg_staff

### 3.5 Node management (Phase E)

For permitted actors: status, health check, restart, reconnect, synchronization, diagnostics, connection test, error reporting, logs when API allows. Every op: permission + ownership + input validation + real status + audit log. **No fake success.**

### 3.6 Global CLI (Phase B)

Install `/usr/local/bin/pgclock` (or equivalent) wrapping project-aware commands:

`status | start | stop | restart | logs | health | backup | restore | migrate | doctor`

Requirements: works outside project directory, clear errors, permission checks, compatible with systemd deployment.

---

## 4. Migration phases (implementation order)

> **Gate:** This document is Phase 1 deliverable. Coding starts only after approval.

### Phase A — Database foundation (first implementation)

**Goal:** PostgreSQL primary + Alembic + safe data migration + engine-aware backup.

| Step | Work |
|------|------|
| A1 | Add Alembic project (`alembic.ini`, `alembic/env.py` async) |
| A2 | Generate baseline revision from current `Base.metadata` |
| A3 | Encode legacy `_migrate_sqlite` outcomes into baseline / follow-up revisions |
| A4 | Add PostgreSQL-specific indexes (incl. referral partial unique) |
| A5 | Change production defaults: installer + `.env.example` prefer PostgreSQL |
| A6 | Gate `create_all` to non-production / empty bootstrap only (or remove) |
| A7 | SQLite→PostgreSQL data migration tool (`pgclock migrate` precursor) |
| A8 | Pre-migration backup + validation + restore procedure |
| A9 | Upgrade backup service for `pg_dump`/`pg_restore` |
| A10 | Regression tests: migrate, rollback, backup, restore, count checks |

**Preserve:** all tables listed in §1.3 + `web_admin.json` + uploads/private + settings.

### Phase B — Global CLI

| Step | Work |
|------|------|
| B1 | Installable `pgclock` entrypoint resolving install root |
| B2 | Non-interactive: status/start/stop/restart/logs/health |
| B3 | backup/restore/migrate wired to Phase A services |
| B4 | `doctor` — env, DB connectivity, Alembic head, PG API, systemd, disk, permissions |
| B5 | Deprecate menu-only ops for production runbooks (keep menu for install UX) |

### Phase C — Complete PasarGuard admin integration

| Step | Work |
|------|------|
| C1 | Inventory PasarGuard admin/role limitation API fields |
| C2 | Extend create/edit UI for Owner/Admin/Sub-admin/Reseller/pg_staff |
| C3 | Enforce user limits: max users, traffic, expiry, HWID, device |
| C4 | Enforce resource limits: nodes, hosts, groups, templates, inbounds |
| C5 | Enforce CRUD/view/manage permissions; fail closed |
| C6 | Remove silent `role_id` drop; surface errors instead |
| C7 | Tests: quotas and permission matrix per role |

### Phase D — Unified identity

| Step | Work |
|------|------|
| D1 | Single password policy module matching PasarGuard |
| D2 | Create/change password always syncs PG + web |
| D3 | Store/use pg_staff PG credentials or token securely (eliminate owner-client for staff mutations) |
| D4 | Fix reseller conversion without PG password |
| D5 | Sync account status (disabled → deny web + bot + PG ops) |
| D6 | Verification harness after every identity mutation |

### Phase E — Advanced node management

| Step | Work |
|------|------|
| E1 | Expand PasarGuard client for restart/health/sync/logs/diagnostics as API allows |
| E2 | Web + bot UIs with progress + real status |
| E3 | Permission/ownership checks + action audit log |
| E4 | Never report success without confirmed PG response |

### Phase F — Security hardening (continuous, enforced each phase)

- No Owner token fallback for cross-tenant work  
- No privilege escalation / cross-admin / cross-reseller access  
- Encrypt reseller bot tokens; rotate/reencrypt tooling  
- Prefer fail-closed PG gates under `STRICT_PG_GATE` for production  
- Persist rate limits or document reverse-proxy requirement  

### Phase G — UX reliability

- Remove false alerts / fake success  
- Progress indicators for backup, restore, migration, synchronization  
- Clear actionable errors  

### Phase H — Full regression suite

Cover Owner, Admin, Reseller, pg_staff, Bot, Web, Database, PasarGuard sync matrices defined in the project brief.

---

## 5. SQLite → PostgreSQL migration plan

### 5.1 Preconditions

1. Freeze writes (stop bot/panel or enable maintenance mode).  
2. Take **full ZIP backup** (current tool) + copy `.env` / `data/`.  
3. Provision PostgreSQL 14+ (recommended 16), database `pgclock`, role with limited privileges.  
4. Confirm disk space ≥ 3× SQLite DB size.  
5. Record Alembic head and app `VERSION`.

### 5.2 Procedure

```text
1. pgclock backup                  # or existing web/bot backup
2. Create empty PostgreSQL database
3. Set DATABASE_URL=postgresql+asyncpg://...
4. pgclock migrate                 # alembic upgrade head → schema
5. Run offline ETL:
     SQLite dump → type normalize → COPY/INSERT into PostgreSQL
6. Validate (§5.3)
7. Switch service to new DATABASE_URL
8. pgclock health && pgclock doctor
9. Smoke: owner login, reseller login, pg_staff, bot /start, create test user
10. Keep SQLite file offline for rollback window
```

### 5.3 Data validation checklist

| Check | Method |
|-------|--------|
| Row counts per table | SQLite `COUNT(*)` vs PG |
| Owner admin file present | `web_admin.json` unchanged |
| Reseller profiles | usernames, billing balances, watermark |
| Wallet balances | `bot_users.wallet_balance` vs last `wallet_transactions` |
| Orders / payments | status histograms match |
| Tickets | open counts match |
| Settings KV | key set equality |
| pg_staff | active rows match |
| FK integrity | PG `NOT VALID` / constraint verify |
| String lengths | reject/truncate report for `VARCHAR(n)` overflows |
| Referral uniqueness | partial unique index present |
| Billing idempotency | unique on `idempotency_key` |

### 5.4 Backup before migration

- Mandatory safety archive via existing backup service **before** cutover.  
- Additionally: filesystem snapshot or `cp -a data data.pre-pg-$(date -u +%Y%m%dT%H%M%SZ)`.  
- After PostgreSQL cutover: first `pg_dump` backup must succeed before deleting SQLite.

### 5.5 Restore procedure

| Scenario | Action |
|----------|--------|
| Failed ETL before cutover | Discard PG DB; restore SQLite from safety ZIP; keep old `DATABASE_URL` |
| Failed after cutover | `pg_restore` from pre-cutover dump **or** point `DATABASE_URL` back to SQLite file from safety ZIP + restart |
| Partial data corruption | Restore PG dump; re-run validation; do not mix SQLite row patches into live PG |

---

## 6. Risk analysis

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| Data loss during SQLite→PG | Critical | Medium | Mandatory backup; dual retention; validation gates |
| Schema drift (legacy SQLite missing constraints) | High | High | Baseline Alembic + repair revision; validate FKs |
| VARCHAR length violations on PG | High | Medium | Pre-scan + report; truncate only with operator approval |
| Referral race without partial unique index | High | Medium | Create PG equivalent index in baseline |
| Backup silently backs up wrong engine | Critical | High today | Engine-aware backup; refuse mismatched restore |
| pg_staff owner-client bypass | Critical | Medium | Phase D identity; until then expand quota/ACL tests |
| role_id silent drop | High | Medium | Fail closed; surface PG error |
| CLI misuse (restore/migrate) | High | Medium | Confirmations, dry-run, permission checks |
| Downtime during cutover | Medium | High | Maintenance window; freeze writes |
| Dual-password divergence during Phase D | High | Medium | Sync-on-write; verification harness |

---

## 7. Rollback strategy

### 7.1 Per-phase rollback

| Phase | Rollback |
|-------|----------|
| A (DB) | Keep SQLite artifact; revert `DATABASE_URL`; `alembic downgrade` only when revision is proven reversible; restore ZIP if needed |
| B (CLI) | Remove `/usr/local/bin/pgclock`; fall back to `bash pgclock.sh` |
| C–E | Feature-flag or revert PR branch; no DB drop |
| F–H | Standard git revert + migration downgrade if schema touched |

### 7.2 Production cutover rollback (DB)

1. Stop service.  
2. Restore previous `.env` (`DATABASE_URL` → SQLite).  
3. Restore `data/bot.db` from pre-migration safety backup if modified.  
4. Start service.  
5. Confirm `/health` and owner login.  
6. Quarantine the abandoned PostgreSQL database for forensics (do not drop immediately).

### 7.3 Non-goals for rollback

- Do not attempt bidirectional live sync between SQLite and PostgreSQL.  
- Do not use `create_all` as a rollback mechanism.  
- Do not restore PG dumps into SQLite.

---

## 8. Implementation constraints (binding)

1. **No large uncontrolled rewrite.**  
2. **No direct work on `main`.** Feature branches: `cursor/<name>-b96b`.  
3. **No coding until this plan is approved** (this document is the approval artifact).  
4. After each phase: changed files, DB changes, migration instructions, tests, remaining risks.  
5. Security invariants from Phase 6 of the brief are mandatory in every PR.  
6. Preserve current functionality unless a change is required for correctness/security.

---

## 9. Proposed first implementation PR (after approval)

**Scope:** Phase A only (foundation).

Suggested sequence of commits/PRs:

1. Alembic scaffolding + baseline revision (no behavior change if still on SQLite).  
2. Stop using `_migrate_sqlite` / production `create_all` once baseline applied.  
3. PostgreSQL defaults in installer + docs.  
4. Offline SQLite→PG migrator + validation.  
5. Engine-aware backup/restore.  
6. Tests.

**Out of scope for first coding PR:** CLI rewrite, identity unification, node ops, UI redesign.

---

## 10. Approval checklist

Reviewers should confirm:

- [ ] Current architecture report matches production understanding  
- [ ] PostgreSQL-as-default is accepted for production installs  
- [ ] Alembic replaces `create_all` / manual ALTER for schema management  
- [ ] SQLite→PG plan, validation, backup, restore are acceptable  
- [ ] Phase order A→B→C→D→E (+F/G/H continuous) is approved  
- [ ] Authorization to begin Phase A implementation on a new branch after sign-off  

---

*Prepared as Lead Architect takeover analysis for the PGClock stable upgrade. Based on inspection of repository code, models, configuration, integrations, CLI, and `docs/AUDIT_3_8_3.md` — not on prior agent assumptions.*
