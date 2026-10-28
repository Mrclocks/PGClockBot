# Integration API release operating guide

This is a locally implemented release candidate for one existing PasarGuard panel.
Local implementation, pre-PR review and handoff are complete. External verification
is **deferred / not performed** by the user's 2026-10-25 decision. The candidate is
not production-verified and remains disabled. Use the [roadmap](INTEGRATION_API_ROADMAP.md)
for reused and newly executed local test counts, the refreshed baseline comparison
and the review submission record.
Historical checkpoint failures and their targeted repairs remain in that document.

## Supported behavior

- Backend Bearer authentication, current integration/shop/principal permissions,
  shared SQL request budgets and safe, private errors apply to every v1 route.
  Customers and services are qualified by the integration and canonical customer.
  The website must resolve `customer_id` from its own authenticated session; never
  accept an arbitrary customer selector from its browser. PGClockBot does not
  authenticate a website's end users or attest website-side reauthentication.
- Website registration creates a neutral customer/mapping, never a Telegram user,
  shop membership, contact-verification record, order, payment or wallet credit.
  Website entry is explicitly denied until an authorized owner changes its policy.
- Catalog IDs are stable per integration. Only active, allowlisted, non-trial plans
  with reproducible explicit groups and supported ASCII naming are exposed.
  Mutable panel templates, custom naming variables and unsupported activation
  semantics are excluded. Catalog terms are conditional observations; local terms
  are checked at acceptance and live groups, role/device limits, owner, billing and
  quota are recomputed before dispatch. A later change fails unstarted work.
- Current live own-account PG evidence is required for writes. Read outages may
  use identity-bound authorization evidence for at most five minutes; live denial
  invalidates it. No owner/parent credential fallback or fabricated admin exists.
  Ambiguous principal-to-PG-account mappings cannot dispatch API work.
- A permanent unique integration/external-reference digest survives key rotation,
  short request-key expiry and operation archival. Changed kind/customer/service/
  body conflicts. A replay returns the original current operation; archival returns
  410 with that scoped ID. Operation/replay tombstones are retained; no operation
  archival or destructive pruning job is enabled in this release.
- Queued work claims a 60-second lease, with at most two running operations per
  integration and batches of eight. Dispatch preparation and network work have
  bounded timeouts below that lease. Expired unstarted leases may requeue; a
  committed intent never automatically writes again. Transient evidence outages
  defer unstarted work with a 60-second admission delay, until the 24-hour queue
  deadline. A lost lease fences local dispatch/finalization but cannot stop a
  request already received by PG.
- Saved target, API service identity, mutation guard and non-expiring byte/user
  commitments commit before the panel write. Exactly one write uses the saved
  absolute target and a one-use permit, with no automatic auth retry. Identity,
  owner, group set, quota/time, device limit and operation marker must match an
  uncached observation before sealing success. Renewal and pack increments use
  shared business arithmetic and retain the same local service and panel user.
- Packs require an explicit retaining integration allowlist, correct shop/category
  and active pack. API amounts use exact integer bytes/seconds; fractional-day
  packs are unsupported. Unlimited dimensions cannot receive an additive pack.
  Supported pending-start packs activate the existing pending duration and return
  an `activates_on_hold` warning. Renewal previews are advisory and reserve nothing.
- Only private `/access` and `/qr` disclose connection credentials. The URL must
  use the configured panel HTTPS origin and configured subscription path, with a
  single valid token, no userinfo/query/fragment/control characters. This stricter
  v1 subset does not accept alternate public subscription origins or URL formats.
  URL storage is encrypted with `secret_box`; legacy plaintext delivery fields stay
  null. Private reads recheck current identity and URL digest. Unexpected rotation,
  missing delivery or mismatched terms keeps the service under review. QR is
  generated locally from exactly that validated URL and carries its access version.
- Telegram consent needs the actual authenticated sender, explicit bot confirmation
  and independent website confirmation before expiry. Opening a link is insufficient.
  Unlink works independently of public API availability. It never transfers services,
  balances or roles, exposes web services in legacy lists or opts into notifications.

## Routes, schemas and backend usage

The [OpenAPI artifact](INTEGRATION_API_OPENAPI.json) describes all mounted v1 methods,
strict inputs, the fixed error envelope, accepted-operation Location/Retry-After,
private access and binary QR responses. Regenerate locally without starting a
server or touching a database:

```bash
python -m app.cli.integration_openapi > docs/INTEGRATION_API_OPENAPI.json
```

[Generic backend building blocks](examples/integration_backend.py) show durable
submission/polling identifiers and exact-byte HMAC verification. Configure a
reused `httpx.AsyncClient` on the website backend with an HTTPS base URL, TLS
verification and explicit timeouts. Keep all Bearer/signing/delivery credentials
server-side and out of request logs, analytics, browser bundles and URLs. Do not
log private API response bodies or HTTP exception/request objects from these paths.

Persist the customer mapping and business intent before submission. Reuse the
same `external_operation_ref` and `Idempotency-Key` after a timeout. Persist the
returned operation ID; poll the customer-qualified Location until terminal or
review. Unknown/review is not a request to submit another intent. Retail payment
and refund policy belong to the website and are independent of technical state.

## Webhooks and operator management

The protected `/integrations/{id}/operations` page lists bounded operations, event
status, native uncertainty and control evidence, even with a disabled integration.
It requires current management and operation-read permissions. Additional controls
require `integration_operations_reconcile` or `integration_webhooks_manage`;
pack grants require `plans` and `integration_services_addons`. Existing feature
permissions are reused; no permissive default is added. All forms use CSRF and
signed single-use action tickets. Key creation is HTTPS-only, private and one-time.

Events commit with operation/service/consent versions. Bodies contain public IDs,
versions, timestamps and safe states/codes; never private URLs, panel/Telegram IDs,
external references, display text or upstream bodies. Events created without a
configured destination remain retained and do not become automatically deliverable
when a destination is later added. Destination changes advance a version; explicit
scoped redelivery is required to bind historic events to the current destination.

Signing secrets are distinct from API credentials and encrypted at rest. Rotation
uses key IDs and 0–1440 minutes overlap. Receivers explicitly retire the old key by
that deadline. HMAC-SHA256 signs `timestamp.event_id.body` over the exact body bytes.
Verify the signature with a constant-time comparison and a five-minute timestamp
window, then atomically deduplicate the stable event ID with applying only newer
resource versions. Acknowledge only after commit; poll for gaps/out-of-order events.

Delivery is at least once. The body/event ID stay stable; attempt ID, timestamp and
signature change. There are at most 13 automatic attempts: immediately; 1m, 5m,
30m, 2h, 6h, 24h; days 2–7, with bounded jitter. Retry-After is capped at 24h.
Five seconds covers a delivery attempt, DNS is bounded, the total response read is
capped at 16 KiB, and two deliveries may run per integration. Final-attempt crashes
become exhausted without an extra send. Failed/unacknowledged events are retained.

URLs require an ASCII hostname, HTTPS/443, no credentials/query/fragment, and bounded
length. Registration and every connection validate all A/AAAA answers. Any private,
loopback, link-local, multicast, reserved, unspecified or mapped-internal address
rejects the destination. The transport connects to a validated numeric address
with the original hostname for certificate verification and SNI; it does not resolve
again, follow redirects or use environment proxies. Deployment must also enforce
network egress rules; those rules and actual DNS/TLS behavior are not locally proven.

Read-only reconciliation claims the original unknown/review operation and uses its
saved identity/absolute target and own principal read authority. It can seal a
fully verified existing effect without another panel write, even while public API
access is disabled. It cannot force a failure, free ambiguous capacity, change a
username/plan, delete a user or reapply a relative increment. Native status changes
lack the unique operation marker used by API targets: matching status alone does
not prove an old request ended, so their unknown guards remain held. External
quiescence/evidence is required; a force-clear/reapply control is intentionally absent.

## Migration and rollback

Feature revisions 0071 foundation, 0072 administration and 0073 security follow
upstream `0070_shop_miniapp_key`; subsequent revisions are: 0074 identity expansion; 0075 channel
ownership; 0076 consent history; 0077 catalog/read snapshots/request replay; 0078
operation/target/audit/lease/guard/capacity; 0079 encrypted delivery/pack grants;
0080 event/destination/key/attempt/control evidence. Each new migration is repeat-safe
and retains populated records on downgrade. Merged upstream revisions 0067–0070 are unchanged. Only this feature’s unmerged
revisions were renumbered during target integration.

On an existing installation, under normal backup/change procedures:

1. Keep route, execution and webhook flags false; keep the DB API switch disabled.
2. Deploy the compatibility-aware code and start normally. `init_db` runs the usual
   `alembic upgrade head`; no intermediate revision or manual backfill is required.
3. Migration 0075 creates missing canonical customers and service associations in
   bounded 500-row keyset batches before validating coverage. Repeat runs preserve
   public IDs, blocked identities, shop/review flags and all financial data. An
   existing contradictory association still stops the migration rather than
   reassigning a customer. The optional backfill CLI remains available for repair
   and maintenance after installation; it is not an upgrade prerequisite.
4. 0075 then changes owner nullability and adds its channel constraint.
   SQLite requires a dedicated migration connection with FK enforcement off for its
   parent-table rebuild and verifies integrity afterward; normal Alembic supplies it.
   Request connections must retain FK enforcement. PostgreSQL uses native alteration.
5. Check canonical coverage, indexes/constraints, original shops/panel IDs and
   financial totals using the deployment's own controlled procedures. These checks
   are covered by synthetic SQLite/PostgreSQL tests. The corrected startup path
   was also exercised on a separately restored operator-supplied PostgreSQL backup;
   no production database or external panel was contacted.

Legacy panel fencing and canonical adapters run only when both
`INTEGRATION_EXECUTION_ENABLED=true` and global `integration_api_enabled=1` are
active. The process loads that one switch before starting clients/workers; supported
settings writes refresh it after commit. Restart every process after an external
SQL/CLI change to the switch. These activation/history snapshots are bounded process booleans and add no hot-path query before first activation.
API authorization still checks the database switch directly on each request.

The internal, monotonic `integration_execution_started` setting is inserted in the
same transaction as the saved panel target. It is not an operator activation
switch and must remain with retained operation evidence. Startup observes its
presence in the existing settings-key query. After any execution history, native
allocation/user-slot accounting continues to include durable commitments even if
execution is disabled; a lost response is not freed capacity. Fresh disabled
installations skip the integration worker scans and adapter/fence sessions.
Settings updates unrelated to the API switch and shop overrides cannot change
the global activation snapshot.

For shutdown/rollback, pause new execution and outbound sending, disable public
registration/access, stop the old process and account for any already-sent request.
Wait for or investigate external work; a lease timeout is not proof of non-application.
Keep targets, customer mappings, secret-box root, encrypted delivery/signing keys,
operation/reference/request tombstones, consent/action history, guards and capacity
commitments. Retaining downgrade disables the DB API switch but is not a worker
shutdown procedure. Use a compatibility-aware binary supporting nullable bot owners
and the retained schema; an arbitrary pre-feature binary is unsafe once web services
exist. Do not physically drop populated integration tables/columns as rollback.
See the [Persian rollback checklist](INTEGRATION_API_ROLLBACK_FA.md) for the two
supported operator paths.

## Validation and external gates

For this local handoff, all checks requiring an actual PasarGuard panel, staging
setup/deployment, live infrastructure probes, real credentials or real Telegram/
webhook deliveries are **deferred / not performed**. The operator currently has no staging panel available. A separately restored
backup was tested locally, but an actual test bot and PasarGuard write/renewal
exercise is still required before the mutating changes are merged. Earlier automated evidence was verified against its saved fingerprint. Target
integration required fresh affected database/concurrency, security and full-suite
checks; the final submission checkpoint in the roadmap records their results.
Deferral does not satisfy the future activation gates below or relax any runtime
authorization, current-principal or live-PG-evidence requirement. Public API,
execution, webhook and database activation switches remain disabled.

Local tests use synthetic identities, isolated SQLite/PostgreSQL schemas and fake
panel/Telegram/webhook dependencies. They prove local scope, atomicity, state,
privacy, migration and concurrency behavior; they do not prove a particular deployed
PG build's write or response semantics. No real webhook, Telegram, payment or panel
credential is used. No Node check is needed because browser JavaScript was unchanged.

Before any activation, verify in a controlled deployment: the actual supported PG
schema, role/quota/identity/group/device fields, note marker retention, absolute
renewal/on-hold semantics, user lookup and subscription URL shape; upstream/proxy
absence of write retries; late-response behavior; the original principal's own
credentials; TLS/proxy provenance; DNS/SNI/certificate and egress controls; strong
persistent secret-box root and restorable evidence backups; worker lifecycle and
monitoring. Do not activate if any required proof is missing. Templates, arbitrary
subscription origins, delivery rotation, ambiguous native reapplication and destructive
evidence cleanup remain unsupported in v1.

Default-off controls are `INTEGRATION_API_ROUTES_ENABLED`,
`INTEGRATION_EXECUTION_ENABLED`, `INTEGRATION_WEBHOOKS_ENABLED`, the database
`integration_api_enabled=0`, disabled new integrations, denied website entry and
explicit grants/allowlists. This work has not changed any live deployment setting.
