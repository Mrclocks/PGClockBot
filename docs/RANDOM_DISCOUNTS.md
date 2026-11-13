# Random checkout discounts

PGClockBot can give each eligible order an unpredictable discount while avoiding
payable amounts already protected in that shop. Configure it in **Marketing →
Discounts and gift codes → Random discount** (`/random-discounts`). Staff need the
new `random_discounts` permission; it is disabled by default for delegated accounts.

## Configuration

One rule can be active per shop. Stop it before creating a replacement; previous
orders retain their original prices.

- **Automatic:** applies when creating fixed-plan, custom-plan, wholesale, renewal
  and volume/time add-on orders. Free orders, trials, wallet top-ups and reseller
  application fees are excluded.
- **Code:** the customer enters the configured code through the existing bot or
  Mini App discount checkout. The code does not disclose its next discount.
- **Range:** integer currency amounts or integer percentage bounds. Percentage
  bounds become an inclusive interval of integer currency reductions: round the
  minimum upward and the maximum downward. The minimum percentage must be below
  100; the final payable amount always remains positive.
- **Step:** currency units between eligible reductions, starting at the calculated
  minimum. It also applies to percentage ranges. A smaller step provides more
  distinct payable amounts. Amount ranges allow at most 10,000 candidates.
- **Protection window:** 1–10,080 minutes. Pending orders and payments can protect
  an amount for longer than this interval.

For example, a 100,000-toman order with a 100–1,000-toman range and step 1 has 901
possible payable amounts. With step 100 it has 10. A range with only one available
amount can serve only one protected order at a time.

## Selection and persistence

The service locks the active rule in a short database transaction, rechecks the
order's shop, customer, status and absence of payments, and queries protected
amounts. It excludes recent or outstanding orders, recent or pending payments
(including that shop's wallet top-ups), and unexpired random reservations.
Recently completed orders remain protected using their update time.

`secrets.randbelow` selects uniformly among the available integer positions. A
rank calculation skips occupied positions without enumerating a large percentage
range. At most 10,001 distinct protected candidate amounts are read; exceeding
10,000 returns a safe capacity error instead of performing an unbounded scan.
An exhausted range returns a retry-later message. The service never enlarges the
configured reduction, falls back to an occupied amount, or makes the order free.

The same transaction writes an immutable draw, a unique `(shop_key, final_amount)`
reservation, and the order's payable amount and discount totals. The caller commits
before starting a payment or making a panel request. No panel or provider call is
part of discount allocation.

Every retry of the same order reuses its saved result, including after a receipt
rejection, payment-method change, rule replacement or rule stop. A different code
cannot replace a saved random discount. A customer cannot cancel an unfinished
discounted order and immediately draw again for the same base price and rule;
the protection window must expire. A completed purchase permits another purchase,
which still receives a different protected payable amount.

Ordinary discount codes and random discounts do not stack. A code explicitly
supplied at order creation, including the Mini App code checkout, takes precedence
over an automatic rule. Existing catalog price adjustments are applied first;
wholesale tier reductions remain eligible for the subsequent random reduction.

## Boundaries

- Protection is per shop. Two shops sharing a bank card or account do not receive
  a shared amount guarantee.
- This feature allocates prices for participating orders. A later wallet top-up,
  an ordinary-code purchase or an external transfer can still use the same amount.
- A unique amount helps distinguish transfers; it does not prove payment. Existing
  payment-ID, receipt, provider amount, authorization and delivery checks remain
  required. Provider-specific payable rial amounts retain their own handling.
- Cancelling an order retains its reservation until expiry. Stopping a rule does
  not refund, release, or reprice any existing order.
- Routine manual and scheduled order cleanup preserves protected draws and their
  payments until expiry. Intentional permanent customer or shop deletion removes
  that customer's or shop's discount history and reservations.
- Random code names remain reserved in their shop after a rule stops. Gift-code
  creation checks the same namespace to prevent shadowing an existing random code.
  Simultaneous gift-code and random-rule creation still uses separate database
  tables, so identical names created concurrently have no shared unique constraint.
- A large reduction interval is more useful than frequent rule replacement. A
  replacement preserves old amount reservations; a new rule also begins a new
  customer draw window.

## Database and verification

Migration `0112_random_discounts` follows `0111_referral_shop`. It adds policy,
draw and amount-reservation tables, `orders.random_discount_amount` (default 0),
`orders.random_discount_id`, and indexed amount lookups. The additive legacy
SQLite startup helper runs before stamping the current migration head. Upgrade
and downgrade support SQLite and PostgreSQL and preserve existing order rows and
payable amounts.

Behavior tests cover concurrent allocation, range exhaustion, exact monetary
steps, code checkout, payment retries, recent and pending payment amounts,
customer anti-reroll, shop boundaries, permissions, invalid numeric values,
hostile titles and idempotent renewal/add-on requests. Migration tests upgrade
twice and downgrade twice with existing rows and foreign-key enforcement.

### Verification record

Validated against upstream `dev` commit `01c2b97c` on macOS with Python 3.12.
Both source snapshots excluded local credentials; the complete suite was divided
into four stable file partitions. PostgreSQL checks used a disposable local
PostgreSQL 16 instance, which was stopped after testing.

- New feature tests: **50 passed, 1 skipped**. The optional PostgreSQL migration
  test was also exercised separately: **7 migration tests passed** with live
  PostgreSQL enabled. **27 order/allocation tests passed on PostgreSQL**.
- Clean development baseline: **7,777 passed, 11 failed, 42 skipped**.
- Full feature run: **7,823 passed, 12 failed, 43 skipped**. The additional failure
  came from an attribution unit test's simulated order lacking a persisted id.
  Its unrelated discount-service boundary was stubbed, its original assertions
  retained, and the shop passed to that boundary additionally asserted. Its full
  test file then passed **14 tests**. The remaining 11 failing nodes match the
  clean baseline exactly.
- Security manifest: all **388 files** ran. The parallel run reported
  **4,712 passed, 12 failed, 1 skipped**; 11 failures were the parallel runner being
  unable to serialize existing subtest parameters. Those exact 11 tests passed
  when rerun sequentially. The remaining concurrent top-up receipt failure also
  occurs in the clean baseline.
- Focused reruns after the populated-database downgrade and checkout summary
  refinements passed: **12 passed, 1 skipped** for migration/cleanup and **30 passed**
  for order summaries and allocation.
- The lint ratchet and design-system checks passed. All five new test files are
  included in the security manifest.

The baseline failures comprise six existing source/snapshot assertions, three
Linux-specific host-metric checks on macOS, the source archive's missing Git
metadata in the backup-baseline test, and a concurrent receipt test that shares
one database session between requests. These were kept outside this feature.
