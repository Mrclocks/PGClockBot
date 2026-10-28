# Integration service compatibility inventory

Status: compatibility, canonical ownership and dual-proof linking are implemented
locally. The current execution/delivery/webhook boundary is recorded in the
[release operating guide](INTEGRATION_API_OPERATIONS.md). Activation stays disabled.
The table preserves the Phase 2B inventory and its original scope.

| Boundary | Compatibility behavior |
|---|---|
| Construction | When both execution and global API controls are enabled, delivery, manual provisioning and verified import dual-write the canonical legacy adapter in their original transaction. Otherwise the optional adapter performs no queries. The historical 2B slice exposed no API constructor; current execution creates API services in its separately gated durable flow. |
| Ownership and legacy channels | `service_ownership` checks legacy origins and canonical adapter equality. Customer readers exclude web origins without consulting Telegram links. Legacy rows with absent canonical fields remain readable through their original scoped bot owner. Migration 0075 fills missing canonical coverage automatically before changing owner nullability; contradictory associations still fail closed. |
| Block/unblock | With both activation controls enabled, explicit panel, bot and bulk block transitions synchronize the canonical legacy adapter in the same transaction. They never change web customer state. Backfill alone never reactivates blocked customers. |
| Panel presentation | Separate `/integrations/{id}/customers` view checks the live owner, fixed shop and customer permission. It returns bounded neutral customer metadata and scoped service counts, without Telegram links, subscription credentials or fake bot identities. Existing bot customer metrics remain bot customer metrics. |
| Reporting | Dashboard, daily, finance and subordinate service counts no longer require an inner bot-user join. The existing correlated demo exclusion includes neutral web customers while excluding demo legacy buyers. Shop/review predicates are retained. |
| Allocation | Existing `sum_service_bytes` and external-allocation matching already use the selling shop and panel identity, independent of the bot owner. Web limits count in the same aggregate; review flags and original shop/identity are preserved. Unresolved commitments remain in shared allocation and user-slot gates after execution is disabled. A permanent execution-history marker is saved with the target and observed by the existing startup settings-key query; never-used installations avoid those optional queries. |
| Reseller billing | `tick_reseller_usage` bills the same panel admin traffic watermark/ledger; it is independent of local bot owners. Web traffic must belong to that same validated principal at provisioning. No customer wallet, receipt, refund or synthetic payment is introduced. |
| Notifications and automation | Scheduled Telegram quota alerts explicitly select legacy-origin services with a bot owner. Web services carry no plaintext subscription token. Wallet automation skips neutral owners; legacy order notifications still require their original order/buyer. Linking does not opt into any channel delivery. |
| Deletion and review | Legacy service administration rejects web origins before external deletion; native panel detachment retains API service evidence. A selling shop with integration history cannot be hard-deleted/reassigned through legacy customer deletion. Shop review assignment selects only legacy origins. |
| Lifecycle | Existing principal resource counts already include services by `owner_principal_id`; disablement does not transfer their ownership. API-origin records require a mapped canonical customer and their originating operation at the current scoped execution boundary. |

## Migration and rollback

`0075_service_channel_ownership` follows 0074. It verifies aggregate adapter and
legacy service coverage, adds integration/origin references and lookup indexes,
then permits a missing bot owner only for explicit API-origin rows. Such rows
require canonical/public IDs, integration, origin, principal, resolved shop and
empty legacy delivery columns. Cross-table integration/customer/shop/principal
checks remain mandatory in the scoped API service constructor and each mutation;
the row constraint alone does not establish that authority.

Normal `init_db` upgrades directly to head. Migration 0075 automatically backfills
customers and legacy services in 500-row keyset batches, preserving existing public
IDs and blocked states. No intermediate revision or manual CLI step is required.
Contradictory associations stop the upgrade without guessing or changing
owner/shop/review evidence. The CLI remains an optional maintenance tool. SQLite parent-table
rebuilding requires a dedicated migration connection with foreign keys disabled;
0075 refuses an enforcing connection before changes and checks foreign-key
integrity after rebuilding. The normal Alembic connection has enforcement off.
PostgreSQL uses native alteration. Downgrade disables API access and retains all
associations, nullable legacy ownership and constraints; use a customer-aware
compatibility binary, never an arbitrary pre-feature application.

## Historical Phase 2B evidence (2026-10-21)

- Affected existing legacy/customer/reporting matrix: 116 passed, 16 subtests passed.
- Focused new ownership/presentation/block and migration matrix: 9 passed,
  including two PostgreSQL cases, no skips. Initial new fixture/assertion errors
  were corrected before this complete run. Test connection discovery errors were
  local setup issues, not PostgreSQL success evidence.
- Lint checkpoint: 77 changed Python files, no added findings (before the later
  linking and final canonical-claim changes). That checkpoint did not include final lint or broader security/full validation;
  current evidence and review repairs are recorded in the roadmap.
- No production database/panel, real Telegram, wallet/payment credential or webhook
  destination was used. No commit, push, PR, deployment or activation occurred.

## Shared mutation boundary

All production `get_pg*` factories now attach the API service fence to the existing
single-panel transport. Legacy-origin users retain their established flows.
API-owned user reset, revoke, delete, owner transfer, quota edits and template
replacement through native PGClockBot pages are refused. Absolute status changes
(including the billing `/disabled` path) take a durable native mutation guard,
commit intent, send once without auth retry and verify identity/owner/status before
releasing it. An uncertain native status change retains its guard and appears in
the scoped operator page; matching status alone cannot prove an older request has
stopped. Direct edits in PasarGuard itself remain outside this application fence.

API workers use a one-use exact-target/client/lease permit. Durable commitments
enter both shared allocation totals and user-slot gates, including the saved PG
account identity; lease/temporary-hold expiry cannot release them. Verified success
atomically replaces a commitment with the scoped service quota. No new customer
wallet, payment, order, refund or Telegram delivery flow is introduced.
