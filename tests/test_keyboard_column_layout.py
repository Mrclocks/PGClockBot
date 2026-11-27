"""User keyboard column layout, list caps and admin plan list rows."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.bot.keyboards import (
    LIST_BUTTON_CAP,
    admin_plans_list_keyboard,
    admin_reseller_plans_list_keyboard,
    admin_user_service_actions,
    list_overflow_line,
    pack_detail_rows,
    pay_methods,
    plans_keyboard,
    services_keyboard,
)


def _plan(plan_id: int, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=plan_id, name=name, price=1, category_id=None, button_style=None, is_trial=False
    )


class UserKeyboardLayoutTests(unittest.TestCase):
    def test_pay_methods_wallet_alone_and_back_cancel_share_row(self) -> None:
        ui = {
            "pay_wallet_enabled": "1",
            "pay_card_enabled": "1",
            "pay_gateway_enabled": "1",
            "pay_psp_enabled": "0",
            "pay_crypto_enabled": "0",
            "pay_stars_enabled": "0",
            "pay_discount_enabled": "0",
        }
        rows = pay_methods(7, ui).inline_keyboard
        self.assertEqual([b.callback_data for b in rows[0]], ["pay:wallet:7"])
        self.assertEqual([b.callback_data for b in rows[1]], ["pay:card:7", "pay:gateway:7"])
        self.assertEqual([b.callback_data for b in rows[-1]], ["pay:back:7", "pay:x:7"])

    def test_plans_use_two_columns_only_for_short_labels(self) -> None:
        plans = [_plan(1, "a"), _plan(2, "b"), _plan(3, "x" * 40)]
        rows = plans_keyboard(plans, {}, back_callback="shop:list").inline_keyboard
        self.assertEqual([len(r) for r in rows], [2, 1, 1])

    def test_services_are_capped_and_two_columns(self) -> None:
        services = [SimpleNamespace(id=i, pg_username=f"u{i}") for i in range(LIST_BUTTON_CAP + 5)]
        rows = services_keyboard(services, {}).inline_keyboard
        self.assertEqual(sum(len(r) for r in rows), LIST_BUTTON_CAP)
        self.assertTrue(all(len(r) <= 2 for r in rows))
        self.assertIn("5", list_overflow_line(len(services)))
        self.assertEqual(list_overflow_line(3), "")


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


class AdminPlanListTests(unittest.TestCase):
    def _admin_plan(self, plan_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            id=plan_id, name=f"p{plan_id}", is_active=True, is_trial=False,
            pg_template_id=1, pg_group_ids="", button_style=None,
        )

    def test_user_plan_list_has_add_button_and_caps_at_forty(self) -> None:
        plans = [self._admin_plan(i) for i in range(LIST_BUTTON_CAP + 7)]
        markup = admin_plans_list_keyboard(plans, {}, add_callback="adm:plan:add")
        callbacks = _callbacks(markup)
        self.assertIn("adm:plan:add", callbacks)
        self.assertEqual(sum(c.startswith("adm:plan:view:") for c in callbacks), LIST_BUTTON_CAP)

    def test_empty_reseller_plan_list_offers_add_not_keyboard_hint(self) -> None:
        markup = admin_reseller_plans_list_keyboard(
            [], {}, mode="fixed", add_callback="adm:resplan:add:fixed"
        )
        texts = [b.text for row in markup.inline_keyboard for b in row]
        self.assertIn("adm:resplan:add:fixed", _callbacks(markup))
        self.assertFalse(any("کیبورد" in text for text in texts))

    def test_detail_rows_pair_toggle_with_delete_and_keep_back_alone(self) -> None:
        from aiogram.types import InlineKeyboardButton as Btn

        rows = [
            [Btn(text="a", callback_data="x:1")],
            [Btn(text="t", callback_data="tg:1")],
            [Btn(text="b", callback_data="x:2")],
            [Btn(text="d", callback_data="dl:1")],
            [Btn(text="back", callback_data="bk")],
        ]
        packed = pack_detail_rows(rows, pair_prefixes=("tg:", "dl:"))
        self.assertEqual([[b.callback_data for b in r] for r in packed],
                         [["x:1", "x:2"], ["tg:1", "dl:1"], ["bk"]])

    def test_user_service_actions_have_unique_callbacks(self) -> None:
        callbacks = _callbacks(admin_user_service_actions(1, 2))
        self.assertEqual(len(callbacks), len(set(callbacks)))


if __name__ == "__main__":
    unittest.main()
