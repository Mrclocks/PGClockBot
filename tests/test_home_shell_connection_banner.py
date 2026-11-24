"""The fast /home shell has not asked the panel yet, so it must not show a red «اتصال برقرار نیست» banner."""

from __future__ import annotations

import re
import unittest
from types import SimpleNamespace


BANNER = "اتصال برقرار نیست"


def render_home(*, nodes: dict | None = None) -> str:
    from app.api.app import templates
    from app.api.home_pages import _EMPTY_ACTION, _EMPTY_FUNNEL, _unchecked_overview
    from app.services.home_overview import empty_period_stats

    overview = _unchecked_overview()
    if nodes is not None:
        overview["nodes"] = nodes
    context = {
        "request": SimpleNamespace(url=SimpleNamespace(path="/home"), state=SimpleNamespace(), query_params={}),
        "staff": {"role": "admin", "pg_is_owner": True, "permissions": [], "pg_permissions": ["pg_nodes"]},
        "overview": overview, "pg_health": {"unchecked": True}, "show_pg_nodes": True,
        "funnel": dict(_EMPTY_FUNNEL), "periods": empty_period_stats(), "action_center": dict(_EMPTY_ACTION),
    }
    return templates.env.get_template("_home_dash_body.html").render(context)


def node_tile_state(html: str) -> str:
    return re.search(r'data-state="([a-z]+)" href="/pg/nodes"', html).group(1)


class HomeShellBannerTests(unittest.TestCase):
    def test_the_shell_shows_no_connection_alarm_before_anything_is_checked(self):
        html = render_home()
        self.assertNotIn(BANNER, html)
        self.assertEqual(node_tile_state(html), "neutral")
        self.assertIn("بررسی نشده", html)

    def test_nodes_that_are_really_down_still_raise_the_banner(self):
        html = render_home(nodes={
            "ok": False, "error": "پاسخی نیامد", "nodes": [], "total": 0, "connected": 0, "warn": 0,
            "error_count": 0, "overall": "err",
        })
        self.assertIn(BANNER, html)
        self.assertEqual(node_tile_state(html), "err")


if __name__ == "__main__":
    unittest.main()
