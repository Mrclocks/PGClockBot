# Query profile

Before optimizing database work, measure it. The query profile counts the SQL statements of every
web panel request and every bot button, finds statements that repeat inside one of them, and finds
the ones whose count grows with the amount of data: the clearest sign of a query per row (N+1).

## 1. Profile a version (local, throwaway databases)

```bash
export TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5433/pgclock_test
scripts/query_profile.sh            # HEAD; or scripts/query_profile.sh origin/dev
```

`$QP_OUT/report.md` (default `/tmp/pgclock-query-profile/report.md`) has, for the web panel and for
the bot:

| Table | Reads as |
|---|---|
| Heaviest units of work | statements and database time of the heaviest run of each route or button |
| Repeated statements | one statement shape run 5 times or more inside one request or tap |
| Statements that grow with the data | each URL or button with `QP_SMALL` (default 20) and `QP_SCALE` (default 200) more customers, orders, payments, services, tickets and wallet entries, with the statements that grew and the line in `app/` that ran them |

How to read the growth table:

- **Both runs have data.** A screen that shows a section only when there is something to show runs
  the same statements in both, so what is left is work done per row.
- **Cheapest run.** Each URL is compared at its cheapest run in each scale. A badge cache that
  happened to be cold, or another tab of the same page, does not count as growth.

How it runs:

- **Web.** The same sample data, fake panel and offline bot as the UI regression check
  (`docs/CLEANUP_SAFETY_NET.md`). The app is served by `scripts/query_profile_server.py`, which is
  `python -m app.main` with a recorder around each request. `scripts/ui_capture.cjs` then opens every
  page, tab and modal at 1440 px. Routes are grouped by template (`/users/{user_id}/edit`).
- **Bot.** `scripts/query_profile_bot.py` runs the real dispatcher in the chat simulator of the test
  suite. It taps every customer and owner menu button, every admin-panel button and every button of
  the admin sections they open.
- **What is counted.** Only statements run by the request or tap itself, and by tasks it starts.
  Scheduler jobs running at the same time are not counted (`scripts/query_counter.py`).

To check one change, profile the base and the change and compare the two reports. A pull request
that claims fewer queries puts the before and after numbers of the affected rows in its description.

## 2. Measure a live server (`pg_stat_statements`)

The profile above uses sample data. To see which statements cost most on a real server, enable
PostgreSQL's own statistics. This is read-only and does not change PGClockBot. It costs a little
shared memory and a few percent of query time.

1. Load the extension, then restart PostgreSQL:

   ```bash
   sudo -u postgres psql -c "ALTER SYSTEM SET shared_preload_libraries = 'pg_stat_statements'"
   sudo systemctl restart postgresql
   ```

   If `shared_preload_libraries` already lists other libraries, keep them in the list.

2. Create the extension in PGClockBot's database:

   ```bash
   sudo -u postgres psql -d <database> -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements"
   ```

3. After a day of normal use, list the statements that took the most total time:

   ```sql
   SELECT calls,
          round(total_exec_time::numeric / 1000, 1) AS total_s,
          round(mean_exec_time::numeric, 2)         AS mean_ms,
          rows,
          left(regexp_replace(query, '\s+', ' ', 'g'), 120) AS query
   FROM pg_stat_statements
   WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
   ORDER BY total_exec_time DESC
   LIMIT 25;
   ```

   - **High `calls` with a low `mean_ms`:** a statement run per row or per tap.
   - **High `mean_ms`:** a statement that may need an index; check it with `EXPLAIN`.

   `SELECT pg_stat_statements_reset();` starts a new measurement.

The statistics hold statement shapes with `$1` placeholders, not the values, so customer data does
not end up in them. Share the output only after checking it.

## Limits

- **Coverage.** The sample data covers the common states of each page and button, not every state
  live data can reach. A unit of work the tour does not open is not measured.
- **Timing.** Database times come from a local server with little data. Compare them with each
  other, not with production.
