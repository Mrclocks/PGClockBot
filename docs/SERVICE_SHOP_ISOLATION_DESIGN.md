# Service and customer isolation between shops

Status: design. This document changes no behavior. Pull request A (owner boundaries, tickets, payments, per-service checks, customer view) is implemented separately; section 7 lists what pull request B adds.

## 1. Problem

A person is one `bot_users` row (`telegram_id` is unique). The row carries one shop (`reseller_id`), assigned the first time the person opens a shop bot (`get_or_create_user`, "first-touch attribution"). Almost everything that decides what a shop, a customer or the platform may see is keyed on that user-level shop:

- the web panel and the shop bot list customers with `BotUser.reseller_id == shop`, then load their services by `bot_user_id` only;
- customer lists, details, subscription links, QR codes and deletes in the bots and the Mini App check only `bot_user_id`;
- notifications, campaigns, rewards and the panel client choice read `user.reseller_id`.

Consequence: a platform customer who opens a shop bot is moved to that shop. The shop's staff then see, renew, extend and delete the platform-sold services, the platform loses the customer from its own list, and both bots show the same services. Several Owner-side routes also reach into shop data without a scope check (listed in section 8).

The features that already compare the service's shop with the current bot (renew, add-on packs, cancellation, automation) use `service_shop_id` (`app/services/service_automation.py`): the shop of the fulfilling order, falling back to the user's attribution. That helper is the right idea but is not applied everywhere and is wrong for services without an order.

## 2. Invariants

1. A service belongs to exactly one shop (or the platform), decided when it is created and never inferred from who the customer is today.
2. A bot, panel session or Mini App for shop S may read or change a service only if the service's shop is S and it is not under review. The platform bot and the Owner web panel are the platform shop (`NULL`); the Owner reads shop data only through the web panel and only where this document allows it (section 6).
3. A person can be a customer of several shops. Their services, tickets, orders, wallets and notifications stay in the shop that produced them.
4. A user-level action (block, role, delete, wallet adjustment, direct message) is allowed only for the shop the user is attributed to; it never reaches the services of another shop.
5. Every check happens where the mutation happens, on the loaded record, not only on the list that led to it.

## 3. Decision: shop on the service

Add `user_services.shop_id` (nullable `ForeignKey("bot_users.id")`, indexed). `NULL` means platform, the same convention as `orders.reseller_id`.

Rejected alternatives:

- **One `bot_users` row per (telegram_id, shop).** Cleanest model, but `telegram_id` is unique and referenced by auth, referral, wallet and admin identity. Splitting rows is a data migration with no safe rollback.
- **Reuse `user_services.owner_principal_id`.** It is the Phase 1E principal dimension and is itself backfilled from the sticky `BotUser.reseller_id` (`resource_principal.py`), so it carries the same error and its `NULL` means "unknown", not "platform".
- **Keep deriving the shop from the order remark.** Works only for `order:N`; imported, linked and manually created services have no order.

Where the shop is known at creation (all three creation sites):

| Site | Shop source |
| --- | --- |
| `orders.py` delivery (`remark = order:N`) | `order.reseller_id` |
| `bot_user_admin.py` manual provisioning | the shop of the acting scope (`shop_rid`) |
| `service_import.py` import | the import `shop_id` |

## 4. Backfill, review state and the report

Migration `NNNN_service_shop_id` (idempotent, keyset batches, `downgrade` drops what it added):

1. `remark = 'order:N'` and the order belongs to the same user: `shop_id = order.reseller_id`. This is the only place the remark text is parsed; from this release on `shop_id` is written when the service is created (delivery, manual provisioning, import) and nothing parses the remark again.
2. Every other service (`imported`, `linked`, `manual:*`, malformed remark) has no order. Its shop can only be guessed from the customer's current `reseller_id`, which is wrong for a customer who was attributed to a shop after the service existed. These rows are **not** assigned silently:
   - a customer whose attribution is consistent with the rest of their data (no order or service of another shop) gets `shop_id` from the attribution;
   - a customer with evidence of two shops (a platform order or service next to a shop attribution, or the reverse) has such services marked `shop_needs_review = true` and `shop_id` left `NULL`.

`NULL` stays "platform" for resolved rows; a row under review is excluded from every shop's view and from every staff action until the owner resolves it, so an unresolved service is never handed to the wrong shop. The migration writes the list of services under review to the migration log, and an owner-only page (or CLI command) lists them with the customer, the evidence and a one-click choice of shop. The report is produced by the same query that sets the flag, so it can never disagree with the backfill.

## 4a. Shop membership

The user list of a panel and the bot's customer queries cannot rely on `bot_users.reseller_id`, because one person can be a customer of several shops. Add a light table `bot_user_shops(bot_user_id, shop_id)` (`shop_id` `NULL` = platform; two partial unique indexes (`postgresql_where`) keep one row per pair) written idempotently on first contact with a bot, on order creation, on service creation and on import, and backfilled from `reseller_id`, orders and services. `reseller_id` stays for compatibility and for "which shop first attributed this person". User-level actions (block, role, delete, wallet adjustment, message) still belong only to the attributed shop; a delete is refused with a clear message while the person has services of another shop.

Rejected: an `EXISTS` over orders and services as the membership rule. It cannot represent a customer who opened a bot but has not bought, and costs two index probes per row in every list.

## 5. One check, one helper

`app/services/service_scope.py` (new module, no Telegram or FastAPI types):

- `service_in_shop(service, shop_id) -> bool` and `assert_service_in_shop(service, shop_id)` (raises the domain `ValueError` with the existing Persian message).
- `shop_services_clause(shop_id)` for queries (`UserService.shop_id IS NULL` or `== shop_id`).
- `shop_member_clause(shop_id)`: a join on `bot_user_shops` (section 4a). A customer stays visible to the platform after attribution and is visible to a shop that never attributed them.
- `current_shop_reseller_id()` stays the single source of "which bot is this".

`service_shop_id` becomes a thin reader of the column. Call sites to convert, grouped by pull request in section 7.

## 6. Roles

| Actor | Rule |
| --- | --- |
| Customer in a shop bot or Mini App | Sees and manages only services with that shop. The platform Mini App shows only platform services and uses the platform catalog and purse. |
| Shop owner and shop staff (bot and web panel) | Customers by `shop_member_clause`; services only by `shop_services_clause`. User-level actions only for attributed customers. |
| Platform admin in the platform bot | Platform services only. Shop customers and shop services are denied. |
| Owner in the web panel | Own scope (`/users`, orders, payments) is the platform. Owner may still use `/pg/users` for the whole panel, but a delete there requires a reason, is logged, and detaches only through the same scoped helper. Owner may not reach shop customers or shop wallets through `delete-user`, broadcast or payment approval. |
| Resellers' own personal account | Treated as a customer of the platform. The shop panel never lists it. From a shop bot it sees only services of that shop. |
| L1 and L2 principals | Unchanged; scope stays by their linked shop. |

## 7. Pull requests

Pull request A (no migration; implemented):

- scope `POST /resellers/{id}/delete-user`; add `reseller_id IS NULL` to the broadcast audience; Owner approval and rejection (single and bulk) only for platform payments, as the bot already does;
- ticket visibility, counters and actions by `Ticket.reseller_id`; the fail-open in bulk ticket close;
- `order.reseller_id == current shop` in the `pay:*` handlers;
- `get_owned_service` requires the acting shop and refuses a service whose fulfilling order belongs to another shop; the panel client is chosen from the service's shop;
- customer lists, details, links and the Mini App show only services the bot in use sold (two queries per list through `services_in_shop`).

Pull request B (migration and behavior change):

- `user_services.shop_id`, `shop_needs_review`, `bot_user_shops`, backfill and the review report; `service_shop_id` and `services_in_shop` read the column;
- stop first-touch attribution for a customer who already has a service or an order (kept out of A because without membership a shop would stop seeing a customer who buys from it after buying elsewhere);
- panel and bot user lists and the edit modal through the membership table and the service shop;
- notification bot and panel client chosen from the service or order shop;
- campaign audience and scheduler fallback through the helper;
- loyalty redemption and wheel by the shop of the bot in use; point balances per shop are a follow-up.

## 7a. Behavior change for customers

A customer of two shops sees two separate lists. A service opened from the wrong bot is reported as not found. Support should be told before pull request A ships.

## 8. Findings this design closes

Owner side: `delete-user` without scope; broadcast to every shop; wallet top-up approval for another shop's wallet; whole-panel PG views without a logged reason.
Shop and bot side: service list, details, links, delete and operations by user-level shop only; tickets of re-attributed users; payment buttons without the order's shop; notifications, campaigns and rewards keyed on user attribution.

## 9. Tests

For each converted path: a second shop and a second customer cannot read or change the record; a customer with services in two shops sees only the current shop's; a platform customer opening a shop bot keeps their services and is not attributed; an Owner POST for a shop's customer or wallet is refused; a missing or 404 panel answer never deletes a local row. Security and isolation tests go in `scripts/security_test_manifest.txt`. The migration test upgrades, upgrades twice and downgrades, with order-based, imported and malformed-remark rows.

## 10. Open items

- Whether a 404 from the panel client can happen for a live panel user owned by another admin (not verified; the delete path is hardened regardless).
- Point balances per shop.
- The Owner's `/pg/users` design stays; only its logging and reason requirement change.
