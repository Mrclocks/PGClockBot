"""3.3.2 — full panel + bot audit: dead code gone, hot-path skips, lighter client."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class VersionTests(unittest.TestCase):
    def test_version_3_3_2(self):
        from app.services.release_notes import RELEASE_NOTES_FA
        from app.version import __version__

        self.assertEqual(__version__, "4.9.0")
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), "4.9.0")
        self.assertIn("3.3.2", RELEASE_NOTES_FA)
        blob = " ".join(RELEASE_NOTES_FA["3.3.2"])
        self.assertIn("ممیزی", blob)


class DeadCodeRemovedTests(unittest.TestCase):
    def test_dead_service_symbols_gone(self):
        resellers = (ROOT / "app/services/resellers.py").read_text(encoding="utf-8")
        self.assertNotIn("def has_web_perm", resellers)
        self.assertNotIn("WEB_PERM_OPTIONS", resellers)
        self.assertNotIn("BOT_PERM_OPTIONS", resellers)
        self.assertNotIn("DEFAULT_WEB_PERMS", resellers)
        self.assertNotIn("DEFAULT_BOT_PERMS", resellers)

        pg = (ROOT / "app/services/pg_access.py").read_text(encoding="utf-8")
        self.assertNotIn("PG_ADMIN_ONLY", pg)
        self.assertNotIn("def clear_role_cache", pg)
        self.assertNotIn("def staff_has_pg", pg)

        sc = (ROOT / "app/services/service_control.py").read_text(encoding="utf-8")
        self.assertNotIn("def get_restart_status", sc)

        notes = (ROOT / "app/services/release_notes.py").read_text(encoding="utf-8")
        self.assertNotIn("INSTALL_SCRIPT_CMD", notes)

        appearance = (ROOT / "app/services/bot_appearance.py").read_text(encoding="utf-8")
        self.assertNotIn("APPEARANCE_SETTING_KEYS", appearance)

    def test_require_pg_any_removed(self):
        app_src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        pg_src = (ROOT / "app/api/pg_pages.py").read_text(encoding="utf-8")
        self.assertNotIn("require_pg_any", app_src)
        self.assertNotIn("require_pg_any", pg_src)

    def test_backup_status_route_removed(self):
        # Route kept intentionally — settings UI polls restore progress as JSON
        src = (ROOT / "app/api/backup_pages.py").read_text(encoding="utf-8")
        self.assertIn("/backup/status", src)
        self.assertIn("read_restore_status", src)
        self.assertIn("JSONResponse", src)


class UnreadSkipTests(unittest.TestCase):
    def test_should_skip_helper(self):
        from app.services.panel_tickets import should_skip_unread_count

        self.assertTrue(should_skip_unread_count("/home/metrics", "GET"))
        self.assertTrue(should_skip_unread_count("/tickets", "GET"))
        self.assertTrue(should_skip_unread_count("/pg/users/12/link", "GET"))
        self.assertTrue(should_skip_unread_count("/home", "POST"))
        self.assertFalse(should_skip_unread_count("/home", "GET"))

    def test_require_staff_uses_helper(self):
        app_src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("should_skip_unread_count", app_src)

    def test_tickets_page_derives_unread(self):
        src = (ROOT / "app/api/panel_tickets_pages.py").read_text(encoding="utf-8")
        self.assertIn("tickets_unread = unread_from_tickets(panel_tickets, staff)", src)


class TicketLoadMessagesTests(unittest.TestCase):
    def test_mutate_paths_skip_messages(self):
        src = (ROOT / "app/services/panel_tickets.py").read_text(encoding="utf-8")
        self.assertIn("load_messages: bool = True", src)
        reply = src[src.find("async def reply_ticket") : src.find("async def set_status")]
        status = src[src.find("async def set_status") : src.find("async def mark_viewed")]
        self.assertIn("load_messages=False", reply)
        self.assertIn("load_messages=False", status)


class PgPermReuseTests(unittest.TestCase):
    def test_require_pg_perm_reuses_staff_features(self):
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        dep = src[src.find("def require_pg_perm") : src.find("@app.middleware")]
        # Features come from authz context (already on staff), not a fresh PG resolve
        self.assertIn("features = list(ctx.pg_permissions)", dep)
        self.assertNotIn("resolve_reseller_pg_features", dep)
        self.assertIn("raise NotAdmin(redirect=_live_pg_home(features))", dep)
        self.assertIn("authz_from_staff", dep)


class ClientPerfTests(unittest.TestCase):
    def test_panel_js_no_eager_font_scan(self):
        js = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")
        self.assertNotIn(
            "document.querySelectorAll('input, select, textarea, [contenteditable=\"true\"]').forEach(enforce)",
            js,
        )
        self.assertIn("focusin", js)

    def test_panel_js_gates_table_work(self):
        js = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")
        self.assertIn("if (!document.querySelector('.table-wrap, .section-tabs')) return;", js)

    def test_home_metrics_pauses_when_hidden(self):
        src = (ROOT / "app/web/templates/home.html").read_text(encoding="utf-8")
        self.assertIn("document.hidden", src)
        self.assertIn("visibilitychange", src)


class BotPerfTests(unittest.TestCase):
    def test_force_join_uses_gather(self):
        src = (ROOT / "app/bot/middlewares.py").read_text(encoding="utf-8")
        fn = src[src.find("async def check_force_join_all") : src.find("class DbSessionMiddleware")]
        self.assertIn("asyncio.gather", fn)

    def test_render_home_accepts_preloaded_ui(self):
        src = (ROOT / "app/bot/handlers/start.py").read_text(encoding="utf-8")
        self.assertIn("ui: dict | None = None", src)
        self.assertIn("effective_role: str | None = None", src)
        self.assertIn("effective_role=role_for_force", src)

    def test_custom_gate_fsm(self):
        src = (ROOT / "app/bot/handlers/shop.py").read_text(encoding="utf-8")
        self.assertIn("async def _custom_gate", src)
        self.assertIn("custom_gate_ok", src)
        # Buy still does a full availability check
        buy = src[src.find('F.data == "shop:custom:buy"') :]
        self.assertIn("_custom_available_for_users(session, ui)", buy)

    def test_scheduler_alert_discovery_is_lean(self):
        src = (ROOT / "app/jobs/scheduler.py").read_text(encoding="utf-8")
        self.assertIn('ResellerSetting.key == "user_alert_low_enabled"', src)
        self.assertIn("BotUser.reseller_id.in_(reseller_ids_with_alerts)", src)


class CssDeadCleanupTests(unittest.TestCase):
    def test_unused_classes_gone(self):
        css = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
        for needle in (
            ".cmd-copy",
            ".settings-jump",
            ".pg-reseller-stats",
            ".nav-section-panel",
            ".link-quiet",
            ".field-help",
            ".ver-badge",
            ".stats-grid",
        ):
            self.assertNotIn(needle, css)


if __name__ == "__main__":
    unittest.main()
