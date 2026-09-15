"""Guide catalog + built static help site."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class GuideCatalogTests(unittest.TestCase):
    def test_topics_have_required_fields(self):
        from app.services.guide_catalog import NAV_ORDER, TOPICS, topic

        self.assertEqual(set(NAV_ORDER), set(TOPICS))
        for tid in NAV_ORDER:
            t = topic(tid)
            assert t is not None
            self.assertTrue(t["title"])
            self.assertTrue(t["summary"])
            self.assertTrue(t["slug"])
            self.assertTrue(t["nav_group"])

    def test_help_href_default_and_external(self):
        from app.services import guide_catalog as gc

        gc.panel_help_payload.cache_clear()
        href = gc.help_href("plans")
        self.assertTrue(href.startswith("/help/plans"))

    def test_built_guide_present(self):
        guide = ROOT / "app" / "web" / "static" / "guide"
        self.assertTrue((guide / "index.html").is_file())
        self.assertTrue((guide / "plans" / "index.html").is_file())
        self.assertTrue((guide / "search-index.json").is_file())
        self.assertTrue((guide / "catalog.json").is_file())
        html = (guide / "plans" / "index.html").read_text(encoding="utf-8")
        self.assertIn("پلن‌ها", html)
        self.assertIn("guide-search", html)
        self.assertIn("guide-nav-group-title", html)
        self.assertIn("guide-pager-btn", html)
        self.assertIn("guide-hero-icon", html)
        self.assertIn("guide-nav-group--bot", html)
        self.assertIn("guide-nav-group--pg", html)
        css = (guide / "guide.css").read_text(encoding="utf-8")
        self.assertIn("--bot-line", css)
        self.assertIn("--pg-line", css)
        self.assertIn(".guide-hero--bot", css)
        self.assertIn(".guide-hero--pg", css)
        self.assertIn('class="guide-callout guide-callout--error"', html)
        self.assertNotIn("&lt;div", html)
        self.assertNotIn("{% for", html)

    def test_tables_are_scroll_wrapped(self):
        guide = ROOT / "app" / "web" / "static" / "guide"
        roles = (guide / "roles" / "index.html").read_text(encoding="utf-8")
        self.assertIn("guide-table-wrap", roles)
        self.assertIn("<table>", roles)
        css = (guide / "guide.css").read_text(encoding="utf-8")
        self.assertIn(".guide-table-wrap", css)
        self.assertIn("overflow: auto", css)
        self.assertNotIn("position: sticky;\n  right: 0;", css)
        js = (guide / "guide.js").read_text(encoding="utf-8")
        self.assertIn("resolveGuideHref", js)
        self.assertIn("guide-table-wrap", js)

    def test_search_index_hrefs_are_guide_root_relative(self):
        import json

        guide = ROOT / "app" / "web" / "static" / "guide"
        items = json.loads((guide / "search-index.json").read_text(encoding="utf-8"))
        self.assertGreater(len(items), 5)
        for it in items:
            href = it["href"]
            self.assertFalse(href.startswith("../"), msg=href)
            self.assertFalse(href.startswith("/"), msg=href)
            self.assertTrue(
                href.endswith("/index.html") or href.endswith("index.html"),
                msg=href,
            )
            slug = href.split("/")[0]
            self.assertTrue((guide / slug / "index.html").is_file(), msg=href)
        js = (guide / "guide.js").read_text(encoding="utf-8")
        self.assertIn("resolveGuideHref(h.href)", js)
        self.assertIn("guideRoot +", js)

    def test_page_title_macro_has_help(self):
        macros = (ROOT / "app" / "web" / "templates" / "macros.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("page-help-btn", macros)
        self.assertIn("help=None", macros)
        plans = (ROOT / "app" / "web" / "templates" / "plans.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("help='plans'", plans)

    def test_page_title_macro_renders_help_from_globals(self):
        """Macros imported without ``with context`` must see guide_topics globals."""
        from jinja2 import Environment, FileSystemLoader, select_autoescape

        from app.services.guide_catalog import panel_help_payload

        env = Environment(
            loader=FileSystemLoader(str(ROOT / "app" / "web" / "templates")),
            autoescape=select_autoescape(["html"]),
        )
        panel_help_payload.cache_clear()
        env.globals["guide_topics"] = panel_help_payload()
        tpl = env.from_string(
            '{% from "macros.html" import page_title %}'
            "{{ page_title('plans', 'پلن‌ها', help='plans') }}"
        )
        html = tpl.render()
        self.assertIn("page-help-btn", html)
        self.assertIn("مطالعه کامل", html)
        self.assertIn("/help/plans", html)

    def test_help_mounted_in_app_factory(self):
        src = (ROOT / "app" / "api" / "app.py").read_text(encoding="utf-8")
        self.assertIn('"/help"', src)
        self.assertIn("help_docs", src)
        self.assertIn("path.startswith(\"/help\")", src)

    def test_sidebar_has_help_link(self):
        base = (ROOT / "app" / "web" / "templates" / "base.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("side-help-btn", base)
        self.assertIn('href="/help/"', base)
        self.assertIn("راهنما", base)
        self.assertNotIn("nav-section-help", base)
        self.assertNotIn('data-nav-mode="help"', base)
        # Footer no longer carries a second help entry
        self.assertNotIn('href="/help/">راهنما</a>', base.split("site-footer", 1)[-1] if "site-footer" in base else "")
        css = (ROOT / "app" / "web" / "static" / "panel.css").read_text(encoding="utf-8")
        self.assertIn(".side-help-btn", css)
        self.assertIn("rgba(34, 197, 94", css)
        self.assertNotIn(".nav-section-help", css)
        self.assertIn(".page-help-pop", css)
        self.assertIn("position: fixed", css)
        js = (ROOT / "app" / "web" / "static" / "panel.js").read_text(encoding="utf-8")
        self.assertIn("placeHelpPop", js)



if __name__ == "__main__":
    unittest.main()
