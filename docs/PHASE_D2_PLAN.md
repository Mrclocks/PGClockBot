# Phase D2 — Username/Password Sync + Grant Flow Correction

**Status:** AWAITING APPROVAL — **no implementation**  
**Depends on:** D1 (approved) · D0 decisions (locked)  
**Branch (docs):** `cursor/phase-d2-plan-b96b`  
**Does not include:** D3 legacy UX badges/tooling beyond what D2 needs to stop silent conversion · D4 Bot identity · permissions/authz changes · Owner↔PG password sync

---

## Binding decisions (from D0)

1. **pg_staff** remains first-class PG-only secondary admin.  
2. Owner grants: **PG-only** → `grant_web_access` / `update_web_access`; **shop** → reseller flow.  
3. Never silently convert pg_staff → reseller.  
4. For pg_staff: `web_username == pg_username` (normalized).  
5. No Owner credential fallback (Phase C invariant).  
6. Password policy = PasarGuard (D1 already shipped).  
7. Opt-in staff→reseller migrate is **out of D2** (explicit later action, not default).

---

## 1. Current grant paths (as-is)

| Intent (product) | UI | Route | Service | Result today |
|------------------|----|-------|---------|--------------|
| «اعطای دسترسی وب» (`source=none`) | Modal requires **reseller plan** | `POST /pg/admins/{u}/web-access` | `provision_existing_pg_admin` | **ResellerProfile**; may delete staff |
| «ارتقای دسترسی وب» (`source=pg_staff`) | Same modal + plan | Same route | Same | **Converts** staff → reseller via `revoke_web_access` |
| Toggle / revoke | Row actions | `…/toggle`, `…/revoke` | `set_active` / `revoke_web_access` | **PgStaffAccess only** |
| PG-only grant (correct) | **None** | — | `grant_web_access` / `update_web_access` | Orphaned service |
| Self-serve staff password | `/security` | credentials POST | `change_staff_credentials` | Syncs enc; **allows web ≠ pg** |

`provision_existing_pg_admin` docstring explicitly: creates reseller, removes legacy `PgStaffAccess`.

### Identity mappings (as-is)

```text
PG admin username
  ├─ Owner web_admin.json          (independent; may differ from PG_USERNAME)
  ├─ PgStaffAccess.pg_username ── web_username (may differ) + web_password_hash + enc?
  └─ ResellerProfile.pg_admin_username ── web_username (forced == pg on grant) + shop + BotUser
```

Login order: Owner → Reseller → pg_staff (unchanged in D2).

---

## 2. Problems D2 must fix

| ID | Problem |
|----|---------|
| G1 | Single grant route always creates **reseller** |
| G2 | Staff edit modal labeled «ارتقای» **silently converts** via revoke |
| G3 | `grant_web_access` / `update_web_access` not wired to HTTP |
| G4 | UI copy: web username «جدا از یوزر پاسارگارد» while reseller grant requires equality |
| G5 | `grant_web_access` / `change_staff_credentials` allow `web ≠ pg` |
| G6 | Toggle/revoke only work for staff rows; reseller lifecycle is elsewhere (OK) — but grant UX conflates both |
| G7 | Blank password on current “upgrade” can leave null enc (conversion path) — conversion must leave D2 |

**Not D2:** Owner web ≠ `PG_PASSWORD` (approved separate). Bot PG. Deep-link menu clamp. Silent `role_id` drop (optional tiny fix if touched; prefer leave unless free).

---

## 3. Proposed architecture

### 3.1 Two Owner intents (required)

```text
POST /pg/admins/{username}/web-access/staff     → grant_web_access | update_web_access
POST /pg/admins/{username}/web-access/reseller  → provision_existing_pg_admin
```

Alternatively: one route with required `access_kind=staff|reseller` and **no default** (reject if missing). Prefer **two routes** for clarity and CSRF-safe forms.

| Intent | Service | Requires plan? | Password | Deletes staff? |
|--------|---------|----------------|----------|----------------|
| PG-only (pg_staff) | `grant_web_access` / `update_web_access` | **No** | Required on create; required on update if enc missing; optional keep if enc present | **Never** |
| Shop (reseller) | `provision_existing_pg_admin` | **Yes** | Required when creating/updating enc | **Only if** staff row exists **and** operator used reseller intent — **D2 rule: refuse if staff exists** (force revoke staff first *or* block with message). **Do not auto-delete.** |

**Critical D2 rule for `provision_existing_pg_admin`:**  
If `PgStaffAccess` exists for that PG admin → return error: «ابتدا دسترسی pg_staff را حذف کنید یا از مسیر اعطای فروشگاه استفاده نکنید» — **remove** the current `revoke_web_access` conversion block. No silent conversion.

### 3.2 Username equality (pg_staff)

Enforce `normalize(web_username) == normalize(pg_username)` in:

- `grant_web_access`  
- `update_web_access`  
- `change_staff_credentials`  
- Owner staff grant form (username field locked to PG username / hidden / readonly)

Reseller grant already enforces equality — keep.

### 3.3 Password sync (PG-linked)

On create/update password for staff:

1. D1 `validate_password_strength(..., username=pg_u)`  
2. bcrypt web hash  
3. Fernet enc (fail if None — D1)  
4. Owner `modify_admin` password sync  
5. `reset_pg()` cache clear  

Same shared-secret model as reseller. Owner web password remains **unsynced** to env PG.

### 3.4 UI (`pg_admins.html`)

For `source=none`:

- Button **اعطای دسترسی PG (ادمین فرعی)** → staff modal (no plan; username = PG username fixed; password required).  
- Button **اعطای دسترسی نماینده (فروشگاه)** → reseller modal (plan required; username = PG username fixed).

For `source=pg_staff`:

- **ویرایش دسترسی / رمز** → `update_web_access` (not reseller).  
- Toggle / revoke unchanged.  
- **No** «ارتقای به فروشگاه» in D2 (deferred explicit migrate).

For `source=reseller`: keep link to reseller edit (unchanged).

### 3.5 `provision_existing_pg_admin` changes

- Remove auto `revoke_web_access` conversion.  
- If staff row present → hard error.  
- Keep equality + D1 password rules.  
- Blank password: only when updating **existing reseller** with enc already set; if enc missing → require password (D1-aligned).

---

## 4. DB impact

| Change | Schema? |
|--------|---------|
| Enforce username equality in app | **No migration** |
| Stop deleting staff on reseller grant | **No migration** — behavioral |
| Existing rows with `web_username ≠ pg_username` | **Data remediation** (D2 ops + D3); app rejects further edits until renamed |
| New columns | **None** required for D2 |

Optional (not required): unique check already on `web_username` / `pg_username`.

**Alembic:** no new revision for D2 unless we add a one-shot data migration to rename mismatched staff usernames (prefer **manual/ops** in D2; automated rename only if approved — default: **reject until operator fixes**).

---

## 5. Legacy account risks

| Cohort | Risk | D2 handling |
|--------|------|-------------|
| Staff `web ≠ pg` | Login still works; sync/edit will fail equality | Block update until web renamed to pg (or force rename in update API to pg_username) |
| Staff null enc | Fail-closed reads (C5) | `update_web_access` with password required |
| Staff that were already converted to reseller | Cannot auto-revert | Leave as reseller; document |
| Dual confusion (operators expect one button) | Wrong grant | Dual UI + clear Persian labels |
| Existing bookmarks to old single POST | Breaks | Keep old `/web-access` as **410/redirect with error** or require `access_kind` |

**Recommended rename strategy for mismatched staff:**  
`update_web_access` / `change_staff_credentials`: if `web ≠ pg`, either (A) force `web_username = pg_username` when password is set, or (B) return clear error asking Owner to set web username to PG username. Prefer **(A) on Owner update** with password, **(B) on self-serve** if new_username ≠ pg.

---

## 6. Files to change (implementation, after approval)

| File | Change |
|------|--------|
| `app/api/pg_pages.py` | Split grant routes; wire staff → `grant_web_access`/`update_web_access`; reseller → `provision_existing`; stop conversion |
| `app/web/templates/pg_admins.html` | Dual modals/buttons; fix copy; lock username to PG name; remove «ارتقای فروشگاه» |
| `app/services/pg_staff_access.py` | Enforce `web == pg` in grant/update/change_staff_credentials |
| `app/services/resellers.py` | `provision_existing_pg_admin`: refuse if staff exists; **delete** auto-`revoke_web_access` conversion |
| `app/api/security.py` | pg_staff branch: require `new_username == row.pg_username` |
| `docs/PHASE_C_LEGACY_MIGRATION.md` | Update remediations for dual grant |
| `docs/PHASE_D2_*.md` | Implementation record |
| `tests/test_phase_d2_*.py` | New suite (below) |
| Possibly `tests/test_phase_d1_*.py` | Update “grant still provision_existing” guard — invert for staff route |

**Do not change:** authz/permissions, bot handlers, Owner `PG_PASSWORD` sync, C1–C5 client selection, Alembic (unless approved data migration).

---

## 7. Migration steps (operators)

1. Deploy D2 after approval.  
2. Inventory:

```sql
-- Mismatched staff usernames
SELECT id, pg_username, web_username, is_active,
       (pg_admin_password_enc IS NULL OR pg_admin_password_enc = '') AS missing_enc
FROM pg_staff_access
WHERE lower(web_username) != lower(pg_username);

-- Null enc staff
SELECT id, pg_username, web_username FROM pg_staff_access
WHERE pg_admin_password_enc IS NULL OR pg_admin_password_enc = '';
```

3. For mismatched rows: Owner opens **ویرایش دسترسی pg_staff**, sets password (D1-strong), system sets `web_username = pg_username` (if strategy A).  
4. For null enc: same edit with new password.  
5. For shop needs: use **اعطای نماینده** only when **no** staff row exists (revoke staff first if switching intentionally).  
6. No automatic conversion scripts.

---

## 8. Test plan

### Unit / service

- `grant_web_access` rejects `web ≠ pg`.  
- `update_web_access` / `change_staff_credentials` reject or force equality per chosen strategy.  
- `provision_existing_pg_admin` **errors** when staff row exists; **does not** call `revoke_web_access`.  
- `provision_existing` still works when no staff row (reseller path).  
- Password sync: modify_admin + enc + hash on staff grant/update.  
- Encrypt None still fails (D1 regression).

### HTTP / UI contract

- Staff grant POST does **not** call `provision_existing_pg_admin`.  
- Reseller grant POST does **not** call `grant_web_access`.  
- Old single-intent behavior (plan required for all grants) gone.  
- Template: no «فعال‌سازی فروشگاه» on staff edit; staff modal has no `plan_id`.

### Isolation regressions

- C1–C5, owner-bypass, D1 policy: green.  
- pg_staff `_staff_pg` / read never Owner client.  
- Reseller still `get_pg_for_reseller`.

### Negatives

- Cannot create reseller by posting plan to staff endpoint.  
- Cannot create staff by omitting plan on reseller endpoint.  
- No Owner fallback introduced.

---

## 9. Implementation order (within D2, after approval)

1. Service-layer equality + stop conversion in `provision_existing`.  
2. New/split routes in `pg_pages.py`.  
3. Template dual UX.  
4. Security self-serve equality.  
5. Tests + docs.  
6. Stop for D3 approval.

---

## 10. Open choices for approver (answer before code)

| # | Question | Recommendation |
|---|----------|----------------|
| Q1 | Force-rename mismatched staff web→pg on Owner password update, or error-only? | **Force-rename on Owner update**; error on self-serve if they pick a different name |
| Q2 | Keep deprecated `POST …/web-access` as hard error, or alias to staff-only? | **Hard error** with Persian message pointing to new actions |
| Q3 | Include revoke-reseller-web from admins page in D2? | **No** — out of scope |

---

## 11. Stop

**No code until this plan is approved** (including Q1–Q3).
