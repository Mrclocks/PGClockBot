# PGClockBot engineering rules

These rules apply to every change, whether written by a person or by an AI agent. Read this file before editing code. When a rule conflicts with a quick fix, follow the rule and explain the trade-off in the pull request.

Rules marked **MUST** are required. Rules marked **SHOULD** are the default and need a reason to skip. The cleanup of code that does not meet these rules yet is tracked in [docs/ENGINEERING_REMEDIATION_PLAN.md](docs/ENGINEERING_REMEDIATION_PLAN.md); do not fold that cleanup into a feature change.

## 1. Core rules

- Implement features independently within PGClockBot's existing architecture, naming conventions, service boundaries, and UI patterns.
- Commit messages, code comments, identifiers, tests, release notes, and product documentation should describe PGClockBot behavior and the concrete problem or feature being implemented.
- Preserve shop isolation, scoped wallet balances, authorization checks, and idempotent payment and delivery behavior when extending commerce flows.
- Introduce changes incrementally and verify affected behavior with focused tests before expanding the scope.
- Retain required notices for existing dependencies and assets.
- Support one PasarGuard panel only. Reseller credentials are scoped accounts on that same panel; do not add multiple panel connections or other panel backends.

## 2. Architecture map

```
app/api/       FastAPI routes: web panel, Mini App, JSON endpoints   (thin: parse, authorize, call a service, render)
app/bot/       aiogram 3 routers, keyboards, FSM states              (thin: parse update, call a service, reply)
app/services/  business logic and integrations                      (all rules live here; no Telegram or FastAPI types)
app/db/        SQLAlchemy 2.0 models, session, schema helpers
app/jobs/      APScheduler jobs                                      (orchestration only, logic stays in services)
app/cli/       operator CLI
app/web/       Jinja templates and static assets
alembic/       migrations
tests/         pytest + unittest tests, Node tests for browser code
```

- **MUST** put business rules in `app/services/`. A handler or route parses input, checks permission, calls one service function, and renders the result. It does not contain pricing, wallet, quota, or state-machine logic.
- **MUST** keep dependencies flowing downward: `api`/`bot`/`jobs` -> `services` -> `db`. A service never imports from `app.api` or `app.bot`.
- **MUST NOT** add routes to `app/api/app.py` or handlers to `reply_nav.py`. These files are already far too large. Add a new module with its own `register_*` function or `APIRouter`/`Router` and register it in one line.
- **SHOULD** create a new focused module when a new feature needs more than about 150 lines. Name it after the domain noun (`service_cancellations.py`, `campaigns.py`), not after a version or ticket.
- The project runs on SQLite (default) and PostgreSQL. Code and migrations **MUST** work on both.

## 3. Hard invariants for commerce, tenancy and security

Money, quota and access paths are the most expensive place to be wrong.

- **Tenancy.** Every query and every write that touches customer data **MUST** be scoped by shop (`reseller_id` / `shop_id`). Resolve the shop from the authenticated principal or the loaded record, never from a request field. Re-check scope at the point of mutation, not only at the point of display. Add a test that a second shop and a second customer cannot read or change the record.
- **Wallets.** Credit and debit only through `app/services/wallet.py` with the correct `shop_id`. Never write `wallet_balance` or `ShopWallet.balance` directly from a handler. Money is an integer in the configured currency unit; **MUST NOT** use `float` for money.
- **Idempotency.** Every payment, delivery, renewal, refund and notification path **MUST** be safe to run twice. Use one of: an atomic status claim (`UPDATE ... WHERE status = :expected` and check `rowcount`), a unique key or partial unique index, or a lease token with an expiry. Never read a status, decide in Python, then write.
- **Unknown outcomes.** If a call to the panel or a payment provider may have succeeded (timeout, transport error, crash after the write), **MUST NOT** refund, release, or repeat blindly. Persist the intended target before the call, move the record to a review state, and make the retry reuse the saved target.
- **Order of side effects.** Commit the claim before the network call. Never hold a database transaction open across a network call. Never send a Telegram message inside a transaction that can still roll back.
- **Authorization.** Use the existing `authz` helpers (`can_shop`, `authz_from_staff`, the `require_*` dependencies). A new capability gets its own permission key, added to `FEATURE_PERMS` and documented in the pull request. Deny by default.
- **Secrets.** Never log, return, or template tokens, passwords, API keys, subscription tokens or card numbers. Use `redact.py` and `secret_box.py`. Compare secrets with `secrets.compare_digest`. Generate tokens with `secrets`, never `random`.
- **Input.** Treat all customer text as hostile. Escape with `html.escape` for Telegram HTML, rely on Jinja autoescape for templates, and escape on insertion in browser code (`esc()` in `miniapp.js`). Never use `|safe` or `innerHTML` with unescaped data. Validate type and range of every numeric field (reject `bool`, NaN, negatives, values above the column range).
- **SQL.** Use SQLAlchemy expressions or bound parameters. **MUST NOT** build SQL with f-strings from external input. Identifiers that must be interpolated (schema helpers) come from a fixed allow-list in code.
- **Error text.** Show customers a safe message (`user_safe_error`). Log the detail server-side. Do not echo exception text from the panel, the database or a provider to a user.
- **Single panel.** Do not add a second panel connection, panel abstraction layer, or alternative panel backend.

## 4. Clean code

- **Names.** Use intention-revealing English identifiers: `refund_amount`, not `amt2`. Functions are verbs, booleans read as questions (`is_cancelled`, `has_open_order`). Follow the surrounding naming style.
- **Size.** A function **SHOULD** fit on one screen (about 40 lines) and do one thing. **MUST NOT** exceed cyclomatic complexity 15 in new code (`ruff` rule `C901`). When a function grows past that, extract named helpers or replace a long `if/elif` chain with a dispatch table. A function that already exceeds the limit **MUST NOT** get more branches; extract first.
- **Parameters.** More than five parameters is a signal. Group related values into a dataclass or a typed dict. Use keyword-only arguments (`*,`) for anything that is not obvious by position.
- **No dead code.** Delete unused imports, variables, parameters, commented-out code, and unreachable branches in the code you touch. Do not leave `TODO` without an owner and a reason.
- **No duplication.** Search for an existing helper before writing one (`rg "def .*<noun>" app`). Reuse `formatting.py`, `numbers.py`, `safe_format.py`, `db_safe.py`, `redact.py`, `list_query.py`, `shop_scope.py`. Three copies of the same block means extract it.
- **Types.** Annotate every new function (parameters and return). Use modern syntax: `list[int]`, `X | None`, `from __future__ import annotations`. Use `TypedDict`, `dataclass(slots=True)` or pydantic models for structured data instead of ad hoc dicts passed across modules.
- **Comments.** Explain *why*, not *what*. A short module docstring states the module's responsibility. Do not restate the code, do not reference tickets, tools or authors, and do not leave history in comments.
- **Imports.** Put imports at the top of the file, grouped stdlib / third-party / local. A function-local import is allowed only to break a real import cycle or to defer a heavy optional dependency; say so in a one-line comment. Prefer fixing the cycle by moving code.
- **Constants.** No magic numbers or strings repeated across files. Name them at module level (`RECIPIENT_LIMIT = 5000`). Persian user-facing strings that admins can edit belong in settings; fixed ones stay near the code that uses them.
- **Boundaries.** Raise `ValueError` (or a specific domain error) for expected rule violations in services; routes and handlers translate it to an HTTP status or a Telegram alert. Do not return `None` or `False` to mean "failed for a reason the caller must show".
- **Boy-scout rule, with limits.** Leave the lines you touch a little better. Do not reformat, rename, or reorganize code unrelated to your change; a drive-by rewrite hides the real diff and breaks review. Mechanical cleanups get their own pull request.

## 5. Python and library conventions

- Python 3.12. Dependencies are pinned in `requirements.txt` with upper bounds; adding one needs a written reason and a license check. **SHOULD** prefer the standard library or a library already in the project.
- **SQLAlchemy 2.0 style only**: `select()`, `session.scalars()`, `session.execute()`, `Mapped[...]`/`mapped_column`. No legacy `Query` API, no `session.query`.
- **Async everywhere in request/bot paths.** Use `httpx.AsyncClient`, `aiosqlite`/`asyncpg` through SQLAlchemy async, `asyncio.sleep`. **MUST NOT** call `time.sleep`, `requests`, `urllib.request`, blocking file I/O, `subprocess.run`, bcrypt/hash work or heavy image work directly in an `async def`. Wrap unavoidable blocking calls in `asyncio.to_thread(...)`.
- **Time.** Store and compare timezone-aware UTC (`datetime.now(timezone.utc)`). **MUST NOT** use `datetime.utcnow()` or naive `datetime.now()`. Use `time.monotonic()` for durations, and `zoneinfo` for display time zones.
- **HTTP clients.** Reuse a client, set explicit timeouts, and handle `httpx.HTTPError`. Do not create a client per call in a loop.
- **FastAPI.** Use `Depends` for auth and the DB session, pydantic v2 models for JSON bodies, and return `HTTPException` with a safe message. `raise ... from None` or `from exc` when converting exceptions.
- **aiogram 3.** Routers with filters (`F.data.startswith(...)`), `FSMContext` for multi-step input, and the project's `safe_edit_text` / `safe_*` helpers so a deleted or unchanged message does not crash the handler. Callback data is limited to 64 bytes; keep it short and validate every id it carries against the current user.
- **Files and paths.** `pathlib.Path`, context managers, and `tempfile`; never build paths from user input without resolving and checking they stay inside the intended directory.
- **Logging.** `logger = logging.getLogger(__name__)`. **MUST NOT** use `print` outside `app/cli`. Use `logger.exception` in a handler of last resort, `logger.warning(..., exc_info=True)` for expected-but-notable failures, and lazy `%s` formatting. Never log secrets or full customer messages.
- **Randomness and ids.** `secrets.token_hex` for tokens and request keys, `uuid4` for opaque ids, never `random` for anything a user could guess.

## 6. Error handling

- **MUST NOT** write `except: pass` or `except Exception: pass`. If failure is genuinely acceptable (best-effort cleanup, optional notification), catch the narrowest exception and log at `debug` or `warning` with `exc_info=True`.
- **MUST NOT** catch `Exception` in a financial, delivery or quota path unless the handler records the failure state, rolls back or reconciles, and re-raises or returns a defined result.
- After a caught database error on a shared session, call `rollback_quiet(session)` (see `db_safe.py`) before running another query.
- When translating one exception into another, always chain it (`raise X from exc`, or `from None` to hide internals deliberately).
- Do not use exceptions for normal control flow in hot loops.
- Background jobs **MUST** catch per item, log, and continue; one bad row must not stop the batch or kill the scheduler.

## 7. Performance

PGClockBot serves many shops and customers from one process and one PasarGuard panel. Assume tables grow and the panel is slow.

- **No N+1 queries.** Never run a query inside a loop over rows. Load related rows with `selectinload`/`joinedload`, or fetch them in one `WHERE id IN (...)` query and index the result in a dict. Review any `await session.get/scalar/execute` inside a `for` loop.
- **Bound every query.** Every list endpoint and job loop has a `LIMIT`, and large scans use keyset pagination (`WHERE id > :last ORDER BY id LIMIT :n`) instead of `OFFSET` or `.all()` on a whole table. Do not load full rows when you need one column or a count (`select(func.count())`, `select(Model.id)`).
- **Index what you filter and join on.** A new `WHERE`/`ORDER BY`/foreign key on a large table needs an index in the model and the migration. Use partial unique indexes for "at most one open row" rules. Check the query plan (`EXPLAIN`) for anything on `orders`, `payments`, `user_services`, `bot_users`.
- **Aggregate in SQL.** Do sums, counts, group-bys and existence checks in the database, not by loading rows into Python.
- **Panel calls are the slow path.** Never call PasarGuard once per row in a request. Fetch in a batch or only for the visible page, run independent calls concurrently with `asyncio.gather` under a bounded `asyncio.Semaphore`, reuse the short TTL cache in `pg_read_cache.py`, set a timeout, and degrade gracefully (show the local row) when the panel is down. Cache what you can in the database (see `sync_service_quota_cache`) and refresh it where you already fetched.
- **Telegram limits.** Stay below the rate limits: at most about 30 messages per second overall and 1 per second per chat. Bulk sends go through a throttled, resumable worker, honor `TelegramRetryAfter`, and never run inline in a request.
- **Short transactions.** Open the session late, commit once per unit of work, and never hold locks while waiting on the network. Keep claim-then-act transactions tiny.
- **Do not block the event loop.** Anything over about 50 ms of CPU or any blocking I/O goes to `asyncio.to_thread` or a job.
- **Hot paths stay cheap.** Menus, callbacks and Mini App refreshes run on every tap. Avoid reading all settings repeatedly in one request (load once, pass along), avoid rebuilding large keyboards per call when the inputs did not change, and avoid per-request file reads.
- **Payload size.** JSON for the Mini App returns only the fields the screen shows, paginated. Do not ship the whole service list with panel info for every row.
- **Frontend.** Defer non-critical scripts, avoid layout thrash (batch DOM writes), debounce search inputs, use event delegation for lists, and avoid reflowing long tables. Do not add a UI framework or a large dependency for one widget.
- **Measure, then optimize.** For a change justified by speed, record a before/after number (query count, wall time, payload size) in the pull request. Do not add caches without an invalidation rule and a size bound.

## 8. Database and migrations

- Every model change ships with an Alembic migration in the same pull request. Revision ids follow `NNNN_short_name`; `down_revision` is the current head. Check `alembic/versions/` on the target branch right before you open the pull request, because another merge may have taken your number.
- Migrations **MUST** be idempotent (inspect columns, indexes and tables first) and **MUST** have a working `downgrade` that keeps existing rows. Give new `NOT NULL` columns a `server_default`.
- **MUST** work on SQLite and PostgreSQL: no dialect-specific SQL without a guard, partial indexes declared with both `sqlite_where` and `postgresql_where`, and batch mode for SQLite `ALTER`.
- Do not drop or rename a column in the same release that stops using it; remove it in a later release.
- Never edit a migration that has been merged to `dev` or `main`; add a new one.
- Add a test that upgrades an existing database, runs the upgrade twice, and downgrades (see `tests/test_customer_features.py::CustomerFeatureMigrationTests`).

## 9. User interface, Persian text and accessibility

- **MUST** build every web panel page, modal and component to [docs/DESIGN_SYSTEM.md](docs/DESIGN_SYSTEM.md). It is the single source for sizes, spacing, dividers, colours, motion and the cancel-button rule, and its "Building a new page or modal" section lists the markup and macros to start from. A new UI pattern is added to that document first, in the same pull request; do not invent one-off spacing, sizes or colours.
- **MUST** keep `python scripts/design_check.py` passing: a template or stylesheet you change must not gain design findings (modal outside `.ds`, «انصراف» in a form modal, inline `style=`, literal colours in CSS). CI runs it on every pull request.
- **MUST** keep one design language everywhere, on desktop and phone, in pages and modals: the five-step type scale and the radius scale (§2), shape-means-role (a circle is always a button, a tinted square is never one), one primary action per page with the rest in the «⋯» menu, the capsule header for page titles on phones (§7), the list → detail pattern for every settings area and settings modal (§6-2), the empty, loading and error states (§6-3), Persian digits for displayed numbers (§2), and the motion rules (§7-1: only `transform`, `opacity` and `clip-path` animate, durations from tokens, off under reduced motion). Go through the checklist in §11 before opening a pull request that changes the UI.
- User-facing strings are Persian and RTL. Use `dir="ltr"` for URLs, usernames and numbers inside RTL text. Use the shared number and date formatters instead of hand-formatting.
- Reuse the existing CSS variables (`var(--space-*)`, `var(--card)`, `var(--border)`) and component classes (`.card`, `.btn`, `.form-field`). Do not add one-off colors or inline styles.
- Templates extend `base.html`, include the CSRF token in every POST form, and rely on autoescape. Inline scripts must be compatible with the CSP nonce handling in `csp_nonce.py`; prefer an external file.
- Every interactive control needs a text label or `aria-label`, keyboard focus, and a visible state. Destructive actions ask for confirmation and say what will happen.
- Show empty, loading and error states. A failed panel call must not blank the whole page.
- Admin-editable bot texts go through settings and `message_variables`/`safe_format`, which validate placeholders.
- Browser code is plain JavaScript in `app/web/static`. Keep functions small, escape all inserted data, avoid global state, and add a Node test (`tests/*.test.cjs`) for non-trivial logic.

## 10. Testing

- **MUST** add or update a test for every behavior change and every bug fix, and write the test so it fails without the change. A fix without a regression test is incomplete.
- Prefer the smallest test that proves the behavior: service-level tests with an in-memory SQLite database and a fake panel client, not tests that need Telegram or a real panel. No test may use the network or real credentials.
- Cover the failure paths that matter: duplicate and concurrent calls, panel timeout or unknown outcome, wrong shop, wrong customer, missing permission, out-of-range numbers, and hostile text (escaping).
- Tests assert behavior, not source text. **MUST NOT** add tests that read a source file and assert on its strings or line layout; they break on harmless refactors (many existing ones do).
- Name tests after the behavior (`test_repeated_approval_does_not_credit_twice`).
- Security, isolation, wallet and authorization tests are added to `scripts/security_test_manifest.txt` so CI always runs them.
- Do not weaken, skip or delete an existing test to make a change pass. If a test is wrong, fix it in a separate commit and say why.

How to verify a change:

```bash
python -m pytest tests/test_<area>.py -q          # focused tests first
node tests/<name>.test.cjs                        # browser logic, if touched
bash scripts/run_security_tests.sh               # what CI runs
scripts/lint_changed.sh                          # lint ratchet, see section 11
python scripts/design_check.py --details         # design ratchet for templates and CSS, see section 9
python -m pytest tests -q -p no:cacheprovider    # full suite before opening the PR
```

The full suite currently has known unrelated failures on `dev`. Compare against a clean `dev` checkout; a pull request must not add a failure. Report the counts in the description.

## 11. Static analysis

`ruff.toml` defines the lint rules. The codebase does not pass them yet, so enforcement is a ratchet:

```bash
pip install ruff
scripts/lint_changed.sh            # fails if a changed file gains findings vs the base branch
scripts/lint_changed.sh --details  # also prints the findings of the failing files
```

- **MUST** keep `scripts/lint_changed.sh` passing: a file you edit must not have more findings than before.
- Fixing existing findings in a file you are already editing is welcome when it is small and clearly safe. Large mechanical sweeps go in their own pull request (see the remediation plan).
- Do not silence a finding with `# noqa` or `# type: ignore` without a reason on the same line.

## 12. Git, pull requests and releases

- Pull requests target the **`dev`** branch. Branch from the latest `dev` of the upstream repository (`upstream/dev`); `main` is for releases. Rebase or merge `dev` before opening the pull request and re-check migration numbers.
- One logical change per pull request and per commit. Do not mix a feature with a refactor, a formatter run, or a dependency bump.
- Commit subjects use a type prefix (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `perf:`, `chore:`) and state the behavior. The body explains the problem, the behavior after the change, and anything a reviewer must know (migrations, permissions, settings).
- A pull request description **MUST** cover: what problem it solves, how it works (data model, flows, concurrency and failure handling), migrations, new permissions or settings, how it was tested (with counts), and what is intentionally out of scope. State any new known limitation honestly.
- Do not bump `VERSION`, `app/version.py` or release notes in a feature pull request; the maintainers cut releases.
- Never commit secrets, `.env` files, database files, backups, logs, local tooling output or editor files. Check `git status` before committing and delete scratch files you created.
- Never force-push a shared branch, rewrite merged history, or merge your own pull request.
- Keep license notices and attributions for third-party code and assets intact.

## 13. Before you finish: checklist

- [ ] The change lives in the right layer and in a focused module; no new routes in `app/api/app.py`.
- [ ] Tenancy, wallet, permission and idempotency rules from section 3 hold, and there is a test for each that applies.
- [ ] No query in a loop, no unbounded query, no blocking call in async code, no naive datetime.
- [ ] No swallowed exceptions; failures are logged and the user sees a safe message.
- [ ] New code is typed, has no dead code, and respects the complexity and size limits.
- [ ] Migration added, idempotent, works on SQLite and PostgreSQL, has a downgrade, and the revision number is current.
- [ ] Focused tests, the security manifest, and the full suite were run; no new failures.
- [ ] `scripts/lint_changed.sh` passes.
- [ ] UI changes follow docs/DESIGN_SYSTEM.md and `python scripts/design_check.py` passes; new patterns are documented there.
- [ ] No stray files in the diff; the description is complete and accurate.
