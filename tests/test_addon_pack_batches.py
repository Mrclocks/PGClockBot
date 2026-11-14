"""Several general volume/time packs can be added at once; nothing is saved when one row is wrong."""

from __future__ import annotations

import unittest

from sqlalchemy import func, select

from app.api.plan_catalog_extras import pack_rows_from_form, packs_saved_message
from app.db.models import ServiceAddonPack
from app.services.service_addons import MAX_PACK_ROWS, PackRow, create_packs, default_pack_name
from app.services.shop_scope import ShopScopeError
from tests.test_addon_general_scope import OWNER, AddonCase

SHOP = {"role": "reseller", "bot_user_id": 5, "permissions": ["plans"]}


class CreatePacksTests(AddonCase):
    async def count(self) -> int:
        async with self.Session() as session:
            return int(await session.scalar(select(func.count()).select_from(ServiceAddonPack)))

    async def test_rows_become_packs_with_names_from_their_amount(self) -> None:
        async with self.Session() as session:
            packs = await create_packs(session, OWNER, kind="volume", rows=[
                PackRow(5, 50_000), PackRow(10, 90_000, "success"), PackRow(2.5, 30_000, "inherit"),
            ])
        self.assertEqual([p.name for p in packs], ["۵ گیگ", "۱۰ گیگ", "۲.۵ گیگ"])
        self.assertEqual([p.price for p in packs], [50_000, 90_000, 30_000])
        self.assertEqual([p.button_style for p in packs], [None, "success", None])
        self.assertTrue(all(p.is_general and p.owner_reseller_id is None for p in packs))

    async def test_one_bad_row_saves_nothing(self) -> None:
        before = await self.count()
        for rows in ([PackRow(5, 1), PackRow(0, 1)], [PackRow(5, 1), PackRow(5, -1)], [PackRow(7, 1), PackRow(1.5, 1)]):
            kind = "duration" if rows[0].amount == 7 else "volume"
            async with self.Session() as session:
                with self.assertRaises(ValueError):
                    await create_packs(session, OWNER, kind=kind, rows=rows)
        self.assertEqual(await self.count(), before)

    async def test_typed_name_needs_a_single_row(self) -> None:
        async with self.Session() as session:
            with self.assertRaises(ValueError):
                await create_packs(session, OWNER, kind="volume", name="ویژه", rows=[PackRow(1, 1), PackRow(2, 1)])
            [pack] = await create_packs(session, OWNER, kind="volume", name="ویژه", rows=[PackRow(1, 1)])
        self.assertEqual(pack.name, "ویژه")

    async def test_row_count_is_bounded(self) -> None:
        async with self.Session() as session:
            with self.assertRaises(ValueError):
                await create_packs(session, OWNER, kind="volume", rows=[])
            with self.assertRaises(ValueError):
                await create_packs(session, OWNER, kind="volume", rows=[PackRow(1, 1)] * (MAX_PACK_ROWS + 1))

    async def test_rows_share_the_category_and_plan_scope(self) -> None:
        async with self.Session() as session:
            tied = await create_packs(session, OWNER, kind="volume", rows=[PackRow(1, 1), PackRow(2, 1)], category_id="1")
            planned = await create_packs(session, OWNER, kind="duration", rows=[PackRow(3, 1), PackRow(7, 1)], plan_id=3)
        self.assertEqual({(p.category_id, p.is_general) for p in tied}, {(1, False)})
        self.assertEqual({(p.plan_id, p.category_id) for p in planned}, {(3, None)})

    async def test_a_shop_cannot_tie_packs_to_a_platform_plan_or_category(self) -> None:
        before = await self.count()
        async with self.Session() as session:
            with self.assertRaises(ValueError):
                await create_packs(session, SHOP, kind="volume", rows=[PackRow(1, 1), PackRow(2, 1)], plan_id=1)
            with self.assertRaises((ShopScopeError, ValueError)):
                await create_packs(session, SHOP, kind="volume", rows=[PackRow(1, 1)], category_id="1")
        self.assertEqual(await self.count(), before)

    async def test_shop_packs_belong_to_that_shop(self) -> None:
        async with self.Session() as session:
            packs = await create_packs(session, SHOP, kind="volume", rows=[PackRow(1, 1), PackRow(2, 1)])
        self.assertEqual({p.owner_reseller_id for p in packs}, {5})


class FormRowTests(unittest.TestCase):
    def test_repeated_fields_pair_into_rows(self) -> None:
        rows = pack_rows_from_form([1.0, 5.0], [10, 50], ["inherit", "danger"])
        self.assertEqual(rows, [PackRow(1.0, 10, "inherit"), PackRow(5.0, 50, "danger")])
        self.assertEqual([r.button_style for r in pack_rows_from_form([1.0], [10], [])], ["inherit"])

    def test_uneven_fields_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            pack_rows_from_form([1.0, 2.0], [10], [])
        with self.assertRaises(ValueError):
            pack_rows_from_form([1.0, 2.0], [10, 20], ["inherit"])

    def test_names_and_messages(self) -> None:
        self.assertEqual(default_pack_name("duration", 30), "۳۰ روز")
        self.assertEqual(packs_saved_message(1), "بسته افزونه ذخیره شد")
        self.assertEqual(packs_saved_message(3), "۳ بسته افزونه ذخیره شد")


if __name__ == "__main__":
    unittest.main()
