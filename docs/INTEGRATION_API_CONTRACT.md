# PGClockBot Integration API contract — v1

Migration references use the final unmerged feature revisions 0071–0080, after
upstream `0070_shop_miniapp_key`. Historical test counts are in the roadmap.

Status: Phases 2B, 2C and 3–7 are implemented locally for the conservative v1
subset described in the [release operating guide](INTEGRATION_API_OPERATIONS.md).
Phase 8 local preparation and validation are complete; the roadmap records actual
evidence and unperformed external release gates. Route
registration, execution and outbound webhook sending default to disabled. No
production activation is authorized. The original design/source baseline remains
upstream `dev` commit `ebd27e717b2d95de7b3b5931efedbcf246a02029`.

The numbered contract is normative. Historical Phase 1/2A implementation-boundary
sections below describe those checkpoints, not the current feature inventory.
Current supported behavior, deliberate restrictions and external deployment gates
are listed in the release guide; unsupported panel behavior fails closed.

Read with the [customer identity decision](INTEGRATION_CUSTOMER_IDENTITY.md) and
[delivery roadmap](INTEGRATION_API_ROADMAP.md). Examples and fixtures are synthetic.
No consuming website's private specification is needed or incorporated.

## 1. Responsibility and architecture

The website authenticates its customers and owns membership, retail pricing,
customer payments, receipts and customer refunds. PGClockBot authorizes and
executes technical service operations on its existing single PasarGuard panel.
API credentials stay on the website backend. No browser/customer bearer token,
customer-payment approval endpoint or customer wallet mutation is introduced.

An integration has one immutable owning `OrgPrincipal` and one immutable shop
(platform shop is explicitly represented by null). Its credentials, customer
mappings, allowed plans, operations and services share that boundary. Credential
rotation never changes the integration identity. Changing owner/shop requires a
new integration; resource transfers are outside v1.

Use focused `app/api/` routers and typed `app/services/` domain modules, following
the existing downward dependencies. Routes authenticate/validate/render and call
services; jobs only orchestrate bounded batches. Do not add route bodies to
`app/api/app.py`, call Telegram from technical execution, or introduce another
panel connection/backend. Separate API technical operations from paid `Order`
and `Payment` state machines. Reuse/extract affected technical rules without
creating an approved/free payment or changing a customer wallet.

### Original source evidence (design baseline)

| Source and symbols inspected | Existing behavior relevant to the design |
|---|---|
| [models.py](../app/db/models.py), `BotUser`, `UserService`, `Order`, `OrgPrincipal` | Service/customer ownership currently uses bot users; shop, principal and panel identities are distinct. Integration foundation entities now live in `integration_models.py`; customer/service ownership remains unchanged. |
| [authz.py](../app/services/authz.py), `authz_from_staff`, `can_shop`, `authorize`; [shop_scope.py](../app/services/shop_scope.py), `shop_owner_id`, `is_platform_admin` | Live shop permissions and explicit principal/scope checks exist. Legacy shop-feature admin bypass is insufficient for tenant authorization. |
| [resellers.py](../app/services/resellers.py), `FEATURE_PERMS`; [principal_pg_authz.py](../app/services/principal_pg_authz.py), `authorize_pg_user_action` | Feature keys and PG action matrices are separate. Missing principal/PG capability denies operations. |
| [pasarguard.py](../app/services/pasarguard.py), `get_pg_for_reseller`, `get_pg_for_principal`, `_mutation_request`, `user_subscription_url` | Reseller/principal credentials address the same configured panel. Tenant clients do not fall back to owner credentials; mutations have a separate request path from resilient reads. |
| [provision_gate.py](../app/services/provision_gate.py), `assert_provision_create`, `assert_provision_renew`, volume hold helpers; [pg_quota.py](../app/services/pg_quota.py), `assert_can_create_user`, `assert_can_modify_user` | Existing billing, role, max-user, time/data/device and allocation checks must remain applicable. Template and custom payload checks differ. |
| [billing.py](../app/services/billing.py), `assert_billing_allows_provision`, `tick_reseller_usage`; [volume_allocation.py](../app/services/volume_allocation.py), `sum_service_bytes`, `sum_active_holds`, `hold_volume` | PAYG empty-balance policy can block provisioning. Allocation counts services by selling shop and time-limited holds. Expiring holds alone cannot protect an unresolved API write. |
| [orders.py](../app/services/orders.py), `deliver_order`, `_claim_renewal_order`, `_write_renewal_target` | Delivery commits a paid-order claim; snapshot renewal saves an absolute target before writing. This is not a generic durable API queue; create delivery also has rollback/delete behavior unsuitable for unknown API outcomes. |
| [service_renewals.py](../app/services/service_renewals.py), `renewal_terms`, `calculate_renewal`; [service_addons.py](../app/services/service_addons.py), `apply_service_addon`, `_apply_pack_to_service` | Renewal arithmetic covers remaining quota/time and on-hold states. Add-ons compute changes from live state; they do not supply a persisted API target/reconciliation protocol. |
| [service_cancellations.py](../app/services/service_cancellations.py), `lock_service_mutation`; `Order.uq_orders_service_quota_mutation` in models | Existing order/cancellation exclusion needs to share a durable guard with API mutations, rather than use independent locks. |
| [app.py](../app/api/app.py), `get_session_user`, `require_staff`, `require_perm`, `csrf_origin_guard`; [csrf.py](../app/services/csrf.py) | Panel uses signed session cookies, live staff reload, origin checks and double-submit tokens for cookie mutations. Mini App has a specific exception, not a generic API exemption. |
| [panel_connection.py](../app/api/panel_connection.py), `PanelConnectionMiddleware`; [pg_read_cache.py](../app/services/pg_read_cache.py) | Panel read budget skips `/api/`; API needs an explicit read budget. Existing cache is identity-scoped, 4-second TTL, maximum 256 entries, invalidated by identity. |
| [secret_box.py](../app/services/secret_box.py), `encrypt_secret`; [redact.py](../app/services/redact.py), `user_safe_error`; [db_safe.py](../app/services/db_safe.py), `rollback_quiet` | Existing encryption, redaction and rollback utilities can be reused. A new API must still allowlist error/output fields; redaction does not make arbitrary upstream payloads safe. |
| [alembic_runner.py](../app/db/alembic_runner.py), [alembic/env.py](../alembic/env.py), [0060_service_shop_scope.py](../alembic/versions/0060_service_shop_scope.py) | Existing migration entry points support both databases and show inspected changes/bounded backfills. New entities require their own migrations and tests. |

## 2. Authentication, permissions and isolation

### 2.1 Credentials and eligible owners

HTTPS only. Authenticate exactly one `Authorization: Bearer` credential containing
a non-sensitive random selector and at least 256 bits of secret material generated
by `secrets`. Persist a SHA-256 digest of the high-entropy secret and compare with
`secrets.compare_digest`; do not store a recoverable API bearer secret. Credential
IDs/selectors are not secrets. Unknown selectors take an equivalent dummy-digest
verification path. Reject query-string credentials, duplicate auth headers,
cookies as API authentication, and ambiguous/multiple authentication mechanisms.

Management requires an authenticated panel principal and `integrations_manage`.
Eligible owners are active explicit depth-0 Owner (platform shop only), or active
depth-1/2 principals with an active, completely configured reseller profile and a
resolvable shop and own PG identity. Depth-2 also requires its active parent.
Bare `role=admin`, a customer, and bare PG staff without a shop are ineligible.
Hierarchy visibility never widens an integration beyond its one shop or permits
use of a parent's/sibling's credentials. No automatic inheritance of API grants.

Create and rotate credentials only in the protected management workflow: display
the secret once in a private `no-store` response; never persist it in flashes,
URLs, audit details or logs. Default expiry is 90 days, maximum 365 days; no
non-expiring credential in v1. Rotation permits an explicitly bounded overlap
(maximum 24 hours); immediate revocation is always available. Default scopes are
empty. Revoke credentials on compromise and retain their non-secret audit IDs.

### 2.2 Authorization formula

Every request resolves credential -> integration -> current owner and fixed shop
from server records. Evaluate:

`active credential ∧ enabled API/integration ∧ eligible active owner/parent
 ∧ credential scope ∧ integration grant ∧ owner feature permission
 ∧ resource integration/shop/customer scope ∧ applicable PG authorization
 ∧ local safety/provision/quota/billing policy`.

Use `authz_from_staff`, `can_shop` and principal authorization helpers with a
server-resolved principal context, not a fabricated admin dictionary. The API
authentication dependency is new; do not call the cookie-only `require_staff` to
authenticate backend requests. Management continues using `require_*` dependencies.
Owner shop-feature bypass does not bypass PG permission, resource scope, or API
grants. Load local status/ACL each request and before dispatch; no positive
permission cache that outlives revocation. PG capability snapshots may be used
for outage reads only if previously verified for that same principal/identity,
at most 5 minutes old; missing/expired capability evidence returns `503`. Live PG
rejection always wins. Dispatch requires live PG authorization and limits.

New `FEATURE_PERMS` declarations (added in Phase 1A, absent from defaults):

| API scope / panel capability | New feature key | Existing authority also required |
|---|---|---|
| Panel integration/credential/allowlist management | `integrations_manage` | Exact own shop and eligible principal; cannot grant permissions the owner lacks |
| `plans:read` | `integration_plans_read` | `plans`; allowed shop catalog; live PG read for template/group resolution |
| `customers:read` | `integration_customers_read` | Exact integration/customer mapping |
| `customers:write` | `integration_customers_write` | Approved website entry policy; exact integration |
| `services:read` | `integration_services_read` | PG `users.read`/mapped read-simple capability and principal local safety |
| `services:provision` | `integration_services_provision` | `plans`, PG `users.create` and `users.read`, all creation gates |
| `services:renew` | `integration_services_renew` | `plans`, PG `users.update` and `users.read`, all renewal gates |
| `services:addons` | `integration_services_addons` | `plans`, PG `users.update` and `users.read`, modification/renewal gates |
| `services:access` | `integration_service_access` | `integration_services_read`, PG `users.read`, ready exact service; credential must also carry `services:read` |
| `operations:read` | `integration_operations_read` | Exact integration/customer/operation |
| `telegram:link` | `integration_telegram_link` | Own mapping, both identity proofs; no role acquisition |
| Panel reconciliation | `integration_operations_reconcile` | `integrations_manage`, original operation scope; current PG authority for any action |
| Panel webhook destination/redelivery management | `integration_webhooks_manage` | `integrations_manage`, exact integration; no implicit provisioning grant |

`capabilities:read` needs valid authentication/owner scope only and reports the
intersection, never underlying ACLs, credentials, balances or another shop.
All new keys are absent from `DEFAULT_FEATURE_PERMS` and deny by default.
Suggested fixed Persian labels include «مدیریت یکپارچه‌سازی‌ها» and
«دریافت خصوصی اطلاعات اتصال»; actual management UI remains Persian/RTL.

### 2.3 Object scoping and middleware

Read and mutation queries join the integration and mapped customer and constrain
the stored selling shop and principal. Reject unresolved shop ownership. All
service/operation item routes require `customer_id` as an additional selector;
a mismatch is `404`, including customer 2 within integration A. Lists require a
customer filter. Mutations recheck these constraints in the atomic claim and
again before dispatch. A body cannot supply owner, shop, panel endpoint, panel
credentials, username, raw quota payload or another integration. Existing legacy
services are not implicitly assigned to an integration after Telegram linking.

The backend credential can act for every customer in its integration. Website
session authorization is therefore the website's responsibility; the required
customer selector catches mismatches, not a malicious backend impersonating its
own customer. Separate integrations are the server-to-server trust boundary.

Keep panel session/CSRF checks unchanged. For `/api/v1/`, reject any non-empty
`Cookie` header (including session/setup cookies) before auth, and require bearer
auth on every route. Backend clients send no cookies or Origin. No permissive
CORS, browser login, redirects to login, or cookie fallback. Cookie-free requests
already avoid the existing cookie CSRF path; do not exempt all `/api/` paths from
CSRF. Middleware may reject a cookie-bearing unsafe request before the router;
it must never reach domain execution. Panel integration forms, login, setup and
Mini App retain their current distinct authentication and CSRF behavior. Future
middleware edits belong in focused modules with only registration in `app.py`.

## 3. Wire format, schemas and endpoint methods

Base path `/api/v1`. Requests and JSON responses are UTF-8 `application/json`;
unknown input fields are rejected. All response fields are allowlisted. Errors
use stable English codes and fixed safe Persian messages; a website maps codes
to its UI. JSON date/time strings are timezone-aware RFC 3339 UTC. Request IDs
are server-generated opaque IDs; never echo unvalidated text into logs/headers.
Money, if returned by a later financial contract, must be integer currency units;
v1 catalog/operation responses contain no retail price or payment fields.

Resource public IDs are typed opaque strings (`cus_`, `svc_`, `op_`, `pln_`,
`pack_`, `lnk_` plus a UUID); no sequential internal IDs. Catalog IDs are stable
server mappings to existing plans/packs. Scope remains mandatory with opaque IDs.

| Schema | Fields (required unless marked optional) |
|---|---|
| `CustomerInput` | `external_customer_ref: string`; optional `display_name: string` (default empty). No email, phone or Telegram assertion. |
| `Customer` | `id`, `external_customer_ref`, `display_name`, `state: active|blocked`, `telegram_linked: boolean`, `created_at`. Does not expose internal customer/principal/Telegram IDs. |
| `TechnicalTerms` | `data_limit_bytes: integer|null`, `duration_seconds: integer|null`, `activation: immediate|on_first_use`, `device_limit: integer|null`, `terms_revision: string`. Null means unlimited only in this validated catalog schema. |
| `Plan` | `id`, `name`, `technical_terms: TechnicalTerms`, `can_provision`, `can_renew`. No website price; only explicitly allowed eligible plans are listed. |
| `Freshness` | `source: local|panel`, `synced_at: timestamp|null`, `freshness: fresh|stale|unknown`, `panel_available: boolean|null`. Panel availability is the latest observation, not a guarantee. |
| `Quota` | `limit_bytes`, `used_bytes`, `remaining_bytes`, `expires_at`, `on_hold_seconds` (each nullable), `volume_unlimited: boolean|null`, `time_unlimited: boolean|null`. Null numeric values are unknown unless an explicit unlimited flag is true. |
| `Service` | `id`, `customer_id`, `plan_id: string|null`, `state: provisioning|ready|review|cancelled`, `panel_status: active|on_hold|disabled|limited|expired|unknown`, `quota: Quota`, `freshness: Freshness`, `resource_version: integer`, `created_at`. No subscription URL/token, QR, raw panel ID/username, connection config or panel response. |
| `ExecutionInput` | `customer_id`, `external_operation_ref`, and `plan_id` + `terms_revision` for provisioning/renewal, or `pack_id` + `terms_revision` for add-ons. Exactly one service per provisioning request. |
| `Operation` | `id`, `customer_id`, `external_operation_ref`, `kind: provision|renew|addon`, `state` from section 6, `service_id: string|null`, `resource_version`, `created_at`, `updated_at`, `next_poll_after_seconds: integer`, `failure: {code, message}|null`. No target payload, delivery credential or internal reconciliation evidence. |
| `RenewalPreview` | `service_id`, `customer_id`, `plan_id`, `terms_revision`, `calculated_at`, `current: Quota`, `projected: Quota`, `warnings: string[]` (machine codes). Informational, neither a reservation nor the eventual absolute target. |
| `Addon` | `id`, `name`, `kind: volume|duration`, `amount_bytes: integer|null`, `amount_seconds: integer|null`, `terms_revision`, `warnings: string[]` (machine codes). Exactly one amount is positive. No retail price. |
| `Access` | `service_id`, `customer_id`, `subscription_url`, `validated_at`, `access_version: integer`. Only private access endpoint returns this bearer URL. |
| `Page<T>` | `items: T[]`, `next_cursor: string|null`, `has_more: boolean`. No unbounded totals. |
| `Error` | `error: {code: string, message: string, request_id: string, retryable: boolean, details: object|null}`. The only allowed details are optional `field: string` and scoped `operation_id: string`; never echo input values. |

### 3.1 Initial endpoints

Every row needs the scope(s) shown and the corresponding feature/authority checks
above. `customer_id` query parameters are required where listed. Successful
JSON responses are direct schema objects, except lists use `Page<T>`.

| Method and path | Request/query | Success | Scope |
|---|---|---|---|
| `GET /capabilities` | None | `200 {api_version:"v1", scopes:string[], operations:string[], customer_entry_policy:"website", billing_mode:"fixed"|"payg"|"platform", limits:{page_max, read_per_minute, writes_per_minute, access_per_minute}, authorization_synced_at:timestamp|null}` | `capabilities:read` |
| `GET /plans` | `cursor?`, `limit?` | `200 Page<Plan>` | `plans:read` |
| `POST /customers` | `CustomerInput`, required `Idempotency-Key` | `201 Customer` created; `200 Customer` already mapped | `customers:write` |
| `GET /customers/{customer_id}` | None | `200 Customer` | `customers:read` |
| `GET /services` | `customer_id`, `cursor?`, `limit?` | `200 Page<Service>` | `services:read` |
| `GET /services/{service_id}` | `customer_id` | `200 Service` | `services:read` |
| `POST /provisioning-requests` | `ExecutionInput` with plan, required `Idempotency-Key` | `202 Operation`, `Location` operation URL including customer selector | `services:provision`, `operations:read` |
| `GET /operations/{operation_id}` | `customer_id` | `200 Operation` | `operations:read` |
| `GET /services/{service_id}/access` | `customer_id` | `200 Access` | `services:access`, `services:read` |
| `GET /services/{service_id}/qr` | `customer_id` | `200 image/png`; `X-Access-Version` of the validated URL | `services:access`, `services:read` |
| `GET /services/{service_id}/renewal-preview` | `customer_id`, `plan_id` | `200 RenewalPreview` | `services:read`, `services:renew`, `plans:read` |
| `POST /services/{service_id}/renewals` | `ExecutionInput` with plan, required `Idempotency-Key` | `202 Operation` | `services:renew`, `operations:read` |
| `GET /services/{service_id}/addons` | `customer_id`, `cursor?`, `limit?` | `200 Page<Addon>` | `services:read`, `services:addons` |
| `POST /services/{service_id}/addons` | `ExecutionInput` with pack, required `Idempotency-Key` | `202 Operation` | `services:addons`, `operations:read` |

All paths in the table append to `/api/v1`. Execution replays return `202` and
the same operation ID with its current state, even when terminal; GET returns
`200`. After archival, both return `410 operation_archived` with the original
scoped `operation_id` in error details. They never authorize another panel call.
Include `Retry-After` on accepted
operations (default 5 seconds) and rate-limit responses. Provision/renew grants
do not include private access. A customer POST with an existing reference never
updates its display name or merges identity; a different name is
`customer_reference_conflict`, requiring an explicit future update contract.

Optional Phase 2C (excluded from 2A/2B) needs narrowly scoped link workflow routes:

| Method/path | Contract |
|---|---|
| `POST /customers/{id}/telegram-link-challenges` | `telegram:link`, `Idempotency-Key`, empty object; `201 {challenge_id, start_token, expires_at}`. Return the secret once; a replay returns `200 {challenge_id, expires_at}` without the token. A lost token requires a new challenge/key. |
| `GET /customers/{id}/telegram-link-challenges/{challenge_id}` | `telegram:link`; `200 {challenge_id, state: awaiting_telegram|awaiting_website|linked|expired|cancelled, expires_at}`; never candidate identity. |
| `POST /customers/{id}/telegram-link-challenges/{challenge_id}/confirm` | `telegram:link`, `Idempotency-Key`, `{confirm:true}` after website reauthentication; `200 {linked:true}` only after both proofs. |
| `DELETE /customers/{id}/telegram-link` | `telegram:link`, `Idempotency-Key`; `204`, repeated unlink also `204`. Requires website reauthentication; bot-side revocation is a separate authenticated bot workflow. |

These routes follow the [identity proof contract](INTEGRATION_CUSTOMER_IDENTITY.md#4-linking-conflicts-and-unlinking).
Integration/credential/destination management and reconciliation remain protected
panel workflows, not execution API scope. Phase 7 adds
`GET /api/v1/operations?customer_id=...&cursor=...&limit=...` under
`operations:read`, returning `Page<Operation>` using the same bounds. It is not
an operation retry endpoint.

### 3.2 Bounds and pagination

- External customer/operation references: 1–128 ASCII characters from
  `[A-Za-z0-9._:-]`, case-sensitive, no trimming/case folding or Unicode aliasing.
  Examples: `customer-104`, `provision-204`. Reference namespace is the integration.
- Idempotency key: 16–128 characters from the same alphabet, generated with high
  entropy by the client. Reject bools, duplicate headers and whitespace tricks.
- Display name: at most 128 Unicode characters and 512 UTF-8 bytes; no control
  characters. It is untrusted text; escape at Telegram/browser insertion and use
  Jinja autoescape. Do not accept HTML or template placeholders as executable data.
- Body: maximum 16 KiB, enforced while streaming as well as by Content-Length;
  no compressed request bodies, file uploads or nested arbitrary metadata.
- JSON integers use strict types: no bool, string coercion, float, NaN or Infinity.
  Public byte fields are bounded to `0..2^53-1`, duration seconds to
  `1..315360000` (10 years) or null for catalog unlimited, devices to `1..1000`
  or null. Effective bounds are the smaller of these, column ranges and PG/local
  policy. Zero panel limits are normalized to explicit unlimited fields; clients
  cannot submit raw quotas. Legacy plans outside supported bounds are ineligible.
- `limit` defaults to 25 and accepts strict integers 1–100. Keyset order is stable
  internal ID descending; never OFFSET. Opaque signed cursors bind integration,
  shop, customer, filters, resource kind, last key and initial high-water mark,
  expire after 15 minutes, and are at most 1024 bytes. Reusing a cursor under another
  customer/integration/filter returns `invalid_cursor`. New inserts require a new
  traversal; deletes can shorten pages. It is not a transaction-wide snapshot.
- Query strings at most 2048 bytes. Reject unknown filters. Item reads return
  only fields declared above; no raw JSON passthrough or URL-bearing Mini App reuse.

### 3.3 Errors

Example safe validation error (synthetic):

```json
{
  "error": {
    "code": "invalid_request",
    "message": "درخواست معتبر نیست.",
    "request_id": "req_00000000-0000-4000-8000-000000000001",
    "retryable": false,
    "details": {"field": "customer_id"}
  }
}
```

| HTTP status | Stable codes and meaning |
|---|---|
| `400` | `invalid_request`, `invalid_cursor`, `idempotency_key_required`, `unsupported_authentication` (including cookies) |
| `401` | `invalid_credential` for missing, expired, revoked or invalid bearer; `WWW-Authenticate: Bearer`; do not reveal selector existence |
| `403` | `integration_disabled`, `principal_inactive`, `permission_denied`, `customer_blocked`, `website_entry_not_enabled` |
| `404` | `not_found`: missing resource and wrong integration/shop/customer are indistinguishable |
| `409` | `idempotency_conflict`, `external_reference_conflict`, `customer_reference_conflict`, `service_busy`, `terms_changed`, `link_conflict`, `access_not_ready` |
| `410` | `challenge_expired`, `operation_archived` (minimal durable operation/reference still exists) |
| `413` / `415` | `payload_too_large` / `unsupported_media_type` |
| `422` | `plan_ineligible`, `addon_ineligible`, `quota_exceeded`, `billing_blocked`, `unsupported_terms`, `link_not_confirmed` |
| `429` | `rate_limited` or `queue_limit_reached`, always with bounded `Retry-After` |
| `503` | `api_disabled`, `panel_unavailable`, `authorization_unavailable`, `temporarily_unavailable` before acceptance |
| `500` | `internal_error`; do not disclose SQL, panel exceptions, secrets or customer text |

After acceptance, execution failure is an operation state/error, not a later
HTTP failure on polling. Polling unknown/review operations still returns `200`.
`retryable` permits repeating the same request/key/reference; it never instructs
the caller to mint a new reference to bypass review. Log request/operation IDs
and safe classifications using redaction; never raw headers, bodies, references,
subscription paths, contact details or upstream exceptions that may hold secrets.

## 4. Eligibility, snapshots, limits and freshness

### 4.1 Plans and obligations

The integration manager explicitly allowlists shop-owned active non-trial sales
plans and eligible shop add-on packs. Empty allowlist denies all. API v1 excludes
trials, custom-priced/wholesale/reseller-plan purchases, linked/imported service
adoption, and arbitrary quota/group/template/owner selection. A reseller plan
continues to determine existing reseller capacity/billing; it is not an API sale.

Resolve plan/template technical terms through the same principal client and
authorization. Expose a plan only if its groups, data/time/device limits and
activation semantics can be validated and materialized into an explicit target.
Template-only behavior that cannot be snapshotted/reproduced safely is
`unsupported_terms`, not an excuse to use a mutable template on retry. Version 1
does not promise every legacy catalog plan is API-eligible.

`terms_revision` is a canonical digest of relevant plan/pack and resolved template
terms, groups, activation mode and naming policy. Catalog price is not a website
retail quote. At acceptance, require the current revision and persist all technical
terms, selected catalog IDs, resolved PG owner, and applicable billing mode/rate
policy evidence. Before first dispatch, revalidate plan/pack active/allowlisted
status, the revision, owner authority, PG limits, capacity and billing. A changed
revision/allowlist rejects an unstarted operation with `terms_changed` or
`plan_ineligible`; never silently substitute new terms. Once write intent is
committed, reconcile the saved target despite later catalog changes.

Use `assert_provision_create`, `assert_provision_renew`/`assert_provision_modify`,
PG role limits and shop allocation holds in customer-neutral entry points. Fixed
mode still obeys subscribed capacity and existing local/PG limits. PAYG obeys the
configured billing-enabled and empty-balance policy; a website payment is not
reseller credit. Do not invent a universal upfront debit when existing fixed or
PAYG policy does not have one. Existing reseller usage billing continues exactly
once through its established ledger/watermark; API services must be included in
its scoped accounting. No billing mode override or customer wallet debit/credit.

### 4.2 Durable capacity and service exclusion

Use the existing allocation gate, but extend its shared accounting to include
durable unresolved API reservations before execution is enabled. A reservation
records operation, shop, byte delta and (for create) user-slot commitment; count
it in all relevant PGClockBot gates across API, bot, jobs and panel. Unknown
commitments do not expire with a worker lease or `VolumeHold` TTL. At confirmed
success, atomically substitute the service allocation and release its reservation;
at a definitive non-write failure, release it once. Avoid double-counting a panel
user already included in external-allocation sync by matching persisted ownership
and operation target; uncertain overlap fails closed and is reviewed.

This is a required extension, not behavior already supplied by `hold_volume`.
Do not route API unknown writes through `settle_failed_hold` and assume TTL expiry
resolves uncertainty. Lock shop allocation while making commitments; never keep
that database transaction open across a panel read/write. Existing live PG counts
remain authoritative limits; local reservations also protect concurrent in-flight
creates before those counts reflect them.

One persistent service-mutation guard, unique by internal service ID, covers
renewals, add-ons, cancellation and other PGClockBot mutations of that service.
Legacy order claims, automation and supported panel mutation paths must consult
the same guard. Do not rely on an API index independent of the existing
order-only partial index. An unresolved operation retains exclusion across lease
expiry/restart. A new operation for that service returns `service_busy`; the
caller retries the same reference after resolution. Do not queue an incompatible
target calculated from a pre-mutation snapshot.

### 4.3 Rate limits and retention

Initial per-integration limits, shared across credentials and workers:

| Budget | Default |
|---|---|
| All reads, including polling/catalog | 120 requests per UTC minute |
| All writes, including customer/link operations and replays | 20 requests per UTC minute |
| Private URL/QR reads (also consume read budget) | 10 requests per UTC minute |
| New queued/running operations | At most 100 per integration; unknown/review commitments have a separate non-expiring safety cap of 100 |
| Per-customer link challenges | At most 3 outstanding, 5 new challenges per hour |
| Worker concurrency | At most 2 panel mutations per integration and 8 process-wide; service guard further serializes each service |

Implement SQL-backed atomic fixed-window counters keyed by integration, budget
and UTC window; all workers share them. A fixed window permits boundary bursts
and is not a sliding guarantee. Before credential lookup, apply a bounded IP
budget (60 auth failures/minute/IP, deployment-wide counters) with trusted-proxy
rules; never create unlimited selector-keyed buckets. Counter/storage failure
returns `503` for acceptance/private access. Limit overrides require owner
management, are bounded and audited. Retain inactive rate buckets 48 hours.

| Data | Proposed default policy |
|---|---|
| Idempotency responses/digests | 30 days from acceptance; authenticate before every replay; link-start replay storage excludes its one-time secret |
| Durable external-operation ledger | Never expire/reuse while integration identity exists; retain after retirement as a minimal tombstone, including operation ID, reference digest, input digest and terminal/review state |
| Complete terminal operation payload/history | 180 days after terminal state, then compact to tombstone plus required service/financial associations; archived GET returns `410` with no new execution |
| Queued/running/unknown/review records, guards/reservations | Never age-delete; 24-hour queue expiry may fail only a provably unstarted operation |
| Customer mappings/services | While active services/evidence depend on them; minimize display names after retirement, retain reference tombstones to prevent accidental reuse |
| Events and delivery attempts | 90 days after terminal delivery/disposition; unresolved events remain durable until acknowledged or explicitly archived by an authorized operator |
| Auth/security audit metadata | 180 days; never bearer/challenge secrets, delivery URLs or full request bodies |
| Link challenges | Expire at 10 minutes; remove secret digest/candidate proof within 24 hours after completion/expiry; keep minimal consent audit 180 days |

Reference digests use SHA-256 over the restricted canonical reference; preserve
them with input hashes when compacting. There is no reference reset or public
hard-delete endpoint in v1. Retiring an integration must not delete active
services or unresolved evidence. These are technical defaults, not legal retention
advice; deployment policy may retain evidence longer. Changing retention must
never erase the durable duplicate barrier or unresolved commitments.

### 4.4 Read freshness and private delivery

Lists use bounded local snapshots and make **zero panel calls per row**. Existing
quota cache does not contain every API field (notably durable usage/fetch errors);
later phases must add the necessary bounded snapshot storage. Never label plan
estimates as observed usage. `synced_at` records the last successful validated
observation. Fresh means at most 60 seconds old; older is stale; no observation
is unknown. `panel_available=null` means not checked. Do not update `synced_at`
on failure, replace missing quota with zero, or equate missing expiry to unlimited.

Detail may attempt one bounded refresh using the same principal client's short
TTL read cache, a 3-second overall read budget and at most four concurrent refreshes
per integration. On panel timeout, return the local service with stale/unknown
freshness and the latest availability observation. If local authorization evidence
is unavailable/expired, deny with `503`; outage fallback never invents authority.
Previews require validated current panel state and return `503` on outage. Cache
identity includes principal/client identity; never cache API authorization or
share resource serializers across integration/customer boundaries. Mutations
invalidate the relevant identity cache after verified success; reconciling reads
bypass caches.

Private access/QR requires `ready`, no cancellation/review, and a validated
subscription URL associated with the exact panel identity, service and current
access version. It is the only API response allowed to contain delivery secrets.
Return `Cache-Control: private, no-store`, `Pragma: no-cache`,
`Referrer-Policy: no-referrer`; disable response-body logging/tracing for these
routes. No subscription URL, token, QR data or config in summaries, operation
results, notifications, webhooks or logs. Link challenges are separate one-time
identity proofs, also secret and `no-store`.

Validate subscription URL shape/origin against configured single-panel public
subscription settings and the stored service, never a caller-provided URL. A
known valid unchanged URL may be served from encrypted at-rest API delivery
storage during an outage for at most 5 minutes since validation. After that,
refresh or return `503`; never regenerate/create a user to recover delivery.
Missing/malformed URL leaves operation/service under review, not successful ready
delivery. Later repair can validate the same target and complete it. Encrypt new
API delivery storage using `secret_box`; do not duplicate API URL/token secrets
into legacy plaintext `UserService.subscription_url`/`subscription_token` fields.
Existing legacy plaintext fields are not a claim that encryption is already
universal. Use the panel ID for API service refresh/accounting.

Generate QR locally from **exactly** the validated URL bytes used by `/access`;
same `access_version`, no third-party QR service, offload blocking work with
`asyncio.to_thread`. Until explicit rotation is supported, these endpoints must
agree; unexpected panel-side rotation makes the service review-only. No automatic
Telegram delivery even when a customer has linked Telegram.

## 5. Submission and durable duplicate prevention

Execution requests require both `Idempotency-Key` and immutable
`external_operation_ref`. The latter identifies one website business intent,
not an HTTP attempt or credential. The website must persist it before submission.

Use two independent constraints:

1. Short-lived unique `(integration_id, idempotency_key_digest)` binds method,
   normalized route/service, mapped customer and canonical validated body digest.
   Reusing a key for another method, customer, kind, service or input is `409`.
2. Durable unique `(integration_id, external_operation_ref_digest)` across **all**
   operation kinds binds that same semantic input and original operation ID.
   Neither credential ID nor the short-lived key participates in this uniqueness.

Canonical input excludes transport/request ID and idempotency key, includes schema
version, operation kind, target service, customer, catalog ID and terms revision.
Use deterministic typed serialization and a SHA-256 digest. Input defaults are
normalized before hashing; reject unknown fields rather than drop them. Duplicate
lookups after authentication precede mutable plan/availability checks: a legitimate
replay returns its original outcome even if the plan is now disabled. Current
read authorization and scope are still required. First-time submissions revalidate
all acceptance rules. Missing current write authority does not grant an API write
replay route; an authorized `operations:read` credential can poll instead.

In one short transaction insert or conflict-resolve customer-scoped operation,
durable reference and idempotency association; check constraints under concurrency
and rollback on database errors before re-querying. Same input/key returns the
original operation; same external reference/new key/rotated credential also returns
it. Different input with either binding returns `409`, with no panel effect.
Expiration of the short-lived record cannot bypass the durable ledger. Archived
operations return `410` and their original scoped operation ID, never a fresh
operation. A new idempotency key bound to a replay still cannot later identify
different input.

Customer creation additionally has permanent mapping uniqueness. Link workflows
have their own atomic challenge/consent uniqueness. No API endpoint interprets
a new key/reference as permission to retry a known unknown operation.

## 6. Operation execution and recovery

### 6.1 States and transitions

| State | Meaning | Allowed next states |
|---|---|---|
| `queued` | Accepted, no panel write attempt started | `running`, `failed` (authorization/eligibility/queue expiry before dispatch) |
| `running` | Leased preparation, dispatch or local finalization | `queued` only with durable proof dispatch never began; `succeeded`; `failed` only with definitive no-effect evidence; `outcome_unknown`; `needs_review` for a known partial/mismatched result |
| `outcome_unknown` | A write may have happened or old worker may still be in flight | `succeeded` after read-only verification of the saved target and local finalization; otherwise retained for review |
| `needs_review` | Ambiguous identity/terms, missing delivery, partial result, or policy intervention required | `succeeded` after read-only verification of the saved target and local finalization; otherwise retained; no force-fail/reapply control in v1 |
| `succeeded` | Verified target, durable service/local effect and result committed; provision delivery validated | Terminal |
| `failed` | Definitively rejected/unstarted/confirmed no effect, reservations safely released | Terminal |

There is no automatic transition from unknown/review to the beginning of a new
operation. No public retry, cancel, delete or refund endpoint in v1. A transient
read/preparation error can back off in `queued` only when dispatch never began;
fail unstarted requests after 24 hours. Unknown outcomes have no automatic expiry.
All transitions use conditional status/version/token updates and append audit
evidence; never read/decide/write status without an atomic claim.

### 6.2 Claim, target and call ordering

1. Claim `queued` with a random lease token, version and 60-second deadline.
   The current worker bounds preparation, write and read-back below that deadline
   and does not schedule periodic heartbeats. In a short transaction obtain the persistent
   service guard if applicable and verify integration/customer/shop ownership.
   Commit the claim. A different worker cannot replace its target.
2. Perform bounded live reads and authorization/limits outside open DB transactions.
   Recheck local status/authorization version after reads. Prepare exact technical
   payload and target identity. Under the allocation lock, record durable capacity
   reservations, baseline, resolved terms and stable panel target; commit.
3. Provision target includes a unique unpredictable saved panel username generated
   with the allowed naming policy, expected PG owner, groups, quotas,
   activation and an operation marker. Persist it before create; a username match
   alone is insufficient. Renewal/add-on target includes the unchanged panel-user
   ID, owner and baseline identity, absolute data limit, expiry or on-hold duration,
   activation status, and fields that must be preserved. Never save only "+GB" or
   "+days". Commit a `dispatch_intent` marker with token/version before the call.
4. Recheck the lease and dispatch permit; use the existing single-panel client for
   the saved principal, explicit 20-second mutation timeout and no generic retry
   wrapper. **No DB transaction is held across any network call.** Once intent
   is committed, a crash is potentially unknown even if no bytes were actually
   sent. The conservative gap is intentional.
5. Validate returned/read-back identity, owner and target terms. Seal local service,
   encrypted delivery data, operation state/version, reservation substitution and
   later outbox event in one transaction using the lease token/version. Notification
   happens after commit. A lost local commit after panel success is reconciled,
   never compensated by blind panel deletion or customer refund.

Renewal uses the existing validated remaining-quota/time arithmetic. Compute the
absolute target only after acquiring the service guard and live snapshot; preview
does not freeze a target. Preserve panel-user identity and do not reset traffic.
Add-ons reuse category/general-pack selection and unlimited-dimension rejection.
For `on_hold`, renewal retains first-use semantics when finite; existing add-on
behavior activates the service and preserves pending duration/volume. Snapshot
that behavior explicitly and expose `activates_on_hold` in previews/pack
eligibility warnings. No change to unsupported native panel semantics is implied.
Do not recompute a relative target on retry. If first-use activation happens
meanwhile, compare the saved duration with validated activation/expiry evidence;
insufficient evidence requires review rather than restoring `on_hold`.

### 6.3 Definitive failure, unknown outcome and stale workers

Validation/auth rejection before dispatch is definitive. A validated panel
rejection documented to have no write effect may be definitive. An HTTP status
alone, proxy response, malformed success, transport error, cancellation, worker
crash, database failure after dispatch or timeout cannot establish non-application.
Unexpected 5xx and response loss become `outcome_unknown`. A partial create that
has no valid delivery URL is review-only and still holds its commitments.

Lease expiry only permits recovery inspection. If no dispatch intent exists and
the previous lease is fenced before intent, an atomic requeue is safe. Otherwise
move to unknown and do not send another write. A stale worker must fail every
local heartbeat/transition after token loss. This fences the database, not an
already-sent PasarGuard request: do not claim an external exactly-once guarantee.
The service guard and reservations stay held until that attempt is resolved.

Reconciliation uses uncached reads of the **saved** target on the same panel with
the original principal scope. Creation requires username/operation marker,
owner, groups and technical terms to agree, then binds the returned panel ID.
Renewal/add-on verifies unchanged panel identity and affected target fields;
ordinary traffic usage and elapsed time are interpreted using the persisted
baseline/activation evidence, not as permission to add quota again. If target
matches, finish locally without writing again. Mismatch, an unreadable owner,
unexpected URL rotation or unprovable on-hold transition goes to `needs_review`.

A temporary `404` is not proof that an old create cannot still complete. V1
provides no reapplication, force-failure or ambiguous-capacity-release action.
An unmatched target stays unresolved; any future reapplication workflow would
require separate authorization and proof that prior requests cannot apply later.
Current reconciliation only reads and seals a fully verified saved effect. It
never changes username, substitutes a plan, uses an owner fallback or issues
relative increments. No automatic deletion/refund/release compensates unknown
writes. Operator actions record the original operation and its evidence.

Direct native-panel edits are outside the local guard. If they conflict with a
saved target, stop for review; v1 cannot guarantee compare-and-swap or perfect
attribution without panel support. This conservative manual resolution is an
explicit limitation, not an unresolved instruction to blindly retry.

### 6.4 Revocation and permission changes

| Change | Accepted operation behavior |
|---|---|
| Submission credential expires/revokes/rotates | Reject new requests/replays through it. The operation belongs to the integration and may continue if integration, owner's current authority and grants remain valid. Poll with a replacement scoped credential or panel. No dependency on the old secret. |
| Integration/global API disabled or grants removed before dispatch | Stop acceptance; definitively fail provably unstarted work (`integration_disabled`/`permission_denied`), release only safe reservations. Do not resume it as a new intent on re-enable. |
| Owner/parent inactive, PG authority or plan eligibility removed before dispatch | Same unstarted failure rule; if live evidence cannot be obtained, defer without writing up to queue deadline. |
| Disable/revoke/permission change races with committed dispatch | Record request boundary; cannot retract an external request. Mark unknown/review as needed, preserve guard/commitments, permit verification/finalization of the existing effect, prohibit new writes without current authority. No refund/deletion. |
| Already succeeded | Preserve service/result/evidence; new access/poll requests require current credentials/authorization. Revocation does not delete a customer's active service. |

Internal recovery may persist evidence and finalize a verified existing effect
without resurrecting disabled public API access. If the original principal no
longer has read authority, defer to an audited currently authorized operator;
never silently borrow owner or parent credentials in the worker. Global execution
pause keeps recovery records/commitments intact. See the identity decision for
application rollback versus physical schema downgrade.

## 7. Webhooks

The implementation uses a transactional outbox. Persist each event in the same transaction
as the resource state/version change; sender reads only committed rows in bounded
batches. Events include `event_id`, `type` (`operation.updated`, `service.updated`,
`customer.telegram_link_changed`), `occurred_at`, `resource_id`, `customer_id`,
`resource_version`, operation state and safe failure code where applicable. They
never contain delivery credentials, panel usernames/IDs, raw references, phone,
Telegram ID, customer display text or upstream payloads. Per-resource versions
increase atomically; there is no global ordering guarantee.

Delivery is at least once. Event ID and body remain stable on retry; attempt ID,
timestamp and signature may change. Consumers store event IDs, ignore older
versions, and poll current resource/operation state to recover gaps or conflicting
versions. `2xx` acknowledges; redirects are failures, never followed. Receivers
should durably store before acknowledging. Polling remains available regardless
of webhook destination health; it is not evidence that a delivery was acknowledged.

Sign exact bytes using HMAC-SHA256 over `timestamp + "." + event_id + "." + body`.
Headers carry event ID, timestamp, key ID and signature version. Each integration
has a distinct webhook signing secret, generated with `secrets`, encrypted with
`secret_box` because sending requires recovery, and delivered once through the
protected management workflow. API bearer credentials are never signing keys.
Receivers use constant-time comparison and a 5-minute timestamp window plus
durable event deduplication. Signing rotation uses explicit key IDs and at most
24 hours overlap; delayed retries receive a fresh timestamp/signature with the
same stable event ID/body. Never log signatures or secrets.

Attempt delivery immediately, then at offsets of 1 minute, 5 minutes, 30 minutes,
2 hours, 6 hours and 24 hours from event creation, then on days 2 through 7
(at most 13 automatic attempts total); add bounded jitter without changing event
identity. Honor valid
`Retry-After` up to 24 hours for `429`/`503`. Record safe status/latency/failure
class and attempt time, not response bodies. Use a 5-second total timeout,
16 KiB response-read cap and at most two concurrent deliveries per integration.
After exhaustion retain an unresolved failed event and expose it in scoped panel
status with explicit authorized redelivery of the original event. Retention does
not erase unacknowledged events automatically. Destination changes do not send
historic events to a new host silently; require explicit scoped redelivery.

One enabled destination per integration initially, managed in the panel with
`integration_webhooks_manage`, CSRF and an audit trail. Require HTTPS, a valid
hostname, port 443, at most 2048 bytes, no userinfo, fragments or query secrets.
No arbitrary URLs on execution requests. Resolve and validate **all** A/AAAA
addresses at registration and every connection; reject loopback/private/link-local,
multicast, unspecified, reserved, IPv4-mapped internal IPv6, metadata addresses
and internal hostnames. Mixed public/private DNS results are rejected. Pin the
validated address for that connection while preserving hostname TLS verification
and SNI; the HTTP client must not resolve a different destination after validation.
Revalidate on retries, disallow redirects and environment proxies, bound DNS/time
and response size, and use an egress policy blocking internal networks as defense
in depth. DNS rebinding or an unverifiable address fails closed. These protections
are new webhook requirements, not a claim that a panel URL validator alone suffices.

Disabled integrations retain events; outbound sends pause until authorized
re-enable/retirement disposition. Unresolved operations/events are visible to
authorized operators even if the submission credential has been revoked.

## 8. Validation plan and implementation boundary

Future tests must assert behavior with fake clients and synthetic identities;
no network/real credentials. Run SQLite and PostgreSQL migration/constraint tests,
focused service/API tests, Node tests only for nontrivial management browser logic,
security manifest tests and lint. Before an implementation PR, compare full-suite
counts with a clean then-current `upstream/dev`; do not weaken known failing tests.

| Required scenario | Expected proof |
|---|---|
| Non-Telegram registration -> provision -> private delivery -> renewal | Same neutral customer and panel service survive; no Telegram message, payment approval, customer wallet or role mutation |
| Customer 2, integration B in same shop, and another shop | Each read, list, preview, mutation, replay, operation poll, URL/QR and link action rejects mismatches; platform null is tested separately |
| Permissions, no principal/parent, revoked/expired credentials | Fail closed, including Owner PG limits and no scope default; changing owner ACL after acceptance prevents unstarted writes |
| Session and CSRF | Cookie-only/mixed auth cannot execute API calls; panel cross-site/missing-token POSTs remain rejected; Mini App keeps signed initData |
| Duplicate/concurrent submission | Same key/ref creates one operation; changed kind/customer/service/body conflicts; after 30-day pruning/rotation a new key still cannot repeat the external intent |
| Crash before claim, after target, after intent, after panel write, before local seal | Claims/targets survive; only a provably undispatched attempt requeues; successful panel write is reconciled without another write |
| Lease expiry and late worker completion | Old token cannot finalize or send a newly authorized dispatch; potentially in-flight panel call remains unknown with durable guard/capacity |
| Concurrent renewal/add-on/cancellation/automation/panel mutation | Shared guard prevents incompatible writes, including legacy order claims; absolute targets apply once |
| Unlimited, expired and on-hold services | Correct arithmetic, activation behavior, no traffic reset, no limited add-on on an unlimited dimension; activation races stop when evidence is insufficient |
| Panel create returns wrong owner/terms or no valid URL | No success/access; no blind delete, release, refund, username change or recreation; operator review preserves evidence |
| Quota/credit and hold expiry | Fixed/PAYG and current principal limits apply; unresolved commitments still count after hold TTL; no double reseller charge or customer-wallet change |
| Panel outage and cache identity | Local lists retain snapshots/timestamps; missing usage stays unknown; previews and expired private access fail safely; no sibling client/cache fallback |
| URL/QR privacy | Separate permission; matching URL bytes/version; absent from all summaries, logs, errors, events and Telegram notifications; no third-party QR call |
| Input, pagination and rate budgets | bool/NaN/overflow, hostile text, unknown fields and oversized streamed bodies handled safely; stable scoped keysets; SQL counters shared across multiple workers; bounded query/panel-call counts |
| Webhook duplicates, reorder, outage, rotation | Stable event dedup/version, signed exact payload, durable outbox, bounded retries and polling; no lost unresolved event |
| SSRF | Internal/mixed DNS, rebinding, mapped IPv6, redirect and proxy cases rejected using fake DNS/transport |
| Upgrade twice/downgrade/re-upgrade | Legacy and active web customers/services remain linked; populated downgrade retains required schema/guards/ledgers; incompatible full removal fails before mutation |

Add isolation, auth, financial and relevant failure tests to
[security_test_manifest.txt](../scripts/security_test_manifest.txt). Phase 0 runs
documentation checks only; none of these implementation tests are claimed here.

### Remaining issues and release gates

The contract chooses conservative unknown-outcome handling, independent customer
ownership, explicit channel policy and data-retaining downgrade. These core design
questions are resolved for implementation review. The following require evidence
in their implementation phases, not a product decision about a particular website:

- Confirm supported panel payload/identity/activation evidence using the current
  client and fixtures; unprovable outcomes stay review-only. Exactly-once native
  panel writes and arbitrary external panel edits cannot be guaranteed.
- Inventory and adapt affected legacy service readers, mutation entry points and
  billing/allocation accounting before enabling web services. This focused review
  is not a complete call-site or security audit.
- Validate both database migration paths, populated retained-schema downgrade,
  worker fencing, reservation overlap with panel sync, query plans and rate-limit
  behavior. Numeric/rate/retention defaults are chosen here; deployment tuning must
  preserve their stated bounds and duplicate/safety invariants.
- No public deletion/refunds, service transfer/import, bulk provisioning, link
  rotation, customer wallet automation or linked web-service bot/Mini App view in
  v1. Unsupported template terms remain unavailable instead of being guessed.

### Phase 1A implementation boundary

Phase 1A implements this boundary against refreshed `upstream/dev`
`ebd27e717b2d95de7b3b5931efedbcf246a02029`:

- [Foundation models](../app/db/integration_models.py): `Integration`,
  `IntegrationPlan`, `IntegrationCredential`. An `intg_` UUID is stable across
  rotations. A required `scope_kind=platform|shop` disambiguates platform null
  from unresolved scope. Owner, identity and shop are immutable through the
  management services and ORM updates. Creating another integration transfers
  no association. Direct operator SQL is not a supported reassignment workflow.
- [Owner resolution](../app/services/integration_owners.py) resolves authenticated
  management staff through existing principal helpers; request principal/shop
  fields do not establish authority. API requests load the stored owning principal,
  current profile/ACL, own PG identity and required active parent. No fabricated
  admin role or parent/Owner credential fallback is used.
- [Integration management services](../app/services/integrations.py) create
  disabled integrations, validate grants against current owner feature authority,
  and manage at most 100 explicit plan associations. This foundation cap bounds
  authorization reads; larger allowlists need a separately paginated design.
  Empty denies all. Plan reads
  recheck shop ownership, active/non-trial status and supported local scalar bounds.
  Live groups/template validation and technical-term materialization remain future
  catalog/execution work; membership in this allowlist does not prove either.
- [Credential services](../app/services/integration_credentials.py) use
  `pgint_<32 lowercase hex selector>.<64 lowercase hex secret>`. The secret has
  256 bits of entropy and only its SHA-256 digest is persisted. Known and unknown
  selectors perform a digest comparison. Expiry defaults to 90 days and cannot
  exceed 365 days; rotation defaults to no overlap, accepts at most 24 hours,
  and never extends the old expiry. An atomic rotation claim allows exactly one
  replacement per old credential. Management mutations serialize per integration.
  Revocation is immediate and repeat-safe. `CredentialIssuance.bearer` is the
  sole designated one-time secret result and is excluded from its representation;
  ordinary `CredentialView` and authority objects contain no secret/digest.
  Callers must never log/serialize issuance results outside a protected one-time
  delivery response. The protected issuance workflow is added in Phase 1B-1 below.
- [Authority resolver](../app/services/integration_authority.py) reloads global
  enablement (`integration_api_enabled=0` by default), credential/integration
  status, owner ACL, fixed scope and eligible allowlist. `local_scopes` is precisely
  the credential/grant/feature intersection; it is **not** proof of panel authority.
  As specified in section 2.2, `capabilities:read` is authentication-only and is
  implicitly available even with empty stored scopes. Private access additionally
  requires `services:read` in that intersection.
- Required panel scopes fail with `authorization_unavailable` without injected,
  identity-bound `IntegrationPGEvidence`. Existing principal PG authorization
  helpers enforce its mapped user/catalog actions. Read observations expire after
  5 minutes; mutation scopes require live evidence; a live rejection denies.
  No positive authority/evidence cache or production evidence fetcher is added.
  Later fetchers must observe the exact principal's single-panel identity, honor
  live rejections and use verified snapshots only during outages. The foundation
  never authorizes dispatch, quotas or billing on its own.
- [Bearer dependency](../app/api/integration_auth.py) and its dedicated safe-error
  handler are reusable but unmounted. An isolated application exercises them.
  HTTPS (including a correctly trusted TLS proxy scheme), exactly one Bearer,
  no non-empty cookies, no Origin, no alternate auth headers and no credential
  query parameters are required. Missing/expired/revoked/unknown secrets share
  `401 invalid_credential` with a Bearer challenge. Duplicate/mixed auth is `400`.
  Error envelopes contain fixed Persian messages, generated request IDs and
  `no-store` headers. Register `register_integration_auth_errors` when later routes
  are mounted so only integration errors use this contract's envelope. Any
  injected network verifier runs after closing the read transaction; local status
  and revocation are rechecked after it returns. Authority objects are request
  snapshots, never cached dispatch permits. Existing panel/CSRF/Mini App code is
  unchanged.

Migration `0071_integration_foundation` follows inspected head
`0070_shop_miniapp_key`, creates only the three foundation tables and
indexes and repairs missing indexes on repeat upgrade. Existing customer/service
columns, wallets and rows are untouched. Required identity/scope fields on new
empty tables have no invented authority defaults; status/grants/revision have
safe server defaults. No feature permissions are backfilled and the migration
never enables the API.

An inspected downgrade removes unused foundation tables only when all three are
empty. If any foundation record exists, it reports schema retention through Alembic output (even if revision logging is
disabled), preserves all
three tables, associations, digests and indexes, and supports repeated re-upgrade.
Both downgrade paths turn an existing global switch off. An Alembic revision
rollback with retained schema is intentional; it is not a physical rollback or
proof that older code can manage retained integrations. The tested application
rollback target for this change is this Phase 1A foundation with API disabled and
no public execution. Earlier binaries are not validated rollback targets.
Decommissioning populated foundation data needs a separately reviewed procedure.

Phase 1A does not implement customers, provisioning, delivery, operations, worker
queues, webhooks, management UI, administrative audit storage or shared production
rate limiting. No business or diagnostic endpoint has been added. See the roadmap
for actual validation results and the later Phase 1B-1/1B-2 implementation boundaries.


### Phase 1B-1 implementation boundary

Protected management is implemented in
[integration_management_pages.py](../app/api/integration_management_pages.py),
with only import/registration added to `app/api/app.py`. All reads and mutations
use the existing live `require_staff` dependency and the foundation's
`management_owner` resolver. The existing `integrations_manage` permission covers
management and read-only administrative history; there are no new permissions,
settings, dependencies, panel backends or public API routes in this slice.

| Panel route | Behavior |
|---|---|
| `GET /integrations` | Current eligible owner's integrations, ascending ID keyset, 25 per page; form for creating a disabled integration with empty grants. |
| `GET /integrations/{id}` | Fixed owner/shop, status/grants, up to 100 selected plan IDs, 25 eligible candidate plans and 25 secret-free credential summaries, independently paginated. |
| `POST /integrations` | Create in server-resolved owner/shop scope; no owner/shop selector or reassignment. |
| `POST /integrations/{id}/configure` | Replace status/grants within current owner authority; retirement remains terminal. |
| `POST /integrations/{id}/plans` | Replace the complete allowlist, at most 100 IDs; recheck shop, active/non-trial state and existing local eligibility bounds. |
| `POST /integrations/{id}/issue` | Issue under an integration-bound ticket, explicitly confirmed, with a private one-time response. |
| `POST /integrations/{id}/credentials/{selector}/rotate` | Check exact integration/selector, atomically replace once, and deliver the new bearer privately once. |
| `POST /integrations/{id}/credentials/{selector}/revoke` | Immediately revoke only that integration's selected credential; replacement credentials are independent. |
| `GET /integrations/audit` | Current owner/fixed shop history across its integrations, including failed creation without a target; 25 per page. |
| `GET /integrations/{id}/audit` | Own integration's administrative events, descending integer keyset, 25 per page; no write/delete UI. |

Owner hierarchy visibility never expands these queries. The same eligible owner
can manage each of its own integrations, but credentials and action tickets are
bound to one exact integration. The plan form explicitly replaces the full list,
including selections outside the candidate page. Empty means deny all. Current
local eligibility is rechecked on every update; no live PG catalog evidence or
production evidence fetcher is introduced. Ineligible existing IDs remain visible
so the owner can remove them; they do not become effective plan grants.

All mutation forms retain double-submit CSRF and existing Origin/session checks.
The focused management middleware bounds URL-encoded POST bodies to 16 KiB before
CSRF parsing, including chunked requests. Inputs have strict scope/enum/type/range
and length limits; unknown owner/shop fields and repeated form fields are rejected.
Every management response, including errors and redirects, has
`Cache-Control: private, no-store, max-age=0`, `Pragma: no-cache`,
`Referrer-Policy: no-referrer` and `Vary: Cookie`. Rendering uses Jinja autoescape,
existing components and formatters, Persian/RTL labels and LTR identifiers. Changes
other than creating the initial disabled/empty integration require an explicit
confirmation checkbox explaining their consequences. No new JavaScript is needed.

#### Private bearer delivery and form replay

Only a successful issuance/rotation POST renders
[integration_credential_once.html](../app/web/templates/integration_credential_once.html).
The ASGI request scheme must be HTTPS, including correctly configured trusted TLS
termination; a caller-supplied forwarded-protocol header alone grants nothing.
The response labels a readonly, accessible LTR textarea as a one-time private key.
There is no bearer GET, secret flash, redirect parameter, session/browser storage,
recoverable bearer store or digest display. Credential metadata queries project
only non-secret fields. Existing 90-day default / 365-day maximum expiry,
zero-default / 24-hour maximum rotation overlap, original-expiry ceiling and
immediate revocation remain in the foundation services.

Each rendered form receives a separate server-signed, 20-minute ticket containing
a generated correlation ID, authenticated principal, fixed scope, exact action,
integration and (where applicable) credential selector. Tickets use a management
signing salt distinct from session cookies. They contain no bearer, input payload
or digest, and creating a form makes no database record. A SQL unique action claim
is committed together with the mutation and audit. Reusing a consumed ticket,
even with changed fields, returns a safe `409` and never issues another secret.
Expired/tampered tickets require a fresh form; authorization is rechecked before
every claim and mutation. This is a synchronous form replay barrier, not an
execution queue, worker, public idempotency API or durable operation framework.

Refreshing a successful POST cannot recover its bearer. If the response is lost,
first inspect the credential metadata/history, revoke the affected credential and
explicitly issue another using a fresh form. A rotation that lost its response
can leave a live replacement; revoke that replacement by its metadata selector
before issuing again. Do not infer rollback from a transport/commit error: retrying
the original ticket is safe whether its transaction committed or rolled back.
There is no promise of replaying a lost one-time value.

#### Atomic administrative records and retention

[integration_management.py](../app/services/integration_management.py) owns one
transaction for the action claim, foundation mutation and
[administrative audit](../app/services/integration_admin_audit.py). Foundation
mutation functions retain their default commit behavior for existing callers;
the protected workflow explicitly uses the narrow `commit=False` flush boundary.
It never adds an audit after a committed integration mutation. Audit/storage
failure or cancellation rolls back the whole unit, including the claim. A
savepoint rolls back expected domain failures before recording a denied action.
No network call or Telegram send occurs within this workflow.

Records allow only correlation ID, authenticated actor principal, fixed scope,
integration ID, fixed action/outcome/error classification, UTC creation time,
non-secret credential selector and bounded status/grant/plan-ID snapshots. Plan
names, arbitrary text, request bodies/headers, website specifications, bearers,
secret digests and subscription credentials are excluded. The integration target
is a historical identifier rather than a cascading FK; service authorization and
mutation locking validate it, while history can survive a downgrade even if only
a failed creation exists. Actor/shop foreign keys and filter/order indexes remain.

Valid signed/scoped attempts that fail domain validation, including invalid
parsed fields, get a denied event with safe classifications and no payload.
Unauthenticated requests, CSRF/origin rejection, invalid tickets, unresolvable or
inactive authority and foreign/missing targets do not invent an audit scope or
disclose target existence. Consumed-ticket conflicts do not create another event.
These are administrative records; public API authentication/security-attempt
logging was deferred at this stage and is implemented in Phase 1B-2 below. Storage error logs include only the
exception class, never SQL parameters or arbitrary exception text.

The scheduler runs a bounded retention batch hourly, independently of Telegram.
It deletes at most 500 audit events older than 180 days and rechecks the
`evidence_hold` exclusion during deletion. Held evidence is never age-deleted.
Minimal action/correlation tombstones remain durable after audit pruning, so
retention cannot reopen a consumed form. This slice has no unresolved asynchronous
management actions. Future operational/reconciliation work must connect unresolved
evidence to holds before introducing age cleanup for those records; that linkage,
operator hold-management UI and action decommissioning are deferred. A sustained
backlog above 12,000 eligible events/day needs separately measured maintenance
tuning; no unbounded catch-up loop is introduced.

#### Administration migration and rollback

`0072_integration_admin_audit` follows the inspected `0071` head. It adds only
`integration_management_actions`, `integration_admin_audit` and indexes for scoped
pagination, credential lists, replay lookup and retention. Repeat upgrade inspects
tables/indexes and repairs missing indexes on SQLite and PostgreSQL. Existing
foundation rows, customers, service ownership and wallets are untouched.

Downgrade disables an existing `integration_api_enabled` setting. If action or
audit rows exist, it reports retained schema and preserves both tables, indexes
and replay/evidence data; repeat re-upgrade does not duplicate records. Otherwise
it removes only unused administration tables and the two additional foundation
list indexes. The foundation's existing populated-retention downgrade remains
unchanged. Both actual two-revision populated downgrade/re-upgrade and unused
extension removal/re-upgrade are exercised, as is history containing only a failed
creation with no integration row. This is an Alembic data-retaining rollback, not
certification of an older binary. The tested application remains the current
Phase 1B-1 implementation with API access disabled; older application binaries,
including unaudited direct-management callers, are unverified rollback targets.

Phase 1A is preserved. Phase 1B-1 adds administrative management/auditing only.
The Phase 1B-2 implementation below adds shared request budgets, trusted-proxy IP
handling and authentication auditing. These remain separate from management history. Customer
migration, business/diagnostic/authentication-test endpoints, production PG evidence,
provisioning/panel writes, wallets, execution workers and webhooks remain excluded.
See the roadmap for validation counts actually observed.


### Phase 1B-2 implementation boundary

This slice changes only the reusable authentication boundary and its supporting
security storage, transport capture and maintenance. It adds no production
business, diagnostic or authentication-test route. `integration_api_enabled`
remains `0` by default; existing management/CSRF protections, failed management
actions and their transactional administrative audit remain intact. No permission,
rate override, new dependency or release version is introduced.

#### Shared admission and concurrent failure accounting

`integration_rate_counters` uses a composite primary key `(subject, budget, window)`.
The window is the integer UTC epoch minute. SQLite/PostgreSQL conditional upserts
atomically insert or increment only below the fixed limit. The database is the
only admission authority. Integration IDs, rather than credential selectors, key
120 reads/minute, 20 writes/minute and 10 private reads/minute. Rotation and overlap
therefore preserve the budget. Dependency configuration supplies `RequestBudget`;
mutation scopes require `WRITE`, and `services:access` requires `PRIVATE`. Caller
methods, headers, query fields and claimed selectors never choose a budget.
Private requests charge read and private in one transaction; exhaustion rolls back
both increments. A request-local set prevents repeated authority checks from
charging an applicable budget again. It cannot grant any initial admission.
Budgets are charged after credential verification and before local/PG authorization;
a subsequent denial does not refund the integration charge.

Before parsing/looking up a credential, admission reserves one of 60 slots per
resolved IP/minute. The counter represents **retained authentication failures plus
in-flight attempts**. At most the remaining slots can enter concurrently. A success
releases its reservation; failures classified `invalid_request`,
`unsupported_authentication`, `invalid_credential`, `integration_disabled`,
`principal_inactive` or `permission_denied` retain it. Infrastructure unavailability,
global disable and integration rate exhaustion release a reservation if one was
acquired. This is a failure budget: sequential successes can exceed 60/minute,
subject to their separate integration budgets. Pending requests can temporarily
occupy all slots, even if they eventually succeed. Cancellation, process death or
an uncertain commit conservatively retains the reservation until window rollover;
there is no retry that invents a release. A denied pre-auth reservation is never
released as if it had been acquired.

Exhaustion returns Persian `rate_limited`/`429`, `Retry-After` bounded to 1–60
seconds, private/no-store headers and the generated request ID. Fixed windows
permit boundary bursts. Counter/storage errors return safe `security_unavailable`
`503`, without local fallback. Admission commits before any injected PG call;
authority-read transactions end before that call, and credential revocation,
principal/shop/grants/global switch and required scopes are rechecked afterwards.
There is no production PG evidence fetcher. No execution queue/concurrency limits
from later phases are implemented.

#### Deployment proxy boundary

The standard `app.main` runner now runs Uvicorn with `proxy_headers=False` and
installs `IntegrationPeerCapture` **outside** Uvicorn's existing proxy middleware.
The immutable snapshot records the original socket peer and transport scheme
before normalization. The inner Uvicorn middleware uses that installed version's
normal `FORWARDED_ALLOW_IPS` default/environment; panel/login scope normalization
therefore retains its prior behavior. Integration resolution uses the original
snapshot exclusively and never reinterprets the normalized `request.client`.

`INTEGRATION_TRUSTED_PROXIES` is a comma-separated allowlist of native IPv4/IPv6
CIDRs, empty by default. `TRUST_PROXY`, `TRUST_PROXY_HOPS`, arbitrary forwarded
headers and `FORWARDED_ALLOW_IPS` do not grant integration proxy trust. Configure
only actual controlled peers, including each controlled hop in a proxy chain;
never configure all-address ranges (`/0` is rejected). IPv4-mapped addresses are
normalized to IPv4; mapped allowlist networks, zone identifiers, ports/bracketed
addresses and ambiguous/malformed chains are rejected. At most 16 CIDRs, 16
forwarded hops and 2,048 total forwarding-header value bytes are accepted.

An untrusted socket peer uses its socket IP and transport scheme; forwarding
headers are ignored. A trusted peer must provide exactly one `X-Forwarded-For`;
walk right to left through trusted peers and stop at the first untrusted address.
Validate the entire bounded chain, including ignored prefixes. Optional
`X-Forwarded-Proto` must be a single `http` or `https` value set by the closest
trusted proxy. Duplicate headers, a `Forwarded` header or malformed data from a
trusted peer fail closed with `503`; no parsing fallback is attempted. Missing,
invalid or unverifiable socket provenance also fails closed. HTTPS is required
using the raw transport or this validated proxy scheme.

For a single local Nginx TLS terminator, set e.g.
`INTEGRATION_TRUSTED_PROXIES=127.0.0.1/32,::1/128`, restrict direct listener access,
and have the terminator overwrite these headers:

```nginx
proxy_set_header X-Forwarded-For $remote_addr;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header Forwarded "";
```

For multiple controlled proxies, the edge must remove caller-supplied forwarding
information, subsequent controlled proxies append their observed peer, and the
closest proxy overwrites the scheme with the verified external scheme. Prevent
untrusted direct access to trusted proxy addresses. Custom ASGI runners must
install capture before **any** proxy normalization, with server proxy processing
disabled; installing it inside already-normalized Uvicorn middleware is unsupported.
An app served directly without the capture adapter has no verified provenance and
its integration dependencies return `503`. Operational activation must verify the
real deployment path; isolated applications are not deployment certification.

#### Authentication evidence, privacy and amplification bounds

The separate `integration_security_audit` table records only the fixed event
`authentication`, allowlisted outcomes, a generated UUID correlation ID, timestamp,
count and optional verified integration/owner/fixed-shop attribution. Successful
responses and errors expose the generated `X-Request-ID`; incoming IDs are ignored.
A claimed selector never establishes attribution. Only a successful active,
unexpired credential digest comparison followed by loading its integration may
establish it. Later revocation/authorization failure can retain that already-proven
identity; invalid/unknown/expired/revoked credentials remain anonymous. No raw
Bearer, digest, selector, authorization header, body, URL, delivery credential or
exception text is stored. Storage warnings contain only a fixed category and
exception class. Existing administrative history queries do not include this table;
anonymous events are never placed into an ordinary tenant's history.

IP addresses are stored only as HMAC-SHA256 under the configured `WEB_SECRET`,
domain-separated with a version and UTC date. The same daily pseudonym keys
pre-auth admission and detailed audit records. There is no raw IP, subnet or port
in these tables. Workers must share a strong, non-placeholder secret of at least
32 characters and the same UTC clock/database. No secret is automatically created
by this boundary. Pseudonyms permit linkage within a day and are not anonymized
against an operator holding the secret. Rotating the secret resets IP pseudonyms
and their current budget; integration budgets remain unaffected. UTC day rollover
coincides with a fixed-minute rollover.

A global SQL counter allows at most 1,000 detailed audit rows/minute across all
workers, including successful outcomes. Above it, one anonymous aggregate per
fixed outcome/minute records the count and one sample generated request ID,
without IP or tenant attribution. Thus rejected floods cannot create more than
1,012 audit rows/minute; overflow sacrifices per-request identity/correlation
coverage intentionally. Aggregate counts are not a complete request trace. Audit
writes and any successful pre-auth release commit together before authority is
returned. An audit write/commit failure rolls back that release, returns `503`,
and never grants authority. Previously committed integration charges remain spent.
A total storage outage cannot promise durable evidence; admission remains denied.
This does not alter management mutation/audit atomicity.

The security job runs every five minutes without overlap, deleting at most 500
counter rows older than 48 hours and 500 security rows older than 180 days. Both
queries use retention indexes and recheck eligibility at deletion. Held evidence
(`evidence_hold`) never expires, including anonymous/overflow evidence; the existing
controlled operator evidence-hold procedure applies. There are no API operations
or unresolved commitments in this slice. Retention defines deletion eligibility,
not a guarantee that a bounded job drains an outage/attack backlog immediately.
Each table drains at most 144,000 rows/day at this cadence; sustained traffic above
that or a large outage backlog requires separately scheduled, measured bounded
maintenance. Do not delete held evidence to catch up.

#### Migration and rollback

The inspected Alembic head was `0072_integration_admin_audit`;
`0073_integration_security` adds only the counter/evidence tables and indexes.
Upgrade inspects tables/indexes, repairs missing indexes, and is repeat-safe on
SQLite and PostgreSQL. Downgrade always sets an existing API enable setting to
`0`. If either security table contains data, it reports retained schema and keeps
both tables, indexes, counters and evidence. Otherwise it removes only these
unused extensions. Re-upgrade preserves rows and does not enable the API. Retained
historical scope identifiers intentionally have no live foreign-key dependency,
so even anonymous-only evidence survives downgrade through an empty foundation.
Populated foundation, credentials, management claims and administrative evidence
retain their existing rollback policy. The supported application rollback is this
security-aware version with API disabled and schema retained; an older binary or
physically exact decommission is not certified.

Phase 1 implementation is complete only with its applicable focused/security/lint
gates recorded in the roadmap. Full-suite/clean-dev comparison remains a separate
mandatory pre-PR gate, deferred in this slice. Deployment verification, real PG
authority evidence and public API activation remain later gates. The exact next
implementation phase is **Phase 2 — Customers independent of Telegram**; no work
from that phase is included here.


## Phase 2A implemented boundary (2026-10-21)

Migration `0074_customer_identity` is additive after preserved 0071–0073. It adds
`Customer`/`IntegrationCustomer`, unique public and legacy adapter IDs, the two
integration mapping uniqueness constraints, nullable canonical service customer
and stable public service IDs. Legacy bot ownership stays required; no API-origin
service, operation foreign key, placeholder operation table, linking table or
secret envelope is added. See the [identity operating procedure](INTEGRATION_CUSTOMER_IDENTITY.md#9-phase-2a-implementation-and-operating-procedure)
for the optional committed 500-row maintenance backfill, inconsistent-owner handling and exact
retained-schema 0074↔0073 rollback support. Current complete upgrades do not stop at this historical expansion boundary:
0075 runs the automatic bounded backfill during ordinary `init_db`. Backfill never
changes a stored shop or its review flag, financial identity, balance, panel ID or
principal association.

Internal domain functions are `create_or_get_integration_customer(session, bearer,
CustomerInput(...))` returning `CustomerRegistration(customer, created)` and
`read_integration_customer(session, bearer, public_customer_id)` returning the
allowlisted `CustomerView`. They require current enabled API/integration, valid
unrevoked credentials, eligible owner and corresponding customer scope/grant/feature.
Caller-built `IntegrationAuthority`, owner or shop fields are not accepted.
Registration locks the integration before credential reads, then resolves current
authority after acquiring the lock. The customer/mapping pair commits atomically;
a failed registration rolls both back. This lock is shared with protected
configuration and credential revocation. Unique constraints remain the final
identity boundary. Registration supports caller rollback with `commit=False`.

Creation additionally requires explicit per-integration
`customer_entry_policy=website`; existing and new integrations default to `denied`.
A `customers:write` grant alone is insufficient. The existing protected configure
form changes this policy and records its before/after value in the same transaction
as the policy change and permanent form replay marker. Owner/shop reassignment is
still rejected. No new permission is needed beyond existing `integrations_manage`;
no fake contact verification is created. Reads require current `customers:read`.

References follow section 3.2 exactly without normalization. Display names follow
its character/UTF-8/control bounds and are stored as hostile text, never executed.
Matching same-integration reference/name input returns the original public ID;
different names raise `customer_reference_conflict`. A blocked customer raises
`customer_blocked` on reads or replay; registration never reactivates it. Other
integration/shop and legacy adapter identities resolve as `not_found`. Matching
references across websites always create separate identities.

No customer endpoint is mounted and no HTTP Idempotency-Key store is introduced
in this internal slice. Permanent reference uniqueness provides registration replay;
the future HTTP contract still requires the header and the existing secure
transport/bearer/rate-limit dependency with the write budget. Backend credentials
can act for all mapped customers in their own integration; website session identity
is still the website's responsibility. `telegram_linked` is always false in 2A.
Public activation, production PG evidence, all technical operations and verified
linking remain deferred. Keep `integration_api_enabled=0` by default.
