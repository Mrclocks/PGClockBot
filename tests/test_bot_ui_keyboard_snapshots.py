"""Safety net for the keyboards customers, resellers and admins see.

Each scenario builds a real keyboard from ``app.bot.keyboards`` with fixed
inputs and the default settings. The serialized keyboards are compared with a
reviewed snapshot, and every keyboard is checked against Telegram limits, so
a label, color, order or callback change cannot slip through unreviewed.
"""

from __future__ import annotations

import unittest
from collections.abc import Callable
from types import SimpleNamespace

from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup

from app.bot import keyboards as kb
from app.bot import nav_inline as hubs
from app.bot import reply_keyboards as rk
from app.db.models import Plan, ResellerProfile
from app.services.resellers import FEATURE_PERMS
from app.services.users import DEFAULT_SETTINGS
from tests.bot_ui_snapshots import (
    CALLBACK_DATA_MAX_BYTES,
    INLINE_KEYBOARD_MAX_BUTTONS,
    assert_matches_snapshot,
    dump_markup,
    inline_action_fields,
    inline_buttons,
    reply_buttons,
)

Markup = InlineKeyboardMarkup | ReplyKeyboardMarkup
_ALL_SHOP_PERMS = ",".join(key for key, _label in FEATURE_PERMS)
_LONG_LIST_SIZE = 60
STATUS_MENU_ORDER = "shop,services,connection_status,wallet,support,loyalty,reseller_apply"


def default_ui() -> dict[str, str]:
    return dict(DEFAULT_SETTINGS)


def status_enabled_ui() -> dict[str, str]:
    """Defaults plus the connection status button, which installs may still have on."""
    ui = default_ui()
    ui["menu_order"] = STATUS_MENU_ORDER
    return ui


def sample_plan(plan_id: int = 3, **overrides: object) -> Plan:
    fields: dict[str, object] = {
        "id": plan_id,
        "name": f"پلن {plan_id}",
        "price": 150_000,
        "duration_days": 30,
        "data_limit_gb": 50,
        "is_active": True,
        "sort_order": plan_id,
    }
    fields.update(overrides)
    return Plan(**fields)


def sample_reseller_profile(*, billing_mode: str = "fixed") -> ResellerProfile:
    return ResellerProfile(
        id=11,
        user_id=21,
        is_active=True,
        billing_mode=billing_mode,
        web_permissions=_ALL_SHOP_PERMS,
        bot_permissions=_ALL_SHOP_PERMS,
    )


def sample_service(service_id: int) -> SimpleNamespace:
    return SimpleNamespace(id=service_id, pg_username=f"user_{service_id}", status="active")


def _reply_scenarios(ui: dict[str, str]) -> dict[str, Callable[[], Markup]]:
    profile = sample_reseller_profile()
    status_ui = status_enabled_ui()
    return {
        "reply.main.user_new": lambda: rk.main_reply_keyboard("user", ui=ui),
        "reply.main.user_with_status": lambda: rk.main_reply_keyboard(
            "user", has_services=True, ui=status_ui
        ),
        "reply.main.user_with_services": lambda: rk.main_reply_keyboard(
            "user", has_services=True, ui=ui
        ),
        "reply.main.admin": lambda: rk.main_reply_keyboard("admin", has_services=True, ui=ui),
        "reply.main.admin_preview": lambda: rk.main_reply_keyboard(
            "admin", has_services=True, ui=ui, as_user=True, preview_exit_action=kb.REPLY_ACTION_ADMIN
        ),
        "reply.main.shop_owner_on_main_bot": lambda: rk.main_reply_keyboard(
            "user", has_services=True, ui=ui, show_reseller_creds=True, profile=profile
        ),
        "reply.main.reseller": lambda: rk.main_reply_keyboard(
            "reseller", has_services=True, ui=ui, profile=profile
        ),
        "reply.shop": lambda: rk.shop_reply_keyboard(ui),
        "reply.wallet": lambda: rk.wallet_reply_keyboard(ui),
        "reply.support": lambda: rk.support_reply_keyboard(ui),
        "reply.loyalty": lambda: rk.loyalty_reply_keyboard(ui),
        "reply.service_actions": lambda: rk.service_actions_reply_keyboard(ui),
        "reply.pay": lambda: rk.pay_reply_keyboard(42, ui),
        "reply.topup_pay": lambda: rk.topup_pay_reply_keyboard(ui),
        "reply.cancel": lambda: rk.cancel_reply(ui),
        "reply.persistent": lambda: rk.persistent_reply_keyboard(ui),
        "reply.submenu_chrome": lambda: rk.submenu_chrome_reply_keyboard(ui),
        "reply.reseller_apply": lambda: rk.reseller_apply_reply_keyboard(ui),
        "reply.review": lambda: rk.review_reply_keyboard(ui),
        "reply.admin.hub": lambda: rk.admin_reply_keyboard(ui),
        "reply.admin.ops": lambda: rk.admin_ops_reply_keyboard(ui),
        "reply.admin.people": lambda: rk.admin_people_reply_keyboard(ui),
        "reply.admin.people_without_representatives": lambda: rk.admin_people_reply_keyboard(
            ui, can_manage_representatives=False
        ),
        "reply.admin.product": lambda: rk.admin_product_reply_keyboard(ui),
        "reply.admin.marketing": lambda: rk.admin_marketing_reply_keyboard(ui),
        "reply.admin.system": lambda: rk.admin_system_reply_keyboard(ui),
        "reply.admin.pg": lambda: rk.pg_reply_keyboard(ui),
        "reply.admin.pg_read_only": lambda: rk.pg_reply_keyboard(ui, can_create_user=False),
        "reply.admin.users": lambda: rk.admin_users_reply_keyboard(ui),
        "reply.admin.resellers": lambda: rk.admin_resellers_reply_keyboard(ui),
        "reply.admin.settings": lambda: rk.admin_settings_reply_keyboard(ui),
        "reply.admin.backup": lambda: rk.admin_backup_reply_keyboard(ui),
        "reply.admin.broadcast": lambda: rk.admin_broadcast_reply_keyboard(ui),
        "reply.admin.loyalty": lambda: rk.admin_loyalty_reply_keyboard(ui),
        "reply.admin.loyalty_without_tiers": lambda: rk.admin_loyalty_reply_keyboard(
            ui, include_tiers=False
        ),
        "reply.admin.plans_audience": lambda: rk.admin_plans_audience_reply_keyboard(ui),
        "reply.admin.plans_kind_users": lambda: rk.admin_plans_kind_reply_keyboard("users", ui),
        "reply.admin.plans_kind_resellers": lambda: rk.admin_plans_kind_reply_keyboard(
            "resellers", ui
        ),
        "reply.admin.plans_add_users": lambda: rk.admin_plans_add_type_reply_keyboard("users", ui),
        "reply.admin.plans_add_resellers": lambda: rk.admin_plans_add_type_reply_keyboard(
            "resellers", ui
        ),
        "reply.reseller.hub": lambda: rk.reseller_hub_main_keyboard(profile, ui),
        "reply.reseller.plans": lambda: rk.reseller_plans_reply_keyboard(ui),
        "reply.reseller.settings": lambda: rk.reseller_settings_reply_keyboard(ui),
    }


def _inline_scenarios(ui: dict[str, str]) -> dict[str, Callable[[], Markup]]:
    plans = [sample_plan(3), sample_plan(4, button_style="success")]
    many_plans = [sample_plan(i) for i in range(1, _LONG_LIST_SIZE + 1)]
    contact = {"id": "main", "title": "پشتیبانی", "telegram": "@support", "enabled": True}
    terms_prompt = SimpleNamespace(btn_label="قوانین را می‌پذیرم", gate="entry", style_id="terms_accept")
    return {
        "inline.main_menu.user": lambda: kb.main_menu("user", has_services=True, ui=ui),
        "inline.main_menu.admin": lambda: kb.main_menu("admin", has_services=True, ui=ui),
        "inline.shop_kind": lambda: kb.shop_kind_keyboard(
            ui, fixed_on=True, trial_on=True, custom_on=True, wholesale_on=True
        ),
        "inline.plans": lambda: kb.plans_keyboard(
            plans, ui, custom_enabled=True, wholesale_enabled=True
        ),
        "inline.plans_long_list": lambda: kb.plans_keyboard(many_plans, ui),
        "inline.wholesale_plans": lambda: kb.wholesale_plans_keyboard(plans, ui),
        "inline.wholesale_qty": lambda: kb.wholesale_qty_keyboard(5, ui, plan_id=3),
        "inline.custom_gb": lambda: kb.custom_gb_keyboard(20, ui),
        "inline.custom_days": lambda: kb.custom_days_keyboard(30, ui),
        "inline.pay_methods": lambda: kb.pay_methods(42, ui),
        "inline.services": lambda: kb.services_keyboard([sample_service(7), sample_service(8)], ui),
        "inline.services_long_list": lambda: kb.services_keyboard(
            [sample_service(i) for i in range(1, _LONG_LIST_SIZE + 1)], ui
        ),
        "inline.service_actions": lambda: kb.service_actions(7, ui),
        "inline.support_contacts": lambda: kb.support_contacts_keyboard([contact], ui),
        "inline.force_join": lambda: kb.force_join_inline_keyboard("@channel_one\n@channel_two", ui=ui),
        "inline.terms": lambda: kb.terms_inline_keyboard(terms_prompt, ui),
        "inline.order_review": lambda: kb.order_review(42, ui),
        "inline.payment_review": lambda: kb.payment_review(55, ui),
        "inline.payment_resend": lambda: kb.payment_resend(55, ui),
        "inline.payment_resume": lambda: kb.payment_resume(55, ui),
        "inline.reseller_app_review": lambda: kb.reseller_app_review(6, ui),
        "inline.reseller_app_review_no_approve": lambda: kb.reseller_app_review(
            6, ui, allow_approve=False
        ),
        "inline.admin.home": lambda: kb.admin_home(ui),
        "inline.admin.main_menu": lambda: kb.admin_main_menu(ui),
        "inline.admin.plan_actions": lambda: kb.plan_actions(3, ui),
        "inline.admin.plan_kind_users": lambda: kb.admin_plan_kind_keyboard("users", ui),
        "inline.admin.plan_kind_resellers": lambda: kb.admin_plan_kind_keyboard("resellers", ui),
        "inline.admin.plans_add_users": lambda: kb.admin_plans_add_type_keyboard("users", ui),
        "inline.admin.plans_list": lambda: kb.admin_plans_list_keyboard(plans, ui),
        "inline.admin.users_plans_overview": lambda: kb.admin_users_plans_overview_keyboard(plans, ui),
        "inline.admin.resellers_plans_overview": lambda: kb.admin_resellers_plans_overview_keyboard(
            [], [], ui
        ),
        "inline.admin.user_actions": lambda: kb.admin_user_actions(
            5, is_blocked=False, role="user", ui=ui, has_services=True
        ),
        "inline.admin.user_actions_blocked_confirm_delete": lambda: kb.admin_user_actions(
            5, is_blocked=True, role="user", ui=ui, confirm_delete=True
        ),
        "inline.admin.user_services": lambda: kb.admin_user_services_keyboard(5, [7, 8]),
        "inline.admin.user_service_actions": lambda: kb.admin_user_service_actions(5, 7),
        "inline.admin.user_service_adjust": lambda: kb.admin_user_service_adjust_keyboard(
            5, 7, days=10, gb=5
        ),
        "inline.admin.user_service_delete_confirm": lambda: kb.admin_user_service_delete_confirm(5, 7),
        "inline.admin.user_risk_tag": lambda: kb.admin_user_risk_tag_keyboard(5),
        "inline.admin.reseller_actions": lambda: kb.admin_reseller_actions(6, has_shop_services=True),
        "inline.admin.reseller_risk_tag": lambda: kb.admin_reseller_risk_tag_keyboard(6),
        "inline.admin.reseller_capacity_adjust": lambda: kb.admin_reseller_capacity_adjust_keyboard(
            6, days=10, gb=5
        ),
        "inline.admin.backup_files": lambda: kb.backup_files_keyboard(
            [{"id": "backup-2026-01-01", "size_human": "1.2 MB"}]
        ),
        "inline.admin.item_style_picker": lambda: kb.item_button_style_picker_keyboard(
            callback_prefix="adm:st:sup:color:set:main", back_callback="adm:st:sup", ui=ui
        ),
        "inline.admin.plan_style_picker": lambda: kb.plan_button_style_picker_keyboard(
            callback_prefix="adm:plan:style:3", back_callback="adm:plan:3", ui=ui
        ),
    }


def _hub_scenarios(ui: dict[str, str]) -> dict[str, Callable[[], Markup]]:
    """Inline panels: since inline-only navigation these are the menus people see."""
    profile = sample_reseller_profile()
    payg_profile = sample_reseller_profile(billing_mode="payg")
    services = [sample_service(7), sample_service(8)]
    return {
        "hub.wallet": lambda: hubs.wallet_hub_keyboard(ui),
        "hub.wallet_topup_presets": lambda: hubs.wallet_topup_presets_keyboard(ui),
        "hub.topup_methods": lambda: hubs.topup_methods_keyboard(ui),
        "hub.support": lambda: hubs.support_hub_keyboard(ui),
        "hub.loyalty": lambda: hubs.loyalty_hub_keyboard(ui),
        "hub.services_list": lambda: hubs.services_list_keyboard(services, ui),
        "hub.services_list_with_import": lambda: hubs.services_list_keyboard(
            services, ui, allow_import=True, reseller_capacity_label="ظرفیت: ۳ از ۱۰"
        ),
        "hub.service_card": lambda: hubs.service_card_keyboard(7, ui),
        "hub.service_more": lambda: hubs.service_more_keyboard(7, ui),
        "hub.service_more_external": lambda: hubs.service_more_keyboard(7, ui, external=True),
        "hub.admin.groups": lambda: hubs.admin_groups_hub_keyboard(ui),
        "hub.admin.groups_with_queues": lambda: hubs.admin_groups_hub_keyboard(
            ui, pending_payments=3, pending_tickets=1, pending_cancellations=2
        ),
        "hub.admin.ops": lambda: hubs.admin_ops_hub_keyboard(ui, pending_payments=3, pending_tickets=1),
        "hub.admin.people": lambda: hubs.admin_people_hub_keyboard(ui),
        "hub.admin.people_without_representatives": lambda: hubs.admin_people_hub_keyboard(
            ui, can_manage_representatives=False
        ),
        "hub.admin.product": lambda: hubs.admin_product_hub_keyboard(ui),
        "hub.admin.marketing": lambda: hubs.admin_marketing_hub_keyboard(ui),
        "hub.admin.system": lambda: hubs.admin_system_hub_keyboard(ui),
        "hub.admin.pg": lambda: hubs.pg_hub_keyboard(ui),
        "hub.admin.pg_read_only": lambda: hubs.pg_hub_keyboard(ui, can_create_user=False),
        "hub.admin.loyalty": lambda: hubs.admin_loyalty_manage_hub_keyboard(ui),
        "hub.admin.loyalty_without_tiers": lambda: hubs.admin_loyalty_manage_hub_keyboard(
            ui, include_tiers=False
        ),
        "hub.admin.users": lambda: hubs.admin_users_hub_keyboard(ui),
        "hub.admin.resellers": lambda: hubs.admin_resellers_hub_keyboard(ui),
        "hub.admin.settings": lambda: hubs.admin_settings_hub_keyboard(ui),
        "hub.admin.backup": lambda: hubs.admin_backup_hub_keyboard(ui),
        "hub.admin.broadcast": lambda: hubs.admin_broadcast_hub_keyboard(ui),
        "hub.admin.plans_audience": lambda: hubs.admin_plans_audience_hub_keyboard(ui),
        "hub.admin.plans_audience_users": lambda: hubs.admin_plans_audience_keyboard("users", ui),
        "hub.admin.plans_audience_resellers": lambda: hubs.admin_plans_audience_keyboard("resellers", ui),
        "hub.admin.plans_add_users": lambda: hubs.admin_plans_add_type_hub_keyboard("users", ui),
        "hub.admin.plans_add_resellers": lambda: hubs.admin_plans_add_type_hub_keyboard(
            "resellers", ui
        ),
        "hub.reseller.manage": lambda: hubs.reseller_manage_hub_keyboard(
            profile, ui, can_add_representative=True, pending_payments=2, pending_tickets=1
        ),
        "hub.reseller.manage_payg": lambda: hubs.reseller_manage_hub_keyboard(payg_profile, ui),
        "hub.reseller.main_bot": lambda: hubs.reseller_main_bot_hub_keyboard(profile, ui, show_pg=True),
        "hub.reseller.access_card": lambda: hubs.reseller_access_card_keyboard(profile, ui),
        "hub.reseller.settings": lambda: hubs.reseller_settings_hub_keyboard(ui),
        "hub.reseller.plans": lambda: hubs.reseller_plans_hub_keyboard(ui),
    }


def all_scenarios() -> dict[str, Callable[[], Markup]]:
    ui = default_ui()
    scenarios = _reply_scenarios(ui)
    scenarios.update(_inline_scenarios(ui))
    scenarios.update(_hub_scenarios(ui))
    return scenarios


def build_all() -> dict[str, Markup]:
    return {name: build() for name, build in all_scenarios().items()}


class KeyboardLimitTests(unittest.TestCase):
    def test_inline_buttons_fit_telegram_limits(self) -> None:
        for name, markup in build_all().items():
            if not isinstance(markup, InlineKeyboardMarkup):
                continue
            buttons = inline_buttons(markup)
            with self.subTest(keyboard=name):
                self.assertLessEqual(len(buttons), INLINE_KEYBOARD_MAX_BUTTONS)
            for button in buttons:
                with self.subTest(keyboard=name, button=button.text):
                    self.assertTrue(button.text.strip())
                    self.assertEqual(len(inline_action_fields(button)), 1)
                    if button.callback_data is not None:
                        self.assertLessEqual(
                            len(button.callback_data.encode("utf-8")), CALLBACK_DATA_MAX_BYTES
                        )

    def test_reply_buttons_have_visible_text(self) -> None:
        for name, markup in build_all().items():
            if not isinstance(markup, ReplyKeyboardMarkup):
                continue
            for button in reply_buttons(markup):
                with self.subTest(keyboard=name, button=button.text):
                    self.assertTrue(button.text.strip())


class KeyboardSnapshotTests(unittest.TestCase):
    def test_keyboards_match_reviewed_snapshot(self) -> None:
        dumped = {name: dump_markup(markup) for name, markup in build_all().items()}
        assert_matches_snapshot(self, "keyboards", dumped)


if __name__ == "__main__":
    unittest.main()
