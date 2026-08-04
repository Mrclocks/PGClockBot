# Phase D4 — Web/Bot Identity Consistency (Planning Only)

**Status:** AWAITING APPROVAL — **no implementation**  
**Depends on:** D3 approved (`cursor/phase-d3-legacy-remediation-b96b` / PR #146)  
**Branch (docs):** `cursor/phase-d4-plan-b96b`  
**Base for future impl:** D3 tip (not raw `main` / 3.8.3 — C0–C5 + D1–D3 required)  
**This PR:** docs only vs `main` (`PHASE_D4_PLAN.md`). Parent `PHASE_D_PLAN.md` pointer update lands with the D4 impl branch off D3.

---

## Goals

1. **Same identity decisions** on Web and Telegram Bot for each principal.  
2. **Same role boundaries:** Owner · pg_staff · Reseller (no silent cross-role).  
3. **No permission drift** between channels (shop ACL already C4; keep green).  
4. **No Owner fallback** for reseller / pg_staff PG ops (C1/C2/C5 hold).  
5. **Preserve working flows** — behavior-preserving unless an approved decision explicitly changes product.

**Out of scope unless separately approved:** full Bot PG product for reseller/pg_staff · Alembic drop of `bot_permissions` · forcing Telegram for pg_staff · reopening D1–D3 credential/grant work.

---

## Binding architecture (locked from D0–D3)

```text
Owner          → web_admin.json + env PG_*     | Bot: ADMIN_IDS / BotUser.admin
pg_staff       → PgStaffAccess (PG-only)       | Bot: none (web-only)
Reseller       → ResellerProfile + BotUser     | Bot: shop (+ bot_admin_ids staff)
```

| Principal | Web login | Shop (Web+Bot) | PG client | Telegram Bot |
|-----------|-----------|----------------|-----------|--------------|
| Owner / platform Admin | `web_admin.json` → `role=admin` | Full | `get_pg()` | Platform admin tools via `ADMIN_IDS` / `Role.ADMIN` |
| **pg_staff** | `PgStaffAccess` | **None** | `get_pg_for_staff` (enc) | **No BotUser / no bot ACL** |
| Reseller | `ResellerProfile` | `web_permissions` (C4) | `get_pg_for_reseller` when linked | Shop menus on shop bot; credentials on main bot |
| Shop `bot_admin_ids` | No web login | Shop keys of **owner** profile on shop bot only | N/A | Telegram-only assistant (by design) |

**Invariants D4 must not break**

1. No Owner credential fallback for reseller / pg_staff.  
2. pg_staff stays `pg_staff` — no conversion.  
3. Shop decisions use `authz` / `web_permissions` (never `bot_permissions` column for reads).  
4. Bot PG management remains **platform-admin only** unless Q2 opens a Bot PG flag.  
5. Valid web + bot logins keep working.

---

## 1. Analysis — Bot authentication flow

```text
Update
  → UserMiddleware (token → main vs shop bot; get_or_create_user; block list)
  → effective_menu_role / load_reseller_actor
       · shop bot + owner|bot_admin_ids → menu role "reseller"
       · main bot + BotUser.reseller → menu role "user" (+ credentials UX)
       · platform admin on shop bot → forced "user" (admin router blocked)
  → Handlers: is_platform_admin | has_bot_perm / can_shop_feature | _is_admin
```

| Step | Module | Notes |
|------|--------|-------|
| Token / tenancy | `app/bot/middlewares.py` | Injects `reseller_owner_id`, `is_reseller_bot` |
| User row | `app/services/users.py` | Syncs `ADMIN_IDS` ↔ `BotUser.role=admin` |
| Shop actor | `app/services/reseller_access.py` | Owner or `bot_admin_ids` only on **shop** bot |
| Menu role | `effective_menu_role` | Channel-aware; not the same as web session `role` |
| Platform admin | `app/bot/auth.py::is_platform_admin` | `Role.ADMIN` **or** `ADMIN_IDS` — **independent** of `web_admin.json` |
| Shop ACL | `can_shop_feature` / `has_bot_perm` → `authz.shop_feature_allowed` | C4 parity with Web `require_perm` |
| PG on bot | `admin*.py` / `admin_pg_users.py` | Always `get_pg()` after `_is_admin` — Owner client by design for platform admin |

**Gaps**

- Web Owner ≠ Bot Owner unless the same human is in both `web_admin.json` and `ADMIN_IDS`.  
- No `pg_staff` on bot (correct per binding; must stay explicit).  
- Synthetic negative `telegram_id` BotUsers from reseller provision can appear in admin UIs / notify paths.

---

## 2. Analysis — Bot permission checks

| Mechanism | Where | Source of truth |
|-----------|-------|-----------------|
| `is_platform_admin` | `bot/auth.py`, admin handlers | Telegram role / `ADMIN_IDS` |
| `_BlockPlatformAdminOnResellerBot` | `bot/__init__.py` | Drop admin router on shop bots |
| `can_shop_feature` | `bot/auth.py` | `authz.shop_feature_allowed` |
| `has_bot_perm` | `resellers.py` (alias) | Same → `web_permissions` |
| `_gate` / per-handler early return | reseller_* handlers | Actor + feature key |
| Force-join | middleware | Only `menu_role==user` |

**Not middleware-enforced:** admin routers rely on per-handler `_is_admin`. Missed checks remain a process risk (not new in D4, but D4 should add contract tests / optional shared helper — not silent ACL widening).

---

## 3. Analysis — Web permission checks

| Mechanism | Where | Source of truth |
|-----------|-------|-----------------|
| Login order | `api/app.py` | Owner → reseller → pg_staff |
| `require_staff` | live DB refresh | Reseller shop ACL + pg_staff enc/role |
| `require_perm` | `can_shop(authz_from_staff)` | C0/C4 |
| `require_pg_perm` | `can_pg_page` | Mapped `pg_permissions` |
| Sidebar | `base.html` | `permissions` + `pg_permissions`; `pg_admins` admin-only |
| PG menus clamp | `effective_pg_menu_keys` | Overview-only without staff/reseller read client |
| PG client | `staff_pg_read_client` / `_staff_pg` | `get_pg` / `get_pg_for_reseller` / `get_pg_for_staff` — **no Owner fallback** |

---

## 4. Analysis — PG feature visibility & menu consistency

| Surface | Owner | Reseller (linked) | pg_staff | Notes |
|---------|-------|-------------------|----------|-------|
| Web sidebar PG | Full | Role-mapped keys | Role-mapped; clamp if !enc | `effective_pg_menu_keys` |
| Web PG pages | Owner client | Reseller client + ownership | Staff client or fail-closed | C1/C2/C5 |
| Bot PG submenu | Platform admin only | **None** | **None** | Intentional asymmetry (P5) |
| Bot shop hub | N/A (admin hub) | `FEATURE_PERMS` via authz | N/A | Must match Web shop keys |

**Consistency rule for D4:**  
Shop feature key set and allow/deny outcomes must stay identical Web↔Bot (C4).  
PG **management** visibility is channel-asymmetric by product decision — document it; do not silently add Bot PG.

---

## 5. Remaining duplicated / divergent ACL logic

| Item | Status after C4/D3 | D4 action |
|------|--------------------|-----------|
| Shop feature allow/deny | Shared via `authz` | Keep green; parity tests |
| `web_permissions` vs `bot_permissions` | Write-mirrored; reads ignore bot col | Document; optional deprecate (Q3) |
| Platform admin check | Bot: `ADMIN_IDS`+role; Web: session `admin` | Document independence; optional link (Q1) |
| `PrincipalKind.OWNER` | Unused (both → PLATFORM_ADMIN) | Optional session `is_owner` (Q1) |
| Bot admin `_is_admin` copies | Thin wrappers of `is_platform_admin` | Optional single import / contract tests |
| PG client selection | Web isolated; Bot admin = Owner client | Keep; guard “no get_pg for non-admin” |
| `bot_admin_ids` | Bot-only shop staff | Document as intentional (no web) |
| Synthetic telegram IDs | Provision path | UX/notify hygiene (Q4) |
| Identity matrix in UI | Missing | In-product help (Q4) |

---

## 6. Proposed D4 architecture (implementation after approval)

### 6.1 Documentation + contracts (always in D4)

1. Ship `docs/PHASE_D4_IDENTITY_MATRIX.md` (operator-facing matrix + “who uses which channel”).  
2. Update `PHASE_D_PLAN.md` / C final pointers: D4 status.  
3. Code comments / module docstrings at bot auth + web login pointing at the matrix.  
4. **Contract tests** locking:  
   - pg_staff → no shop keys on Web or Bot paths  
   - Bot PG handlers require `is_platform_admin` and use `get_pg()` only in those modules  
   - Reseller/pg_staff Web paths never call Owner fallback  
   - C4 shop parity suite still green  

### 6.2 Guardrails (recommended, low risk)

1. Assert in bot PG admin modules (source/contract): no `get_pg_for_staff` / `get_pg_for_reseller` usage that would imply Bot PG without a feature flag.  
2. Notify / admin list helpers: skip or label synthetic (`telegram_id < 0`) users — do not treat as real Telegram people.  
3. Prefer `can_shop_feature` / `authz` at new call sites; avoid new inline CSV parsing.

### 6.3 Optional product toggles (need Q answers)

| Option | If yes | If no |
|--------|--------|-------|
| **Q1** Owner session flag | Cookie/session `is_owner` when username matches `web_admin` / env; `PrincipalKind.OWNER` used for docs/telemetry only — **no ACL change** vs today’s full admin | Leave Owner ≡ platform Admin in session |
| **Q2** Bot PG for reseller | Feature-flagged handlers using `get_pg_for_reseller` + `AuthzContext` — **never** Owner token; large product surface | **Defer** — document “PG manage = Web” |
| **Q3** `bot_permissions` column | Keep mirror writes; add “deprecated for reads” comment/tests | Plan drop in later phase (not D4 schema) |
| **Q4** In-product help | Short Persian blurb on `/security` or Owner home: who is Owner / staff / reseller / bot | Docs-only |
| **Q5** Shared platform-admin helper | Behavior-preserving re-export so bot+web docs agree; still two credential stores | Docs-only |

**Recommended defaults (for approval tick):**  
Q1 **defer** (docs only) · Q2 **defer Bot PG** · Q3 **keep mirror** · Q4 **yes help text** · Q5 **contract tests + thin shared docstring**, no behavior change.

### 6.4 Explicit non-goals

- Creating `BotUser` / Telegram identity for pg_staff.  
- Reintroducing Owner fallback.  
- Auto-linking `ADMIN_IDS` ↔ `web_admin.json` without approval.  
- Changing reseller shop menu keys or C4 soft-upgrade rules.  
- Merging D4 with D1–D3 credential work.

---

## 7. Affected files (likely — after approval)

| File | Likely change |
|------|----------------|
| `docs/PHASE_D4_PLAN.md` | This plan |
| `docs/PHASE_D4_IDENTITY_MATRIX.md` | Operator matrix (impl slice) |
| `docs/PHASE_D_PLAN.md` | Mark D4 approved / done |
| `app/bot/auth.py` | Cross-refs; optional helpers |
| `app/services/authz.py` | Optional `PrincipalKind.OWNER` wiring if Q1 |
| `app/api/app.py` | Optional `is_owner` session field; help context |
| `app/web/templates/security.html` or home | Q4 help blurb |
| `app/services/notifications.py` / reseller list UX | Synthetic ID labeling |
| `app/bot/handlers/admin*.py` | Contract-only unless Q2 |
| `tests/test_phase_d4_*.py` | New suite |
| `tests/test_phase_c4_bot_web_alignment.py` | Must stay green |

**Do not change (default):** `pg_staff_access` grant model · credential_policy · `_staff_pg` fail-closed · reseller provision refuse-if-staff.

---

## 8. Migration risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Operators expect Bot PG for resellers | Medium | Docs + Q2 defer; Web PG is SoT |
| Owner web works but Bot admin tools don’t (or vice versa) | Medium | Matrix docs; optional Q1 link guidance without auto-merge |
| Session `is_owner` split breaks shared-admin installs | Medium | Feature-flag; default off; ACL unchanged |
| Bot PG flag (if enabled) expands attack surface | High | Fail-closed clients only; same authz; no Owner token |
| Synthetic IDs in broadcast/notify | Low–Med | Filter/label in D4 hygiene |
| Touching shop ACL “for cleanup” | High | Do not change C4 rules; parity tests gate merge |
| Starting D4 from `main` without C/D | **Critical** | Impl branch from D3 tip only |

**Rollback:** Docs/UI-only → safe revert. Session flag → disable flag. Never alembic-downgrade C5 for D4.

---

## 9. Required tests (implementation)

| Case | Expect |
|------|--------|
| C4 shop parity | Unchanged green |
| C0–C5 / D1–D3 regression | Green |
| pg_staff Web | `permissions=[]`; no shop sidebar; no bot identity helpers invent shop ACL |
| Bot PG handlers | Only platform admin; `get_pg()`; no staff/reseller client without Q2 flag |
| Reseller Web PG | `get_pg_for_reseller` / fail-closed — never Owner |
| Staff Web PG | `get_pg_for_staff` / fail-closed — never Owner |
| Synthetic telegram_id | Listed as internal / skipped in notify where touched |
| Optional `is_owner` | Set only for Owner file match; `role=admin` ACL identical |
| Menu key set | `FEATURE_PERMS` Web sidebar ∩ Bot reseller hub identical for same profile |

---

## 10. Open decisions (must answer before D4 code)

| # | Question | Options | Recommend |
|---|----------|---------|-----------|
| **Q1** | Distinguish Owner in Web session (`is_owner` / `PrincipalKind.OWNER`)? | A) Defer docs-only · B) Add flag, ACL unchanged · C) Full Owner/Admin product split | **A** |
| **Q2** | Bot PG for reseller (or staff)? | A) Defer — Web only · B) Feature-flag reseller Bot PG via own client · C) Full Bot PG parity | **A** |
| **Q3** | `bot_permissions` column? | A) Keep mirror · B) Document deprecate · C) Drop later (not D4) | **A** (+ comment) |
| **Q4** | In-product identity help text? | A) Yes · B) Docs only | **A** |
| **Q5** | Extra shared platform-admin helper beyond contracts? | A) Contracts/docs only · B) Thin shared helper, behavior-preserving | **A** or **B** |

---

## 11. Exit criteria (plan approval)

- [ ] Q1–Q5 answered.  
- [ ] Confirm: pg_staff remains web-only.  
- [ ] Confirm: no Owner fallback.  
- [ ] Confirm: preserve existing working Web + Bot shop flows.  
- [ ] Impl base = D3 tip.  
- [ ] **No code until this plan is approved.**

---

## 12. Stop line

**Planning only.** Do not start D4 implementation until decisions above are recorded.
