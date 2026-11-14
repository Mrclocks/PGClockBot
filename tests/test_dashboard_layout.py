"""Dashboard order, the connection banner, page counters and the bot reason (DESIGN_SYSTEM.md §6, §6-3)."""

from __future__ import annotations

import os
import re
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.api.app import templates
from app.services import home_overview

ENV = templates.env


def render(body: str, **ctx: object) -> str:
    imports = '{% from "_home_widgets.html" import connection_banner %}{% from "macros.html" import stat_strip %}'
    return ENV.from_string(imports + body).render(**ctx).strip()


BOT_DOWN = {"name": "ربات تلگرام", "detail": "عدم اتصال به تلگرام", "state": "err", "href": "/settings?tab=bot"}
PG_DOWN = {"name": "پاسارگارد", "detail": "اتصال به پاسارگارد برقرار نشد", "state": "err", "href": "/pg"}
PG_UP = {"name": "پاسارگارد", "detail": "آنلاین", "state": "ok", "href": "/pg"}
UNCHECKED = {"name": "نود", "detail": "بررسی نشده", "state": "neutral", "href": "/pg/nodes"}


class ConnectionBannerTests(unittest.TestCase):
    def test_nothing_is_shown_while_every_link_is_up_or_unchecked(self) -> None:
        self.assertEqual(render("{{ connection_banner(tiles) }}", tiles=[PG_UP, UNCHECKED]), "")

    def test_every_failing_link_and_its_reason_is_one_banner(self) -> None:
        html = render("{{ connection_banner(tiles) }}", tiles=[BOT_DOWN, PG_UP, PG_DOWN])
        self.assertEqual(html.count('role="alert"'), 1)
        self.assertIn("ربات تلگرام: عدم اتصال به تلگرام · پاسارگارد: اتصال به پاسارگارد برقرار نشد", html)
        self.assertNotIn("آنلاین", html)

    def test_check_leads_to_the_first_failing_link(self) -> None:
        html = render("{{ connection_banner(tiles) }}", tiles=[PG_UP, PG_DOWN, BOT_DOWN])
        self.assertIn('href="/pg">بررسی</a>', html)

    def test_reasons_are_escaped(self) -> None:
        html = render("{{ connection_banner(tiles) }}", tiles=[{**BOT_DOWN, "detail": "<script>x</script>"}])
        self.assertNotIn("<script>", html)


class StatStripTests(unittest.TestCase):
    CELLS = [
        {"label": "نزدیک انقضا", "value": "1٬234", "icon": "expiring", "href": "/users?filter=expiring", "active": True},
        {"label": "<b>حجم</b>", "value": 0, "icon": "low_volume", "href": "/users?filter=low_volume", "active": False},
    ]

    def test_each_counter_is_a_filter_link_and_marks_the_active_one(self) -> None:
        html = render("{{ stat_strip(cells, 'فیلتر کاربران') }}", cells=self.CELLS)
        self.assertIn('<nav class="stat-strip" aria-label="فیلتر کاربران">', html)
        links = re.findall(r'<a class="stat-cell[^"]*" href="[^"]+"[^>]*>', html)
        self.assertEqual(len(links), 2)
        self.assertIn('class="stat-cell is-active" href="/users?filter=expiring" aria-current="true"', links[0])
        self.assertNotIn("aria-current", links[1])
        self.assertIn('class="stat-cell-value">1٬234</strong>', html)
        self.assertNotIn("<b>حجم</b>", html)


class _Response:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _Client:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    async def get(self, url: str) -> _Response:
        return _Response(self._payload)


class BotReasonTests(unittest.IsolatedAsyncioTestCase):
    async def test_telegram_description_is_logged_not_shown(self) -> None:
        payload = {"ok": False, "error_code": 401, "description": "Unauthorized <provider text>"}
        with patch.object(home_overview, "_telegram_http", return_value=_Client(payload)), \
                self.assertLogs(home_overview.__name__, "WARNING") as logs:
            result = await home_overview._probe_bot("123:abc")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], home_overview.BOT_REFUSED_MESSAGE)
        self.assertIn("Unauthorized", "".join(logs.output))
        self.assertNotIn("123:abc", "".join(logs.output))

    async def test_a_working_bot_reports_its_username(self) -> None:
        payload = {"ok": True, "result": {"username": "shop_bot", "first_name": "Shop", "id": 1}}
        with patch.object(home_overview, "_telegram_http", return_value=_Client(payload)):
            result = await home_overview._probe_bot("123:abc")
        self.assertEqual((result["ok"], result["username"], result["error"]), (True, "shop_bot", None))


if __name__ == "__main__":
    unittest.main()


class DashboardOrderTests(unittest.TestCase):
    """Work and sales come first and connection health last; a failing link shows as the banner on top."""

    def dashboard(self, *, bot_ok: bool) -> str:
        bot = {"ok": bot_ok, "unchecked": False, "username": "shop_bot", "error": None if bot_ok else "عدم اتصال به تلگرام"}
        ctx = {"overview": {"bot": bot, "nodes": {"ok": True, "overall": "ok", "connected": 1, "total": 1}},
               "staff": {"role": "admin", "pg_permissions": []}, "pg_health": {"ok": True, "unchecked": False},
               "periods": {}, "action_center": {}, "show_pg_nodes": False}
        return ENV.get_template("_home_dash_body.html").render(**ctx)

    def test_figures_and_work_come_before_connection_tiles(self) -> None:
        html = self.dashboard(bot_ok=True)
        self.assertLess(html.index("kpi-strip"), html.index("home-main-grid"))
        self.assertLess(html.index("home-main-grid"), html.index("status-box"))
        self.assertNotIn('role="alert"', html)

    def test_a_failing_link_is_announced_above_the_figures(self) -> None:
        html = self.dashboard(bot_ok=False)
        self.assertLess(html.index('role="alert"'), html.index("kpi-strip"))
        self.assertIn("ربات تلگرام: عدم اتصال به تلگرام", html)


class UsersPageTests(unittest.TestCase):
    """The users page has no counters above its list: the filter row already offers each of them."""

    def test_no_counters_and_every_filter_stays_in_the_filter_row(self) -> None:
        from tests.test_marketing_nav import OWNER, _page

        html = _page("users.html", "/users", OWNER, rows=[], c={"total": 0, "expiring": 2, "alerts": 1}, flt="all",
                     q="", filter_href=lambda filter_key="all", q="", color=None: f"/users?filter={filter_key}")
        self.assertNotIn("stat-strip", html)
        tabs = html.split('aria-label="فیلتر کاربران"', 1)[1].split("</nav>", 1)[0]
        for key in ("expiring", "low_volume", "alerts"):
            self.assertIn(f"filter={key}", tabs)
        self.assertIn('class="search-submit"', html)
        self.assertNotIn(">جستجو</button>", html)
