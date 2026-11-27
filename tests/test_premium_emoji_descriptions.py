"""Premium emoji survive in plan and category descriptions, message templates and connection status texts."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace

import sqlalchemy as sa
from aiogram.types import MessageEntity
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.bot.connection_status_ui import monitor_text
from app.services.catalog_descriptions import description_html, describe_items
from app.services.customer_notice_texts import notice_text_key
from app.services.rich_text import (
    keeps_premium_emoji,
    pack_rich_text,
    pack_setting_from_message,
    premium_text_from_message,
    premium_text_html,
)
from tests.postgres_support import scoped_sync_engine

ROOT = Path(__file__).resolve().parents[1]
STAR = "⭐"
EMOJI_ID = "5368324170671202286"
TAG = f'<tg-emoji emoji-id="{EMOJI_ID}">{STAR}</tg-emoji>'


def emoji_at(offset: int) -> MessageEntity:
    return MessageEntity(type="custom_emoji", offset=offset, length=1, custom_emoji_id=EMOJI_ID)


def message(text: str, *entities: MessageEntity) -> SimpleNamespace:
    return SimpleNamespace(text=text, entities=list(entities))


class PremiumTextTests(unittest.TestCase):
    def test_a_bot_message_keeps_its_premium_emoji_after_the_strip(self) -> None:
        plain, packed = premium_text_from_message(f"  {STAR} سریع", [emoji_at(2)])
        self.assertEqual(plain, f"{STAR} سریع")
        self.assertEqual(premium_text_html(plain, packed), f"{TAG} سریع")

    def test_other_formatting_and_cut_off_emoji_are_not_kept(self) -> None:
        bold = MessageEntity(type="bold", offset=0, length=3)
        self.assertEqual(premium_text_from_message("سرعت بالا", [bold]), ("سرعت بالا", None))
        plain, packed = premium_text_from_message(f"ab{STAR}", [emoji_at(2)], limit=2)
        self.assertEqual((plain, packed), ("ab", None))

    def test_a_stale_premium_value_is_ignored_and_text_is_escaped(self) -> None:
        _, packed = premium_text_from_message(f"{STAR} قدیمی", [emoji_at(0)])
        self.assertEqual(premium_text_html("<b>جدید</b> & تازه", packed), "&lt;b&gt;جدید&lt;/b&gt; &amp; تازه")

    def test_hostile_text_around_the_emoji_is_escaped(self) -> None:
        plain, packed = premium_text_from_message(f"{STAR}<script>", [emoji_at(0)])
        self.assertEqual(premium_text_html(plain, packed), f"{TAG}&lt;script&gt;")


class DescriptionTests(unittest.TestCase):
    def item(self, text: str, *entities: MessageEntity, name: str = "حجمی") -> SimpleNamespace:
        plain, packed = premium_text_from_message(text, list(entities))
        return SimpleNamespace(name=name, description=plain, description_rich=packed)

    def test_the_category_description_block_shows_premium_emoji(self) -> None:
        block = describe_items([self.item(f"{STAR} پرسرعت", emoji_at(0))])
        self.assertEqual(block, f"<b>حجمی</b>\n{TAG} پرسرعت")

    def test_a_description_edited_on_the_web_shows_its_new_plain_text(self) -> None:
        item = self.item(f"{STAR} پرسرعت", emoji_at(0))
        item.description = "متن تازه"
        self.assertEqual(description_html(item), "متن تازه")

    def test_a_long_description_is_cut_without_breaking_the_emoji_tag(self) -> None:
        item = self.item(STAR + "ا" * 400, emoji_at(0))
        shown = description_html(item)
        self.assertTrue(shown.startswith(TAG))
        self.assertTrue(shown.endswith("…"))

    def test_a_plain_description_is_escaped_as_before(self) -> None:
        item = SimpleNamespace(name="x", description="a < b", description_rich=None)
        self.assertEqual(description_html(item), "a &lt; b")


class SettingTextTests(unittest.TestCase):
    def test_usage_alert_and_notice_templates_keep_premium_emoji_as_html(self) -> None:
        for key in ("user_alert_time_text", "user_alert_traffic_text", notice_text_key("service_renew")):
            with self.subTest(key=key):
                stored = pack_setting_from_message(key, message(f"{STAR} {{service_name}} & ...", emoji_at(0)))
                self.assertEqual(stored, f"{TAG} {{service_name}} &amp; ...")
                self.assertTrue(keeps_premium_emoji(key))

    def test_a_template_without_premium_emoji_is_stored_as_typed(self) -> None:
        self.assertEqual(pack_setting_from_message("user_alert_time_text", message("  ساده  ")), "ساده")

    def test_connection_status_texts_keep_premium_emoji(self) -> None:
        stored = pack_setting_from_message("connection_status_title", message(f"{STAR} وضعیت", emoji_at(0)))
        self.assertEqual(monitor_text({"connection_status_title": stored}, "connection_status_title"), f"{TAG} وضعیت")

    def test_a_plain_connection_status_text_is_escaped_as_before(self) -> None:
        self.assertEqual(monitor_text({"connection_status_empty": "a < b"}, "connection_status_empty"), "a &lt; b")

    def test_telegram_profile_texts_stay_plain(self) -> None:
        # Telegram's bot name and descriptions accept plain text only.
        for key in ("bot_tg_name", "bot_tg_short_description", "bot_tg_description"):
            self.assertFalse(keeps_premium_emoji(key))


def _load_migration():
    path = ROOT / "alembic/versions/0143_rich_descriptions.py"
    spec = importlib.util.spec_from_file_location("rich_descriptions_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RichDescriptionMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.migration = _load_migration()
        self.engine = scoped_sync_engine()
        self.addCleanup(self.engine.dispose)
        with self.engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE plans (id INTEGER PRIMARY KEY, description TEXT)"))
            conn.execute(sa.text("CREATE TABLE plan_categories (id INTEGER PRIMARY KEY, description VARCHAR(255))"))
            conn.execute(sa.text("INSERT INTO plans (id, description) VALUES (1, 'متن')"))

    def _run(self, conn, *, down: bool = False) -> None:
        with Operations.context(MigrationContext.configure(conn)):
            (self.migration.downgrade if down else self.migration.upgrade)()

    def columns(self, conn, table: str) -> set[str]:
        return {col["name"] for col in sa.inspect(conn).get_columns(table)}

    def test_upgrade_twice_then_downgrade_keeps_descriptions(self) -> None:
        packed = pack_rich_text(STAR, [emoji_at(0)])
        with self.engine.begin() as conn:
            self._run(conn)
            self._run(conn)
            for table in ("plans", "plan_categories"):
                self.assertIn("description_rich", self.columns(conn, table))
            conn.execute(sa.text("UPDATE plans SET description_rich = :v WHERE id = 1"), {"v": packed})
        with self.engine.begin() as conn:
            self._run(conn, down=True)
            self._run(conn, down=True)
            self.assertNotIn("description_rich", self.columns(conn, "plans"))
            self.assertEqual(conn.scalar(sa.text("SELECT description FROM plans WHERE id = 1")), "متن")

    def test_revision_follows_the_current_head(self) -> None:
        self.assertEqual(self.migration.down_revision, "0142_service_link_resets")


if __name__ == "__main__":
    unittest.main()
