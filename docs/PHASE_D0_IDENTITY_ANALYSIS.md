# Phase D0 — Identity Architecture Analysis

**Status:** Analysis complete — awaiting decision checklist approval  
**Parent:** `docs/PHASE_D_PLAN.md`  
**Code changes:** None

---

## Confirmed

- **pg_staff** is an independent secondary admin (PG-only).  
- Must **not** require conversion to reseller for web/PG access.  
- Phase C authz isolation remains the security baseline.

---

## Principal map (target)

| Kind | Storage | Web | Shop | PG | Bot |
|------|---------|-----|------|----|-----|
| Owner | `web_admin.json` + env `PG_*` | yes | full | owner client | `ADMIN_IDS` (separate) |
| platform Admin | same session as Owner today | yes | full | owner client | same |
| pg_staff | `PgStaffAccess` | yes | no | staff client + enc | none (web-only) |
| Reseller | `ResellerProfile` + `BotUser` | yes | yes | reseller client when linked | yes |

---

## As-is critical defect

Owner «اعطای دسترسی وب» → `provision_existing_pg_admin` → **Reseller** and may **delete** `PgStaffAccess`.

True pg_staff path `grant_web_access` exists in service layer only.

This is the primary Phase D product defect relative to the confirmed decision.

---

## Flow inventory (summary)

See full detail in `PHASE_D_PLAN.md` §1–2.

| Flow | Works for architecture? |
|------|-------------------------|
| Owner setup | Partial (web≠PG sync) |
| Create PG admin | Partial (no password policy; silent role drop) |
| Grant web (UI) | **Fail** vs pg_staff-first-class |
| `grant_web_access` service | Pass (orphaned) |
| Reseller provision | Pass for reseller intent |
| Staff password `/security` | Pass for remediating enc |
| Login/session | Pass isolation; partial readiness UX |

---

## Open decisions — RESOLVED

1. Username equality for pg_staff: **yes** (`web == pg`) — enforce in D2.  
2. Owner dual credentials: **keep separate** by default.  
3. platform Admin split: **defer** to D4.  
4. Opt-in migrate staff→reseller: **allowed later**, never automatic.  
5. Password digits: **yes** — PasarGuard requires ≥2 digits; mirror exactly.

D1 implementation: see `docs/PHASE_D1_CREDENTIAL_POLICY.md` (approved).  
D2 planning: see `docs/PHASE_D2_PLAN.md` — **awaiting approval before code**.
