"""Guide titles, groups and settings headings use the menu glossary names; old names stay searchable."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.services.guide_catalog import TOPICS
from app.services.panel_alert_notifications import failures_text
from app.services.panel_nav import NavViewer, build_sidebar
from app.services.resellers import FEATURE_PERMS

# Guide topic -> (title, old name kept as a search alias)
RENAMED = {
    "home": ("داشبورد", "داشبورد وب پنل"),
    "inbox": ("مرکز اقدام", "اعلان‌ها"),
    "settings-panel": ("تنظیمات سیستم", "تنظیمات وب پنل"),
    "bot-dashboard": ("نمای کلی فروشگاه", "نمای کلی ربات"),
    "finance": ("مالی", "مدیریت مالی"),
    "connection-status": ("تست اتصال سرویس", "وضعیت اتصال"),
}
OWNER = NavViewer(role="admin", perms=frozenset(), pg_perms=frozenset({"pg_overview"}), is_owner=True,
                  can_add_representative=False, can_manage_representatives=True, marketing_hrefs=())


class GuideGlossaryTests(unittest.TestCase):
    def test_renamed_topics_use_the_new_title_and_keep_the_old_one_as_alias(self) -> None:
        for topic_id, (title, old) in RENAMED.items():
            with self.subTest(topic_id):
                self.assertEqual(TOPICS[topic_id]["title"], title)
                self.assertIn(old, TOPICS[topic_id]["aliases"])

    def test_topic_titles_match_the_owner_sidebar(self) -> None:
        labels = {item.label for section in build_sidebar(OWNER, path="/") for item in section.items}
        for topic_id in ("home", "inbox", "settings-panel", "bot-dashboard", "finance", "connection-status"):
            self.assertIn(TOPICS[topic_id]["title"], labels, topic_id)

    def test_guide_groups_are_the_sidebar_sections(self) -> None:
        groups = {t["nav_group"] for t in TOPICS.values()}
        self.assertNotIn("وب پنل", groups)
        self.assertNotIn("پنل ربات", groups)
        self.assertLessEqual({"سیستم", "فروشگاه", "پاسارگارد"}, groups)

    def test_alert_and_permission_use_the_page_names(self) -> None:
        self.assertIn("از مالی ←", failures_text(2))
        self.assertEqual(dict(FEATURE_PERMS)["tickets"], "پشتیبانی")


if __name__ == "__main__":
    unittest.main()
