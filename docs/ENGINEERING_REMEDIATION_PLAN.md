# Engineering remediation plan

This plan brings the existing code up to the standards in [AGENTS.md](../AGENTS.md). New code must meet those standards immediately; this document covers code written before them.

Principles:

- **Ratchet, do not big-bang.** `scripts/lint_changed.sh` stops things from getting worse while the counts below go down.
- **One mechanical category per pull request.** A cleanup pull request changes no behavior and contains no feature. Reviewers can then verify it by pattern.
- **Money paths first.** Order matters more than count. Fix where an error is most expensive (payments, wallets, delivery, quota, authorization), then everything else.
- **Every phase has a measurable exit.** Re-run the commands in the baseline table; a phase is done when its target is met and CI keeps it there.
- **Behavior is protected by tests before it is moved.** Before splitting or rewriting a hot function, add characterization tests that pin its current output.

## Baseline

Measured on the `dev` branch plus the renewal/cancellation/campaign pull request (2026-10-08). Python 3.12, ruff 0.16 with the repository `ruff.toml`. Re-run the commands to refresh.

| Area | Measure | Baseline | Command |
|---|---|---|---|
| Size | Python files / lines in `app/` | 208 / ~110,700 | `find app -name '*.py' \| xargs wc -l` |
| Size | Functions over 100 lines (over 200) | 121 (39) | AST scan, see Appendix |
| Size | Longest functions | `create_api_app` 4,674 lines, `register_pg_pages` 2,186, `register_reseller_pages` 1,466, `reply_main_nav` 792 | AST scan |
| Size | Largest files | `api/app.py` 5,057; `bot/handlers/admin.py` 4,274; `reply_nav.py` 2,799; `services/orders.py` 2,482 | `wc -l` |
| Size | `panel.css` / `panel.js` | 11,143 / 4,435 lines | `wc -l app/web/static/*` |
| Complexity | Functions over cyclomatic complexity 15 | 112 | `ruff check app --select C901` |
| Complexity | Functions over 60 statements | 81 | `ruff check app --select PLR0915 --config 'lint.pylint.max-statements=60'` |
| Errors | `try/except/pass` | 230 | `ruff check app --select S110` |
| Errors | `try/except/continue` | 6 | `ruff check app --select S112` |
| Errors | `except Exception` (all) | ~1,000 | `rg 'except Exception' app --glob '*.py' \| wc -l` |
| Errors | `raise` without `from` inside `except` | 27 | `ruff check app --select B904` |
| Dead code | Unused imports / variables | 127 / 11 | `ruff check app --select F401,F841` |
| Correctness | Redefinitions, duplicate dict keys, undefined names | 4 / 1 / 1 (`F811`, `F601`, `F821`) | `ruff check app --select F811,F601,F821` |
| Async | Blocking calls in async functions (`ASYNC`) | 8 (`ASYNC109`, `ASYNC230`, `ASYNC240`) | `ruff check app --select ASYNC` |
| Async | `time.sleep` call sites (must be confirmed to run off the event loop) | 9 | `rg 'time\.sleep' app --glob '*.py'` |
| Time | Naive `datetime.now()` | 4 | `ruff check app --select DTZ` |
| Performance | Avoidable loop/list/dict overhead | 27 (`PERF`) | `ruff check app --select PERF` |
| Structure | Indented (function-local) import lines | ~2,200 | `rg '^\s{4,}(from\|import) ' app --glob '*.py' \| wc -l` |
| Typing | Service functions without a return annotation | 54 of 1,864 | AST scan |
| Typing | `# type: ignore` / `# noqa` | 13 / 7 | `rg` |
| SQL | f-string SQL (`text(f"...")`) | 11, all in schema helpers, the SQLite->PG migrator, `validate.py` and `baseline.py`, interpolating table/column names from code | `rg '\b(text\|sql_text\|execute)\(f["'"'"']' app --glob '*.py' \| rg -v 'edit_text'` |
| Tests | Test files | 300 | `ls tests` |
| Tests | Full-suite failures on a clean `dev` | 54 (3,142 passed) | `pytest tests -q` |
| CI | What CI runs | curated security suite only; no lint, no full suite | `.github/workflows/` |

Notes on reading the numbers:

- `F821` in `billing.py` is a string annotation (`tuple["BotUser", int]`) whose import is function-local; it is harmless at runtime and is fixed with a `TYPE_CHECKING` import.
- The 11 f-string SQL sites use table and column names from code, not user input, but they must be re-verified one by one in Phase 1 and moved to an explicit allow-list.
- Most of the 54 failing tests assert on source text or UI strings of older versions. They are stale tests, not failing features, and must be triaged (Phase 5), not "fixed" by editing product code.

## Phase 0. Tooling and safety net (no behavior change)

Goal: make the rules enforceable and visible before touching code.

1. Merge `ruff.toml` and `scripts/lint_changed.sh` (already in the repository).
2. Add a second CI job, `lint`, that runs `scripts/lint_changed.sh upstream/dev` (fetch the base branch with enough history for the merge base). It fails a pull request that adds findings.
3. Add a third CI job that runs the full suite and compares the failure list with a committed baseline file (`scripts/known_failures.txt`). A new failure fails the job; fixing a known one requires removing it from the file. This turns "54 unrelated failures" into a number that can only go down.
4. Add `pyproject.toml` (or `pytest.ini`) with the pytest configuration (`testpaths`, `asyncio` mode, warnings as errors for `DeprecationWarning` from our own code) so every environment runs tests the same way.
5. Add a `Makefile` or `scripts/check.sh` with the five commands from AGENTS.md section 10, so humans and agents run one thing.
6. `CLAUDE.md` already imports `AGENTS.md` so Claude Code reads the rules. Add `.github/pull_request_template.md` mirroring the checklist in AGENTS.md section 13.

Exit: CI shows lint, security tests, and full-suite-vs-baseline on every pull request.

## Phase 1. Silent failures and correctness in money paths

Goal: no error is swallowed where money, quota or access is decided.

Order of modules: `orders.py`, `wallet.py`, `payment_*`, `receipts.py`, `delivery.py`, `billing*.py`, `service_addons.py`, `service_renewals.py`, `service_cancellations.py`, `loyalty.py`, `gift_codes.py`, `resellers.py`, `authz.py`, `reseller_access.py`, `pg_access.py`, then the rest.

1. Triage every `S110`/`S112` in these modules into three groups and fix per group:
   - best-effort and truly harmless (UI refresh, optional notification): keep, narrow the exception type, add `logger.debug(..., exc_info=True)`;
   - hides a failure that changes state: log at `warning` and record the failure state, or re-raise;
   - hides a failure of a money or access decision: remove the `try`; let it fail or route it to the review state.
2. Fix `B904` (27) by chaining exceptions; fix `F811`, `F601`, `F821` and the unused variables (`F841`, 11), because some of them are real mistakes (a duplicated dictionary key or a redefined function silently shadows the first one).
3. Re-verify each of the 11 f-string SQL sites; replace with bound parameters where a value is interpolated, and with a shared allow-listed helper where an identifier is.
4. Add a lint rule exception only in a module that has a reason documented at the top.

Exit: `S110` and `S112` equal zero in the modules listed above; `B904`, `F811`, `F601`, `F821`, `F841` equal zero repo-wide; every changed path has a test for its failure case.

## Phase 2. Event loop and database performance

Goal: no blocking work in async code, no per-row queries, bounded queries.

1. **Blocking calls.** For each of the 9 `time.sleep`, 3 `urllib.request`, and the `ASYNC` findings, confirm whether it runs in a thread, a CLI process or the event loop. Move any event-loop case to `asyncio.sleep` / `httpx.AsyncClient` / `asyncio.to_thread`. `panel_update.py`, `service_control.py`, `ssl_certs.py`, `host_metrics.py` are the first to check. Also look for bcrypt hashing, image generation (`qrcode_gen.py`) and backup file I/O called directly from handlers.
2. **N+1 audit.** Add a small test helper that counts SQL statements for a request or handler (SQLAlchemy `before_cursor_execute` event). Use it on the heaviest screens first: admin user list, orders/payments lists, reseller dashboard, the Mini App shop payload, service lists, `check_expiring_services` and the campaign and automation ticks. Where the count grows with the number of rows, batch the query (`IN (...)`) or eager-load. Record before/after counts in each pull request.
3. **Unbounded reads.** Find `.all()` on tables that grow (`orders`, `payments`, `bot_users`, `user_services`, notifications), add `LIMIT`/keyset pagination, and move totals into `COUNT`/`SUM` queries.
4. **Indexes.** Review the filter and join columns of the queries from step 2 with `EXPLAIN` on PostgreSQL and SQLite; add missing indexes through migrations.
5. **Panel call fan-out.** Find places that call PasarGuard once per row (service lists, expiry checks, reseller overviews). Replace with page-limited fetches, `asyncio.gather` under a `Semaphore`, and the existing short TTL cache; make sure a panel outage degrades to local data.
6. **Settings reads.** Find handlers that call `get_all_settings` more than once per request and load it once.
7. **Telegram throughput.** Confirm broadcast and campaign sending respect rate limits and `RetryAfter` (campaigns already do); align `broadcast.py` with the same lease/resume design if it does not.
8. **Naive time.** Replace the 4 naive `datetime.now()` calls with timezone-aware UTC and add a test around each comparison they feed.
9. **`PERF` findings (27).** Apply the safe ones mechanically after the audit.

Exit: no `ASYNC` findings; query counts for the listed screens are constant in the number of rows (asserted in tests); `DTZ` is zero; documented before/after numbers for each fix.

## Phase 3. Structure: break up the giant files and functions

Goal: new work stays small, old giants shrink behind tests.

Rules while doing this: move code without changing it (pure move first, edit second), keep route paths and callback data identical, one module per pull request.

1. **`app/api/app.py` (`create_api_app`, 4,674 lines).** Extract route groups into modules registered by a `register_*` function, the way `miniapp_pages.py` and `customer_features.py` already work. Suggested order by risk: static and health routes, settings pages, user pages, payment pages, reseller pages. `register_pg_pages` (2,186) and `register_reseller_pages` (1,466) are then split into `APIRouter` modules per sub-area.
2. **`reply_main_nav` (792 lines).** Replace the `if/elif` chain with a mapping from reply action to handler (`reply_action_map` already exists as a starting point) and move each branch into the handler that owns it.
3. **`bot/handlers/admin.py` (4,274), `admin_plans.py`, `shop.py`, `admin_pg_users.py`, `admin_settings.py`, `loyalty.py`, `reseller.py`.** Split by screen. Keep handlers thin and move any rule into `services/`.
4. **`services/orders.py` (2,482).** Split by responsibility: order creation, payment, delivery, renewal, add-on. `deliver_order` (complexity 35) and `apply_renewal` (34) become small named steps (claim, prepare target, call panel, finalize, release) that are unit-tested one by one.
5. **`services/users.py`, `services/resellers.py`.** Same approach; extract settings, referral, and billing concerns.
6. **Complexity ratchet.** After each split, lower `max-complexity` in `ruff.toml` in steps (15 -> 12 -> 10) once the count of offenders is zero.

Exit: no function over 200 lines, no file over 1,500 lines, no function over complexity 15 in `services/`; `app/api/app.py` only wires modules.

## Phase 4. Typing, imports and modernization

1. Apply `ruff --fix` for `F401` (unused imports, 127) in one reviewable pull request. For re-export blocks such as the one in `bot/keyboards.py`, declare `__all__` or use an explicit `from x import y as y`, so the intent is visible and ruff stops flagging it.
2. Apply `UP006`, `UP035`, `UP037`, `UP034` automatically (about 80 fixes: modern annotations and import locations). No behavior change.
3. Add return annotations to the 54 service functions without one. Start `mypy` (or `pyright`) in non-strict mode on `app/services` only, with an allow-list of modules that already pass; add modules as they are cleaned, never remove one.
4. **Function-local imports (~2,200 lines).** Classify them: (a) real import cycles, (b) heavy optional imports, (c) habit. Fix (c) by moving to the top. For (a), break the cycle by moving shared types or functions to a lower module (for example `models`, `shop_scope`, `numbers`), then hoist. Track the count per package.
5. Replace ad hoc dict payloads that cross module boundaries with `TypedDict`/dataclasses/pydantic models, starting with the renewal preview, cancellation status and campaign progress structures.

Exit: `F401`, `F8xx`, `UP` are zero; the mypy allow-list covers all of `app/services`; local imports exist only with a justification comment.

## Phase 5. Tests and CI

1. **Triage the 54 failing tests.** For each: (a) the product behavior changed on purpose -> update the test to assert behavior; (b) the test asserts source text or a version pin -> rewrite as a behavior test or delete it with a note; (c) a real regression -> fix the product code in its own pull request. Remove entries from `scripts/known_failures.txt` as they are resolved.
2. Make the full suite a required CI check once it is green.
3. Add coverage reporting for `app/services` and require tests for new commerce code through the pull request template (not a global percentage gate).
4. Add concurrency and failure-injection tests for each state machine: payment review, delivery, renewal, add-on, cancellation, campaign. They must exercise duplicate calls, crash between claim and finalize, and unknown panel outcome.
5. Add a PostgreSQL job to CI (service container) for the migration tests and the partial-index behavior; SQLite alone cannot prove the PostgreSQL path.
6. Speed: the full suite takes about five minutes. Run it in parallel (`pytest-xdist`) once tests are isolated; keep the curated security suite as a fast first job.

Exit: full suite green and required; PostgreSQL migration job present.

## Phase 6. Web assets and templates

1. Split `panel.css` (11k lines) into base, layout, components and page files with a cache-busting build-free include order; remove duplicated rules and unused selectors (verify with a coverage tool before deleting).
2. Split `panel.js` (4.4k lines) into feature modules loaded per page; remove global state; add Node tests for pure logic.
3. Move inline `<script>` blocks to external files so the CSP can drop the compatibility shim in `csp_nonce.py`.
4. Audit every `innerHTML`/`|safe` for unescaped data; add the escape-on-insert helper to a shared file and use it everywhere.
5. Accessibility pass on forms and modals (labels, focus return, keyboard close, contrast).

Exit: no file over 1,500 lines in `app/web/static`; zero inline scripts without a nonce; no unescaped dynamic `innerHTML`.

### Design unification track

One design language for every page and modal, on phone and desktop, as written in [DESIGN_SYSTEM.md](DESIGN_SYSTEM.md). Each step is its own pull request; the rules land first so every later step has one reference.

| Step | Scope |
|---|---|
| D1 | Rules: type and radius scales, shape-means-role, capsule header, «⋯» menu, settings list → detail, empty/loading/error states, Persian digits, motion, pull request checklist |
| D2 | Tokens (`--fs-*`, icon sizes), one icon macro, `page_header`, `empty_state`, settings list macro; new `design_check.py` rules as a ratchet |
| D3 | Capsule header, «⋯» menu, 44 controls inside both capsules, content clearance and layering under the bars, on every page |
| D4 | Settings list → detail for bot settings, system settings, marketing, shop settings and my account, with permission and shop-isolation tests |
| D5 | All modals to §3, settings modals to the list pattern |
| D6 | Page content: dashboard order and connection banner, compact stat rows, empty states, list counts, ticket list headings, usage bar from the quota cache, footer, menu tile labels |
| D7 | Persian digits in `num`/`money` and the JS formatters |
| D8 | Mechanical: font sizes and radii to the scales, inline styles, button variants, `!important` |
| D9 | Lock: strict `design_check.py` for every rule at zero, components page, pull request template |

Baseline (`dev` at `d8fde6c`): 49 pages, 218 modals in 30 templates; `design_check.py --report` 390 findings (raw colour 329, inline style 58, cancel in form modal 2, modal outside `.ds` 1); 18 distinct `font-size` values (13px used 92 times), 16 distinct pixel radii, 33 button class combinations, 328 `!important`.

## Phase 7. Dependencies and operations

1. Pin and document why each dependency has its bounds; add `pip-audit` (or equivalent) to CI.
2. Remove the `bcrypt<4.1` workaround when `passlib` support allows, or replace `passlib`.
3. Add structured logging fields (request id, shop id, order id) and make sure secrets are redacted at the logger level, not only by convention.
4. Add health and job-lag metrics for the scheduler jobs (last success time per job) so a stuck background job is visible.

## Order of work and sizing

| Order | Phase | Size | Risk | Depends on |
|---|---|---|---|---|
| 1 | 0 Tooling | S (1-2 days) | Low | none |
| 2 | 1 Money-path errors | M (1 week) | Medium, behavior can change | 0 |
| 3 | 4.1-4.2 mechanical autofixes | S | Low | 0 |
| 4 | 2 Performance | L (2-3 weeks) | Medium | 0, query-count helper |
| 5 | 5 Tests/CI | M-L, in parallel with others | Low | 0 |
| 6 | 3 Structure | XL, incremental | Medium-high | tests from 1, 2, 5 |
| 7 | 4.3-4.5 types and imports | M, incremental | Low | 3 |
| 8 | 6 Web assets | L | Medium | 5 |
| 9 | 7 Dependencies/ops | S-M | Low | 5 |

Each phase is a series of small pull requests. Start every one by recording the baseline numbers from the table, and end it by updating this document with the new numbers.

## Appendix: reproducing the AST scans

```python
import ast, pathlib
big, missing, total = [], 0, 0
for path in pathlib.Path("app").rglob("*.py"):
    tree = ast.parse(path.read_text())
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            length = fn.end_lineno - fn.lineno + 1
            if length > 100:
                big.append((length, str(path), fn.name))
            if path.parts[1] == "services":
                total += 1
                missing += fn.returns is None
print(len(big), "functions over 100 lines;", sum(1 for b in big if b[0] > 200), "over 200")
print(missing, "of", total, "service functions have no return annotation")
```
