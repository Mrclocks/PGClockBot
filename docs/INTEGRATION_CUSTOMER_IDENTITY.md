# Integration customer identity decision

Status: independent customers, compatibility/canonical ownership enforcement and
optional dual-proof Telegram linking are implemented locally. Migrations 0074–0076
retain original shops, owners and unresolved review evidence. API execution and
private delivery are separate from linking. The [release operating guide](INTEGRATION_API_OPERATIONS.md)
records the current supported boundary and activation/rollback requirements.
The original Phase 0 inspection used upstream `dev` commit
`ebd27e717b2d95de7b3b5931efedbcf246a02029`; historical evidence below is preserved.

The [API contract](INTEGRATION_API_CONTRACT.md) defines the authorization and
execution rules. The [roadmap](INTEGRATION_API_ROADMAP.md) defines delivery phases.

## 1. Evidence from the current implementation

The following table records the original design inspection. Later operating
sections and the release guide describe the implemented boundary.

| Area | Observed behavior and source | Consequence for this feature |
|---|---|---|
| Customer identity | [models.py](../app/db/models.py), `BotUser`: unique, non-null `telegram_id`; customer attributes, role, referral and legacy balance share the row. [users.py](../app/services/users.py), `get_or_create_user`: looks up Telegram ID and derives bot administrator behavior from configured IDs. | A web customer cannot safely be created through this function or represented by a fabricated Telegram ID. |
| Ownership | `UserService.bot_user_id`, `Order.user_id`, `Payment.user_id` reference `bot_users`; `UserService.owner` is a `BotUser` relationship. `OrgPrincipal` is a separate administrative identity, optionally linked to a bot user/profile. | A new identity needs a canonical service owner plus an explicit legacy adapter. It must not become a principal. |
| Shops | [service_scope.py](../app/services/service_scope.py), `service_shop_id`, `customer_may_use_service`, `shop_services_clause`: selling shop is `UserService.shop_id`; `shop_needs_review` fails closed. `BotUserShop` records multiple shop memberships. | Neither current customer attribution nor a Telegram link can select or change a service's shop. |
| Existing ownership migration | [0060_service_shop_scope.py](../alembic/versions/0060_service_shop_scope.py): order evidence and bounded backfills; ambiguous services stay under review. | Keep these decisions. Do not re-infer shops during the identity migration. |
| Creation and renewal | [orders.py](../app/services/orders.py), `deliver_order`, `renew_service_with_plan`, `apply_renewal`: paid-order state machine and bot-user ownership. [service_renewals.py](../app/services/service_renewals.py), `validate_renewal_selection`: checks bot user, shop, eligibility and mutation exclusion. | API technical authorization needs its own operation record; do not synthesize paid orders to reuse execution. Extract only affected reusable rules in later phases. |
| Add-ons | [service_addons.py](../app/services/service_addons.py), `create_addon_order`, `apply_service_addon`: customer/shop checks and paid-order claims. | Add customer-neutral selection and execution entry points, preserving these checks. |
| Contact | [purchase_contact.py](../app/services/purchase_contact.py), `purchase_contact_needed`, `verify_purchase_contact`: shop-scoped verification tied to the same bot user's Telegram ID and contact owner. [trial_contact.py](../app/services/trial_contact.py) also handles Telegram/contact anti-abuse. | An external reference is neither a verified phone nor a Telegram identity. Never insert a fake verification row. |
| Mini App | [miniapp_auth.py](../app/services/miniapp_auth.py), `load_mini_user`: signed Telegram initData, existing `BotUser`, block/force-join/terms gates. [miniapp_pages.py](../app/api/miniapp_pages.py), `_owned_service_or_404`: bot customer and platform shop. `_serialize_service` includes the customer's subscription URL. | The Mini App is not web-customer authentication or an API serializer. Do not reuse its URL-bearing summary. |
| Delivery and notifications | [delivery.py](../app/services/delivery.py), `send_delivery_to_user`: a bot, chat ID and order determine delivery. [notifications.py](../app/services/notifications.py), `_send_to_user_chat`, `notify_ticket_message`, shop notification helpers: several paths use bot-user/chat identity; some already skip missing/non-deliverable IDs. | An API delivery must not depend on a chat ID. Existing missing-ID guards are not proof that all paths are customer-neutral. |
| Existing synthetic identities | [platform_identity.py](../app/services/platform_identity.py), `is_synthetic_telegram_id`, `deliverable_telegram_id`; [resellers.py](../app/services/resellers.py) contains provisioned administrative compatibility identities. | Existing internal administrative conventions do not authorize synthetic Telegram IDs for web customers. |
| Staff and users panel | [app.py](../app/api/app.py), `require_staff`, `require_perm`; [bot_user_admin.py](../app/services/bot_user_admin.py) and [users.html](../app/web/templates/users.html) operate on bot users and their services. | Add an explicit scoped web-customer presentation; do not project web identities into staff login or legacy customer counts. |
| Wallet and billing | [wallet.py](../app/services/wallet.py) separates customer purses by shop. [billing.py](../app/services/billing.py), `assert_billing_allows_provision`, and [provision_gate.py](../app/services/provision_gate.py) enforce applicable reseller obligations. | Web customers need no PGClockBot customer wallet. Reseller billing and capacity still apply to their services. |

## 2. Options and decision

| Approach | Compatibility and cost | Decision |
|---|---|---|
| Make `BotUser.telegram_id` nullable and treat `BotUser` as every customer | Keeps service/order foreign keys, but exposes new rows to code that converts IDs, sends messages, derives roles, and assumes Telegram contact/entry gates. It also extends a row that combines customer identity and administrative compatibility. Restoring non-null Telegram IDs cannot represent web customers. | Reject as the default: small schema change, large implicit behavioral change. |
| Add independent `Customer` and integration mappings; adapt existing services with a canonical customer foreign key | Leaves Telegram and principal identity intact. Requires explicit service ownership adapters, nullable legacy service ownership for web-origin services, and a staged compatibility release. Existing orders, payments and wallets retain their owners. | **Recommend.** More explicit migration work, but boundaries and rollback limits can be tested. |
| Keep web customers/services in a completely separate commerce stack | Avoids changing legacy service ownership, but duplicates service lifecycle, quota, billing and panel-user tracking; capacity accounting could miss one stack. | Reject. Share the current service resource and service-layer rules. |

The recommendation is an expansion migration, followed by compatibility work,
then web activation. `Customer` is the internal purchasing/service identity;
Telegram is a channel identity; `OrgPrincipal` is an administrative actor. These
identities remain different even when they describe the same human.

## 3. Proposed model and invariants

`Customer` and `IntegrationCustomer` now exist; linking, API execution associations
and delivery storage below describe later phases. Use opaque UUID public identifiers;
internal integer keys may follow existing conventions. Public IDs never grant access.

| Record | Proposed contents and constraints |
|---|---|
| `Customer` | Internal ID, unique public ID, state (`active`/`blocked`), created time; optional unique `legacy_bot_user_id` for the backfill adapter. No Telegram ID, password, role, wallet, or principal. Web customers have a null legacy adapter permanently. |
| `IntegrationCustomer` | Integration, customer, immutable external reference, display name, timestamps. Unique `(integration_id, external_customer_ref)` and `(integration_id, customer_id)`. Integration determines fixed shop and owning principal. A new web mapping always creates a distinct `Customer`; v1 does not attach mappings to existing customers. |
| `UserService` extensions | `customer_id`, public ID, nullable `integration_id`, nullable originating operation ID, alongside existing `shop_id`, `owner_principal_id`, panel ID and quota cache. `bot_user_id` becomes nullable only in the compatibility stage. Legacy services keep their original bot owner; API services have no bot owner, including after Telegram linking. |
| API service delivery storage | One encrypted delivery envelope per service/access version, associated with the originating integration/customer. API secrets are not copied into legacy plaintext URL/token columns; quota and billing use the panel ID. |
| `CustomerTelegramLink` | Customer/integration, real `BotUser` ID, verified/ended times and consent version. At most one active link per customer and per `(integration_id, bot_user_id)`, enforced by partial unique indexes on both databases. The same Telegram account may explicitly link to one customer in each of several integrations. |
| `CustomerLinkChallenge` | Integration/customer, digest of an unguessable challenge, initiating backend authorization, candidate Telegram identity, separate confirmation state, expiry and consumed time. No raw challenge in durable logs. |

For legacy services, backfill exactly one `Customer` per `BotUser` and populate
`customer_id` from the existing `bot_user_id`. Keep all service IDs, orders,
payments, wallets, point balances, referrals, shop memberships, and panel IDs.
Do not create integration ownership for existing services. Existing synthetic
administrative bot rows may receive an adapter but never a verified Telegram link
or a new delivery destination.

For web services, require the entire tuple:

`integration_id + IntegrationCustomer.customer_id + integration.shop_id + owning principal`.

`UserService.customer_id` is canonical. Transitional writes must enforce:

- Legacy-origin service: `integration_id IS NULL`, `bot_user_id IS NOT NULL`,
  and customer adapter matches that bot owner.
- API-origin service: integration and origin operation are set; customer mapping
  belongs to that integration; `bot_user_id IS NULL`; stored service shop and
  principal match the integration. Unique originating provision operation permits
  only one service. No reassignment, imports, or adoption of legacy services in v1.
- `shop_needs_review` denies all API access. Null shop means platform only for an
  explicitly validated platform integration; missing scope never means platform.

Use foreign keys/composite uniqueness where practical, plus scoped service-layer
checks at creation and mutation. Index integration/customer/public-ID lookups,
shop/customer service pagination, and all foreign keys used for joins. Cross-table
checks cannot be replaced by an unscoped `session.get` or a request-supplied shop.

The legacy bot adapter accepts only legacy-origin services. It must not broaden
`customer_may_use_service` by following Telegram links. API queries never use
`BotUser.reseller_id` or `BotUserShop` as an integration authorization shortcut.
An integration backend is trusted to identify its own logged-in customer; it can
act for all its mapped customers. PGClockBot does not authenticate website sessions.
Nevertheless every customer-qualified route/mutation checks the requested customer
against the service, operation and integration, including for a second customer
inside that same integration.

## 4. Linking, conflicts and unlinking

Linking proves association only. It never merges customers, moves services,
creates shop membership, copies a wallet, changes a principal, or grants a role.
The API response exposes link status, not the Telegram ID, phone or other customers.
Administrative accounts used as Telegram customers gain no additional API rights.

1. The website reauthenticates the customer and asks its backend to start a
   challenge under `telegram:link` for that mapped customer. PGClockBot accepts
   only a generated challenge, not a claimed Telegram ID or phone from the site.
2. Generate 256 bits using `secrets`; store only a digest. Expiry is 10 minutes,
   maximum three outstanding challenges per customer. Return the one-time start
   token only to the authorized backend, with `no-store`. No arbitrary redirect URL.
3. In the existing bot, the real sender proves Telegram control by opening the
   challenge and explicitly confirming the integration and customer association.
   Use authenticated bot update identity, positive deliverable ID and a non-blocked
   `BotUser`. Opening a link alone does not complete consent.
4. The customer then confirms in the reauthenticated website session. The backend
   submits completion for the same challenge/customer and candidate confirmation.
   Both proofs and challenge expiry must hold in a single atomic consume and
   link transaction. Telegram input cannot select a different web customer.
5. Unique constraints resolve races. A repeated completion for the same link is
   idempotent. A different candidate or occupied link returns `link_conflict`;
   never overwrite, merge, or disclose the conflicting identity.

If the Telegram account already owns legacy services, those stay on its legacy
customer. If it already links to another integration, that link grants no access
here. Within the same integration, linking it to a second web customer requires
explicit unlinking and fresh dual proof; there is no implicit account recovery.
An existing customer API reference is immutable, and support cannot silently
rewrite it to resolve a conflict.

Unlink requires either reauthenticated website customer confirmation through the
backend or an authenticated, explicitly confirmed bot action scoped to the current
link. End the link atomically, invalidate outstanding challenges and channel
consents, increment its version and audit the actor. Duplicate unlink succeeds
without other effects. Keep services, website mapping and operation references.
A revoked integration credential cannot unlink through the API; the customer can
still revoke Telegram consent in the bot. Recovery from loss of website login is
the website's responsibility; support requires a separate audited process.

In v1, linked web services remain managed through their website/API. Linking does
not inject them into existing bot/Mini App lists, and does not opt into Telegram
delivery, marketing or financial notifications. Later channel views need a
separate explicit integration/customer context and authorization contract. This
conservative policy prevents a link from exposing credentials in a legacy list.

## 5. Compatibility boundaries that must be adapted

- **Legacy bot and Mini App:** continue using real Telegram authentication and
  existing contact, terms, force-join and block gates. Dual-write canonical
  customers for newly created legacy users/services; preserve their current reads
  until the backfill is verified. Never pass a web customer to `get_or_create_user`.
- **Website entry:** the website owns login, membership, receipts and refunds.
  Integration management must explicitly approve `customer_entry_policy=website`
  for its fixed shop. This declares a separate channel policy; it is not a
  `PurchaseContact` verification or a bypass flag on bot purchases. No raw phone
  assertion is accepted. If the operator requires Telegram contact for every
  channel, do not activate this integration. Bot trials/contact-based anti-abuse,
  referrals, discounts, customer wallets and wallet automation are excluded from
  API v1. Applicable provisioning/billing/capacity gates remain mandatory.
- **Service execution:** customer-neutral technical entry points use the API
  operation and validated customer scope. They do not call `mark_order_free_paid`,
  payment approval or wallet checkout. Legacy order behavior continues separately.
- **Panel presentation:** scoped customer views must label the origin and allow
  missing Telegram identity without links to invalid chat IDs. Add web customers
  through a separate filter/view using neutral ownership, not fake `BotUser` rows.
  Avoid changing existing bot-user metrics to count unrelated web identities.
- **Jobs and notifications:** inventory affected service joins, ownership checks,
  formatting, delete/cancellation paths, and scheduled quota/billing reads before
  activation. Web services must count in shop capacity and reseller traffic; skip
  customer Telegram sends with an explicit channel result. Emit status events
  without credentials. Do not route API services through legacy wallet refunds or
  cancellation flows requiring a bot user.
- **Deletion:** no public customer deletion or reassignment in v1. Retain a minimal
  scoped tombstone while services, operation references or financial obligations
  exist. Customer erasure never cascades to active services or evidence.

## 6. Migration and deployment sequence

Each implementation PR targets the then-current `upstream/dev`. Allocate revision
numbers at implementation time. No migration is created in Phase 0.

1. **Phase 1 foundation:** integration/credential records, immutable owner/shop,
   explicit permissions, disabled-by-default API. Existing customer columns are
   untouched. See the contract's exact first implementation change.
2. **Phase 2A expansion (implemented):** add `Customer`, integration mappings,
   nullable `UserService.customer_id` and public IDs only. Linking tables,
   integration/operation service associations and delivery storage are deferred. Preserve existing
   `bot_user_id NOT NULL` initially. Fill public IDs and customer adapters in
   resumable keyset batches (500 rows), using unique keys to make repeats safe.
   Stop or quarantine inconsistencies; never guess ownership. No web services yet.
3. **Phase 2B compatibility release:** preserve 2A dual-writes and verify coverage before making
   canonical ownership required. Adapt affected service readers/jobs and web
   presentation as above. Keep bot service ownership columns/relationships.
   Only now make `bot_user_id` nullable, with explicit legacy/API invariants and
   migration validation; `BotUser.telegram_id` remains unchanged and non-null.
4. **Web customers and Phase 2C linking (implemented):** `/api/v1` customer HTTP and dual-proof
   link workflows register through the default-off route gate. They create neutral
   customer/mapping pairs and use actual authenticated Telegram sender proof plus
   independent website confirmation. The earlier 2A slice had only internal
   services and no linking tables. Technical execution has its own default-off gate.
5. **Enable technical operations:** require customer-neutral ownership, shared
   mutation exclusion, durable unknown-outcome capacity accounting and scoped
   billing reads before any API provision/renew/add-on can run. Backfill and
   dual-write remain in place; do not drop legacy columns in this release.

Use inspectors for repeat-safe table/column/index changes, SQLite batch alteration,
PostgreSQL-safe constraints, server defaults for new non-null columns and both
SQLite/PostgreSQL partial predicates. [alembic/env.py](../alembic/env.py) and
[alembic_runner.py](../app/db/alembic_runner.py) are existing migration entry points.
The existing shop migration and
[CustomerFeatureMigrationTests](../tests/test_customer_features.py) are useful test
patterns, not evidence that this new migration has already been tested.

## 7. Application rollback and database downgrade

**Application rollback:** disable new submissions, stop/fence workers and resolve
or preserve all running/unknown operations, reservations and mutation guards.
Deploy the last tested customer-aware compatibility release with the expanded
schema retained. Reads and operator reconciliation remain available where
authorized. Active panel services continue to exist; do not delete them, invent
Telegram IDs, issue website refunds, or discard operation references. Do not roll
back to an arbitrary pre-feature binary: nullable legacy owners can crash or be
omitted by old joins, and old workers may ignore API mutation exclusion.

**Database downgrade:** preserving rows is not equivalent to restoring the exact
old schema or making an old application safe. The chosen policy is:

- Before any new identity/mapping/operation data exists, an inspected downgrade
  may remove unused extensions and restore old nullability after proving every
  service has a legitimate bot owner. Keep all pre-existing rows and IDs.
- Once web customers, links, operations, or web services exist, use a
  **data-retaining downgrade**: preserve extension tables, ownership columns,
  nullable bot ownership, constraints and durable operation/reference records.
  A downgrade does not fabricate a bot owner or drop populated extension data.
  Its implementation must report that schema retention is active and support a
  repeat-safe re-upgrade without duplicating identities. The compatibility release
  remains the minimum supported application version.
- A physically exact downgrade to the pre-feature schema with active web
  services is **unsupported**. A request for full removal must fail before any
  schema/data mutation and require a separately reviewed export/decommission
  procedure. A backup restore alone would lose subsequent website/customer data.

Phase 2A deliberately retains the additive schema even when empty. Its tested
0074-to-0073 downgrade disables API access and permits re-upgrade without deleting
identity data. Physical removal of unused extensions is a separate future
decommission procedure, rather than a supported 2A rollback mode. An unconditional
populated table drop or inventing bot owners is never a supported rollback.
Alembic revision rollback with retained tables is intentional; it does not certify
arbitrary older binaries or complete schema reversibility.

## 8. Behavioral acceptance plan

These are future tests, not Phase 0 results. Use isolated SQLite and PostgreSQL
databases, fake panel clients and no network or real credentials. Add relevant
isolation/authorization/financial cases to
[security_test_manifest.txt](../scripts/security_test_manifest.txt).

| Scenario | Required observation |
|---|---|
| Existing data upgrade, twice | Existing bot IDs, Telegram IDs, service/panel IDs, wallets and shops survive; exactly one adapter per legacy user; dual-write creates no duplicates. |
| Web registration, repeated/concurrent | Same integration/reference returns one mapped customer; no bot user, wallet, payment or principal is created. |
| Same reference in integration B, same shop | Different neutral customer; B cannot list/read/mutate A's customer, operations, service or access credentials. |
| Second customer in integration A | A service/operation requested under customer 2 is not returned or mutated as customer 1; provisioning rejects mismatched mapping. The website must also enforce its customer session. |
| Another shop, including platform null scope | Identical IDs/references and Telegram identity never cross the fixed shop; unresolved shops fail closed. |
| Verified link to an existing Telegram buyer | Both proofs required; legacy services, other integrations, shops, balances and roles remain unchanged; web service bot owner remains null. |
| Link races, expired/replayed challenge, conflict | Atomic consume and unique constraints allow one intended link; no takeover or identity disclosure. Test link to a bot administrator too. |
| Unlink and relink | Repeated unlink is harmless; challenges/consent invalidated; services stay owned; relinking requires fresh proofs. |
| Web service through jobs/panel | No invalid chat send or formatting crash; scoped display works; quota/billing includes service; no legacy wallet refund/automation. |
| Parallel renewal/add-on/bot mutation | One durable service guard; no quota write or notification escapes integration/customer/shop scope. |
| Timeout after successful creation, then rollback | Service/operation target and reservations survive; reconcile saved panel ownership/terms; never create another user or lose a web owner. |
| Populated downgrade and re-upgrade | Web customers with active services, links and unknown operations keep all associations/guards/references; retained schema is reported; re-upgrade is repeat-safe. |
| Requested full physical downgrade | Reject before changes when web data cannot be represented; no fabricated Telegram ID and no deletion. |

## 9. Remaining limitations and review boundary

The ownership, linking and rollback policies are decided here; there is no pending
website-specific identity policy. Implementation still needs a complete inventory
of the affected legacy readers/jobs and measured migration behavior on both
databases. No claim is made about unaffected modules. The retained-schema
downgrade and minimum compatible binary must be exercised before web activation.
Linked web services in legacy bot/Mini App views, adoption of existing services,
customer wallet commerce and new contact-verification channels remain out of v1.


## 9. Phase 2A implementation and operating procedure

Migration `0074_customer_identity` follows the preserved `0073_integration_security`.
It adds independent identities, mappings, nullable service `customer_id`/`public_id`,
lookup indexes and a deny-by-default `Integration.customer_entry_policy`.
`BotUser.telegram_id` and `UserService.bot_user_id` remain non-null. No integration,
operation or delivery-secret columns are added to services. Customer public IDs
are `cus_` plus a UUID; service public IDs are `svc_` plus a UUID. There is no
customer wallet, login, principal, administrative role or Telegram ID on `Customer`.

For the current complete release, start normally: `init_db` upgrades to head and
0075 backfills legacy customers/services automatically. There is no manual
prerequisite. For maintenance or historical expansion-only installations, the
resumable CLI remains available in the deployment's existing environment:

```bash
python -m app.cli.customer_identity_backfill
# Optional committed cursors reported by a previous interrupted run:
python -m app.cli.customer_identity_backfill --users-after 500 --services-after 250
```

The command runs user batches before service batches and commits each batch
separately. Both scans use primary-key keyset pagination with at most 500 rows.
Restarting from zero is safe and verifies already assigned identities without
changing persisted public IDs. Use cursors only from committed progress output;
rerun from zero after correcting an inconsistency or when verifying coverage.
Run against the authoritative database after schema upgrade, before 2B enforcement.
The migration intentionally does not embed a long, single-transaction backfill.

The user batch upserts by unique legacy owner using batched queries, including
administrative compatibility rows. A blocked bot user produces a blocked customer;
retries never reactivate an already blocked customer. It does not infer ownership
from reseller attribution. The service batch joins the existing bot owner to its
adapter. Missing adapters or conflicting canonical associations stop the whole
uncommitted batch with a fixed error. Operators must investigate rather than skip
or invent an owner. Existing selling shops, review flags, principal associations,
panel identifiers and financial rows are untouched. An ambiguous selling shop can
receive its legitimate bot-owner identity while retaining `shop_needs_review`.

Compatible writes cover the actual constructors: `users.get_or_create_user`,
`bot_user_admin.admin_create_bot_user`, `resellers.get_or_create_pg_linked_bot_user`,
`orders.deliver_order`, `bot_user_admin.admin_provision_service` and
`service_import.attach_verified`. Adapters share the user transaction; service
identity shares the existing service transaction. Wholesale delivery resolves
its adapter once inside the existing committed delivery claim, before panel work.
Database uniqueness resolves concurrent legacy registration without orphan rows.
No bot/Mini App authorization is changed and no web customer is a BotUser.
Legacy entry blocking/unblocking still reads BotUser; backfill propagates blocking
and never implicitly unblocks a canonical identity. Phase 2B must explicitly
reconcile those state transitions before making canonical state authoritative
for legacy readers.

`integration_customers.create_or_get_integration_customer` and
`read_integration_customer` are typed internal services accepting a bearer rather
than caller-built authority/owner/shop fields. They re-resolve current authority;
mutations serialize on the same integration lock as credential/management changes
and recheck authority after waiting. Matching input replays the mapping; a changed
display name conflicts. Every new mapping gets a fresh customer; references in
other integrations/shops do not merge. A blocked customer fails closed. No generic
operation framework or durable registration ledger is introduced.

Website entry requires explicit `website` policy as well as current write scope,
grant and owner feature permission. Existing protected configuration forms expose
only `denied`/`website`; the policy and its administrative before/after audit commit
atomically under existing permission, signed form, CSRF and immutable scope checks.
This creates no PurchaseContact evidence. Future HTTP callers must also pass the
established transport/authentication/write-budget boundary; internal services do
not mount routes or enable the global switch.

**Supported rollback:** downgrade from 0074 to 0073 disables the global API switch
and retains all added schema, public IDs, mappings, service associations and policy
values, even when empty. Re-upgrade is repeat-safe and repairs missing indexes.
Prerequisite credentials/security/admin-audit evidence is preserved. SQLite adds
the nullable service reference inline to avoid rebuilding a parent referenced by
orders; the other additive changes use Alembic batch operations. PostgreSQL uses
its native foreign-key operation. Neither backend invents IDs or drops populated
extensions. This is a retained-schema rollback, not restoration of the old schema.
Only this 0074↔0073 path is covered here; downgrade through earlier prerequisite
migrations and arbitrary older application binaries are not certified by 2A.

**Next task, Phase 2B:** inventory and adapt remaining legacy service readers,
jobs, notifications, deletion and panel presentation to canonical ownership;
verify backfill/dual-write coverage and preserve all tenancy and financial gates;
only then add explicit legacy/API origin invariants and consider nullable bot
ownership. API-origin creation stays disabled. Phase 2C optionally implements the
verified linking/unlinking design. Phase 2 as a whole is not complete.
