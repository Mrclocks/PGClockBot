# Web-panel behavior during a PasarGuard outage

Authenticated pages previously waited for live PasarGuard capability checks before
sending HTML. Each lookup had its own 30-second HTTP timeout, and admin lookup
fallbacks could repeat that wait. Deferred dashboards then kept an empty mount
behind the navigation clock until a fragment request completed. A stored token
could also make the connection badge report success after the actual probe failed.

## Request and connection limits

`PanelConnectionMiddleware` establishes one deadline for PasarGuard reads within
each web request: `PG_WEB_READ_BUDGET_SEC`, four seconds by default, clamped to
1-15 seconds (an invalid value falls back to the default). API and Mini App requests keep their existing behavior.
The deadline is shared by DNS validation, token acquisition, API-root discovery,
authorization reads and subsequent display reads. HTTP reads consume the remaining
time rather than starting another full timeout for every fallback.

The middleware does not cancel the route, database session or transaction. Only
read operations and token acquisition receive the short deadline. PasarGuard
mutations retain their original timeout and unknown-outcome behavior, including
propagating transport failures without a new retry or refund.

For authenticated POST/mutation requests, the read budget ends when staff
authentication completes. Follow-up verification reads and work spawned by the
mutation keep their existing timeouts; a slow successful write cannot exhaust a
display deadline and incorrectly turn its verification into a timeout.

An unsuccessful API-root discovery remains retryable. A confirmed API root or
valid current-admin response marks discovery complete, so an initial outage does
not permanently keep API-key clients on a dashboard URL after the panel recovers.

A timeout, transport failure, DNS failure or 502/503/504 answer fails the rest of
that page's reads at once. Reads for everyone pause only after three such failures
within thirty seconds, so one slow request does not hide PasarGuard from every
shop. The first pause lasts five seconds; each failed recovery probe doubles it up
to sixty seconds, and a successful read resets it. A request that started before
the pause and fails late does not extend it. Other 5xx answers fail only that read.
`/pg/metrics` polling honours a pause but never starts one. Health is shared for
the configured panel's origin, with at most eight concurrent reads and no waiting
queue. After the pause, one read probes recovery. An older concurrent success
cannot erase a newer failure. Forbidden or unsupported endpoints do not open the
circuit. State is bounded to sixteen origins, including
temporary setup checks, and `reset_pg()` invalidates it after configuration changes.

DNS validation runs in a worker thread before credentials are sent. Requests share
an outstanding lookup, so cancelling a timed-out read does not start another DNS
worker for the same origin. Validations have a sixty-second cache and retain the
existing URL and metadata-host restrictions. Setup/settings validation also runs
outside the event loop. A shared DNS result never replaces a caller's API base path.

## Authorization and data isolation

Authorization continues to fail closed. A missing live capability check never
grants a cookie's permissions or selects the owner client for another shop. A
transport failure remains an unreachable account, rather than proof that an admin
was deleted. The existing four-second GET and eight-second capability caches stay
short; no expired ACL is used as an outage fallback.

GET cache partitions now include an opaque identity for the actual client. A new
client after credential rotation cannot reuse another client's cached account or
role response, even when the panel username is unchanged. Successful web-access
gate results keep their existing forty-five-second cache; temporary outage denials
expire after the first five-second pause so access can recover sooner.

Password verification runs in worker threads for owner, reseller, staff and
principal logins. Owner login captures the credential version before asynchronous
verification: a concurrent password change cannot issue a valid session for the
old password.

## Display behavior

Home and PasarGuard dashboards send a visible shell immediately after their normal
authorization checks. A small status message indicates that statistics are loading.
The navigation clock is reserved for navigation and no longer covers the page
while optional widgets load.

Fragment reads have a fifteen-second browser deadline that includes downloading
the response body; the server already bounds its PasarGuard reads, so the rest of
the wait is the transfer over the viewer's own link. Timeout/failure leaves the
visible shell available and offers a retry. The message names the failing side:
the viewer's connection (the request never reached the panel, or the browser
reports it is offline), a slow transfer (the deadline expired), the session, or
the server. Authentication redirects never inject a login page into the dashboard;
they offer an explicit login link. Fragments must match the existing dashboard
root before replacement. Safe Persian notices describe unavailable panel data.

Panel scripts use content fingerprints in their URLs so browsers cannot retain
the old navigation/widget behavior in the existing immutable one-week cache.
Keys are computed once at process startup, with no filesystem reads during page
requests. Restarting after an asset update refreshes the keys without a release
version bump. The PWA retains only the current exact URL for these scripts.

Live metrics use the same browser deadline and allow only one outstanding poll.
Failed or partial reads retain the last sample with a visible notice. A cached
token alone no longer proves connectivity: a limited role can verify its own
account when system statistics are forbidden. A bounded probe that could not run
or finish reports an unknown state with an explanatory notice.

## Weak servers and lossy links

The web panel talks to PasarGuard server to server, so the viewer's own internet
only affects how fast pages reach the browser. When PasarGuard often looks
unavailable on a working panel, check the route between the two servers first:

- Point `PG_BASE_URL` at the closest address of the panel (same data centre or a
  private network address) instead of a public CDN hostname when possible.
- Behind nginx or another reverse proxy, do not lower `keepalive_timeout` below a
  few seconds, so reused connections are not closed just as a request is sent.
- `/api/system` and `/api/nodes/realtime_stats` are the heaviest dashboard reads on
  a small PasarGuard server. Their slowness affects only the dashboard widgets.

## Measured behavior

The same fake panel returned a read timeout after 0.8 seconds on every admin
lookup. No network or real credentials were used.

| Scenario | Clean dev | Updated branch |
| --- | ---: | ---: |
| First admin access check | 4.0039 s | 0.8025 s |
| Next check during the outage | 4.0058 s | 0.0001 s |
| Panel calls across both checks | 10 | 1 |

Regression tests also cover a hung token request, slow/unsafe DNS, a shared read
deadline, concurrent reads, recovery probes, expired read budgets during a write,
unknown mutation outcomes, recreated credentials, denied capabilities and rendered
authenticated/unauthenticated pages. Browser tests execute fragment timeouts and
navigation/metrics behavior.

## Validation and scope

Validation used Python 3.12 on macOS and fake panel transports:

- Focused Python tests: 181 passed, with 41 passing subtests, including 25
  connection/authentication regressions. Final validation after updating dev:
  78 passed, with two passing subtests, covering the connection, asset-cache,
  dashboard and API-key behavior.
- Browser Node tests: 30 passed.
- Security manifest: 1,263 passed, including the asset-cache and mutation
  follow-up regressions.
- Expanded asset/PWA/UI validation: 210 passed and two failed. Both failure
  identities (`test_update_api_enabled`, `test_update_warns_stay_on_page`) also
  fail on clean dev and concern existing update-page source assertions.
- Full suite on clean `upstream/dev` (`122d6b77`): 3,645 passed, 79 failed and
  5 skipped. The updated branch's full run: 3,675 passed, 76 failed and 5 skipped.
  Comparing failure identities found no new failures; all remaining failures
  also occur on clean dev. These runs include the final discovery/concurrency,
  mutation follow-up and asset-cache regressions.
- Subsequently merged `upstream/dev` (`bff415c0`), which changed only release
  metadata. The focused and browser runs above passed after this merge.
  Release-note validation returned 12 passed and two failed on both the final
  branch and clean `bff415c0`; both failures are existing source assertions.
- `scripts/lint_changed.sh --details` and `git diff --check` passed.

No migration, dependency, permission, setting or release-version change is needed.
Commerce state machines and mutation retry policies are intentionally unchanged.
The limits apply per process; multiple web workers have separate small circuits
and caches. Linked accounts still require their existing PasarGuard authorization;
an outage is not an offline authorization grant.
