# PGClockBot Integration API Roadmap

Status: Phases 2B/2C and 3–7 are implemented locally for the conservative v1 subset
in the [release operating guide](INTEGRATION_API_OPERATIONS.md). Phase 8 local
release preparation, pre-PR review and local handoff are complete. By the user's
2026-10-25 decision, external verification is **deferred / not performed** for this
handoff. The candidate is not production-verified. Future activation still requires
the documented external gates and separate authorization. All activation switches
remain disabled.
The focused pre-PR review checkpoint at the end records subsequent repairs and
final-file verification. Historical evidence is preserved as distinct invocations.
The submission checkpoint at the end supersedes historical target/migration references:
feature revisions are now 0071–0080 after upstream `0070_shop_miniapp_key`.

Goal: allow any website's backend to register customers without Telegram accounts
and delegate service provisioning and lifecycle management to PGClockBot through
limited API permissions. The integration uses the existing single PasarGuard panel
and preserves existing authorization, quota, credit, and billing rules.

Engineering documentation and implementation prompts are written in English.
Product UI text remains Persian and RTL, following the repository's UI conventions.

## 1. Agreed decisions

- Customers can receive services without Telegram accounts. Linking a Telegram
  identity is optional and requires proof of ownership.
- The consuming website manages customer login, membership policies, retail
  pricing, collection of funds, receipts, and customer refunds.
- An API request authorizes a technical operation. It must not fabricate an
  approved payment record or change a customer's wallet balance.
- Existing reseller quota, credit, and billing restrictions continue to apply
  wherever relevant. A customer's payment to a website does not bypass them.
- An integration belongs to a principal and a fixed shop scope. The authenticated
  identity determines the shop; a request cannot choose its own shop scope.
- The API is versioned and server-to-server. Its credentials remain in the
  consuming website's backend.
- Contracts, examples, and test fixtures are generic and independent of any
  particular website.

## 2. Version 1 scope

- Integration, credential, permission, and allowed-plan management in the existing panel.
- Customers independent of Telegram, with mappings to each website's customer references.
- Optional, verified Telegram identity linking that preserves resource boundaries.
- Authorized plan and service reads, usage, and the last synchronization time.
- Provisioning from existing plans, with one service per provisioning request.
- Operation tracking and recovery after connection failures or process restarts.
- Private retrieval of a service's subscription URL and matching QR code under a separate scope.
- Renewals and traffic/duration add-ons applied to the existing service.
- Signed webhooks with durable event storage and retries.
- OpenAPI documentation, integration examples, and an operations guide.

Later extensions: bulk provisioning, assigning existing services to an integration,
service deletion, usage resets, and subscription-link rotation. Each requires its
own contract and permissions. Customer payment APIs, website membership engines,
and panel infrastructure management are outside this feature's scope.

## 3. Dependencies and delivery milestones

| Phase | Deliverable | Dependencies | Exit criterion |
|---|---|---|---|
| 0 | API contract and customer identity decision | None | Resolve the design questions in section 4 and produce a reviewable migration plan. |
| 1 | Integrations and scoped credentials | 0 | Revoked or out-of-scope credentials cannot access resources. |
| 2 | Independent customers and optional Telegram linking | 0 and 1 | Web customers work while existing customer behavior remains compatible. |
| 3 | Read-only API | 1 and 2 | Every read enforces integration, shop, and customer isolation. |
| 4 | Durable operations and execution queue | 1 and 2 | Duplicate submissions and restart recovery do not duplicate effects. |
| 5 | Provisioning and private delivery | 3 and 4 | The customer-to-delivery flow works without Telegram. |
| 6 | Renewals and add-ons | 5 | Changes affect the same service and are not applied twice. |
| 7 | Webhooks and operation management | 4 through 6 | Destination outages and repeated notifications are recoverable. |
| 8 | Final documentation and release validation | All phases | The acceptance scenarios in section 7 and release gates pass. |

The first internal milestone is the end of phase 5: customer registration,
provisioning, operation tracking, and private delivery can be exercised together.
Phase 8 produces a local release candidate; actual-panel and deployment gates
remain required before release. This roadmap does not commit to
calendar dates; estimate effort after selecting the migration approach in phase 0.

## 4. Phase details

### Phase 0 — Contract and customer compatibility

Tasks:

- Document resources, inputs, outputs, errors, permissions, and operation states
  in `docs/INTEGRATION_API_CONTRACT.md`.
- Record the customer architecture decision in
  `docs/INTEGRATION_CUSTOMER_IDENTITY.md`, examining dependencies on `BotUser`,
  service ownership, notifications, contact verification gates, and the users panel.
- Select a migration path to independent customer identity. Separate internal
  customer identifiers from channel identifiers; do not invent Telegram IDs for
  web customers.
- Define downgrade behavior when web customers and active services exist,
  preserving data without inventing identifiers. Do not finalize column
  nullability changes before resolving this question.
- Define linking to an existing Telegram customer, conflicting links, and
  unlinking. Identity linking alone must not grant access to or transfer other services.
- Specify eligible roles, new `FEATURE_PERMS` entries, and how API scopes
  intersect with the principal's existing permissions and PasarGuard authorization.
- Define allowed-plan and reseller-credit policies, technical term snapshots,
  and handling of plan changes between request acceptance and execution.
- Define request limits, input lengths, pagination, operation/event retention,
  and accepted-request behavior when credentials are revoked or an integration is disabled.
- Specify how existing session and CSRF middleware interact with the new routes.
  Panel management retains its protections; the server-to-server API has its own
  authentication path and identity.

Exit criterion: review the contract and migration decision. Resolve all design
questions about customer ownership, provisioning authorization, data-preserving
rollback, and unknown operation outcomes before implementing dependent phases.

**Historical Phase 0 checkpoint (2026-10-21; superseded implementation status):** focused source inspection against latest fetched
`upstream/dev`, commit `ebd27e717b2d95de7b3b5931efedbcf246a02029`, produced and
reviewed together [the API contract](INTEGRATION_API_CONTRACT.md) and
[the customer identity decision](INTEGRATION_CUSTOMER_IDENTITY.md). These record:

- Independent canonical customers, legacy bot-user adapters and integration-scoped
  references; optional dual-proof Telegram links do not merge or transfer resources.
- Exact resources/schemas, scope intersections, plan eligibility and snapshots,
  website channel policy, private delivery permission, limits and retention.
- Durable external-operation uniqueness, committed targets/claims, shared service
  exclusion and capacity reservations, and conservative unknown-outcome recovery.
- A staged compatibility migration, supported application rollback and populated
  data-retaining database downgrade. Full physical pre-feature downgrade with
  active web services is explicitly unsupported; no fabricated Telegram IDs.
- Later webhook outbox/signing/retry/SSRF requirements and behavioral acceptance
  scenarios, including another customer, integration and shop.

The core phase 0 design questions are resolved as recommendations for review.
Current code does not yet provide customer-neutral ownership, shared API mutation
guards or non-expiring unknown-outcome reservations. Inventorying affected legacy
call sites, validating panel reconciliation evidence and testing both database
migration paths are implementation/release gates documented in the deliverables,
not completed work or a claim of a full source audit. Phase 0 validation is limited
to documentation links/anchors, examples, scope/whitespace checks and the lint
ratchet; no runtime, migration, security or full-suite tests are claimed.
Completed checks: 3 Markdown documents, 59 local links/anchors, 54 cited source
symbols and 1 JSON example passed; the lint ratchet passed with 0 changed Python
files. The isolated branch contains only the three design documents.

### Phase 1 — Integrations, credentials, and authorization

Tasks:

- Add an integration entity bound to a principal and shop, with its status and allowed plans.
- Store rotatable and revocable credentials using hashes and non-sensitive display
  identifiers. Define secure initial credential delivery and exclude credentials
  from logs and routine responses.
- Enforce principal permissions, integration permissions, credential scopes, and
  existing quota checks together.
- Add Persian integration management pages with CSRF protection, accessibility,
  and audit records for administrative actions.
- Implement request rate limits with defined multi-worker behavior and `429` responses.

Exit criterion: reject disabled integrations, revoked credentials, inactive
principals, insufficient permissions, and out-of-scope requests. Credential
rotation must not change the integration's identity.

#### Phase 1A — Historical foundation checkpoint

The integration/credential models, immutable owning principal and explicit fixed
shop scope, allowed local plan associations, secure expiring/rotatable/revocable
keys, feature declarations, disabled global switch, typed authority resolver and
unmounted Bearer dependency are implemented. See the
[implementation boundary](INTEGRATION_API_CONTRACT.md#phase-1a-implementation-boundary)
for exact semantics, the distinction between local scopes and PG evidence, and
data-retaining downgrade/application rollback limits. Customer/service ownership
and existing panel/session/CSRF protections remain unchanged.

Validation evidence is recorded below. Completion of this foundation does not
complete Phase 1 or permit an operational public API.

#### Phase 1B-1 — Historical management and administrative auditing checkpoint

The own-owner/shop Persian/RTL panel now manages disabled integration creation,
terminal status and grants, bounded eligible plan associations, non-secret
credential metadata, one-time issuance/rotation and immediate revocation.
Existing live staff/session/CSRF protections and `integrations_manage` apply.
Administrative state, a durable form replay claim and allowlisted audit metadata
commit atomically; audit failure rolls back all three. Retention is bounded and
preserves replay tombstones and held evidence. See the exact
[Phase 1B-1 boundary](INTEGRATION_API_CONTRACT.md#phase-1b-1-implementation-boundary)
for input limits, private/lost-response behavior, failure recording, maintenance
limits, migration and application rollback guarantees.

Validation results are recorded below. Phase 1A prerequisites are preserved in the
same worktree, carried onto `codex/integration-api-phase1b-1` at the same refreshed
`upstream/dev` commit. No commit, push, PR or version change is made.

#### Phase 1B-2 — Historical admission and authentication security checkpoint

SQL-backed fixed-minute integration budgets and concurrent pre-auth failure
reservations, original-peer proxy trust, generated correlation IDs and bounded
secret-free authentication evidence are implemented in the reusable dependency.
No operational router or production PG evidence fetcher is added; the default
switch stays disabled. Read the exact
[Phase 1B-2 boundary](INTEGRATION_API_CONTRACT.md#phase-1b-2-implementation-boundary)
for deployment configuration, privacy, overflow aggregation, failure accounting,
retention backlog limits and retained-schema rollback. Validation is recorded below.

The next implementation phase is Phase 2. Full-suite comparison against clean
refreshed upstream/dev is explicitly deferred until before a PR; prior full-suite
counts below are historical evidence, not results for this slice. Deployment and
public activation gates remain separate from Phase 1 implementation completion.

### Phase 2 — Customers independent of Telegram (split delivery)

**2A — implemented expansion:** neutral Customer and immutable integration
mappings; nullable service customer/public-ID extensions; repeat-safe, committed
keyset backfill (at most 500 rows); legacy creation dual-writes; internal scoped
customer registration/read services and protected website entry approval. The
0070 expansion originally retained non-null bot ownership and no customer HTTP
routes; those are historical stage boundaries, superseded by 0071 and Phase 3.

**2B — implemented compatibility and canonical ownership:** affected readers,
jobs, notifications, deletion, reporting and allocation distinguish legacy and
website services. Migration 0071 verifies legacy adapter/service backfill before
allowing nullable bot ownership, with explicit legacy/API origin invariants.
API services retain canonical customer/integration/shop/principal ownership and
no legacy plaintext delivery. Legacy backfill excludes website-origin rows and
can resume past them. See the [compatibility inventory](INTEGRATION_API_COMPATIBILITY.md).

**2C — implemented optional linking:** separate Telegram candidate presentation,
explicit sender confirmation and independent website confirmation; bounded
expiry, conflict/replay barriers, consent history and unlinking. Linking transfers
no services, balances or roles and enables no legacy-channel delivery.

Phase 2 local exit criteria are complete for the supported subset. `/api/v1`
customer/link and technical routes register only when `INTEGRATION_API_ROUTES_ENABLED`
is true; registration defaults off. Runtime access additionally requires the DB
API switch, active integration, eligible current owner and explicit scopes/grants.
Actual deployment activation remains a separate, unperformed gate.

### Phase 3 — Read-only API

Tasks:

- Add the versioned `/api/v1` namespace in focused `app/api/` modules.
- Read integration capabilities, allowed plans, customers, and services belonging
  to the authenticated integration.
- Apply integration scope in addition to shop and customer ownership. Another
  integration in the same shop has no default access to these resources.
- Return local snapshots during panel outages, including synchronization time
  and data freshness.
- Use keyset pagination, bounded responses, and the existing cache. Avoid panel
  requests for every row in a list.
- Allowlist response fields. Exclude delivery credentials from list and summary responses.

Exit criterion: second-customer, second-integration, and second-shop isolation
tests pass. Panel outages do not remove local lists, and stale data is not
presented as live data.

### Phase 4 — Durable operations

Tasks:

- Persist operations with opaque IDs, integration, customer, external reference,
  operation type, and a snapshot of validated inputs.
- Require `Idempotency-Key` for execution requests. Scope keys to the integration
  and operation, and reject reuse with different inputs.
- Enforce durable uniqueness of the operation's external reference, independent
  of credential rotation and idempotency retention. Removing short-lived
  idempotency records must not permit the same external request to execute again.
- Atomically claim work using leases and commit before external calls. Keep
  database transactions closed while waiting on the network.
- Define the states `queued`, `running`, `outcome_unknown`, `needs_review`,
  `succeeded`, and `failed`, including their allowed transitions.
- Recover operations after process restarts. Lease expiry alone must not permit
  another panel write when an earlier attempt may already have written to the panel.
- Distinguish definitive failures from unknown outcomes. Reconciliation must use
  the previously persisted target.

Exit criterion: behavioral tests cover duplicate execution, concurrent workers,
and crashes around commit/network boundaries. Operations with unknown outcomes
are not automatically repeated from the beginning.

### Phase 5 — Provisioning and private delivery

Tasks:

- Use allowed plans and existing provisioning, quota, and billing rules in the service layer.
- Persist the stable target and technical terms before creating a user on the
  existing single PasarGuard panel.
- Reconcile unknown outcomes against the saved target, ownership, and technical
  terms. A matching username alone is insufficient.
- Store the service and its integration, customer, and operation associations.
  Record success only after validating the result.
- Separate delivery-credential retrieval from status reads. Require a separate
  scope, private `no-store` responses, and QR generation from the same validated URL.
- Offload blocking QR work from the event loop. Delivery must not depend on Telegram.

Exit criterion: the web-customer-to-delivery flow works. A timeout after successful
creation does not create a second service. Invalid panel responses or the absence
of a valid subscription URL must not be reported as successful delivery.

### Phase 6 — Renewals and add-ons

Tasks:

- Expose technical renewal previews and eligible add-ons through existing business logic.
- Revalidate ownership, permissions, plan eligibility, quota, and credit at execution time.
- Persist absolute quota/time targets before panel calls and serialize mutations
  to the same service.
- Reconcile targets after timeouts. Do not repeat relative traffic/time increments.
- Cover unlimited, expired, and `on_hold` services according to supported panel behavior.

Exit criterion: retain the same service and panel-user identity. Repeated or
concurrent renewals/add-ons do not duplicate changes. Unknown outcomes prevent
incompatible subsequent mutations until resolved.

### Phase 7 — Webhooks and operation management

Tasks:

- Persist result events in an outbox in the same transaction as the state change.
  Deliver notifications after commit.
- Sign the payload and delivery timestamp. Include a stable event ID and resource
  version so consumers can handle duplicate and out-of-order events.
- Implement bounded retries with backoff, delivery-attempt records, and authorized redelivery.
- Restrict outbound destinations and protect against SSRF, redirects, and access
  to internal networks.
- Exclude delivery credentials and secrets from events, logs, and error reports.
- Add paginated operation lists, webhook delivery status, and an authorized
  reconciliation workflow to the panel. Reconciliation examines the original
  operation rather than creating a new request.
- Retain operation-status polling to recover from webhook outages.

Exit criterion: destination outages do not lose events. Duplicate and out-of-order
deliveries can be handled. Internal or invalid destinations are rejected.

### Phase 8 — Documentation and release validation

Tasks:

- Finalize OpenAPI schemas, synthetic examples, errors, and scopes.
- Provide backend integration guidance covering credential storage, idempotency,
  operation polling, and signature verification.
- Add an end-to-end scenario using a sample client and a fake panel. Tests must
  not use network calls or real credentials.
- Run focused tests, security tests, and lint. Before each PR, compare the full
  suite with a clean checkout of the latest `upstream/dev`; report counts and any
  newly introduced failures.
- Verify migrations on SQLite and PostgreSQL, repeated upgrades, and data-preserving downgrades.
- Document API shutdown, integration revocation, handling of in-flight operations,
  and safe application rollback.
- Ship with the API disabled by default and enable it only for authorized integrations.

Exit criterion: the section 7 scenarios pass, the implementation matches its
contract, no new failures are introduced relative to the baseline, and actual
limitations are documented.

## 5. Initial endpoint contract

The initial endpoint inventory below is implemented within the supported v1 subset.
The authoritative methods, schemas, permissions and required customer selectors are
in [the contract](INTEGRATION_API_CONTRACT.md#3-wire-format-schemas-and-endpoint-methods)
and the generated [OpenAPI artifact](INTEGRATION_API_OPENAPI.json).
Every endpoint is authenticated and integration-scoped.

| Proposed endpoint | Purpose |
|---|---|
| `GET /api/v1/capabilities` | Effective integration capabilities and permissions. |
| `GET /api/v1/plans` | Allowed plans and technical terms. |
| `POST /api/v1/customers` | Idempotent customer creation or retrieval using an external reference. |
| `GET /api/v1/customers/{id}` | Read a customer belonging to the integration. |
| `GET /api/v1/services` | Paginated service list with a required customer selector. |
| `GET /api/v1/services/{id}` | Status, usage, and synchronization time. |
| `POST /api/v1/provisioning-requests` | Accept a provisioning request and return an operation ID. |
| `GET /api/v1/operations/{id}` | Track the result of an operation. |
| `GET /api/v1/services/{id}/access` | Private delivery-credential retrieval under a separate scope. |
| `GET /api/v1/services/{id}/qr` | QR code for the same URL, requiring the delivery scope. |
| `GET /api/v1/services/{id}/renewal-preview` | Technical renewal preview for an allowed plan. |
| `POST /api/v1/services/{id}/renewals` | Submit a renewal for the existing service. |
| `GET /api/v1/services/{id}/addons` | Eligible add-ons. |
| `POST /api/v1/services/{id}/addons` | Submit an add-on operation. |

Integration, credential, webhook-destination, and Telegram-link management use
their own authenticated workflows. Provisioning scope does not authorize these actions.

## 6. Change delivery rules

- Before implementation, create an independent branch from the latest
  `upstream/dev`. Do not base this feature on branches for unrelated changes.
- Each PR contains one logical change and targets `dev`. Split large phases into smaller PRs.
- Complete a phase's exit criteria and tests before implementing dependent phases.
- Keep business logic in focused `app/services/` modules and routes in separate
  `app/api/` modules. Add only one-line registration in `app/api/app.py`.
- Jobs orchestrate work; new services must not depend on FastAPI or aiogram.
- Reuse existing templates, components, and CSS for UI additions.
- Ship new models with additive migrations, indexes, and upgrade/downgrade tests.
  Resolve migration numbering against the current head during implementation and before a PR.
- Keep broad refactoring, dependency updates, and unrelated fixes in separate PRs.
- Update contracts and integration documentation alongside each phase's behavior.

## 7. Required acceptance scenarios

- A customer without Telegram completes registration, delivery, and renewal.
- A second customer, another integration in the same shop, and another shop
  cannot access resources outside their scope.
- Telegram linking grants no administrative role and transfers no resources from
  another website or shop.
- Invalid scopes, revoked credentials, and inactive principals or integrations are rejected.
- Repeated requests with identical inputs return the same operation; reuse with
  different inputs is rejected.
- Rotating credentials or changing `Idempotency-Key` does not re-execute an
  existing external operation reference.
- Tests cover timeouts before and after panel writes, worker crashes, and lease expiry.
- Unknown outcomes do not trigger blind creation, deletion, renewal, or release.
- Concurrent renewals and add-ons do not apply quota changes twice.
- Subscription URLs and QR codes are available only for ready services through
  authorized requests; the QR payload equals the subscription URL.
- Lists, webhooks, logs, and errors contain no delivery credentials or secrets.
- Hostile text and out-of-range numbers, including `bool`, are rejected or escaped as appropriate.
- Panel outages preserve local snapshots and persisted operations.
- Webhook destination outages preserve events, and duplicate consumption can be controlled.
- Pagination, query counts, and panel-call volume remain bounded as lists grow.
- Existing-database upgrades, repeated upgrades, and downgrades with web-customer data are tested.
- Existing bot, Mini App, shop ownership, wallet, and billing behavior has no new failures.

Add security, isolation, authorization, and relevant financial tests to
`scripts/security_test_manifest.txt`. All tests assert behavior rather than
source-file contents or line layout.

## 8. Delivery status

- [x] Define the responsibility boundary between websites and PGClockBot.
- [x] Establish independent customer identity and optional Telegram linking.
- [x] Define scope, dependencies, and phase exit criteria in this roadmap.
- [x] Phase 0: detailed contract and customer compatibility decision (design only).
- [x] Phase 1A: integration/credential/local-authorization foundation, no public API.
- [x] Phase 1B-1: protected management and transactional administrative auditing.
- [x] Phase 1B-2: shared rate limiting and remaining authentication security/auditing.
- [x] Phase 1 implementation: applicable security gates complete; pre-PR/release gates remain explicit.
- [x] Phase 2A: independent identity expansion, internal customer services and legacy dual-writes/backfill.
- [x] Phase 2B: compatibility and canonical ownership enforcement.
- [x] Phase 2C: optional verified Telegram linking/unlinking.
- [x] Phases 3–7: supported read/execution/private-delivery/lifecycle/outbox behavior.
- [x] Phase 8: final local verification and documented external release gates.
- [ ] External release gates — **deferred / not performed** for this local handoff;
  actual-panel/deployment evidence remains required before future authorized activation.

Local handoff decision (2026-10-25): verification requiring a real panel, staging
setup/deployment, live infrastructure, real credentials or external Telegram/webhook
delivery is deferred / not performed. These checks are outside this task; staging
access or credentials are not needed to close the local handoff. Runtime
authorization and live-evidence requirements are unchanged. The existing PR draft
and completed validation evidence below are reused; only its external-check status
is updated. No runtime changes, substitute mock tests or broad-suite reruns were
needed. No outstanding local handoff work remains; the eight unchanged baseline
test failures remain recorded as known local issues.

Historical next step at the Phase 2A checkpoint: Phase 2B — complete reader/job/presentation compatibility and verify
canonical ownership before changing legacy owner nullability. Public routes and
API-origin services remain disabled.

### Phase 1A validation

Implementation and clean detached-worktree baseline both use refreshed
`upstream/dev` commit `ebd27e717b2d95de7b3b5931efedbcf246a02029`. Original uncommitted Phase 0 documents remain
preserved in their original worktree; copies are carried with this change.
Tests use synthetic identities, fake PG evidence and isolated databases only.

| Check | Passed | Failed | Skipped |
|---|---:|---:|---:|
| Focused foundation with PostgreSQL enabled | 69 | 0 | 0 |
| Security manifest with PostgreSQL enabled (204 files) | 2682 | 0 | 0 |
| Clean `upstream/dev` full suite | 5287 | 8 | 31 |
| Phase 1A full suite | 5347 | 8 | 40 |

The focused tests comprise 51 SQLite service/API cases, eight migration cases per
backend and one independent-connection concurrency case per backend. Security
also passed 2,597 subtests; the clean baseline passed 6,112 subtests
and Phase 1A passed 6,115 subtests.
The full-suite failure IDs match the baseline exactly: **no newly introduced
failures**. Its nine additional skips are PostgreSQL-specific cases with the URL
unset for the normal full run; all nine passed separately in the PG-enabled
focused and security runs. SQLite is not substituted for PostgreSQL validation.

Additional completed checks:

- Actual Alembic chains on SQLite and PostgreSQL: baseline migration to `0066`,
  foundation upgrade twice, populated downgrade/re-upgrade, empty extension
  removal/re-upgrade, and preservation of an existing synthetic wallet passed.
  Actual commands also report retained schema despite logging reconfiguration.
- Lint ratchet: 14 changed Python files, no additional findings. Diff whitespace
  checks pass. No browser code was changed, so no new Node test was required.
- Documentation: 65 local links/anchors valid; default-text snapshot JSON valid.
  Its only update is the new disabled switch; the unrelated baseline snapshot
  failure remains unchanged.
- All original user worktrees/changes are preserved. No commit, push, PR or release
  version change was made. The temporary PostgreSQL cluster has been stopped.

Existing full-suite failures, intentionally outside this feature:

- `tests/test_bot_ui_default_texts.py::DefaultTextSnapshotTests::test_default_texts_match_reviewed_snapshot`
- `tests/test_gift_code_rules.py::GiftCodeRulesTests::test_delete_buyer_with_reserved_and_consumed_codes`
- `tests/test_home_overview.py::HostMetricsTests::test_cpu_percent_samples`
- `tests/test_home_overview.py::HostMetricsTests::test_host_metrics_shape`
- `tests/test_home_overview.py::HostMetricsTests::test_memory_stats_linux`
- `tests/test_perf_and_correctness_2_5_8.py::ShopOrderAttributionTests::test_create_order_uses_shop_context_not_sticky_reseller`
- `tests/test_service_quota_cache_list_9_0_3.py::AdminExtendCachesQuotaTests::test_extend_writes_quota_cache`
- `tests/test_service_quota_cache_list_9_0_3.py::AdminExtendCachesQuotaTests::test_partial_modify_response_still_caches_kwargs`

Reproduce using Python 3.12 and the repository's test dependencies. Set
`PGCLOCK_TEST_POSTGRES_URL` to a disposable synthetic database for the PostgreSQL
cases; those tests create and remove uniquely named schemas. Run focused tests,
then the security manifest, lint and full suite:

```bash
python -m pytest tests/test_integration_foundation.py tests/test_integration_migration.py tests/test_integration_concurrency.py -q -p no:cacheprovider
bash scripts/run_security_tests.sh
scripts/lint_changed.sh upstream/dev --details
python -m pytest tests -q -p no:cacheprovider
```

Leave the PostgreSQL URL unset for the baseline-comparable full-suite invocation.
These results validate the foundation boundary, not all of Phase 1 or production
API readiness. No production PG evidence fetcher, management UI, audit storage,
shared rate limiting, customer migration or technical execution is introduced.
These are preserved Phase 1A results. Phase 1B-1 results follow below; the next task at that stage was Phase 1B-2 above.


### Phase 1B-1 validation

Validation uses synthetic principals, isolated SQLite databases and a disposable
local PostgreSQL 16 cluster bound to a Unix socket. No real panel, Telegram,
external service or production credential is used. Refreshed `upstream/dev` remains
`ebd27e717b2d95de7b3b5931efedbcf246a02029`, identical to the Phase 1A baseline;
the full-suite baseline is rerun, not copied from the recorded counts.

| Check | Passed | Failed | Skipped | Passed subtests |
|---|---:|---:|---:|---:|
| Focused foundation/management/migration/concurrency/scheduler, PostgreSQL enabled | 132 | 0 | 0 | 6 |
| Security manifest (208 files), PostgreSQL enabled | 2737 | 0 | 0 | 2603 |
| Clean upstream/dev full suite | 5287 | 8 | 31 | 6112 |
| Phase 1B-1 full suite | 5392 | 8 | 50 | 6121 |

The eight full-suite failure IDs match clean upstream/dev exactly: **no new
failures**. They are the same eight unrelated failures listed in the preserved
Phase 1A validation above. The clean baseline remains 5,287 passed / 8 failed /
31 skipped at the same commit; there is no baseline change. The recorded 5,347
passes were the Phase 1A implementation count, not the clean-dev baseline.

Phase 1B-1 adds 45 passing full-suite cases over Phase 1A and ten additional
PostgreSQL-only skips, for 5,392 passed / 8 failed / 50 skipped. All 19 additional
skips relative to clean dev are PostgreSQL-specific foundation/administration
migration and independent-connection cases with the URL deliberately unset.
Every one passes in the separate PostgreSQL-enabled focused/security runs, with
zero skips. SQLite is not treated as PostgreSQL coverage.

The implementation's full suite was completed both in the requested worktree and
in an independent exact-source validation worktree; both returned identical counts
and failure IDs. A byte comparison verified 1,175 source/test/template files in
the isolated snapshot. The temporary validation worktree is removed after checking.

The lint ratchet passes for all 31 changed Python files, with no added findings;
`git diff --check` passes. All 70 local documentation links/anchors are valid.
The customer identity decision and existing foundation migration are byte-for-byte
preserved. Existing Phase 1A service behavior is covered by its unchanged tests
and additional transaction-boundary regression cases.


Additional evidence:

- Actual Alembic chains on both databases: pre-existing foundation -> administration
  upgrade twice -> populated downgrade through `0066` -> re-upgrade twice -> unused
  extension removal -> re-upgrade twice. Existing synthetic wallet and foundation,
  credential, action and audit rows survive their applicable rollback paths.
- Migration tests also cover failed-creation-only history, permanent replay markers
  after audit pruning, repeated empty downgrade and missing-index repair.
- Independent-connection races on both backends cover duplicate creation, issuance
  and rotation, rotation versus revocation, and rollback after injected audit
  failure. The original foundation tests remain passing.
- Real panel tests exercise live signed sessions and password-version/identity
  revocation, customer rejection, missing permissions, CSRF and cross-origin
  rejection, cross-shop/integration targets, immutable scope, invalid/hostile/
  oversized inputs, HTTPS-only issuance, private one-time delivery and safe errors.
- UI rendering was visually checked with synthetic metadata. No browser JavaScript
  was added, so no new Node test is required. The scheduler test's expected job set
  is updated for the new hourly retention job; no test is weakened or skipped.
- Relevant tests are registered in the security manifest. No release version,
  customer/payment/wallet behavior or unrelated cleanup is changed.

Reproduce with Python 3.12 and the existing test dependencies. Set
`PGCLOCK_TEST_POSTGRES_URL` only to a disposable synthetic PostgreSQL database;
the tests create/drop uniquely named schemas. Run:

```bash
python -m pytest tests/test_integration_*.py tests/test_scheduler_background_jobs.py -q -p no:cacheprovider
bash scripts/run_security_tests.sh -p no:cacheprovider
scripts/lint_changed.sh upstream/dev --details
python -m pytest tests -q -p no:cacheprovider
```

Unset the PostgreSQL URL for the baseline-comparable full run. PostgreSQL-specific
checks are run separately with the URL enabled; SQLite success is not substituted
for PostgreSQL evidence. The next task at that stage was Phase 1B-2 above. Its implementation and validation
are recorded below; later-phase gates and exclusions still apply.


### Phase 1B-2 validation (2026-10-21)

Work continues in the requested `integration-api-foundation/PGClockBot` worktree
on `codex/integration-api-phase1b-1`, preserving its uncommitted Phase 1A/1B-1
prerequisites and unrelated changes. No reset, commit, push, PR or version change
is made. Reused Python 3.12 environment and disposable PostgreSQL 16 cluster use
synthetic identities, per-test SQLite files and uniquely named PostgreSQL schemas;
no panel, Telegram, external service or production credential is involved.

| Check | Passed | Failed | Skipped | Notes |
|---|---:|---:|---:|---|
| New focused security/proxy/concurrency/migration tests | 55 | 0 | 0 | Includes 5 PostgreSQL cases; remaining 50 SQLite/pure ASGI/parser cases |
| Directly affected existing regressions | 28 | 0 | 0 | 34 intentionally deselected; 6 additional passing subtests |
| Security manifest (212 files), PostgreSQL enabled | 2793 | 0 | 0 | 2603 additional passing subtests; 13 warnings |
| Lint ratchet | 45 files | 0 | 0 | Passed against upstream/dev; no added findings |
| Full suite and clean-dev comparison | — | — | — | Deferred, mandatory before PR |

The lint ratchet ran once and passed for all 45 changed Python files; `git diff --check`
passed. The security manifest ran once after focused tests passed and includes all
new files. No successful suite was repeated for documentation edits.

The new tests prove exact read/write/private limits and UTC rollover, shared
credentials and rotation, private/read atomicity, no double charge after injected
PG checks, independent-worker admission/audit races, the failure-plus-pending IP
budget, successful-attempt releases, proxy spoofing/malformed/mapped/multi-hop
inputs, raw versus normalized ASGI transport, missing provenance/secret rejection,
safe counter/audit failures, secret exclusion, verified attribution and tenant
isolation, bounded overflow and held-evidence retention. Migration cases exercise
upgrade twice, index repair, unused removal, populated downgrade through the
foundation and re-upgrade with credential/management/security evidence preserved.
SQLite success is not substituted for the 5 separately identifiable PostgreSQL
cases (one connection race and four migration cases).

Affected existing checks cover Bearer-only/HTTPS/origin/query handling, PG transport
failure and revocation/disable rechecks, scheduler startup with and without Telegram,
nonoverlapping security retention and management rollback on audit failure. An
initial sandbox-only regression invocation could not create the scheduler's local
test lock file (2 failures, 26 passes); the authorized worktree-access rerun above
passes all 28. This is not an unresolved application failure. Initial focused
foundation checks also passed all 51 cases; the final security run covers their
current source again. Test clocks are fixed to avoid real-minute-boundary flakes.

Run focused cases with the existing environment:

```bash
python -m pytest tests/test_integration_security.py tests/test_integration_client_ip.py tests/test_integration_security_concurrency.py tests/test_integration_security_migration.py -q -p no:cacheprovider
bash scripts/run_security_tests.sh -p no:cacheprovider
scripts/lint_changed.sh upstream/dev --details
```

`PGCLOCK_TEST_POSTGRES_URL` must point only to a disposable database; an unset URL
skips PostgreSQL cases and is not passing backend coverage. No dependency reinstall,
baseline recreation, broad browser QA or full-suite run is performed. The exact
next phase is **Phase 2 — Customers independent of Telegram**. Remaining limitations
are bounded audit sampling/retention backlog, operator deployment-proxy verification,
no production PG evidence fetcher and no operational API activation. The later
customer compatibility, execution, queue/concurrency, delivery, linking, webhook,
payment and wallet work is excluded from this slice.


### Phase 2A implementation and validation (2026-10-21)

Continued in `integration-api-foundation/PGClockBot` on the existing
`codex/integration-api-phase1b-1` branch, preserving all uncommitted prerequisites
and unrelated user work. Migration head was inspected as 0069; new migration is
`0070_customer_identity`. No commit, push, PR or release version change is made.
The [identity procedure](INTEGRATION_CUSTOMER_IDENTITY.md#9-phase-2a-implementation-and-operating-procedure)
and [contract boundary](INTEGRATION_API_CONTRACT.md#phase-2a-implemented-boundary-2026-10-21)
describe the actual shipped surface, backfill command and retained-schema rollback.

Validation uses the existing Python 3.12 environment, synthetic data, fake panel
clients, isolated SQLite files and uniquely named schemas in the reused disposable
PostgreSQL 16 cluster. No production panel, Telegram or external service is used.

| Check | Passed | Failed / remaining | Notes |
|---|---:|---|---|
| Focused customer/backfill/migration and affected integration/manual matrix, incremental final cases | 181 | 0 remaining | Includes 9 PostgreSQL cases; no backend substitution |
| Direct legacy backfill/registration/import/delivery regression invocation | 93 | 0 | 17 passing subtests |
| Security manifest, one complete run (218 files), PostgreSQL enabled | 2885 | 2 mock-fixture failures, corrected below | 2603 passing subtests; 22 warnings |
| Corrected delivery security fixtures, targeted rerun | 2 | 0 | Existing panel-owner and rollback assertions retained; identity dependency explicitly mocked and asserted |
| Final policy/contact/rollback checks | 8 | 0 remaining | The 3 PostgreSQL fixture failures in this invocation were corrected and covered by the final target run below |
| Final PostgreSQL populated-evidence and invalid-cursor targets | 15 | 0 | 15 intentionally deselected; includes the 2 added upper-bound cursor cases |
| Lint ratchet against upstream/dev | 61 files | 0 | No added findings |
| Full suite / clean-dev comparison | — | Deferred | Mandatory at PR preparation |

The focused matrix count is consolidated across incremental runs, not a claim of
one fresh 181-case invocation: the comprehensive run had 178 passes plus one new
query-count assertion failure. The explicit SQLite write lock raises the bounded
500-user batch budget to seven statements; its corrected assertion passed in a
targeted rerun. Two additional invalid-cursor cases passed in the final 15-case
run. Final annotation/limit-constant edits retain the same valid-input behavior.
The final populated PostgreSQL fixture uses SQLAlchemy to quote the counter's
reserved `window` column; the three added-fixture SQL failures were corrected and
rerun successfully. No application failure remains unresolved in these checks.

The security manifest ran once. Its only two failures were existing mocked
`deliver_order` sessions missing the new adapter-service dependency; those unit
fixtures now mock and assert that call, keeping every original ownership and
rollback assertion. Their focused rerun passes both. The manifest was not rerun
after these fixture-only corrections, and this is **not** a claim of a fresh
all-green full-manifest invocation. Real database constructor, rollback and race
tests cover the identity writes independently.

Actual Alembic chains on SQLite and PostgreSQL prove existing blocked legacy user
and service upgrade from 0069 to 0070, repeat-safe backfill, downgrade to 0069 with
API shutdown and schema retention, and stable IDs/associations after re-upgrade.
Populated migration tests additionally retain website mappings, credential,
management/audit and security counter/evidence rows, legacy wallets and referenced
synthetic financial rows; index repair and both mapping uniqueness constraints
are exercised. Independent-connection cases cover registration equality/conflict,
management disablement while waiting, legacy registration and concurrent backfill.
SQLite registration races explicitly begin WAL transactions. A 500-user batch has
at most seven statements; SQLite EXPLAIN shows a primary-key service range scan
and indexed legacy-adapter lookup. No whole-table service scan or per-row panel
call is introduced. No browser JavaScript changed; no Node run was needed.

The disposable PostgreSQL cluster is stopped after validation. Full-suite and
clean-dev comparison are deliberately deferred until PR preparation; earlier
phase counts above are historical and do not certify this implementation.
Reproduce with the existing environment and a disposable PostgreSQL URL:

```bash
python -m pytest tests/test_customer_identity*.py tests/test_integration_customers.py tests/test_integration_foundation.py tests/test_integration_management.py tests/test_integration_management_pages.py tests/test_manual_user_provision.py -q -p no:cacheprovider
bash scripts/run_security_tests.sh -p no:cacheprovider
scripts/lint_changed.sh upstream/dev --details
```

Do not enable the API or create API-origin services after running the backfill.
The retained 0070↔0069 schema path is tested; older prerequisite downgrades,
arbitrary older application binaries, remaining legacy reader/job compatibility
and canonical state/ownership enforcement are outside this slice.

The exact next task is Phase 2B: inventory/adapt remaining service readers, jobs,
notifications, deletion and panel presentation, verify backfill and dual-write
coverage, then enforce canonical ownership with explicit origin invariants before
considering nullable legacy owners. Phase 2C verified linking remains separate.
No customer HTTP endpoint, API-origin service, execution framework, delivery
secret, payment/wallet flow or public activation is introduced in 2A.


### Continuation checkpoint — Phase 2B / 2C (2026-10-21)

Phase 2B implementation and inventory are recorded in
[service compatibility](INTEGRATION_API_COMPATIBILITY.md). Migration 0071 verifies
backfill before permitting neutral legacy ownership. New focused ownership and
migration tests passed all 9 cases with PostgreSQL enabled; affected regressions
passed 116 tests and 16 subtests. Later canonical-claim refinements await their
next focused run; no final release result is claimed.

Phase 2C services/models and thin Telegram adapters are implemented in progress.
A focused linking/customer run passed 44 tests with no failures. Independent
connection races, migration replay, bot adapter checks and the shared security
checkpoint remain pending. Next: finish these gates, then implement Phase 3.
Public operational routers remain unmounted. Phases 3–8 are not complete.


### Autonomous continuation checkpoint (2026-10-21)

Work remains on `codex/integration-api-phase1b-1` in the requested foundation
worktree. No commit, push, PR, deployment or production activation has occurred.
All earlier prerequisites and historical Phase 2A evidence remain preserved.

- Phase 2B compatibility and Phase 2C linking are implemented; see the focused
  compatibility inventory. Migration 0071 validates backfill before nullable bot
  ownership, 0072 retains dual-proof consent, and 0073 adds shared HTTP key bindings,
  stable catalog IDs and explicit service quota/freshness snapshots.
- Phase 3 read/customer/link HTTP routes are implemented behind the new
  `INTEGRATION_API_ROUTES_ENABLED=false` registration gate and the existing disabled
  database switch. Principal client construction defers authentication until after
  DB reads close. Lists use local snapshots, strict numeric validation and signed
  tenant/customer/filter-bound cursors; detail refresh is bounded and scoped again.
  Catalog currently supports reproducible explicit-group custom-create plans;
  mutable template plans and unsupported naming semantics are excluded, not guessed.
- Focused read/bot/link/migration exit invocation: 37 passed; catalog/migration/
  PostgreSQL linking-race/PG-evidence invocation: 23 passed; API/service invocation:
  21 passed; linking/shared HTTP binding invocation: 27 passed. These are distinct
  incremental invocations with overlap, not a consolidated fresh-suite count.
- The shared security checkpoint ran all 225 files: 2,935 passed, 5 failed, 2,603
  subtests passed. Fixes then passed a targeted 24-case invocation: generic nullable
  FK purge now preserves canonical origin rules and refuses consent-history erasure;
  two mock fixtures explicitly include the new ownership fields; old actual-chain
  tests now upgrade to 0070, backfill, then continue to head. No fresh all-green
  security manifest is claimed. An affected foundation/customer invocation passed
  102 tests. Final full suite and the final security manifest remain required.
- A later client/hourly-limit/catalog invocation had one short synthetic unlink-key
  fixture error (30 passes); the corrected hourly case passes in the operation
  invocation below. No application failure from that invocation remains.
- Phase 4 is in progress: 0074 and durable operation/reference/audit/guard/capacity
  models, atomic acceptance, scoped polling, leases, saved targets, intent/sent
  markers and conservative recovery are implemented. Durable API commitments now
  enter shared volume and user-slot gates. Six focused operation/hourly tests pass,
  including independent SQLite and PostgreSQL acceptance/claim races. Native-panel
  mutation fencing, worker execution, migration and affected-regression gates are
  still pending. Do not mark Phase 4 or execution ready yet.

Next: finish shared native/API fences and durable accounting validation; then
continue Phases 5–8 consecutively. Final release preparation must distinguish
implemented/local-fake evidence from unperformed actual-panel/deployment checks.
The disposable PostgreSQL cluster is currently running on its local Unix socket
for synthetic tests. Reused Python environment: `/private/tmp/pgclock-phase1a-venv`.


### Release-candidate implementation checkpoint (2026-10-21)

Phases 2B/2C and 3–7 now have supported local implementations. 0074 persists
permanent technical intents and saved targets, one-use dispatch/lease fencing,
shared native guards and durable capacity (including the saved PG account identity).
0075 stores encrypted private delivery and explicit pack handles. 0076 stores
transactional event identities, destination versions, encrypted isolated signing
keys, attempts, monotonic consent-event versions and protected operator actions.
All are frozen additive migrations with retaining downgrades. Existing prerequisites
and uncommitted user work remain preserved.

Execution reuses current own-principal PG evidence, billing, quota and allocation
checks without holding DB transactions across network calls. Native API-resource
quota/reset/revoke/delete/transfer actions are refused; billing status changes use
separate durable guards and verification. Unknown API effects only reconcile by
reading the saved marked target. Native uncertainty remains held without a
force-clear action. Explicit-group plans, strict configured-origin subscription URLs
and exact integer pack amounts define the conservative supported subset. See the
release guide for remaining actual-panel and deployment gates.

Incremental evidence after the prior checkpoint (overlapping invocations, not sums):

| Invocation | Actual result |
|---|---|
| Provisioning/operations | 14 passed |
| API/private/auth affected | 43 passed |
| Native guard/read affected | 15 passed |
| Renewal/add-on affected initial run | 66 passed, 4 failures; three detached test-fixture accesses and one introduced Persian unlimited-error regression |
| Corrected renewal/on-hold cases | 16 passed, 20 subtests passed |
| Event/read/link checkpoint without PG URL | 36 passed, 5 PostgreSQL cases skipped |
| Webhook/execution/management affected | 51 passed |
| Private reconciliation/scheduler/native affected | 32 passed |
| Populated operation/delivery/outbox migrations, actual chain rollback and claims with PG enabled | 6 passed |
| Initial outbox backend matrix | 5 passed, 1 SQLite stale-ORM timestamp comparison failure |
| Corrected lock-order/outbox/claim/own-quota/private/read matrix with PG enabled | 23 passed |
| Control-page original invocation | 56 passed; inherited/imported base cases then removed from duplicate collection |
| New control cases after fixture deduplication | 2 passed |
| New local acceptance policy/terms cases and affected execution/control cases | 18 passed |
| Real execution API/schema/private/renewal cases | 20 passed |
| Lint checkpoint | 156 changed Python files; ratchet passed |

The own-quota unit fixture now explicitly supplies zero durable slots through an
async scalar mock; its original own-client/no-owner-fallback assertions remain.
The last consolidated integration/identity/link matrix and final security/full-suite
results are pending below. No new all-green security result is claimed until that
run completes. The clean-dev baseline remains revision
`ebd27e717b2d95de7b3b5931efedbcf246a02029`, Python 3.12 in the reused environment:
5,287 passed / 8 known failures / 31 skipped / 6,112 subtests. Its historical log is
`/private/tmp/pgclock-phase1b-clean-baseline.log`; the eight failure IDs above remain
the comparison set. Run the comparable final full suite with the PostgreSQL URL
unset, and report the separate PG-enabled matrix rather than mislabeling skips.

Next: finish consolidated focused/security/lint/full-suite verification, compare
failure IDs with the unchanged baseline, record exact counts and stop the disposable
PostgreSQL cluster. No commit, push, PR, version bump, deployment or activation.


### Final local release preparation — 2026-10-22

Phases 2B, 2C and 3–8 are complete for the conservative supported v1 subset in
[the release operating guide](INTEGRATION_API_OPERATIONS.md). Contracts, the
17-path OpenAPI artifact, generic backend/polling/signature examples and the
migration/shutdown/rollback guidance describe the implemented boundary. The
actual-panel and deployment gates remain unperformed; this checkpoint does not
authorize or certify activation. The implementation remains uncommitted in the
requested foundation worktree on `codex/integration-api-phase1b-1`.

Final verification invocations are distinct and overlap; do not sum them:

| Invocation | Actual result |
|---|---|
| Consolidated integration/identity/linking matrix, PostgreSQL enabled | 407 passed, 6 subtests, 9 warnings, 96.64s; SQLite and isolated PostgreSQL cases enabled |
| Final security manifest, 242 files | 3,040 passed, 2,603 subtests, 23 warnings, 691.15s |
| Supplementary queue-expiry and generic-backend HTTP/fake-panel/private URL/QR scenario | 2 passed after correcting a mock return value; collected after the security invocation and included in the corrected full suite |
| Legacy fixture corrections and affected API/worker/backend/migration cases | 33 passed, 1 PostgreSQL case skipped with its URL unset, 5 warnings, 5.95s |
| Deterministic add-on privacy assertion, fixed public handle containing the test price digits | 4 passed, 1 warning, 3.18s |
| Corrected final full suite, PostgreSQL URL unset for baseline parity | 8 failed, 5658 passed, 73 skipped, 32 warnings, 6121 subtests passed in 932.36s (0:15:32) |
| Clean-dev full baseline, same revision/environment | 8 failed, 5287 passed, 31 skipped, 20 warnings, 6112 subtests passed in 933.39s (0:15:33) |
| Final lint ratchet | 162 changed Python files; passed against unchanged `upstream/dev` |
| Final whitespace check | `git diff --check` passed |
| OpenAPI regeneration | Byte-for-byte identical to the saved artifact |

The corrected full suite has exactly the same eight failing test IDs as the
clean-dev baseline at `ebd27e717b2d95de7b3b5931efedbcf246a02029`: no added
failures and no baseline failures relabeled as fixed. Their IDs remain listed
in the earlier baseline evidence above. Full-suite PostgreSQL skips are not
PostgreSQL passes; the separate enabled matrix supplies that local evidence.
The baseline Python environment remains `/private/tmp/pgclock-phase1a-venv`.

The first final full invocation had 5,653 passed / 11 failed / 73 skipped /
6,121 subtests (973.47s). Eight failures matched the baseline. Three additional
fixture failures were corrected without weakening their behavior assertions:
the block-keyboard fixture mocks canonical synchronization and asserts its call;
the eager-load fixture explicitly marks its service as legacy; the populated
gift-code migration fixture upgrades to 0070, backfills canonical identity, then
continues to head. Original keyboard, eager-load, gift/usage and repeat-upgrade
assertions remain. Transient automatic approval-review timeouts were resolved;
the user explicitly approved the fixture edits. The subsequent focused run and
corrected full suite above validate the applied worktree. An intermediate repeat
had 5,657 passed / 9 failed / 73 skipped / 6,121 subtests (938.19s): the three
fixtures passed, but the add-on privacy test falsely matched price digits `777`
inside a random public handle. The test now uses a fixed handle containing those
digits and asserts the exact allowed response fields. Its four affected cases
passed, and the final full run above supersedes that intermediate result. This
does not rewrite
the earlier Phase 2A or five-failure security checkpoint as green.

Evidence logs: `/private/tmp/integration-final-focused.log`,
`/private/tmp/integration-final-security.log`,
`/private/tmp/integration-expiry-e2e-repaired.log`,
`/private/tmp/integration-final-repairs.log`,
`/private/tmp/integration-addon-privacy-corrected.log`,
`/private/tmp/integration-final-full-verified.log`,
`/private/tmp/integration-final-comparison-verified.json`,
`/private/tmp/integration-final-lint.log`. Temporary logs are local verification
evidence, not source files or production artifacts. The disposable PostgreSQL
cluster was confirmed stopped after its synthetic tests; no actual deployment
or production database was involved. Browser JavaScript was unchanged.

All route/execution/webhook flags and the database API switch default off. No
commit, push, PR, release-version change, deployment or production activation
occurred. No real panel/payment/customer credentials or Telegram/webhook sends
were used. Remaining gates are the controlled actual-PG behavior, own-principal
credentials, late-request/proxy semantics, subscription format, TLS/DNS/egress,
evidence backup and worker lifecycle checks in the operating guide. Mutable
templates, alternate subscription origins, delivery rotation, ambiguous native
reapplication and destructive evidence cleanup remain outside the supported v1.
Next action is external release verification under separate authorization; no
further authorized local implementation or verification remains.


### Focused pre-PR review and repairs — 2026-10-22

This follow-up reviewed selected high-impact paths in the actual uncommitted
implementation, including untracked files. It is a focused source/behavior review,
not an exhaustive audit or actual-panel certification. No additional capabilities
or broader panel/URL support were introduced.

| Severity | Trigger and incorrect behavior | Repair and regression evidence |
|---|---|---|
| P2 — fixed | 0071 reused `columns` for index definitions, overwriting the original column inventory. SQLite batch recreation then skipped restoration of the existing lower-case PG username index. | Preserve the inventory with `index_columns`. A behavioral database test verifies indexed lookup across repeated upgrade, retained downgrade and re-upgrade; PostgreSQL verifies the existing expression index is retained. |
| P2 — fixed | Running legacy service backfill after API services existed scanned a valid neutral-owner row and raised a legacy-identity conflict, blocking later legacy rows. | Restrict the bounded keyset query to legacy bot owners. A mixed legacy/web/legacy fixture verifies resume cursors, repeat safety, canonical adapters and byte-for-byte unchanged website row values on SQLite and PostgreSQL. |
| P2 — documentation corrected | Main Phase 2 summaries still said “2B — next,” incomplete Phase 2, non-null ownership and absent customer routes. The normative contract also promised scheduled heartbeats, reapplication/force-failure transitions and a different on-hold warning name. | Current summaries now distinguish the 0070 historical boundary from implemented 0071 ownership and default-off route registration. The contract describes bounded non-heartbeating execution, read-only reconciliation and `activates_on_hold`; original failed runs and targeted repairs remain historical evidence. |

Verified repair locations: `alembic/versions/0071_service_channel_ownership.py:60`
and `app/services/customer_backfill.py:55`. Regression tests are in
`tests/test_integration_ownership_migration.py::test_ownership_upgrade_preserves_username_lookup_index`
and `tests/test_customer_identity_concurrency.py::test_legacy_backfill_skips_website_services_and_resumes_past_them`.
Revision 0071 is still untracked/unmerged in this candidate, so its correction does
not rewrite a migration already released on dev/main. No new revision was allocated.

The focused review inspected the authority/owner/credential resolution and proxy
boundary; scoped customer/service/operation and private-delivery lookups; target,
claim, dispatch, capacity and read-only reconciliation flow; production panel
mutation fencing; canonical backfill and ownership migration; shared allocation
and evidence-retention changes; event signing/retries and the numeric-IP TLS
transport. No further material runtime defect was substantiated within those
paths. Existing bot/financial behavior outside affected paths was not exhaustively
audited. Website login/payment/refund ownership and single-panel scope remain.

Evidence reuse is explicit. The reported 5,658-pass/eight-baseline-failure full
log, 407-pass PG-enabled matrix, 3,040-pass security log and 162-file lint log were
read and verified against the unchanged base revision/environment. No runtime/test
file at review entry was newer than the prior full-suite log. Because the candidate
is uncommitted and no historical content manifest exists, timestamps/HEAD alone
are not cryptographic proof of that old snapshot. Review-entry hashes were saved
locally at `/private/tmp/integration-pre-review-snapshot.json`; current runtime/test
hashes at `/private/tmp/integration-review-runtime-snapshot.json`. The migration,
backfill and their regression changes supersede the old full-suite evidence for
final files; the old 407-case matrix remains historical and is supplemented by
new affected SQLite/PostgreSQL verification.

New verification before the final broad runs:

- Two new SQLite regressions failed against the original code for the documented
  index-loss/backfill-conflict reasons; no assertion was weakened.
- Initial repaired SQLite/affected invocation: 26 passed, 3 PG cases skipped.
- Final affected identity/backfill/ownership/actual-chain/operation-migration matrix
  with PostgreSQL enabled: **55 passed**, 11 warnings, 14.09s, no skips.
- Existing security manifest entries already include both regression files.
- Routes/schemas were unchanged, so the previously byte-identical OpenAPI export
  was retained; no redundant regeneration or browser QA was performed.

Final verification, after all runtime and regression changes:

| Check | Actual result |
|---|---|
| Security manifest, 242 files, PostgreSQL enabled | 3048 passed, 25 warnings, 2603 subtests passed in 673.81s (0:11:13) |
| Final full suite, PostgreSQL URL unset for baseline parity | 8 failed, 5660 passed, 75 skipped, 35 warnings, 6121 subtests passed in 1063.31s (0:17:43) |
| Reused clean-dev baseline, unchanged revision/environment | 8 failed, 5287 passed, 31 skipped, 20 warnings, 6112 subtests passed in 933.39s (0:15:33) |
| Failure-ID comparison | Exactly the same eight failures; zero added or removed baseline failure IDs |
| Lint ratchet | 162 changed Python files passed |
| Whitespace | `git diff --check` passed |

The final runtime/test/migration fingerprint (SHA-256 of sorted path/NUL/file-digest
records for 162 changed/untracked Python files) is
`aa1a51f831856a6379a4eb9f15ee49b9f2ebb094fbd1b588f5e23b95e5fffa36`. Contents were checked unchanged after final verification.
Logs are `/private/tmp/integration-review-focused.log`,
`/private/tmp/integration-review-security.log`, `/private/tmp/integration-review-full.log`,
`/private/tmp/integration-review-lint.log`; comparison and fingerprint are in
`/private/tmp/integration-review-comparison.json`. The final full invocation covers
the repaired files; prior full/security counts are retained as historical evidence.
Full-suite PG skips are not backend passes; the 55-case affected matrix and fresh
PG-enabled security manifest supply the current PostgreSQL evidence.

The original “PostgreSQL stopped” observation was made inside the restricted
sandbox and was not reliable process/socket evidence. This review checked with
escalated local access and found the disposable server running; it reused that
server's isolated synthetic schemas. Final cleanup must use the same escalated
status check. No production database or external panel/webhook/Telegram service
was used. Default-off controls and all actual-panel/proxy/URL/TLS/DNS/egress/backup/
worker-lifecycle release gates remain as documented in the operating guide.


The disposable PostgreSQL cluster was stopped after the PG-enabled runs and its
stopped state was confirmed with escalated local status access. No commit, stage,
push, PR, version change, deployment, feature activation or real notification
occurred. All prerequisites and unrelated user changes remain preserved.

#### Historical reviewed PR draft — target `dev`

Title: `feat: add scoped Integration API with durable technical operations`

Web backends can register customers without Telegram and request scoped technical
services on the existing PasarGuard panel. Websites retain login, retail pricing,
payments and refunds. Add current principal/shop/integration/customer authorization,
dual-proof Telegram linking, bounded reads, durable saved-target provision/renew/
add-on operations, encrypted private URL/QR delivery and signed durable webhooks.
Committed guards/capacity and one-use dispatch prevent blind repeated writes;
unknown effects reconcile by reads only and retain evidence.

Migrations 0067–0076 add identity, credential/audit/replay, ownership, snapshots,
operations, encrypted delivery and outbox records. Existing installs must upgrade
to 0070, run canonical backfill from zero, then upgrade to head. Downgrades retain
populated evidence and require a compatibility-aware binary. Pre-PR repairs retain
the SQLite username index and allow legacy backfill alongside website services.

New `integrations_manage`/`integration_*` feature grants cover catalog, customers,
service lifecycle, private access, operations/reconciliation, Telegram linking and
webhook management; no new default permission is granted.
`INTEGRATION_API_ROUTES_ENABLED`, `INTEGRATION_EXECUTION_ENABLED`,
`INTEGRATION_WEBHOOKS_ENABLED` and DB `integration_api_enabled` default off. Proxy trust is separately
configured with `INTEGRATION_TRUSTED_PROXIES`; website entry, grants and allowlists
remain independent controls.

Validation: affected SQLite/PostgreSQL matrix **55 passed**; security manifest
**3048 passed, 25 warnings, 2603 subtests passed in 673.81s (0:11:13)**; full suite **8 failed, 5660 passed, 75 skipped, 35 warnings, 6121 subtests passed in 1063.31s (0:17:43)**, with exactly the same eight
failures as clean dev (**5,287 passed / 8 failed / 31 skipped**). Lint/whitespace
pass. The earlier 407-case matrix is reused historical evidence, supplemented by
the fresh affected backend run; it is not a new invocation.

V1 supports reproducible explicit-group plans, allowed naming/activation terms,
configured HTTPS subscription origin/path and exact integer add-on amounts.
Mutable templates, alternate delivery origins/rotation, ambiguous native
reapplication, destructive evidence cleanup and customer financial flows are out
of scope. Actual PG schema/write/response/URL behavior, proxy retry/late-request
semantics, TLS/DNS/egress, evidence backups and worker lifecycle checks are
**deferred / not performed** for this local handoff and remain required before any
future authorized activation. Staging deployment and real Telegram/webhook deliveries
are also deferred / not performed. This candidate is locally verified, is not
production-verified and remains disabled; runtime live-evidence requirements remain
in force.


### Review submission and current-target integration — 2026-10-25

The reviewed candidate was preserved in commit `bad2eedf`. Its 162 Python files
matched the recorded runtime/test/migration fingerprint exactly:
`aa1a51f831856a6379a4eb9f15ee49b9f2ebb094fbd1b588f5e23b95e5fffa36`.
The 5,660-pass full suite, 3,048-pass security manifest and 55-pass backend matrix
therefore remain valid evidence for that original candidate. They are historical
evidence, not verification of the subsequently integrated runtime.

Refreshed upstream `dev` is `ee7de1acbf8690caf1f462ddb2104864fcbf6e11`.
Its runtime changes and four newly merged migrations required preserving both
histories and refreshing the clean-dev baseline. Three additive merge conflicts
retained upstream scheduler imports, Mini App permission and security entries
alongside all integration entries. No merged upstream migration was edited.
Only the unmerged feature revisions and their active references were renumbered:

| Original unmerged revision | Final revision |
|---|---|
| 0067 foundation | 0071 integration foundation |
| 0068 administration | 0072 integration administration |
| 0069 security | 0073 integration security |
| 0070 identity | 0074 customer identity |
| 0071 ownership | 0075 service channel ownership |
| 0072 consent | 0076 customer Telegram links |
| 0073 reads | 0077 integration read snapshots |
| 0074 operations | 0078 integration operations |
| 0075 private delivery | 0079 integration private delivery |
| 0076 outbox | 0080 integration event outbox |

The new upstream paginated customer-service reader now uses the existing
`legacy_service_clause`, as the list and search readers do, so canonical-owner
mismatches cannot appear in Mini App pages. Its regression first failed against
the merged reader, then verifies canonical ownership, shop/customer isolation and
filtered pagination with the repair. The security manifest already includes its
regression module. This is compatibility work required by target integration;
no new capability, dependency or activation bypass was added.

Historical submission procedure, superseded by the 2026-10-26 startup repair:
that candidate required an intermediate 0074 upgrade and explicit backfill.
Current installations use ordinary `init_db` / `upgrade head`; 0075 performs the
bounded backfill automatically. Retaining downgrade and a compatibility-aware
binary remain mandatory. Historical checkpoints above use the original numbering.

Final verification after all runtime, migration and regression changes:

| Newly executed check | Result |
|---|---|
| Initial affected SQLite invocation, before pagination repair | 57 passed, 13 PostgreSQL skips, 35 subtests; superseded by the enabled matrix below |
| Affected SQLite/PostgreSQL migrations, retained rollback, ownership and concurrency | 73 passed, 11 warnings, 28 subtests, 32.46s; no skips |
| Security manifest, all 282 files, PostgreSQL enabled | 3393 passed, 33 warnings, 2791 subtests passed in 1020.49s (0:17:00) |
| Full suite, PostgreSQL URL unset for baseline parity | 11 failed, 6008 passed, 76 skipped, 42 warnings, 6331 subtests passed in 1283.61s (0:21:23) |
| Refreshed clean-dev baseline, same Python 3.12 environment | 11 failed, 5634 passed, 32 skipped, 26 warnings, 6322 subtests passed in 1093.78s (0:18:13) |
| Failure-ID comparison | Exactly the same 11 IDs; zero introduced failures |
| Lint ratchet against refreshed upstream/dev | 162 changed Python files passed using recorded environment Ruff 0.17.1 |
| Whitespace | `git diff --check upstream/dev` passed |
| OpenAPI regeneration | Byte-for-byte identical, 17 paths |

Invocations overlap; their counts must not be added. Full-suite PostgreSQL skips
are not backend passes. The enabled affected matrix and security run provide the
final PostgreSQL evidence. The original 5,660-pass/eight-failure full suite,
3,048-pass security manifest and 55-pass backend matrix were fingerprint-verified
and reused only as historical original-candidate evidence. The new broad runs
supersede them for the integrated runtime.

All eight original baseline failures remain. Refreshed dev itself adds these
three pre-existing documentation/UI source-check failures:

- `tests/test_guide_help_docs.py::GuideCatalogTests::test_page_title_macro_has_help`
- `tests/test_loyalty_page_wheel_context.py::PanelStatusPageTests::test_app_uses_branded_status_not_inline_tahoma`
- `tests/test_panel_ui_guards.py::FieldHintAndPlaceholderTests::test_settings_field_help_after_control`

The complete current baseline/candidate failure set is:

- `tests/test_bot_ui_default_texts.py::DefaultTextSnapshotTests::test_default_texts_match_reviewed_snapshot`
- `tests/test_gift_code_rules.py::GiftCodeRulesTests::test_delete_buyer_with_reserved_and_consumed_codes`
- `tests/test_guide_help_docs.py::GuideCatalogTests::test_page_title_macro_has_help`
- `tests/test_home_overview.py::HostMetricsTests::test_cpu_percent_samples`
- `tests/test_home_overview.py::HostMetricsTests::test_host_metrics_shape`
- `tests/test_home_overview.py::HostMetricsTests::test_memory_stats_linux`
- `tests/test_loyalty_page_wheel_context.py::PanelStatusPageTests::test_app_uses_branded_status_not_inline_tahoma`
- `tests/test_panel_ui_guards.py::FieldHintAndPlaceholderTests::test_settings_field_help_after_control`
- `tests/test_perf_and_correctness_2_5_8.py::ShopOrderAttributionTests::test_create_order_uses_shop_context_not_sticky_reseller`
- `tests/test_service_quota_cache_list_9_0_3.py::AdminExtendCachesQuotaTests::test_extend_writes_quota_cache`
- `tests/test_service_quota_cache_list_9_0_3.py::AdminExtendCachesQuotaTests::test_partial_modify_response_still_caches_kwargs`

No existing assertion was weakened, skipped or removed. The pagination regression
first failed without the repair. An initial PostgreSQL-enabled attempt encountered
SQL_ASCII driver setup errors (8 failed, 17 errors); the final matrix used a
separate UTF-8 disposable database. The first lint attempt selected host Ruff
0.14.10 and reported fixture-reexport F811 findings; the final ratchet used Ruff
0.17.1 from the original verification environment, without suppressions or code
changes. These attempts are not relabeled as successful verification.

The final runtime/test/migration fingerprint for 162 Python files is
`e62ba6de3a619cb48e64b4226fbf5ba3433d9efb3e007e91f4d6f839fa6d1dc5`. It remained unchanged throughout final
verification; the later completion record changes documentation only. Both
upstream migration contents and release version/release-note files remain
identical to refreshed dev. The complete security manifest retains every entry
from both the reviewed candidate and current dev.

Local logs are `/private/tmp/integration-submit-affected-verified.log`,
`/private/tmp/integration-submit-security.log`, `/private/tmp/integration-submit-full.log`,
`/private/tmp/integration-submit-baseline.log`, and
`/private/tmp/integration-submit-lint-verified.log`; the exact comparison is
`/private/tmp/integration-submit-comparison.json`. Logs, test databases and local
tooling are not Git artifacts. Both disposable PostgreSQL clusters were confirmed
stopped with escalated local process access after verification.

The review submission preserves the original checkout and all feature
prerequisites, uses the existing fork and targets upstream dev as a draft.
Only generic integration behavior is included. No private website specification,
secrets, temporary output, new dependency, release bump, deployment, activation or
real-panel/staging/external-delivery check is included. All external gates remain
**deferred / not performed**, and runtime live-evidence requirements remain intact.

## Review revision 2026-10-26

This checkpoint supersedes the manual-upgrade prerequisite and combined-PR review
boundary above. See the [six-part review sequence](INTEGRATION_API_REVIEW_SEQUENCE.md).
No feature was activated and no production bot/panel was contacted.

The startup repair automatically backfills canonical owners during migration 0075,
including bounded bulk parameters on SQLite. A populated 0070 fixture reaches head
through the actual `init_db` path without fallback or a CLI step, on both backends,
and repeats/downgrades/re-upgrades without changing legacy financial/service data.
The old candidate fails this regression. A separate restored 0067 PostgreSQL backup
also reproduces the old failure and passes ordinary fixed startup with unchanged
legacy row fingerprints. The archived backup and runtime files remain outside Git.

Optional native fencing/adapters and fresh workers now do no integration I/O while
disabled. The cold platform checkout records the same 22 statement signatures and
one identical POST as clean dev. A permanent history marker keeps prior uncertain
capacity counted after disable/restart. Supported committed platform settings
writes refresh the process switch; shop overrides and unrelated writes do not.

Intermediate branches retain only their available behavior and dependencies;
operation/event/management/example tests are added with the corresponding part.
The assembled final candidate retains the original assertions. The operation
snapshot initially included outbox/retention schedule checks before those workers
existed; those new checks now arrive with the workers in part 4. A separate test
commit fixes the newly added checkout trace's assumption about independent eager
read ordering, without changing query counts, mutation ordering or the request
payload. No existing baseline assertion was weakened to pass the series.

Final review code is based on refreshed `upstream/dev`
`d250a5736d267a1363d08de09030a4a1d0d00ee1` (0.5.4). Published branches
incorporate this through ordinary merges. No upstream migration was added after
0070, and all upstream version/release-note files remain unchanged by the series.
Both upstream and candidate security-manifest entries are retained. A post-merge
import placement error was caught during collection and repaired before the
clean final verification below; that rejected collection is not a successful run.

| Check at the final review source | Result |
|---|---|
| Current dev full-suite baseline | **5,647 passed, 32 skipped, 11 failed**; 6,337 subtests |
| Foundation `f2c2e0bf` full suite | **5,675 passed, 59 skipped, 11 failed**; 6,337 subtests |
| Foundation security manifest | **2,996 passed, 27 skipped**; 2,796 subtests |
| Assembled series `fa707267` full suite (four isolated shards) | **6,040 passed, 78 skipped, 11 failed**; 6,346 subtests |
| Assembled security manifest | **3,412 passed, 46 skipped**; 2,810 subtests |
| Exact failure-ID comparison with current dev | **Identical to current dev: zero added failures** |
| SQLite/PostgreSQL migration and concurrency matrix | **89 passed**, no skips; includes populated `init_db`, repeated upgrades/retaining downgrades, the SQLite 999-variable regression and seven concurrent identity/operation/consent/outbox matrices |
| Disabled checkout/settings, merged user-search/numeric-ID notification behavior, management and API contracts | **56 passed**, 15 subtests |
| Lint ratchet / whitespace | Passed: **25** foundation and **166** assembled changed Python files |
| Generated OpenAPI | **17 paths**, exact export comparison unchanged |
| Queued snapshot matrices: read `40a6dd6a` / operations `ad0f47e9` / outbox `cd0390fc` / management `facae44b` | **183 passed, 3 skipped** / **194 passed, 8 skipped, 8 subtests** / **73 passed, 4 skipped** / **51 passed, 5 skipped, 6 subtests** |
| Disabled trace across independent hash seeds | **13 passed** on each of seeds 0, 1, 2, 42 and 1234 |
| GitHub CI for foundation `f2c2e0bf` | All **6 checks successful**: Linux amd64/arm64, PostgreSQL, security, Python 3.10 and 3.11 |

Foundation and dev use `python -m pytest tests -q -p no:cacheprovider`;
security uses `bash scripts/run_security_tests.sh`. The assembled full suite uses
four disjoint sets of test modules in independent clean checkouts, each run with
`pytest -q -p no:cacheprovider`. Its collected inventory contains exactly 6,129
unique test IDs, assigned once across the four shards. They run in separate clean
checkouts with `PGCLOCK_TEST_POSTGRES_URL` unset for baseline parity. Database
skips in those broad runs are not claimed as PostgreSQL passes. The explicit
89-case matrix sets that variable to a disposable local UTF-8 PostgreSQL database;
all panel/Telegram/webhook clients in tests remain fake. Focused matrices overlap
and must not be added to the broad counts. Python is 3.12 with the same pinned
requirements and Ruff 0.17.1 in all compared environments.

Earlier completed verification against dev `88acca8a`, before its 0.5.4 changes,
recorded foundation **5,668 passed / 59 skipped / 11 failed** and **2,991 security
passes / 27 skips**; assembled **6,033 passed / 78 skipped / 11 failed** and
**3,407 security passes / 46 skips**. Both had exactly the same 11 failure IDs as
that clean dev baseline (**5,640 passed / 32 skipped / 11 failed**). Those results
belong to their frozen earlier sources; the table above records refreshed-dev
verification separately.

The disabled cold checkout benchmark improved from the original combined
candidate's **26 SQL statements / one panel POST** to **22 / one POST**, matching
clean dev's statement action/table signatures and exact POST payload. Only the
adjacent plan/user SELECT pair is normalized: SQLAlchemy may eager-load those
independent relationships in either order. All other reads, writes and transaction
positions remain exact, and the count stays 22. Additive ORM
columns naturally differ. This is checked for each of the three inactive flag
combinations, and no integration session is opened for direct native
create/modify/delete or fresh inactive workers. Retained unknown-outcome capacity
is deliberately still counted after a previously active installation is disabled.

The unchanged current-dev failures are:

- `tests/test_bot_ui_default_texts.py::DefaultTextSnapshotTests::test_default_texts_match_reviewed_snapshot`
- `tests/test_gift_code_rules.py::GiftCodeRulesTests::test_delete_buyer_with_reserved_and_consumed_codes`
- `tests/test_guide_help_docs.py::GuideCatalogTests::test_page_title_macro_has_help`
- `tests/test_home_overview.py::HostMetricsTests::test_cpu_percent_samples`
- `tests/test_home_overview.py::HostMetricsTests::test_host_metrics_shape`
- `tests/test_home_overview.py::HostMetricsTests::test_memory_stats_linux`
- `tests/test_loyalty_page_wheel_context.py::PanelStatusPageTests::test_app_uses_branded_status_not_inline_tahoma`
- `tests/test_panel_ui_guards.py::FieldHintAndPlaceholderTests::test_settings_field_help_after_control`
- `tests/test_perf_and_correctness_2_5_8.py::ShopOrderAttributionTests::test_create_order_uses_shop_context_not_sticky_reseller`
- `tests/test_service_quota_cache_list_9_0_3.py::AdminExtendCachesQuotaTests::test_extend_writes_quota_cache`
- `tests/test_service_quota_cache_list_9_0_3.py::AdminExtendCachesQuotaTests::test_partial_modify_response_still_caches_kwargs`

Per-file SHA-256 checks cover all **166** changed Python sources. The frozen full
and security checkouts match the final assembled source; final follow-ups change
documentation only. Local evidence is outside Git:
`/private/tmp/pgclock-pr967-refreshed-*.log`,
`/private/tmp/pgclock-pr967-refreshed-source-fingerprint.json`, and
`/private/tmp/pgclock-pr967-refreshed-verification.json`. The earlier completion
logs and comparison remain separate and are not relabeled as refreshed runs.


Actual-panel and test-bot evidence remains unavailable. Restored-data and fake-panel
checks do not establish live panel writes, deliveries or deployment behavior.


## Open dependent PRs 2026-10-26

The operator requested opening the entire review sequence now. This supersedes the
earlier choice to wait before creating each later PR. Every PR still targets dev:
[#971](https://github.com/Mrclocks/PGClockBot/pull/971) is ready for review;
[#978](https://github.com/Mrclocks/PGClockBot/pull/978),
[#979](https://github.com/Mrclocks/PGClockBot/pull/979),
[#980](https://github.com/Mrclocks/PGClockBot/pull/980),
[#981](https://github.com/Mrclocks/PGClockBot/pull/981), and
[#982](https://github.com/Mrclocks/PGClockBot/pull/982) are Draft and depend on the
previous PR in this list. The combined #967 remains Draft. After each prerequisite
merges, incorporate dev into the next branch without force-pushing, verify it,
and mark that next PR ready. No automatic merge or runtime activation is included.

All branches incorporated dev `98749f49c8e28b954559694d88f15a408487bea6` (0.5.5)
through ordinary merges before the new Drafts were opened. Upstream migration head
remains 0070; feature migration numbering is unchanged. Release/version files match
dev and were not bumped by this feature. Current validation after that sync:

- Foundation `dd915c09`: **106 passed, no skips, 19 subtests**, including the
  SQLite/PostgreSQL migration/startup matrix, imported-service deletion and
  inherited Mini App branding/frontend behavior; 25-file lint ratchet passed.
- Assembled `256d43dd`: **73 passed, 19 subtests**, covering disabled checkout,
  settings, API/example contracts, management, scheduler and inherited branding;
  166-file lint ratchet passed. The inherited Mini App brand Node test passed
  all **8 checks**.

The completed full/security/CI counts in the previous checkpoint belong to their
recorded pre-0.5.5 sources. They have not been relabeled as a new full/security run
on these merged heads. Current-head CI runs on the published PRs; before each
later Draft is marked ready, recheck its own validation against the merged dev.
The original checkout, restored local preview and production bot/panel were not
changed by this PR-publication step. Real test-bot/PasarGuard acceptance remains
unperformed because staging is unavailable.


## Publication review follow-up

All six replacement branches now incorporate upstream dev
`55286d733f7a1b0a16641b0e6289c5f6f137fe85` (0.5.6) through ordinary merges.
The combined #967 is closed as superseded. #971 remains ready for review;
#978–#982 remain dependent Drafts. Review/merge order and the original-concern
checklist are in the [review index](INTEGRATION_API_REVIEW_SEQUENCE.md).

Migration 0071 now retains empty foundation tables when surviving tables still
reference them. Previously, a retaining downgrade could leave `user_services`
with a foreign key to a dropped `integrations` table and reject a new legacy
service insert. The regression covers repeated downgrade/re-upgrade, retained
rows and FK-enforced writes. The pre-feature migration-tree regression rejects a database still stamped at
0080; it does not substitute for executing the selected older application.
The Persian rollback guide now prepares migration state using current code before
rehearsing a never-activated installation's old binary on a stopped copy. Website
services and uncertain external effects still require a customer-aware binary
or a separately reconciled preactivation restore; physical schema removal is
not implied. No merged migration was edited and no new revision was needed:
upstream still ends at 0070.

The unrelated `app/api/app.py` import reorder is removed. Its remaining feature
diff contains the required imports, registration calls and customer-block
synchronization. Canonical ownership validation remains active: lists use one
SELECT with an indexed EXISTS; a loaded service's owner check may add one indexed
lookup. Disabling that guard would weaken cross-channel isolation. Optional
native fencing/adapters and fresh workers remain gated, with the 22-statement /
one-POST disabled checkout regression intact.

The API provides technical customer/service operations; website retail payments,
refunds and customer wallet charging remain outside scope. Cookie/Origin checks
are not a universal browser detector, and bounded detailed authentication evidence
has aggregate overflow. All activation controls remain off by default. Required
live test-bot/PasarGuard, Telegram/webhook, proxy/TLS/DNS and worker lifecycle
acceptance remains not performed; local databases and fake transports do not
establish those external behaviors.

### Current source and focused verification

The reviewed foundation code is `57040cf1`; the assembled Python source is
`58d42e28`, with subsequent edits restricted to documentation. Fingerprints cover
all 166 changed Python files. The generated 17-path OpenAPI export remains
byte-identical. Release/version files match dev and dependency files are unchanged.

| Check / source | Result |
|---|---|
| Foundation SQLite/PostgreSQL migration/startup matrix, `57040cf1` | **58 passed, no skips** |
| Assembled SQLite/PostgreSQL migration/startup matrix, `58d42e28` | **62 passed, no skips** |
| Assembled SQLite/PostgreSQL concurrency matrix, `58d42e28` | **34 passed, no skips** |
| Read stage `276ade2c`, API/auth/security/catalog/read matrix | **181 passed, 1 PostgreSQL skip** |
| Operations stage `eb9feb6b`, disabled/ownership/isolation plus execution matrix | **65 passed, 1 PostgreSQL skip** across two disjoint file sets |
| Outbox stage `0779d854`, events/consent/webhooks/scheduler matrix | **48 passed, 3 PostgreSQL skips** |
| Management stage `dc937aca`, management/control/disabled/scheduler matrix | **41 passed, no skips** |
| Default-off/runtime/Cookie/Origin/TLS focused check, assembled | **7 passed, no skips** |
| Inherited Mini App branding and pull/badge Node suites | **16 + 5 passed** |
| Lint ratchets, foundation/read/operations/outbox/management/assembled | Passed for **25 / 70 / 135 / 150 / 163 / 166** changed Python files |

Focused matrices overlap; do not add them to the broad suite counts. The two
assembled database matrices are disjoint and total 96 passing cases. Their
PostgreSQL 16.14 database was synthetic, used a private Unix socket with TCP
disabled, and was stopped/removed after validation. Its URL was scoped to those
processes. The SQLite regression exercises the pre-feature migration tree and legacy-shaped
writes. Rehearsing the selected application binary on a database copy remains
required, and the documented destination must have migration head 0070. The
referenced-empty-foundation regression runs on both dialects.

The migration modules and concurrency modules are respectively:

```bash
python -m pytest tests/test_integration_startup_upgrade.py tests/test_integration_ownership_migration.py tests/test_customer_identity_migration.py tests/test_integration_migration.py tests/test_integration_admin_migration.py tests/test_integration_security_migration.py tests/test_customer_linking_migration.py tests/test_integration_read_migration.py tests/test_integration_operation_migrations.py -q -p no:cacheprovider
python -m pytest tests/test_integration_concurrency.py tests/test_integration_operation_concurrency.py tests/test_customer_linking_concurrency.py tests/test_integration_event_concurrency.py tests/test_customer_identity_concurrency.py tests/test_integration_management_concurrency.py tests/test_integration_security_concurrency.py -q -p no:cacheprovider
```

### Current broad verification

| Source / check | Result |
|---|---|
| Clean dev `55286d73` full suite | **5,696 passed, 32 skipped, 11 failed** |
| Foundation `57040cf1` full suite | **5,726 passed, 60 skipped, 11 failed** |
| Assembled `58d42e28` full suite | **6,091 passed, 79 skipped, 11 failed** |
| Assembled security manifest | **3,447 passed, 47 skipped, 0 failed** |
| Exact full-suite failure-ID comparison | **Both foundation and assembled exactly match the same 11 dev failures; zero added failures** |

All broad runs used the same Python 3.12 dependency environment in separate
checkouts with `PGCLOCK_TEST_POSTGRES_URL` unset. Full runs used
`python -m pytest tests -q -p no:cacheprovider --tb=short`; security used
`bash scripts/run_security_tests.sh -p no:cacheprovider --tb=short`.
PostgreSQL skips in these broad runs are not passes; the separate 96-case
SQLite/PostgreSQL matrices above provide executed database evidence.
All 166 changed Python file hashes still match the tested assembled source.
Only documentation changed after that source was frozen.

The unchanged failure IDs are:

- `tests/test_bot_ui_default_texts.py::DefaultTextSnapshotTests::test_default_texts_match_reviewed_snapshot`
- `tests/test_gift_code_rules.py::GiftCodeRulesTests::test_delete_buyer_with_reserved_and_consumed_codes`
- `tests/test_guide_help_docs.py::GuideCatalogTests::test_page_title_macro_has_help`
- `tests/test_home_overview.py::HostMetricsTests::test_cpu_percent_samples`
- `tests/test_home_overview.py::HostMetricsTests::test_host_metrics_shape`
- `tests/test_home_overview.py::HostMetricsTests::test_memory_stats_linux`
- `tests/test_loyalty_page_wheel_context.py::PanelStatusPageTests::test_app_uses_branded_status_not_inline_tahoma`
- `tests/test_panel_ui_guards.py::FieldHintAndPlaceholderTests::test_settings_field_help_after_control`
- `tests/test_perf_and_correctness_2_5_8.py::ShopOrderAttributionTests::test_create_order_uses_shop_context_not_sticky_reseller`
- `tests/test_service_quota_cache_list_9_0_3.py::AdminExtendCachesQuotaTests::test_extend_writes_quota_cache`
- `tests/test_service_quota_cache_list_9_0_3.py::AdminExtendCachesQuotaTests::test_partial_modify_response_still_caches_kwargs`
