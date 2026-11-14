"""Contract tests for safe panel speed work (timing, shell-first, display timeouts)."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]


def render_dashboard_shell(template: str) -> str:
    from app.api.app import templates
    from app.api.home_pages import _EMPTY_ACTION, _EMPTY_FUNNEL, _unchecked_overview
    from app.services.home_overview import empty_period_stats
    from app.services.shop_scope import empty_shop_stats

    context = {
        "request": SimpleNamespace(url=SimpleNamespace(path="/home"), state=SimpleNamespace(), query_params={}),
        "staff": {"role": "admin", "pg_is_owner": True, "permissions": [], "pg_permissions": []},
        "overview": _unchecked_overview(), "pg_health": {"unchecked": True},
        "bot": {"unchecked": True}, "stats": empty_shop_stats(),
        "funnel": dict(_EMPTY_FUNNEL), "periods": empty_period_stats(),
        "action_center": dict(_EMPTY_ACTION), "widgets_deferred": True,
        "widgets_body_url": "/pg/body" if template == "pg_home.html" else "/home/body",
        "view": None, "nodes": [], "counts": {}, "reseller_overview": None,
    }
    return templates.env.get_template(template).render(context)


class PanelTimingTests(unittest.TestCase):
    def test_should_time_home_and_pg(self):
        from app.services.panel_timing import should_time

        self.assertTrue(should_time("/home"))
        self.assertTrue(should_time("/home/body"))
        self.assertTrue(should_time("/pg"))
        self.assertTrue(should_time("/pg/users"))
        self.assertFalse(should_time("/static/panel.js"))
        self.assertFalse(should_time("/login"))

    def test_mark_computes_auth_and_page(self):
        from app.services.panel_timing import begin, finish_log, mark, server_timing_header

        req = MagicMock()
        req.url.path = "/home"
        req.state = MagicMock()
        begin(req)
        mark(req, "handler")
        mark(req, "page_data")
        snap = finish_log(req, status_code=200)
        self.assertIsNotNone(snap)
        assert snap is not None
        self.assertIn("total_ms", snap)
        self.assertIn("auth_ms", snap)
        self.assertIn("page_ms", snap)
        hdr = server_timing_header(snap)
        self.assertIsNotNone(hdr)
        assert hdr is not None
        self.assertIn("total;dur=", hdr)


class PanelDisplayTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_returns_fallback_not_raise(self):
        from app.services.panel_display_timeout import display_await

        async def slow():
            await asyncio.sleep(5)
            return "done"

        out = await display_await(slow(), fallback={"unchecked": True}, label="t")
        self.assertEqual(out, {"unchecked": True})

    async def test_success_passes_through(self):
        from app.services.panel_display_timeout import display_await

        async def ok():
            return {"ok": True}

        out = await display_await(ok(), fallback={"unchecked": True}, label="t")
        self.assertEqual(out, {"ok": True})


class PanelShellFirstContractTests(unittest.TestCase):
    def test_home_body_uses_require_staff(self):
        src = (ROOT / "app/api/home_pages.py").read_text(encoding="utf-8")
        self.assertIn('@app.get("/home/body"', src)
        # Body route must re-run require_staff (not inherit shell auth).
        body = src[src.find("async def home_dashboard_body") : src.find("def _degraded_home_shell")]
        self.assertIn("Depends(require_staff)", body)

    def test_home_shell_after_require_staff(self):
        src = (ROOT / "app/api/home_pages.py").read_text(encoding="utf-8")
        home = src[src.find("async def home_dashboard") : src.find("async def home_dashboard_body")]
        # Depends must appear before fast shell work.
        self.assertLess(home.find("Depends(require_staff)"), home.find("_fast_home_shell"))
        self.assertIn("_wants_full_widgets", home)

    def test_no_panel_navigate(self):
        js = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")
        self.assertNotIn("panelNavigate", js)
        defer = (ROOT / "app/web/templates/_panel_widgets_defer.html").read_text(encoding="utf-8")
        self.assertNotIn("panelNavigate", defer)
        self.assertIn("redirect: 'manual'", defer)

    def test_templates_include_defer_and_body_partials(self) -> None:
        for template in ("home.html", "reseller_home.html"):
            with self.subTest(template=template):
                html = render_dashboard_shell(template)
                self.assertEqual(html.count('id="home-dash"'), 1)
                self.assertIn("وضعیت اتصال", html)
                self.assertIn('id="panel-widget-status"', html)
                self.assertNotRegex(html, r'<div[^>]*id="home-dash"[^>]*>\s*</div>')
                self.assertIn("redirect: 'manual'", html)

    def test_home_body_skips_sidebar_unread(self):
        from app.services.panel_tickets import should_skip_unread_count

        self.assertTrue(should_skip_unread_count("/home/body", "GET"))
        self.assertFalse(should_skip_unread_count("/home", "GET"))

    def test_pg_keeps_require_pg_perm_and_display_timeout(self):
        src = (ROOT / "app/api/pg_pages.py").read_text(encoding="utf-8")
        pg = src[src.find("async def pg_home") : src.find("async def pg_home_body")]
        self.assertIn('Depends(require_pg_perm("pg_overview"))', pg)
        self.assertIn("_pg_chrome_context", src)
        self.assertIn("_pg_display_widgets", src)
        self.assertIn('"/pg/body"', src)
        self.assertIn("widgets_deferred", pg)
        # Body route re-runs same perm.
        body = src[src.find("async def pg_home_body") : src.find("async def pg_host_metrics_json")]
        self.assertIn('Depends(require_pg_perm("pg_overview"))', body)
        self.assertIn('"_pg_dash_body.html"', body)
        # No SPA nav.
        self.assertNotIn("panelNavigate", src)

    def test_pg_shell_first_templates(self) -> None:
        html = render_dashboard_shell("pg_home.html")
        self.assertEqual(html.count('id="pg-dash"'), 1)
        self.assertNotRegex(html, r'<div[^>]*id="pg-dash"[^>]*>\s*</div>')
        self.assertIn('id="panel-widget-status"', html)
        self.assertIn("/pg/body", html)
        self.assertIn("panel-widgets-ready", html)
        self.assertIn("/static/overview-live.js", html)

    def test_deferred_widgets_use_nav_clock_not_veil(self):
        """Deferred /body shows #panel-nav-clock — never the deleted page-load veil."""
        base = (ROOT / "app/web/templates/base.html").read_text(encoding="utf-8")
        self.assertNotIn('id="page-load-veil"', base)
        self.assertNotIn('id="page-skeleton"', base)
        self.assertIn('id="panel-nav-clock"', base)
        defer = (ROOT / "app/web/templates/_panel_widgets_defer.html").read_text(encoding="utf-8")
        self.assertNotIn("armVeil", defer)
        self.assertNotIn("page-loading", defer)
        self.assertIn("panel-nav-clock", defer)
        self.assertIn("panel-widgets-loading", defer)
        for rel in (
            "app/web/templates/home.html",
            "app/web/templates/reseller_home.html",
            "app/web/templates/pg_home.html",
        ):
            src = (ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn("_panel_widgets_loading.html", src)

    def test_pg_body_skips_ticket_chrome(self):
        src = (ROOT / "app/api/pg_pages.py").read_text(encoding="utf-8")
        body = src[src.find("async def pg_home_body") : src.find("async def pg_host_metrics_json")]
        self.assertIn("resolve_pg_open_url", body)
        self.assertIn("_pg_display_widgets", body)
        self.assertNotIn("_pg_chrome_context", body)

    def test_pg_users_paginated(self):
        src = (ROOT / "app/api/pg_pages.py").read_text(encoding="utf-8")
        users_fn = src[src.find("async def pg_users") : src.find("async def pg_users_create")]
        self.assertIn("parse_list_page", users_fn)
        self.assertIn("DEFAULT_LIST_PAGE_SIZE", users_fn)
        self.assertIn("build_list_pager", users_fn)
        self.assertNotIn('"limit": 200', users_fn)
        self.assertIn('Depends(require_pg_perm("pg_users"))', users_fn)
        tmpl = (ROOT / "app/web/templates/pg_users.html").read_text(encoding="utf-8")
        self.assertIn("list-pager", tmpl)
        self.assertIn("pager.has_next", tmpl)
        css = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertIn(".list-pager", css)

    def test_pg_body_skips_sidebar_unread(self):
        from app.services.panel_tickets import should_skip_unread_count

        self.assertTrue(should_skip_unread_count("/pg/body", "GET"))
        self.assertFalse(should_skip_unread_count("/pg", "GET"))

    def test_middleware_registered_observation_only(self):
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("panel_timing_middleware", src)
        self.assertIn("Observation-only timing", src)

    def test_guardrails_untouched(self):
        """Hard security surfaces must not be edited by this speed work."""
        # Spot-check: require_staff definition still present; this file is huge —
        # we only assert timing middleware does not wrap/replace require_staff.
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("async def require_staff(", src)
        timing = src[src.find("panel_timing_middleware") : src.find("async def security_headers")]
        self.assertNotIn("require_staff", timing)

    def test_home_body_ops_use_isolated_parallel_sessions(self):
        src = (ROOT / "app/api/home_pages.py").read_text(encoding="utf-8")
        self.assertIn("async def _parallel_home_ops(", src)
        self.assertIn("async with SessionLocal() as s:", src)
        self.assertIn("asyncio.gather(_funnel(), _periods(), _action())", src)
        # Must not gather concurrent work on the request session.
        self.assertNotIn(
            "await asyncio.gather(\n                _safe_funnel(session",
            src,
        )

    def test_widget_loading_clock_partial_only(self):
        """Clock spinner remains for optional in-page widget placeholder only."""
        base = (ROOT / "app/web/templates/base.html").read_text(encoding="utf-8")
        self.assertNotIn("page-load-veil", base)
        loading = (ROOT / "app/web/templates/_panel_widgets_loading.html").read_text(encoding="utf-8")
        self.assertIn("panel-load-clock", loading)
        css = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertIn(".panel-load-clock-face", css)
        self.assertNotIn("html.page-loading", css)
        # Capability probe cache is bounded (~3 min) and identity-scoped in pg_read_cache.
        cache = (ROOT / "app/services/pg_read_cache.py").read_text(encoding="utf-8")
        self.assertIn("CAPABILITY_PROBE_MAX_AGE_SEC = 180.0", cache)


if __name__ == "__main__":
    unittest.main()
