"""Frequently asked questions: validation, shop isolation, order, counters, the cap and the legacy import."""

from __future__ import annotations

import asyncio
import importlib.util
import unittest
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text

from app.db.models import FaqItem
from app.services import faq
from app.services.faq import FaqDraft, FaqError
from tests.postgres_support import PostgresTestCase, add_shop_users, scoped_sync_engine

ROOT = Path(__file__).resolve().parents[1]
SHOP_A = 41
SHOP_B = 42


def draft(question: str = "سرویس قطع شد؟", answer: str = "از «سرویس‌های من» تمدید کنید.", **kw) -> FaqDraft:
    return FaqDraft(question=question, answer=answer, **kw)


class FaqValidationTests(unittest.TestCase):
    def test_question_is_one_line_and_bounded(self):
        self.assertEqual(faq.clean_question("  چطور\nوصل   شوم؟ "), "چطور وصل شوم؟")
        with self.assertRaises(FaqError):
            faq.clean_question("   ")
        with self.assertRaises(FaqError):
            faq.clean_question("س" * (faq.QUESTION_MAX + 1))

    def test_answer_html_is_made_safe_for_telegram(self):
        self.assertEqual(faq.clean_answer("قیمت < ۱۰۰ <b>تومان</b>"), "قیمت &lt; ۱۰۰ <b>تومان</b>")
        self.assertEqual(faq.clean_answer("<script>x</script>"), "&lt;script&gt;x&lt;/script&gt;")
        with self.assertRaises(FaqError):
            faq.clean_answer("<b>باز")
        with self.assertRaises(FaqError):
            faq.clean_answer("")
        with self.assertRaises(FaqError):
            faq.clean_answer("ج" * (faq.ANSWER_MAX + 1))

    def test_button_style_allows_only_telegram_colors(self):
        self.assertIsNone(faq.clean_button_style(None))
        self.assertIsNone(faq.clean_button_style("inherit"))
        self.assertEqual(faq.clean_button_style(""), "")
        self.assertEqual(faq.clean_button_style("Success"), "success")
        for bad in ("purple", True, "primary;"):
            with self.assertRaises(FaqError):
                faq.clean_button_style(bad)

    def test_icon_must_be_a_premium_emoji_id(self):
        self.assertIsNone(faq.clean_icon(""))
        self.assertEqual(faq.clean_icon(" 5368324170671202286 "), "5368324170671202286")
        for bad in ("abc", "12x", True):
            with self.assertRaises(FaqError):
                faq.clean_icon(bad)

    def test_long_question_is_cut_on_the_button(self):
        label = faq.button_label("س" * 100)
        self.assertEqual(len(label), faq.BUTTON_LABEL_MAX)
        self.assertTrue(label.endswith("…"))

    def test_legacy_text_splits_into_questions(self):
        raw = "❓ حجم تمام شد؟\nتمدید کنید.\n\n❓ لینک کار نمی‌کند؟\nQR را دوباره بگیرید.\nیا پیام بدهید."
        self.assertEqual(
            faq.parse_legacy_faq_text(raw),
            [("حجم تمام شد؟", "تمدید کنید."), ("لینک کار نمی‌کند؟", "QR را دوباره بگیرید.\nیا پیام بدهید.")],
        )

    def test_legacy_text_without_questions_is_kept_whole(self):
        self.assertEqual(faq.parse_legacy_faq_text("هر سوالی داشتید بپرسید"), [(faq.LEGACY_TITLE, "هر سوالی داشتید بپرسید")])
        self.assertEqual(faq.parse_legacy_faq_text("  "), [])


class FaqServiceTests(PostgresTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await add_shop_users(self.Session, SHOP_A, SHOP_B)

    async def test_create_appends_in_order_per_shop(self):
        async with self.Session() as session:
            first = await faq.create_faq_item(session, draft("اول"), reseller_id=SHOP_A)
            second = await faq.create_faq_item(session, draft("دوم"), reseller_id=SHOP_A)
            await faq.create_faq_item(session, draft("پلتفرم"), reseller_id=None)
            items = await faq.list_faq_items(session, reseller_id=SHOP_A)
        self.assertEqual([i.id for i in items], [first.id, second.id])
        self.assertLess(first.sort_order, second.sort_order)

    async def test_other_shop_cannot_read_change_move_or_delete(self):
        async with self.Session() as session:
            item = await faq.create_faq_item(session, draft(), reseller_id=SHOP_A)
        for scope in (SHOP_B, None):
            async with self.Session() as session:
                with self.assertRaises(FaqError):
                    await faq.get_faq_item(session, item.id, reseller_id=scope)
                with self.assertRaises(FaqError):
                    await faq.update_faq_item(session, item.id, draft("دزدی"), reseller_id=scope)
                with self.assertRaises(FaqError):
                    await faq.move_faq_item(session, item.id, step=1, reseller_id=scope)
                with self.assertRaises(FaqError):
                    await faq.delete_faq_item(session, item.id, reseller_id=scope)
                self.assertIsNone(await faq.record_faq_view(session, item.id, reseller_id=scope))
                self.assertEqual(await faq.faq_buttons(session, reseller_id=scope), [])
        async with self.Session() as session:
            stored = await session.get(FaqItem, item.id)
        self.assertEqual(stored.question, "سرویس قطع شد؟")
        self.assertEqual(stored.view_count, 0)

    async def test_hidden_questions_are_not_shown_or_counted(self):
        async with self.Session() as session:
            item = await faq.create_faq_item(session, draft(is_active=False), reseller_id=SHOP_A)
            self.assertFalse(await faq.has_active_faq(session, reseller_id=SHOP_A))
            self.assertEqual(await faq.faq_buttons(session, reseller_id=SHOP_A), [])
            self.assertIsNone(await faq.record_faq_view(session, item.id, reseller_id=SHOP_A))
            await faq.change_faq_item(session, item.id, reseller_id=SHOP_A, is_active=True)
            self.assertTrue(await faq.has_active_faq(session, reseller_id=SHOP_A))

    async def test_views_and_tickets_are_counted_atomically(self):
        async with self.Session() as session:
            item = await faq.create_faq_item(session, draft(), reseller_id=SHOP_A)

        async def view() -> None:
            async with self.Session() as session:
                await faq.record_faq_view(session, item.id, reseller_id=SHOP_A)

        await asyncio.gather(*(view() for _ in range(8)))
        async with self.Session() as session:
            await faq.record_faq_ticket(session, item.id, reseller_id=SHOP_A)
            stored = await session.get(FaqItem, item.id)
        self.assertEqual((stored.view_count, stored.ticket_count), (8, 1))

    async def test_move_swaps_neighbours_and_keeps_ends(self):
        async with self.Session() as session:
            ids = [(await faq.create_faq_item(session, draft(f"س{i}"), reseller_id=SHOP_A)).id for i in range(3)]
            await faq.move_faq_item(session, ids[2], step=-1, reseller_id=SHOP_A)
            await faq.move_faq_item(session, ids[0], step=-1, reseller_id=SHOP_A)
            order = [i.id for i in await faq.list_faq_items(session, reseller_id=SHOP_A)]
        self.assertEqual(order, [ids[0], ids[2], ids[1]])

    async def test_move_fixes_rows_that_share_a_position(self):
        async with self.Session() as session:
            ids = [(await faq.create_faq_item(session, draft(f"س{i}"), reseller_id=SHOP_A)).id for i in range(2)]
            await session.execute(sa.update(FaqItem).values(sort_order=5))
            await session.commit()
            await faq.move_faq_item(session, ids[1], step=-1, reseller_id=SHOP_A)
            order = [i.id for i in await faq.list_faq_items(session, reseller_id=SHOP_A)]
        self.assertEqual(order, [ids[1], ids[0]])

    async def test_concurrent_creates_never_pass_the_cap(self):
        async with self.Session() as session:
            session.add_all(
                FaqItem(reseller_id=SHOP_A, question=f"س{i}", answer="ج", sort_order=i)
                for i in range(faq.MAX_FAQ_ITEMS - 2)
            )
            await session.commit()

        async def create(n: int) -> bool:
            async with self.Session() as session:
                try:
                    await faq.create_faq_item(session, draft(f"تازه {n}"), reseller_id=SHOP_A)
                except FaqError:
                    return False
                return True

        results = await asyncio.gather(*(create(n) for n in range(6)))
        self.assertEqual(sum(results), 2)
        async with self.Session() as session:
            count = await session.scalar(select(sa.func.count(FaqItem.id)).where(FaqItem.reseller_id == SHOP_A))
            positions = list(await session.scalars(select(FaqItem.sort_order).where(FaqItem.reseller_id == SHOP_A)))
        self.assertEqual(count, faq.MAX_FAQ_ITEMS)
        self.assertEqual(len(set(positions)), len(positions))

    async def test_invalid_input_saves_nothing(self):
        async with self.Session() as session:
            with self.assertRaises(FaqError):
                await faq.create_faq_item(session, draft(answer="<b>بی‌پایان"), reseller_id=SHOP_A)
            self.assertEqual(await faq.list_faq_items(session, reseller_id=SHOP_A), [])

    async def test_change_rejects_unknown_fields(self):
        async with self.Session() as session:
            item = await faq.create_faq_item(session, draft(), reseller_id=SHOP_A)
            with self.assertRaises(TypeError):
                await faq.change_faq_item(session, item.id, reseller_id=SHOP_A, reseller_id_override=SHOP_B)

    async def test_legacy_import_runs_once_and_only_on_an_empty_list(self):
        raw = "❓ سوال یک؟\nجواب یک\n\n❓ سوال دو؟\nجواب دو"
        async with self.Session() as session:
            self.assertEqual(await faq.import_legacy_faq(session, raw, reseller_id=SHOP_A), 2)
            with self.assertRaises(FaqError):
                await faq.import_legacy_faq(session, raw, reseller_id=SHOP_A)
            self.assertEqual(await faq.import_legacy_faq(session, raw, reseller_id=SHOP_B), 2)
            questions = [i.question for i in await faq.list_faq_items(session, reseller_id=SHOP_A)]
        self.assertEqual(questions, ["سوال یک؟", "سوال دو؟"])

    async def test_deleting_the_shop_removes_its_questions(self):
        async with self.Session() as session:
            await faq.create_faq_item(session, draft(), reseller_id=SHOP_B)
            await session.execute(text("DELETE FROM bot_users WHERE id = :id"), {"id": SHOP_B})
            await session.commit()
            self.assertEqual(await faq.list_faq_items(session, reseller_id=SHOP_B), [])


class FaqMigrationTests(unittest.TestCase):
    def test_upgrade_twice_then_downgrade(self):
        spec = importlib.util.spec_from_file_location("faq_items_mig", ROOT / "alembic/versions/0138_faq_items.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = scoped_sync_engine()
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE bot_users (id INTEGER PRIMARY KEY)"))
            conn.execute(text("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)"))
            conn.execute(text("INSERT INTO settings VALUES ('faq_text', 'قدیمی')"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                inspector = sa.inspect(conn)
                self.assertTrue(inspector.has_table("faq_items"))
                self.assertIn("ix_faq_items_shop_order", {i["name"] for i in inspector.get_indexes("faq_items")})
                conn.execute(text("INSERT INTO faq_items (question, answer) VALUES ('س', 'ج')"))
                self.assertTrue(conn.execute(text("SELECT is_active FROM faq_items")).scalar())
                migration.downgrade()
                self.assertFalse(sa.inspect(conn).has_table("faq_items"))
            self.assertEqual(conn.execute(text("SELECT value FROM settings")).scalar(), "قدیمی")
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
