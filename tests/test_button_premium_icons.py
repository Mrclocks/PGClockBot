"""Premium emoji on staff-named buttons: support contacts, guide titles and guide links.

A premium emoji sent with the name in the bot becomes the button's icon; web saves keep it;
each shop's contacts stay its own.
"""

from __future__ import annotations

import json

from aiogram.types import MessageEntity

from app.bot.handlers.guides import guide_detail_keyboard, guides_list_keyboard
from app.bot.keyboards import support_contacts_keyboard
from app.services.button_icons import ICON_KEY, clean_icon_id, split_label_and_icon
from app.services.connection_guides import (
    add_guide_link,
    create_guide,
    find_guide,
    serialize_submitted_guides,
    update_guide,
)
from app.services.guide_media import GuideMediaError
from app.services.support_contacts import (
    get_support_contacts,
    save_submitted_support_contacts,
    upsert_support_contact,
)
from app.services.users import clear_settings_cache
from tests.postgres_support import PostgresTestCase, add_shop_users

ICON = "5368324170671202286"
OTHER_ICON = "5370869711888194012"


def premium(label: str, icon: str = ICON) -> tuple[str, list[MessageEntity]]:
    """A Telegram message text that starts with one premium emoji, and its entities."""
    return f"⭐ {label}", [MessageEntity(type="custom_emoji", offset=0, length=1, custom_emoji_id=icon)]


def buttons(markup) -> list:
    return [button for row in markup.inline_keyboard for button in row]


class SplitLabelTests(PostgresTestCase):
    create_tables = False

    async def test_the_premium_glyph_becomes_the_icon_and_leaves_the_label(self) -> None:
        self.assertEqual(split_label_and_icon(*premium("پشتیبان فروش")), ("پشتیبان فروش", ICON))

    async def test_plain_text_has_no_icon(self) -> None:
        self.assertEqual(split_label_and_icon("پشتیبان", None), ("پشتیبان", None))

    async def test_a_name_made_only_of_premium_emoji_keeps_its_text(self) -> None:
        entities = [MessageEntity(type="custom_emoji", offset=0, length=1, custom_emoji_id=ICON)]
        self.assertEqual(split_label_and_icon("⭐", entities), ("⭐", ICON))

    async def test_malformed_icon_ids_are_dropped(self) -> None:
        for raw in ("abc", "1" * 40, True, "", None, '"><b>'):
            with self.subTest(raw=raw):
                self.assertIsNone(clean_icon_id(raw))


class SupportContactIconTests(PostgresTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        clear_settings_cache()
        self.addCleanup(clear_settings_cache)
        await add_shop_users(self.Session, 2, 3)

    async def add(self, *, icon: str | None, reseller_id: int | None = None) -> dict:
        async with self.Session() as session:
            item, err = await upsert_support_contact(
                session, contact_id=None, title="فروش", telegram="@sales_desk",
                icon_custom_emoji_id=icon, reseller_id=reseller_id,
            )
            await session.commit()
        self.assertIsNone(err)
        return item

    async def contacts(self, *, reseller_id: int | None = None) -> list[dict]:
        clear_settings_cache()
        async with self.Session() as session:
            return await get_support_contacts(session, reseller_id=reseller_id)

    async def test_the_contact_button_shows_the_premium_icon_without_the_default_prefix(self) -> None:
        await self.add(icon=ICON)
        [button] = buttons(support_contacts_keyboard(await self.contacts()))
        self.assertEqual((button.text, button.icon_custom_emoji_id), ("فروش", ICON))

    async def test_a_contact_without_an_icon_keeps_the_default_prefix(self) -> None:
        await self.add(icon=None)
        [button] = buttons(support_contacts_keyboard(await self.contacts()))
        self.assertEqual((button.text, button.icon_custom_emoji_id), ("💬 فروش", None))

    async def test_toggling_keeps_the_icon_and_renaming_without_one_clears_it(self) -> None:
        item = await self.add(icon=ICON)
        async with self.Session() as session:
            await upsert_support_contact(
                session, contact_id=item["id"], title="فروش", telegram="@sales_desk", enabled=False,
            )
            await session.commit()
        self.assertEqual((await self.contacts())[0][ICON_KEY], ICON)
        async with self.Session() as session:
            await upsert_support_contact(
                session, contact_id=item["id"], title="فروش", telegram="@sales_desk", icon_custom_emoji_id=None,
            )
            await session.commit()
        self.assertNotIn(ICON_KEY, (await self.contacts())[0])

    async def test_a_web_save_keeps_the_icon_set_in_the_bot(self) -> None:
        item = await self.add(icon=ICON)
        submitted = [{"id": item["id"], "title": "فروش ویژه", "telegram": "@sales_desk", "enabled": True}]
        async with self.Session() as session:
            await save_submitted_support_contacts(session, submitted)
            await session.commit()
        [saved] = await self.contacts()
        self.assertEqual((saved["title"], saved[ICON_KEY]), ("فروش ویژه", ICON))

    async def test_a_shop_icon_stays_in_that_shop(self) -> None:
        await self.add(icon=ICON, reseller_id=2)
        self.assertEqual(await self.contacts(reseller_id=3), [])
        self.assertEqual(await self.contacts(), [])
        async with self.Session() as session:
            await save_submitted_support_contacts(
                session, [{"id": "x1", "title": "سه", "telegram": "@shop_three"}], reseller_id=3,
            )
            await session.commit()
        self.assertNotIn(ICON_KEY, (await self.contacts(reseller_id=3))[0])
        self.assertEqual((await self.contacts(reseller_id=2))[0][ICON_KEY], ICON)


class GuideIconTests(PostgresTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        clear_settings_cache()
        self.addCleanup(clear_settings_cache)

    async def test_guide_and_link_buttons_show_their_premium_icons(self) -> None:
        async with self.Session() as session:
            guide = await create_guide(session, title="V2Box", body="متن", icon_custom_emoji_id=ICON, reseller_id=None)
            guide = await add_guide_link(
                session, guide["id"], label="ویدیو", url="https://youtu.be/abc",
                icon_custom_emoji_id=OTHER_ICON, reseller_id=None,
            )
            await session.commit()
        listed = next(b for b in buttons(guides_list_keyboard([guide], {})) if b.text == "V2Box")
        self.assertEqual(listed.icon_custom_emoji_id, ICON)
        link = next(b for b in buttons(guide_detail_keyboard(guide, {})) if b.url == "https://youtu.be/abc")
        self.assertEqual(link.icon_custom_emoji_id, OTHER_ICON)

    async def test_renaming_without_a_premium_emoji_clears_the_icon(self) -> None:
        async with self.Session() as session:
            guide = await create_guide(session, title="V2Box", body="متن", icon_custom_emoji_id=ICON, reseller_id=None)
            guide = await update_guide(session, guide["id"], reseller_id=None, title="V2", **{ICON_KEY: None})
            await session.commit()
        self.assertNotIn(ICON_KEY, guide)

    async def test_a_settings_form_save_keeps_guide_and_link_icons(self) -> None:
        async with self.Session() as session:
            guide = await create_guide(session, title="V2Box", body="متن", icon_custom_emoji_id=ICON, reseller_id=None)
            await add_guide_link(
                session, guide["id"], label="ویدیو", url="https://youtu.be/abc",
                icon_custom_emoji_id=OTHER_ICON, reseller_id=None,
            )
            await session.commit()
        form = [{"id": guide["id"], "title": "V2Box", "body": "متن تازه",
                 "links": [{"label": "ویدیو", "url": "https://youtu.be/abc"}]}]
        async with self.Session() as session:
            saved = json.loads(await serialize_submitted_guides(session, json.dumps(form), reseller_id=None))
        self.assertEqual(saved[0][ICON_KEY], ICON)
        self.assertEqual(saved[0]["links"][0][ICON_KEY], OTHER_ICON)

    async def test_a_shop_guide_icon_does_not_reach_the_platform_guides(self) -> None:
        await add_shop_users(self.Session, 2)
        async with self.Session() as session:
            guide = await create_guide(session, title="فروشگاه", body="متن", icon_custom_emoji_id=ICON, reseller_id=2)
            await session.commit()
        clear_settings_cache()
        async with self.Session() as session:
            self.assertEqual((await find_guide(session, guide["id"], reseller_id=2))[ICON_KEY], ICON)
            with self.assertRaises(GuideMediaError):
                await find_guide(session, guide["id"], reseller_id=None)
