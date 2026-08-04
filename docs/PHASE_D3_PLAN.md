# Phase D3 — Legacy Account Migration (Planning Only)

**Status:** AWAITING APPROVAL — **no implementation**  
**Depends on:** D2 approved (`cursor/phase-d2-grant-sync-b96b`)  
**Branch (docs):** `cursor/phase-d3-plan-b96b`  
**Binding:** Existing `pg_staff` rows **remain** `pg_staff`. No forced reseller conversion. No Owner credential fallback. Valid logins must keep working.

---

## Goals

1. Remediate legacy `PgStaffAccess` so PG data access works **without** changing role to reseller.  
2. Make credential readiness **visible** to Owner (and clearly messaged to staff).  
3. Support operators who must fix `web_username ≠ pg_username` (D2 Q1: error-only — no auto-rename).  
4. Preserve: web login with existing bcrypt hash; fail-closed PG ops until enc exists; C5 client rules.  
5. Document rollback risks (prefer forward fix).

**Out of scope:** D4 Bot identity · authz/permission changes · automatic staff→reseller · Owner↔`PG_PASSWORD` sync · new Alembic unless explicitly approved for optional reporting helpers.

---

## 1. Legacy cohorts (analysis)

| Cohort | DB state | Web login today | PG menus/data today | Blocker to full PG access |
|--------|----------|-----------------|---------------------|---------------------------|
| **L1** Null enc, `web == pg` | `pg_admin_password_enc` NULL | ✓ (hash) | Overview-only; lists empty; writes denied | Need password set → enc + `modify_admin` |
| **L2** Null enc, `web ≠ pg` | NULL enc + mismatch | ✓ | Same fail-closed | Must fix username **manually** then set password (D2 blocks update/change if mismatch) |
| **L3** Enc present, `web == pg` | Healthy | ✓ | Full mapped menus when role allows | None (already good) |
| **L4** Enc present, `web ≠ pg` | Enc OK but D2 equality breaks future password/username edits | ✓ login | Likely works if session already has creds | Edits fail until username aligned manually |
| **L5** Inactive staff | `is_active=false` | Denied | N/A | Reactivate or revoke |
| **L6** Reseller with null enc | `ResellerProfile` | Shop login may work | PG fail-closed | Reseller edit / password path (not D3 staff focus) |
| **L7** Already converted to reseller (pre-D2) | No staff row | Reseller | Shop+PG if enc set | Leave as reseller; no auto-revert |

**Principle:** L1–L5 stay **pg_staff**. D3 never calls `provision_existing_pg_admin` for remediation.

---

## 2. Current remediation capabilities (post-D2)

Already available (no new product required for *minimum* ops):

| Actor | Action | Service | Notes |
|-------|--------|---------|-------|
| Owner | Edit staff modal → password | `update_web_access` | Requires `web == pg`; password required if enc missing |
| Owner | Revoke / toggle | existing routes | Unchanged |
| Staff | `/security` password change | `change_staff_credentials` | Requires `new_username == pg_username` |
| — | Auto-backfill enc from hash | **Impossible** | bcrypt is one-way |

**Gaps D3 should close (product):**

| Gap | Impact |
|-----|--------|
| No Owner badge for missing enc / username mismatch | Operators cannot see who needs remediation |
| Staff see isolation message but no “ask Owner to reset password” / readiness copy on overview | UX unclear |
| Inventory only via raw SQL | Error-prone |
| Legacy migration doc still mentions old «ارتقای» conversion path | Docs drift |
| Mismatch (L2/L4): Owner cannot fix username via staff modal (readonly = PG name) if DB `web_username` differs — **update requires posted web_username == pg**, so Owner edit with password **can** set web to pg if form posts `uname|lower`… | Verify: staff modal posts readonly `uname|lower` as `web_username`. That **forces** equality on update and would **rename** web to pg when Owner saves password. Conflict with D2 Q1 “error-only / no auto-rename”? |

### Q1 interaction (must resolve in D3 approval)

D2 Q1 said: **error-only, do not auto-rename**.

Staff Owner form currently posts `web_username = uname|lower` (PG name). Calling `update_web_access` with that value when DB has `web ≠ pg` would **change** `web_username` to match PG — that is a rename.

**D3 recommendation (pick one in approval):**

| Option | Behavior |
|--------|----------|
| **A (recommended)** | Owner remediation **explicitly** allows setting `web_username := pg_username` only when operator confirms checkbox «هم‌ترازسازی نام کاربری با پاسارگارد» — not silent; still not free-form rename to arbitrary names |
| **B** | Strict Q1: if mismatch, Owner update **errors** until a dedicated “align username” action runs (same outcome, more clicks) |
| **C** | Keep current form behavior (posting PG name always aligns on save) — treat as intentional Owner remediation, document as exception to Q1 for Owner-only path |

Self-serve `/security` must remain **error-only** if staff tries `new_username ≠ pg` (already D2).

---

## 3. Proposed D3 architecture

### 3.1 Visibility

Extend `ExistingWebAccess` / `web_status` for `source=pg_staff`:

```text
credentials_ready: bool   # staff_has_stored_pg_password
username_aligned: bool    # lower(web) == lower(pg)
needs_remediation: bool   # not ready or not aligned
```

Show on `/pg/admins` cell: badges e.g. «رمز PG ذخیره‌شده» / «نیاز به همگام‌سازی رمز» / «نام کاربری ناهماهنگ».

Optional flash on pg_staff `/pg` overview when `not pg_credentials_ready`: clear Persian CTA (change password via Owner or `/security` if aligned).

### 3.2 Remediation flows (no conversion)

```text
Owner → /pg/admins → ویرایش ادمین فرعی
  ├─ if mismatch → align step (per Q1 option A/B/C)
  └─ set D1-strong password → update_web_access
        → bcrypt + enc + modify_admin + reset_pg
        → row stays PgStaffAccess

Staff (aligned only) → /security → change password
  → same sync; stays pg_staff
```

**Never** in D3 remediation:

- `provision_existing_pg_admin`  
- `revoke_web_access` as side effect of “fix”  
- Owner `get_pg()` for staff list/mutate  

### 3.3 Password sync requirements

| Step | Required |
|------|----------|
| Validate | D1 `validate_password_strength(..., username=pg_u)` |
| Store web | bcrypt `web_password_hash` |
| Store PG | Fernet `pg_admin_password_enc` (fail if None) |
| Live PG | Owner `modify_admin` password only |
| Cache | `reset_pg()` |
| Session | Staff re-login or live `require_staff` sets `pg_credentials_ready` |

Cannot sync without plaintext password (no reverse of bcrypt).

### 3.4 Optional CLI / script (ops)

`scripts/list_pg_staff_remediation.py` (or `pgclock` subcommand if Phase B CLI allows): print L1/L2/L4 rows. **No** password in CLI; no auto-write.

### 3.5 Schema

**No required Alembic.** Readiness is derived from existing columns.  
Do **not** add reseller conversion tables.

---

## 4. Migration steps (operators)

1. Deploy D2 (already) + D3 when approved.  
2. Inventory:

```sql
SELECT id, pg_username, web_username, is_active,
       (pg_admin_password_enc IS NULL OR trim(pg_admin_password_enc) = '') AS missing_enc,
       (lower(web_username) = lower(pg_username)) AS username_aligned,
       pg_role_id
FROM pg_staff_access
ORDER BY is_active DESC, missing_enc DESC, username_aligned ASC;
```

3. For each **active** row with `missing_enc` or `not username_aligned`:  
   - Prefer Owner staff edit + password (D1 policy).  
   - Confirm user can log in after; open a PG list page; confirm not overview-only.  
4. Inactive / unused → revoke.  
5. Do **not** use «اعطای نماینده» to “fix” staff.  
6. Pre-D2 converted resellers: leave; remediate null enc via reseller password UI if needed (document only).

---

## 5. Affected files (implementation, after approval)

| File | Likely change |
|------|----------------|
| `app/services/pg_staff_access.py` | Enrich `ExistingWebAccess` / status dict with readiness flags; optional explicit align helper |
| `app/api/pg_pages.py` | Pass status into template; optional align confirm on staff update |
| `app/web/templates/pg_admins.html` | Badges for enc / mismatch |
| `app/web/templates/pg_home.html` (or overview) | Staff CTA when credentials not ready |
| `app/api/security.py` | Clearer error if mismatch (already errors) |
| `scripts/list_pg_staff_remediation.py` | Optional inventory |
| `docs/PHASE_C_LEGACY_MIGRATION.md` | Rewrite for D2/D3 (remove obsolete conversion UX) |
| `docs/PHASE_D3_*.md` | Implementation record |
| `tests/test_phase_d3_*.py` | New suite |

**Do not change:** authz matrices, bot handlers, `_staff_pg` Owner rules, reseller conversion (keep refuse-if-staff).

---

## 6. Rollback risks

| Action | Risk | Mitigation |
|--------|------|------------|
| Revert D3 UI only | Low — ops lose badges | Safe |
| Revert D2 grant split | High — silent conversion returns | Do not |
| `alembic downgrade` dropping enc columns | **Critical** — loses stored secrets; old code may Owner-fallback | Forward-only; restore from backup if needed |
| `modify_admin` during remount | Changes live PG password | Communicate to staff; use known shared secret |
| Failed encrypt mid-update | Partial state | Keep D1 fail-closed; no commit without enc |

**Rollback policy:** Prefer forward remediation. Do not downgrade past C5 in production.

---

## 7. Test plan

| Case | Expect |
|------|--------|
| L1 null enc login | Authenticated; `pg_credentials_ready=false`; overview-only menus |
| L1 after Owner password update | enc set; ready=true; mapped menus; `get_pg_for_staff` used |
| L2 mismatch self-serve rename | Error (no auto-rename) |
| L2/L4 Owner remediate | Per approved Q1 option A/B/C |
| Status badges | missing_enc / mismatch reflected in `web_status` |
| Remediation never calls | `provision_existing_pg_admin`, Owner client for staff ops |
| Regression | D1/D2/C0–C5 green; no Owner fallback |

---

## 8. Verification checklist (post-implement)

- [ ] Active staff with null enc listed/badged  
- [ ] Owner can remount password without creating reseller  
- [ ] Row remains `PgStaffAccess` after remount  
- [ ] Staff with valid enc + aligned username unchanged (access kept)  
- [ ] Mismatch path follows approved Q1 option  
- [ ] No Owner-token path for staff reads/writes  
- [ ] Docs updated; obsolete «ارتقای فروشگاه» remediation removed  
- [ ] Tests green; stop before D4  

---

## 9. Approval questions

| # | Question | Recommendation |
|---|----------|----------------|
| **Q1** | Owner path when `web ≠ pg`: confirm-align (A), dedicated align action (B), or form posts PG name as today (C)? | **A** |
| **Q2** | Include optional inventory CLI/script in D3? | **Yes** (read-only) |
| **Q3** | Staff overview CTA when not ready? | **Yes** (copy only) |

---

## 10. Stop

**No code until D3 plan + Q1–Q3 approved.**
