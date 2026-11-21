# Cleanup safety net

Optimization and cleanup pull requests must not change how the panel looks, what its pages and the bot's
buttons do, or how a feature behaves. These checks prove it. Run them before opening such a pull request and
put their output in the description.

## 1. Tests (every pull request)

```bash
export TEST_DATABASE_URL=postgresql+asyncpg://user:password@127.0.0.1:5432/pgclock_test
python -m pytest tests -q -p no:cacheprovider -n 8   # full suite: no new failure
bash scripts/run_security_tests.sh                   # what CI runs
python scripts/postgres_only_check.py
scripts/lint_changed.sh
python scripts/design_check.py
```

The bot UI snapshots (`tests/fixtures/bot_ui/`) fail on any change to a keyboard, button label or default
text. `tests/test_bot_menu_tour.py` taps every main-menu and admin-panel button of the real bot and fails when
one does not answer or logs an error.

## 2. Routes and handlers (code moves: splitting files, moving handlers)

```bash
git stash   # or check out the base commit
python scripts/behavior_inventory.py snapshot /tmp/before.json
git stash pop
python scripts/behavior_inventory.py snapshot /tmp/after.json
python scripts/behavior_inventory.py compare /tmp/before.json /tmp/after.json --ignore-modules
```

`no behaviour differences` means every web route keeps its method, handler and permission dependencies, and
every bot handler keeps its router, order and filters. `--ignore-modules` accepts a handler that only moved
to another module.

## 3. Panel screenshots and controls (anything that can touch templates, CSS, JS or page data)

Commit the change, then:

```bash
scripts/ui_regression.sh origin/dev HEAD
```

It serves both versions one after the other from a temporary worktree, with the same sample data
(`scripts/ui_fixture.py`), the same local fake PasarGuard (`scripts/ui_fake_panel.py`), an offline bot and no
outbound internet. It then captures every page, tab and modal at 390 and 1440 px in the light and dark themes
(`scripts/ui_capture.cjs`, about 150 views and 590 screenshots) and compares them (`scripts/ui_compare.py`):

- pixels: any difference above 8 of 255 in a channel, in a screenshot that keeps its size;
- controls: the text and target of every link and button, and every form's method, action and field names.

`ui-compare: … 0 differences` is the pass. A difference image for each changed screenshot is written to
`$UI_OUT/after/diff/` (default `/tmp/pgclock-ui-regression`).

What is deliberately not compared, on both sides alike:

| Not compared | Why |
|---|---|
| Server meters, panel-link health figures, the finance report time | Live figures of the running process. Masked: their box stays, so the layout around them is still compared |
| The dashboard background-job card | Its row count depends on what ran since start. Hidden |
| Backdrop blur, scrollbars, text caret | Chromium does not paint them the same way twice. Turned off for the shot |
| The random example service name | Different on every load by design. Replaced by fixed text |

Needs a local PostgreSQL (`UI_PG_URL`, default `postgresql+asyncpg://postgres@127.0.0.1:5433`), `createdb`/
`dropdb`, Node with Playwright and Pillow. It never touches the checkout's own `.env` or `data/`.

## Limits

The sample data and the fake panel cover the common states of each page, not every state live data can
reach. Pages that only show PasarGuard data are compared in their "panel data unavailable" state. A
pull request that passes all three checks should still be tried on a staging copy before production.

## Performance changes

A pull request that claims fewer database queries measures them with the query profile
(`docs/QUERY_PROFILE.md`) on the base and on the change, and puts both numbers in its description.
