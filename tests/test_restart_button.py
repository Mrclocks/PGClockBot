"""The level-0 reply keyboard ends with an admin-toggleable «شروع مجدد» row."""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import MessageEntity

from app.bot import keyboards as kb
from app.services.rich_text import pack_rich_text
from app.bot.reply_keyboards import main_reply_keyboard, reseller_hub_main_keyboard

LABEL = "🔄 شروع مجدد"


def _rows(markup) -> list[list[str]]:
    return [[b.text for b in row] for row in markup.keyboard]


class RestartRowTests(unittest.TestCase):
    def test_customer_keyboard_ends_with_own_restart_row(self) -> None:
        rows = _rows(main_reply_keyboard("user", has_services=True, ui={}))
        self.assertEqual(rows[-1], [LABEL])
        self.assertEqual(sum(row.count(LABEL) for row in rows), 1)

    def test_compact_layout_keeps_restart_on_its_own_row(self) -> None:
        rows = _rows(
            main_reply_keyboard("user", has_services=True, ui={"menu_layout": "compact"})
        )
        self.assertEqual(rows[-1], [LABEL])

    def test_admin_keyboard_has_restart_below_admin_entry(self) -> None:
        rows = _rows(main_reply_keyboard("admin", ui={}))
        self.assertEqual(rows[-1], [LABEL])

    def test_shop_bot_reseller_keyboard_has_restart(self) -> None:
        rows = _rows(reseller_hub_main_keyboard(None, {}))
        self.assertEqual(rows[-1], [LABEL])

    def test_premium_emoji_in_label_becomes_button_icon(self) -> None:
        packed = pack_rich_text(
            "🔄 شروع مجدد",
            [MessageEntity(type="custom_emoji", offset=0, length=2, custom_emoji_id="5123")],
        )
        markup = main_reply_keyboard("user", has_services=True, ui={"btn_restart": packed})
        restart = markup.keyboard[-1][0]
        self.assertEqual(restart.icon_custom_emoji_id, "5123")
        self.assertEqual(restart.text, "شروع مجدد")

    def test_toggle_off_hides_button_but_label_still_navigates_home(self) -> None:
        ui = {"show_restart_button": "0"}
        rows = _rows(main_reply_keyboard("user", ui=ui))
        self.assertNotIn(LABEL, [t for row in rows for t in row])
        mapping = kb.reply_action_map("user", has_services=False, ui=ui)
        self.assertEqual(mapping.get(LABEL), kb.REPLY_ACTION_HOME)

    def test_admin_preview_keeps_exit_button_last(self) -> None:
        rows = _rows(
            main_reply_keyboard(
                "admin", ui={}, as_user=True, preview_exit_action=kb.REPLY_ACTION_ADMIN
            )
        )
        self.assertNotIn(LABEL, [t for row in rows for t in row])

    def test_customer_view_keyboard_keeps_restart(self) -> None:
        rows = _rows(main_reply_keyboard("admin", ui={}, as_user=True))
        self.assertEqual(rows[-1], [LABEL], "only the staff preview drops the restart row")

    def test_custom_label_is_recognised_as_home(self) -> None:
        ui = {"btn_restart": "♻️ از اول"}
        self.assertTrue(kb.is_home_text("♻️ از اول", ui))
        mapping = kb.reply_action_map("user", has_services=False, ui=ui)
        self.assertEqual(mapping.get("♻️ از اول"), kb.REPLY_ACTION_HOME)
        self.assertFalse(kb.is_home_text("متن دیگر", ui))

    def test_blank_label_hides_button(self) -> None:
        rows = _rows(main_reply_keyboard("user", ui={"btn_restart": " "}))
        self.assertNotIn(LABEL, [t for row in rows for t in row])


class RestartActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_clears_state_and_renders_home(self) -> None:
        from app.bot.handlers import reply_nav

        state = MagicMock(clear=AsyncMock())
        render_home = AsyncMock()
        with (
            patch("app.bot.handlers.start.render_home", new=render_home),
            patch.object(reply_nav.nav, "clear_nav", new=AsyncMock()) as clear_nav,
        ):
            await reply_nav.reply_main_nav(
                MagicMock(), MagicMock(), MagicMock(), state,
                reply_action=kb.REPLY_ACTION_HOME, reply_ui={}, reply_role="user",
            )
        state.clear.assert_awaited_once()
        clear_nav.assert_awaited_once()
        render_home.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()


class RestartAfterPurchaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_keyboard_sent_with_a_delivery_keeps_restart(self) -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from app.db.models import Base, BotUser, Order
        from app.services.delivery import _buyer_reply_markup
        from app.services.users import clear_settings_cache

        clear_settings_cache()
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            session.add_all([
                BotUser(id=1, telegram_id=101, referral_code="u1", role="user"),
                Order(id=1, user_id=1, amount=1_000),
            ])
            await session.commit()
            markup = await _buyer_reply_markup(session, None, await session.get(Order, 1))
        await engine.dispose()
        clear_settings_cache()
        self.assertEqual(_rows(markup)[-1], [LABEL])
