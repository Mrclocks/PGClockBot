# Phase D — Identity & Credential Unification Plan

**Status:** AWAITING APPROVAL — **no implementation**  
**Depends on:** Phase C production acceptance (green)  
**Binding product decision:** **`pg_staff` remains a first-class independent secondary admin.** Do **not** convert pg_staff into reseller-only. No role conversion required for PG-only access.  
**Docs branch:** `cursor/phase-c-acceptance-phase-d-plan-b96b`

---

## Executive verdict

Phase C closed **authorization isolation** (no Owner-token fallback). Phase D must close **identity and credential lifecycle** drift:

- Owner UI «اعطای دسترسی وب» currently provisions a **reseller** and can **delete** `PgStaffAccess`.
- Service `grant_web_access` (true pg_staff + enc sync) is **orphaned** from HTTP.
- Username/password policy is **inconsistent** across create / update / sync / reset paths.
- Web ↔ PasarGuard ↔ Bot identities are **linked differently** per role.

Until D0–D4 are implemented under this plan, operators must use documented workarounds (`PHASE_C_LEGACY_MIGRATION.md`). This document is analysis + design only.

---

## Binding architecture (approved)

```text
Owner          → web_admin.json (+ env PG_* for Owner PasarGuard client)
platform Admin → session role "admin" today (may later split from Owner)
pg_staff       → PgStaffAccess  (PG menus/actions only; NO shop; first-class)
Reseller       → BotUser + ResellerProfile (shop + optional PG link)
```

| Principal | Web login | Shop | PG client | Bot |
|-----------|-----------|------|-----------|-----|
| Owner | `web_admin.json` | Full | `get_pg()` (env) | `ADMIN_IDS` / `BotUser.admin` (independent) |
| platform Admin | same as Owner today | Full | `get_pg()` | same as Owner today |
| **pg_staff** | `PgStaffAccess` | **None** | `get_pg_for_staff` (enc required) | **No bot identity required** |
| Reseller | `ResellerProfile` | `web_permissions` | `get_pg_for_reseller` when linked | `BotUser` + optional shop bot |

**Invariants (carry from Phase C; Phase D must not break):**

1. No Owner credential fallback for reseller / pg_staff ops.  
2. pg_staff stays `role=pg_staff` — grant/edit must **not** force reseller conversion.  
3. Missing `pg_admin_password_enc` → fail closed.  
4. Bot shop ACL remains shared with Web (`authz` / C4).

---

## 1. Current identity flows (as-is)

### 1.1 Owner creation

1. Setup wizard `/setup/admin` → `save_web_admin` (bcrypt in `data/web_admin.json`).  
2. Username: stripped string — **no** `validate_web_username`.  
3. Password: `validate_password_strength`.  
4. `/setup/other` stores Owner **PasarGuard** env creds separately (`PG_USERNAME` / `PG_PASSWORD`).  
5. Owner web password and Owner PG password are **not** synchronized after setup.  
6. `/security` changes Owner web username/password only — does **not** update `PG_PASSWORD`.

### 1.2 PG admin creation

1. Owner `POST /pg/admins` → `get_pg().create_admin`.  
2. Optional `role_id`; on API failure with role → **silently drops** `role_id`, retries with `is_sudo=False`.  
3. **No** server-side `validate_password_strength` / username charset checks (UI text claims policy).  
4. Does **not** create web access.

### 1.3 Web access granting (critical mismatch)

| UI | Route | Actual service | Outcome |
|----|-------|----------------|---------|
| «اعطای / ارتقای دسترسی وب» | `POST /pg/admins/{u}/web-access` | `provision_existing_pg_admin` | **Reseller** + may **delete** `PgStaffAccess` |
| Toggle / revoke | `…/toggle`, `…/revoke` | `set_active` / `revoke_web_access` | Operates on **`PgStaffAccess` only** |

`grant_web_access` / `update_web_access` (pg_staff + enc + `modify_admin`) have **no HTTP caller**.

Modal requires a **reseller plan** and copy implies shop activation — contradicts “pg_staff first-class.”

### 1.4 Reseller creation

- Approve application / `provision_reseller`: shared random username + strong random password → PG admin + web hash + enc.  
- `provision_existing_pg_admin`: forces `web_username == pg_username`; optional password; blank pwd keeps hash but **skips enc sync**; converts staff → reseller.  
- Shop ACL from plan; BotUser may be synthetic (negative `telegram_id`) when no Telegram person exists.

### 1.5 pg_staff creation

- **Intended service:** `grant_web_access` — password required, strength checked, enc + role cache, sync via Owner `modify_admin`.  
- **Product UI:** cannot create bare pg_staff today.  
- **Self-serve:** `/security` → `change_staff_credentials` (can establish enc for legacy rows).  
- Allows `web_username ≠ pg_username` (unlike reseller grant path).

### 1.6 Password changes

| Actor | Path | Policy | PG sync |
|-------|------|--------|---------|
| Owner | `/security` | strength on password | **No** (env PG untouched) |
| Owner CLI | `scripts/set_web_password.py` | **skipped** | No |
| Reseller | `/security`, reseller edit | usually strength | `apply_reseller_panel_password` → enc + `modify_admin` |
| pg_staff | `/security` | strength | enc + `modify_admin` |
| PG create | `/pg/admins` | **skipped** | N/A (sets initial PG password only) |
| `update_web_access` | service | when pwd set | enc + `modify_admin` |
| Blank upgrade | `provision_existing` | may skip | **enc not set** |

### 1.7 Login / session refresh

1. Owner `verify_web_admin` → `role=admin` + `sv`.  
2. Reseller by `web_username` → live ACL + optional PG features.  
3. pg_staff by `web_username` → `pg_credentials_ready`, live `pg_role_id`, deny if no mapped PG pages.  
4. `require_staff` re-reads DB for reseller/pg_staff; Owner trusts cookie `sv` only.

---

## 2. Current problems (mismatches)

### P1 — Role model / UI (blocks first-class pg_staff)

1. Owner grant UI always creates/upgrades **reseller**.  
2. Grant path **deletes** `PgStaffAccess` (forced conversion).  
3. `grant_web_access` orphaned from routes.  
4. Toggle/revoke still target staff rows while grant targets reseller — split lifecycle.  
5. UI copy: «ارتقای دسترسی وب» / plan required — product language ≠ approved architecture.

### P2 — Username rules inconsistent

6. Reseller/unified paths require `web_username == pg_username`.  
7. `grant_web_access` / `change_staff_credentials` allow divergent names.  
8. Modal says web username is “separate from PG” while backend grant requires equality.  
9. Web username max **64**; PG VPN usernames max **32**; PG **admin** create unbounded.  
10. Setup Owner username skips `validate_web_username`; `change_web_admin_username` helper only checks len≥3.  
11. Legacy `/security/username` can rename reseller web without PG equality check.

### P3 — Password policy not universal

12. `POST /pg/admins` create skips strength.  
13. `set_web_password.py` skips strength.  
14. Auto `_rand_password` requires digits; `validate_password_strength` does **not** — policies diverge.  
15. Blank password on staff→reseller upgrade can leave **null enc**.  
16. `encrypt_secret` failure returns `None` — grant can “succeed” without usable PG client.  
17. `complete_reseller_setup` late web password path may skip PG `modify_admin` / enc.

### P4 — Credential sync incomplete

18. Owner web ↔ Owner PG passwords never synced.  
19. Shared secret model (bcrypt for web + Fernet for PG API) only applied on some paths.  
20. No Owner UI to **set/reset enc** for bare pg_staff without conversion.  
21. No admin-list badge for `pg_credentials_ready`.

### P5 — Cross-channel identity

22. Owner web (`web_admin.json`) independent of Telegram `ADMIN_IDS`.  
23. pg_staff has **no** BotUser — fine if web-only, but undocumented as product rule.  
24. Reseller may use synthetic BotUser (negative telegram id).  
25. Session `role=admin` conflates Owner and platform Admin (`PrincipalKind.OWNER` unused).  
26. Bot PG tools still platform-admin only (C4) — Web≠Bot for PG ops.

### P6 — Role integrity

27. Silent `role_id` drop on PG admin create / provision.  
28. Deep-link PG pages vs overview-only menu clamp (data fail-closed, not hard deny).

---

## 3. Proposed architecture

### 3.1 Principal model

```text
IdentityRecord (logical)
  kind: owner | platform_admin | pg_staff | reseller
  web_login: { username, password_hash, session_version? }
  pg_link?: { pg_username, password_enc, role_id }
  shop?: { bot_user_id, permissions, plan_id, … }   # reseller only
  bot?: { telegram_ids[], shop_bot? }                # owner/admin/reseller
```

Storage stays close to today (no big-bang schema rewrite in early D slices):

- Owner → `web_admin.json` + env PG  
- pg_staff → `PgStaffAccess` (+ enc, role_id)  
- Reseller → `ResellerProfile` (+ optional PG link)  
- Authz continues via `AuthzContext` / `PrincipalKind`

### 3.2 Two Owner grant intents (required)

| Intent | UI | Service | Result |
|--------|-----|---------|--------|
| **PG-only secondary admin** | «اعطای دسترسی pg_staff» | `grant_web_access` / `update_web_access` | `PgStaffAccess` only |
| **Shop reseller** | «اعطای دسترسی نماینده» | `provision_existing_pg_admin` / approve | `ResellerProfile`; **never auto-delete staff unless operator confirms migrate** |

Explicit «attach shop» later may convert staff→reseller **only** as an optional, named action — never the default grant.

### 3.3 Unified credential policy

Single module (conceptual name: `credential_policy`):

| Rule | Proposal |
|------|----------|
| Username charset | `[A-Za-z0-9_]+` |
| Username length | Align to PasarGuard admin limits (recommend **3–32** unless PG docs prove higher) |
| Username equality | If PG-linked: `normalize(web_username) == normalize(pg_username)` for **pg_staff and reseller** |
| Password strength | One function for create/update/sync/reset/CLI/UI |
| Digit requirement | Decide once — either add digit to strength or drop digit from generator |
| Shared secret | One plaintext password → bcrypt (web) + Fernet enc (PG client) + live `modify_admin` |
| Encrypt failure | Fail the operation (no silent null enc) |
| Blank password | Forbidden when creating link or when enc missing; allowed on edit only when enc already present |

### 3.4 Sync matrix (target)

| Event | Web hash | PG enc | Live PG `modify_admin` | Env `PG_PASSWORD` |
|-------|----------|--------|------------------------|-------------------|
| Create pg_staff web | ✓ | ✓ | ✓ | — |
| Update pg_staff password | ✓ | ✓ | ✓ | — |
| Create/update reseller PG link | ✓ | ✓ | ✓ | — |
| Create PG admin (Owner) | — | — | create_admin | — |
| Owner web password change | ✓ | — | optional* | optional* |
| Owner PG env change | — | — | — | ✓ |

\*Owner dual-identity: D2 decides whether optional sync when Owner web username equals `PG_USERNAME`.

### 3.5 Login / session (unchanged shape, clearer enrichment)

- Keep three login branches.  
- Always set `pg_credentials_ready` for PG-linked principals.  
- Prefer stored `pg_role_id`, refresh live.  
- Never escalate pg_staff → Owner client.

### 3.6 Bot consistency (D4)

- Document: pg_staff is **web-only** unless a later approved Bot PG feature.  
- Reseller: keep `bot_user_id` as shop identity; synthetic users labeled internal.  
- Owner: document independence of `ADMIN_IDS` vs web login; optional linking later.  
- Do not require Telegram for pg_staff.

---

## 4. Sub-phases D0–D4

### D0 — Identity architecture analysis

**Status of this document:** D0 deliverable (analysis + binding decisions).  

**Remaining D0 checklist before code:**

- [x] Confirm pg_staff first-class (done).  
- [ ] Confirm username equality rule for pg_staff (recommend **yes**, same as reseller).  
- [ ] Confirm Owner dual-cred policy (keep separate vs optional sync).  
- [ ] Confirm platform Admin vs Owner split timing (defer session split vs do in D4).  
- [ ] Confirm whether «attach shop» conversion is allowed as explicit opt-in.

**Exit:** Written decision answers above; no code.

---

### D1 — Credential policy unification

**Problems:** P3, part of P4/P6 (create-admin policy, encrypt null, silent weak passwords).

**Design:**

1. Centralize password + username validators; call from every create/update/sync/reset path including `/pg/admins`, CLI, grants.  
2. Align `_rand_password` with strength rules.  
3. Fail closed if `encrypt_secret` returns None.  
4. Refuse blank password when enc missing.  
5. Stop silent `role_id` drop — surface error (related integrity).

**Migration risks:** Existing weak PG admin passwords remain until rotated; scripts/operators hit new validation errors.

**Tests required:**

- Matrix: every password entrypoint invokes shared validator (source + unit).  
- Create PG admin rejects weak password.  
- Encrypt failure aborts grant.  
- Blank password rejected when enc null (staff + reseller upgrade).  
- Regression: C5 fail-closed / no Owner fallback still green.

---

### D2 — Username/password synchronization

**Problems:** P2, P4 sync matrix, Owner optional sync.

**Design:**

1. Enforce `web_username == pg_username` on `grant_web_access`, `update_web_access`, `change_staff_credentials`, reseller security forms, `provision_existing`.  
2. Fix UI copy (no “separate username” when equality required).  
3. On password change for PG-linked principals: atomic bcrypt + enc + `modify_admin` + cache reset.  
4. Wire Owner HTTP: PG-only grant → `grant_web_access`; shop grant → reseller provision (separate buttons).  
5. Owner password sync policy per D0 decision.

**Migration risks:** Existing pg_staff with `web ≠ pg` need rename; operators must pick one canonical username.

**Tests required:**

- Equality enforced on all PG-linked credential APIs.  
- Password change asserts `modify_admin` + enc updated.  
- Grant route for pg_staff does **not** call `provision_existing_pg_admin`.  
- Grant route for reseller does **not** delete staff unless explicit migrate.  
- UI/API contract tests for two grant intents.

---

### D3 — Legacy account migration

**Problems:** Null-enc staff/resellers; accidental conversions; readiness visibility.

**Design:**

1. Inventory tooling/docs: list null enc, divergent usernames, dual staff+reseller rows.  
2. Owner actions on `/pg/admins` for **pg_staff**: set/reset password → `update_web_access` (no conversion).  
3. Badge: credentials missing / ready.  
4. Optional explicit «migrate to reseller» with confirmation + required password.  
5. Do **not** auto-backfill enc from unknown plaintext (impossible); require password entry.

**Migration risks:** Cannot recover plaintext; staff already converted to reseller need manual reverse if desired (rare). Dual rows need cleanup policy.

**Tests required:**

- Legacy null-enc: login OK, overview-only, no Owner fallback.  
- Owner reset password on staff sets enc; menus expand.  
- Explicit migrate-to-reseller only when confirmed.  
- Badge/API field for readiness.  
- Alembic/legacy columns remain additive-safe.

---

### D4 — Web/Bot identity consistency

**Problems:** P5.

**Design:**

1. Document principal↔channel matrix in-product (help text).  
2. Keep pg_staff web-only unless Bot PG feature approved.  
3. If Bot PG later: reuse `AuthzContext` + `get_pg_for_staff` / `get_pg_for_reseller` — never Owner token.  
4. Optional: distinguish Owner in session (`is_owner` / env username match) without breaking cookies.  
5. Avoid treating synthetic negative telegram IDs as real users in UX.  
6. C4 shop parity must remain green.

**Migration risks:** Session Owner/Admin split can lock shared-admin installs; Bot PG expands attack surface.

**Tests required:**

- C4 Bot/Web shop parity unchanged.  
- pg_staff never receives shop bot ACL.  
- Platform bot tools still `ADMIN_IDS` / `Role.ADMIN`.  
- Restricted principals never use Owner PG client.  
- Optional Owner flag tests if implemented.

---

## 5. Implementation order (after approval)

```text
D0 decisions (answers)  →  D1 policy module  →  D2 sync + Owner UI dual grant
                                         ↘
                                           D3 legacy remediation UX
                                         ↘
                                           D4 channel docs / optional Owner split / Bot PG flag
```

**Do not combine** D1–D4 into one PR. Each slice: branch `cursor/phase-dN-*-b96b`, tests green, then next.

---

## 6. Migration risks (summary)

| Risk | Severity | Mitigation |
|------|----------|------------|
| Operators expect grant = reseller | High | Dual UI labels; docs; confirmation |
| Existing `web ≠ pg` staff | Medium | D3 rename wizard |
| Null enc until password set | Medium | Badge + forced reset |
| Weak PG passwords created pre-D1 | Low | Force rotate on next edit |
| Accidental staff deletion on old UI | High | Remove auto-`revoke_web_access` from default grant |
| Owner/Admin session split | Medium | Defer to D4; feature-flag |
| `encrypt_secret` None | Medium | Fail operation in D1 |

---

## 7. Tests required (cross-cutting)

| Area | Coverage |
|------|----------|
| Policy | Strength/username on create/update/sync/reset/CLI |
| Sync | modify_admin + enc + bcrypt together |
| Role | pg_staff grant never creates ResellerProfile |
| Isolation | C1/C2/C5 suites remain green |
| Shop | C4 Bot/Web parity |
| Legacy | null enc behavior + remediation sets enc |
| UI contract | Two grant endpoints/intents |
| Negatives | No Owner fallback; encrypt fail; blank pwd when enc missing |

---

## 8. Explicit non-goals (this phase plan)

- Implementing code before approval.  
- Reintroducing Owner-token fallback.  
- Forcing all secondary admins to become resellers.  
- Replacing Fernet/bcrypt with a different crypto stack (unless security review demands).  
- Full Bot PG product for staff (optional later under D4 flag).

---

## 9. Approval checklist — D0 RESOLVED

- [x] **pg_staff** stays first-class; grant UI must create `PgStaffAccess`, not reseller (D2).  
- [x] Reseller grant remains a **separate** Owner action (D2).  
- [x] Username equality for PG-linked principals: **enforce** (D2).  
- [x] Owner web vs Owner PG: **keep separate** by default.  
- [x] Password policy: PasarGuard rules (incl. ≥2 digits) — **D1**.  
- [x] Slice order D1 → D2 → D3 → D4.  
- [x] Explicit staff→reseller only as opt-in migrate (not default).

**D1 implemented** · **D2 implemented**.  
**D3 plan:** `docs/PHASE_D3_PLAN.md` — **awaiting approval before implementation.**
