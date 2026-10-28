# Integration API review sequence

The large [candidate #967](https://github.com/Mrclocks/PGClockBot/pull/967) is
closed and superseded by the six pull requests below. All target
`dev`. At the operator's request, all six are open now: **#971 is ready for review**;
**#978–#982 remain Draft** until their immediate prerequisite has merged.

This review source incorporates upstream dev **0.5.6 (`55286d73`)** through normal
merges. Upstream migration head remains 0070; the feature adds 0071–0080.
The [current validation](INTEGRATION_API_ROADMAP.md#publication-review-follow-up)
records this source; the historical validation below belongs to earlier revisions.
`INTEGRATION_API_ROUTES_ENABLED`, `INTEGRATION_EXECUTION_ENABLED` and
`INTEGRATION_WEBHOOKS_ENABLED` still default to false; the global database setting
`integration_api_enabled` defaults to `0`. No deployment or activation is included.

Each branch contains its predecessors. Its GitHub diff against dev is therefore
cumulative until those predecessors merge; use the isolated comparisons below to
review one logical part at a time. Merge in the listed order. After each merge,
incorporate current upstream dev into the next published branch with a normal
merge, resolve any conflicts, recheck migration numbers, run focused/security/lint
and full-suite checks, and mark that next PR ready. This also handles squash
merges without force-pushing published history. Merging remains a maintainer action;
marking a PR ready does not deploy or activate integration.

| Part / PR | Prerequisite / state | Branch | Scope | Migration head |
|---|---|---|---|---|
| [1 — #971](https://github.com/Mrclocks/PGClockBot/pull/971) | None; **Ready** | `codex/integration-customer-foundation` | Canonical/fixed-shop identity and retained schemas; automatic startup backfill; legacy deletion compatibility. No routes, workers or delivery adapters. | `0076_customer_telegram_links` |
| [2 — #978](https://github.com/Mrclocks/PGClockBot/pull/978) | #971; **Draft** | `codex/integration-read-api` | Default-off GET endpoints, backend authentication/admission, scoped catalog/customer/service readers and snapshots. No HTTP mutation, consent or operation routes. | `0077_integration_read_snapshots` |
| [3 — #979](https://github.com/Mrclocks/PGClockBot/pull/979) | #978; **Draft** | `codex/integration-technical-operations` | Customer registration; durable provision/renew/add-on intents; private access; native ownership/fencing adapters and capacity accounting. No outbox or consent delivery. | `0079_integration_private_delivery` |
| [4 — #980](https://github.com/Mrclocks/PGClockBot/pull/980) | #979; **Draft** | `codex/integration-webhooks-outbox` | Transactional signed outbox, protected retries/transport, retained evidence and dual-proof Telegram consent. | `0080_integration_event_outbox` |
| [5 — #981](https://github.com/Mrclocks/PGClockBot/pull/981) | #980; **Draft** | `codex/integration-management-pages` | Authorized administration/customer/audit pages and CSRF/action-ticket controls; existing application import order retained. | unchanged |
| [6 — #982](https://github.com/Mrclocks/PGClockBot/pull/982) | #981; **Draft** | `codex/integration-documentation` | Actual generated OpenAPI enrichment/export, backend example, compatibility/operation/rollback guides and this review index. | unchanged |

Each part has a separate review comparison:

- [Read API](https://github.com/MatinDehghanian/PGClockBot/compare/codex%2Fintegration-customer-foundation...codex%2Fintegration-read-api)
- [Technical operations](https://github.com/MatinDehghanian/PGClockBot/compare/codex%2Fintegration-read-api...codex%2Fintegration-technical-operations)
- [Outbox and consent](https://github.com/MatinDehghanian/PGClockBot/compare/codex%2Fintegration-technical-operations...codex%2Fintegration-webhooks-outbox)
- [Management pages](https://github.com/MatinDehghanian/PGClockBot/compare/codex%2Fintegration-webhooks-outbox...codex%2Fintegration-management-pages)
- [Documentation and OpenAPI](https://github.com/MatinDehghanian/PGClockBot/compare/codex%2Fintegration-management-pages...codex%2Fintegration-documentation)

## Original review concerns

- **Fixed — startup and disabled execution:** automatic migration backfill and
  the optional execution-path gates are described below. Existing assertions and
  the disabled checkout query/request comparison remain in place.
- **Fixed — unrelated import reorder:** `app/api/app.py` retains dev's existing
  import order. Only required integration imports and registration calls remain
  alongside the customer-block synchronization change.
- **Fixed — rollback instructions:** the [Persian guide](INTEGRATION_API_ROLLBACK_FA.md)
  requires preparing Alembic's revision with the current code before testing an
  older binary on a stopped database copy. A never-activated installation and an
  installation with website services or uncertain panel effects have different
  procedures. Migration 0071 retains foundation targets referenced by surviving
  tables even when no integration row exists, so legacy service inserts remain
  valid after downgrade. Retaining downgrades preserve schema and evidence; they do not undo
  external effects or make an arbitrary pre-feature binary compatible.
- **Intentionally retained — ownership validation:** service lists add an indexed
  `EXISTS` predicate inside their existing query; an individual canonical-owner
  check may add one indexed lookup. These checks remain active with execution
  disabled to reject mismatched owners and website-origin services. They are not
  an extra round trip for every list row.
- **Scope corrected — payments:** this API performs technical registration,
  provisioning, renewal and add-on fulfillment. The website owns retail pricing,
  customer payments and refunds. Customer wallet charging is outside this series.
- **Security wording qualified:** HTTPS/Bearer checks reject requests carrying
  nonempty Cookie or Origin headers and alternative credential channels; they are
  not a universal browser detector. Authentication evidence has a detailed-record
  limit with aggregated overflow. Public-address validation, DNS pinning and delivery timeouts remain;
  no dependency was added.
- **Review size remains substantial:** dependent diffs are cumulative. Part 6's
  7,167-line generated OpenAPI JSON export covers 17 paths and accounts for most
  of its documentation lines. The handwritten guides and runtime changes still
  need review through the isolated comparisons above.
- **Outstanding — live acceptance:** real test-bot/PasarGuard operations,
  Telegram/webhook delivery and deployment lifecycle checks remain unperformed.
  Local databases and fake transports do not establish these behaviors. Keep
  mutating parts Draft until their prerequisites and live acceptance gates pass.

## Startup and disabled-path corrections

Ordinary `init_db` / `alembic upgrade head` now performs the 0075 backfill in
bounded batches before its ownership validation. There is no intermediate 0074
stop or manual backfill prerequisite. SQLite bulk parameters also remain within a
999-variable limit; repeated upgrades preserve stable IDs and blocked states.
The optional CLI remains available for maintenance. Contradictory canonical
associations still stop safely rather than guessing ownership.

Native fencing and canonical adapters activate only with both execution and the
global database API switch enabled. The cold platform wallet checkout regression
matches clean dev's 22-statement action/table signatures and its exact one panel POST
payload (only two adjacent independent owner/plan eager reads are normalized) for all three disabled combinations. Additive ORM fields naturally change
column lists. Native create/modify/delete paths open no integration sessions while
disabled. Fresh inactive workers open no integration sessions or queue scans.

A saved target also records the internal, monotonic `integration_execution_started`
setting. Existing startup settings-key loading observes that marker without a new
query. Previously used installations keep durable unknown-outcome capacity counted
even after execution is disabled; this safety accounting must not be removed to
optimize the inactive path. Committed platform switch changes update the activation
snapshot; unrelated settings and shop overrides cannot enable or disable it.
Restart every process after an external database/CLI switch change.

## Current verification on dev 0.5.6

Foundation: **5,726 passed / 60 skipped / 11 failed**. Assembled series:
**6,091 passed / 79 skipped / 11 failed**. Both have exactly the same 11 failure
IDs as clean dev (**5,696 passed / 32 skipped / 11 failed**); zero added failures.
The assembled security manifest recorded **3,447 passed / 47 skipped / no failures**.
Separate SQLite/PostgreSQL migration and concurrency matrices passed **96 cases
without skips**. See the [source-specific commands and limitations](INTEGRATION_API_ROADMAP.md#publication-review-follow-up).
Dependent-stage focused results are recorded separately; assembled full/security
results do not certify an individual Draft stage after its prerequisite merges.

## Historical verification before the dev 0.5.6 synchronization

At dev 0.5.5 (`98749f49`), after synchronization through normal merges, the
foundation migration/import/branding/frontend checks passed **106 tests, no skips,
19 subtests**, and the assembled
disabled/settings/API/management/scheduler/branding checks passed **73 tests and
19 subtests**. The lint ratchet passed for 25 foundation and 166 assembled changed
Python files; inherited Mini App branding Node checks passed all 8 cases. These
focused checks and the earlier broad results are historical evidence, not results
for the current dev 0.5.6 source. The earlier explicit migration/concurrency matrix
recorded all 89 SQLite/PostgreSQL cases passing without skips. The exact commands
and aggregate results are recorded in the
[roadmap revision checkpoint](INTEGRATION_API_ROADMAP.md#review-revision-2026-10-26).
Matrices overlap and must not be added. A PostgreSQL skip is not a PostgreSQL pass.

A separately restored operator-provided PostgreSQL backup reproduced the original
coverage failure at revision 0067. Fixed ordinary startup reached the first part's
0076 head and the complete series' 0080 head on independent copies. Fingerprints
of original users, services, orders, payments and wallets were unchanged. No backup
content, credentials, row values or database dump is part of these branches.

Actual test-bot/PasarGuard creation and renewal, live Telegram/webhook delivery,
proxy retry/quiescence, TLS/DNS/egress, evidence restore and worker lifecycle proof
remain **not performed**. The operator currently has no staging panel available.
A restored database proves migration compatibility; it does not prove external
panel behavior. Keep dependent parts Draft until their prerequisites merge. Before merging the
mutating parts, obtain the separate live acceptance evidence; marking them ready
for review does not establish it. Runtime activation remains disabled. No
deployment or production activation was performed.

The [Persian rollback guide](INTEGRATION_API_ROLLBACK_FA.md) separates installations
that never activated integration from those that may have sent a panel request.
Retain mappings, targets, tombstones, guards, commitments, consent, outbox, root and
signing keys, and execution history. A pre-feature binary is not a safe fallback
once website services or uncertain effects exist.
