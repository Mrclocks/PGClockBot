"""Plan categories + customer service addon packs — scope isolation & note parsing."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from app.db.models import Plan, PlanCategory, ServiceAddonPack
from app.services.plan_categories import (
    category_belongs_to_staff,
    category_matches_shop,
)
from app.services.service_addons import (
    KIND_DURATION,
    KIND_VOLUME,
    amount_label,
    category_ids_equal,
    format_addon_note,
    format_amount_label,
    is_addon_order,
    pack_belongs_to_staff,
    pack_matches_service_category,
    pack_matches_shop,
    parse_addon_note,
)
from app.services.orders import (
    fulfill_paid_order,
    is_addon_order_note,
    is_mutation_order_note,
)


def _staff(*, role: str, bot_user_id: int | None = None) -> dict:
    s: dict = {"role": role}
    if bot_user_id is not None:
        s["bot_user_id"] = bot_user_id
    if role == "admin":
        # Explicit Owner principal (is_platform_admin / is_explicit_owner_staff)
        s.update(
            {
                "org_principal_id": 1,
                "org_depth": 0,
                "org_parent_id": None,
                "org_status": "active",
                "pg_is_owner": True,
            }
        )
    return s


class PlanCategoryScopeTests(unittest.TestCase):
    def test_platform_category_owner_only(self):
        cat = PlanCategory(id=1, name="A", owner_reseller_id=None, is_active=True)
        self.assertTrue(category_belongs_to_staff(cat, _staff(role="admin")))
        self.assertFalse(
            category_belongs_to_staff(cat, _staff(role="reseller", bot_user_id=9))
        )
        self.assertTrue(category_matches_shop(cat, None))
        self.assertFalse(category_matches_shop(cat, 9))

    def test_reseller_category_isolated(self):
        cat = PlanCategory(id=2, name="B", owner_reseller_id=42, is_active=True)
        self.assertFalse(category_belongs_to_staff(cat, _staff(role="admin")))
        self.assertTrue(
            category_belongs_to_staff(cat, _staff(role="reseller", bot_user_id=42))
        )
        self.assertFalse(
            category_belongs_to_staff(cat, _staff(role="reseller", bot_user_id=7))
        )
        self.assertTrue(category_matches_shop(cat, 42))
        self.assertFalse(category_matches_shop(cat, None))

    def test_audience_normalize(self):
        from app.services.plan_categories import (
            AUDIENCE_RESELLERS,
            AUDIENCE_USERS,
            normalize_audience,
        )

        self.assertEqual(normalize_audience("resellers"), AUDIENCE_RESELLERS)
        self.assertEqual(normalize_audience("USERS"), AUDIENCE_USERS)
        self.assertEqual(normalize_audience("nope"), AUDIENCE_USERS)
        self.assertEqual(normalize_audience(None), AUDIENCE_USERS)


class ServiceAddonScopeTests(unittest.TestCase):
    def test_pack_scope(self):
        pack = ServiceAddonPack(
            id=1,
            name="10G",
            kind=KIND_VOLUME,
            amount=10,
            price=1000,
            owner_reseller_id=None,
            is_active=True,
        )
        self.assertTrue(pack_belongs_to_staff(pack, _staff(role="admin")))
        self.assertFalse(
            pack_belongs_to_staff(pack, _staff(role="reseller", bot_user_id=3))
        )
        shop = ServiceAddonPack(
            id=2,
            name="7d",
            kind=KIND_DURATION,
            amount=7,
            price=2000,
            owner_reseller_id=5,
            is_active=True,
        )
        self.assertTrue(pack_matches_shop(shop, 5))
        self.assertFalse(pack_matches_shop(shop, None))
        self.assertFalse(pack_matches_shop(shop, 9))

    def test_pack_category_match_is_general_or_exact(self):
        self.assertTrue(category_ids_equal(None, None))
        self.assertTrue(category_ids_equal(3, 3))
        self.assertFalse(category_ids_equal(None, 3))
        self.assertFalse(category_ids_equal(3, None))
        self.assertFalse(category_ids_equal(3, 4))
        vip = ServiceAddonPack(
            id=10,
            name="VIP 10G",
            kind=KIND_VOLUME,
            amount=10,
            price=5000,
            owner_reseller_id=None,
            category_id=7,
            is_active=True,
        )
        general = ServiceAddonPack(
            id=11,
            name="General 10G",
            kind=KIND_VOLUME,
            amount=10,
            price=1000,
            owner_reseller_id=None,
            category_id=None,
            is_general=True,
            is_active=True,
        )
        orphan = ServiceAddonPack(
            id=12,
            name="Orphan 10G",
            kind=KIND_VOLUME,
            amount=10,
            price=1000,
            owner_reseller_id=None,
            category_id=None,
            is_general=False,
            is_active=True,
        )
        for pack in (vip,):
            pack.is_general = False
        self.assertTrue(pack_matches_service_category(vip, 7))
        self.assertFalse(pack_matches_service_category(vip, None))
        self.assertFalse(pack_matches_service_category(vip, 9))
        # A general pack fits every category and uncategorized services.
        self.assertTrue(pack_matches_service_category(general, None))
        self.assertTrue(pack_matches_service_category(general, 7))
        # A pack that lost its category without being general matches nothing.
        self.assertFalse(pack_matches_service_category(orphan, None))
        self.assertFalse(pack_matches_service_category(orphan, 7))

    def test_note_parse_snapshot_and_legacy(self):
        self.assertEqual(
            parse_addon_note("svc_addon:12:34:volume:10"),
            (12, 34, "volume", 10.0),
        )
        self.assertEqual(
            parse_addon_note("svc_addon:12:34:duration:7"),
            (12, 34, "duration", 7.0),
        )
        # Legacy notes without snapshot still parse
        self.assertEqual(parse_addon_note("svc_addon:12:34"), (12, 34, None, None))
        self.assertIsNone(parse_addon_note("renew:1"))
        note = format_addon_note(3, 9, kind=KIND_VOLUME, amount=2.5)
        self.assertEqual(parse_addon_note(note), (3, 9, "volume", 2.5))
        self.assertTrue(is_addon_order_note("svc_addon:1:2:volume:1"))
        self.assertTrue(is_mutation_order_note("svc_addon:1:2:duration:7"))
        self.assertTrue(is_mutation_order_note("renew:9"))
        self.assertFalse(is_mutation_order_note("wholesale:2"))
        pack = ServiceAddonPack(
            id=1, name="x", kind=KIND_VOLUME, amount=10, price=1, is_active=True
        )
        self.assertIn("گیگ", amount_label(pack))
        self.assertIn("روز", format_amount_label(KIND_DURATION, 7))
        order = MagicMock()
        order.note = "svc_addon:1:2:volume:10"
        self.assertTrue(is_addon_order(order))

    def test_fulfill_paid_order_exported(self):
        self.assertTrue(callable(fulfill_paid_order))


class WiringTests(unittest.TestCase):
    def test_templates_and_routes(self):
        from pathlib import Path

        plans = Path("app/web/templates/plans.html").read_text(encoding="utf-8")
        self.assertIn("modal-plan-categories", plans)
        self.assertIn("modal-service-addons", plans)
        self.assertIn("برچسب دسته", plans)
        # Category success/error flash renders inside the modal, not page chrome.
        self.assertIn('id="plan-categories-flash"', plans)
        self.assertIn("settings-modal-flash", plans)
        self.assertIn('data-modal-strip-keys="categories,ok,err,_"', plans)
        base = Path("app/web/templates/base.html").read_text(encoding="utf-8")
        self.assertIn("open_categories", base)
        self.assertIn("_catalog_modal_open", base)
        css = Path("app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertIn(
            "#modal-plan-unified .plan-panel > .update-card-caption",
            css,
        )
        self.assertIn("user-service-addons", plans)
        self.assertIn("addon_duration", plans)
        self.assertIn("plan-category-audience", plans)
        self.assertIn('name="category_id"', plans)
        self.assertIn("category_linked_plans", plans)
        self.assertIn("plan-pg-warn-dot", plans)
        self.assertIn("خالی", plans)
        self.assertIn("pg_live_template_ids", plans)
        self.assertIn("unified-addon-category", plans)
        self.assertIn("service-addon-category", plans)
        self.assertIn("setVisible(catField, true)", plans)
        self.assertIn("list_packs_for_service", Path("app/bot/handlers/services.py").read_text(encoding="utf-8"))
        self.assertIn(
            "0044_service_addon_pack_category",
            Path("alembic/versions/0044_service_addon_pack_category.py").read_text(
                encoding="utf-8"
            ),
        )
        css = Path("app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertIn("gift-expires-field", css)
        self.assertNotRegex(css, r'input\[type=["\']datetime-local["\']\]')
        gift = Path("app/web/templates/_gift_code_fields.html").read_text(encoding="utf-8")
        self.assertIn("gift-expires-field", gift)
        self.assertRegex(gift, r'name="expires_at"[\s\S]*?type="text"')
        self.assertNotIn('type="datetime-local"', gift)
        # Existing plan kinds (ثابت/…) must remain; labels are additive
        self.assertIn("USER_KINDS", plans)
        self.assertIn("{value: 'fixed', label: 'ثابت'}", plans)
        self.assertIn("{value: 'addon_volume', label: 'بسته حجم'}", plans)
        self.assertIn("data-category-edit", plans)
        self.assertIn("data-addon-edit", plans)
        self.assertIn("service-addon-kind", plans)
        extras = Path("app/api/plan_catalog_extras.py").read_text(encoding="utf-8")
        self.assertIn('/plans/categories', extras)
        self.assertIn('/plans/addons', extras)
        self.assertIn("/edit", extras)
        self.assertIn("audience", extras)
        app = Path("app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("register_plan_catalog_extras", app)
        self.assertIn("is_mutation_order_note", app)
        self.assertIn("fulfill_paid_order", app)
        self.assertIn("category_plans_map", app)
        models = Path("app/db/models.py").read_text(encoding="utf-8")
        self.assertIn("class PlanCategory", models)
        self.assertIn("class ServiceAddonPack", models)
        self.assertIn("category_id", models)
        self.assertIn('audience: Mapped[str]', models)
        mig = Path(
            "alembic/versions/0031_plan_categories_service_addons.py"
        ).read_text(encoding="utf-8")
        self.assertIn("0030_legacy_wallet_isolation_repair", mig)
        # PostgreSQL rejects BOOLEAN DEFAULT 1 — must use sa.true()/false().
        self.assertIn("server_default=sa.true()", mig)
        self.assertNotIn('server_default=sa.text("1")', mig)
        self.assertNotIn("server_default=sa.text('1')", mig)
        mig2 = Path(
            "alembic/versions/0032_plan_category_audience_reseller.py"
        ).read_text(encoding="utf-8")
        self.assertIn("0031_plan_categories_service_addons", mig2)
        self.assertIn("audience", mig2)
        self.assertIn("reseller_plans", mig2)
        svc = Path("app/bot/handlers/services.py").read_text(encoding="utf-8")
        self.assertIn("svc:addon:", svc)
        self.assertIn("create_addon_order", svc)
        addons = Path("app/services/service_addons.py").read_text(encoding="utf-8")
        self.assertIn("format_addon_note", addons)
        self.assertIn("حجم نامحدود", addons)
        css = Path("app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertIn("search-bar--with-tag", css)
        self.assertIn("color-tag-select-wrap.is-tagged", css)
        self.assertIn("overflow-x: auto", css)
        js = Path("app/web/static/panel.js").read_text(encoding="utf-8")
        self.assertIn("color-tag-select", js)
        self.assertIn("data-tag-color", js)
        # Kebab menus must paint above open modals
        self.assertIn("raZ", js)

    def test_bot_manage_parity_wiring(self):
        """Admin/reseller bot hubs expose category + addon manage (web parity)."""
        from pathlib import Path

        from app.bot import keyboards as kb

        aud = dict(kb._admin_plans_audience_entries())
        listing = dict(kb._admin_plans_list_entries())
        res = dict(kb._reseller_plans_submenu_entries())
        self.assertEqual(aud.get(kb.REPLY_ACTION_ADM_PLANS_CATEGORIES), "🏷 برچسب دسته")
        self.assertEqual(aud.get(kb.REPLY_ACTION_ADM_PLANS_ADDONS), "⏱ بسته حجم و زمان")
        self.assertIn(kb.REPLY_ACTION_ADM_PLANS_CATEGORIES, listing)
        self.assertIn(kb.REPLY_ACTION_ADM_PLANS_ADDONS, listing)
        self.assertEqual(res.get(kb.REPLY_ACTION_RES_PLAN_CATEGORIES), "🏷 برچسب دسته")
        self.assertEqual(res.get(kb.REPLY_ACTION_RES_PLAN_ADDONS), "⏱ بسته حجم/زمان")

        manage = Path("app/bot/handlers/plan_catalog_manage.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("create_category", manage)
        self.assertIn("delete_category", manage)
        self.assertIn("create_pack", manage)
        self.assertIn("delete_pack", manage)
        self.assertIn("resolve_catalog_staff", manage)
        self.assertIn("user_safe_error", manage)
        self.assertIn("pcm:cat:", manage)
        self.assertIn("pcm:cat:aud:", manage)
        self.assertIn("pcm:addon:", manage)
        self.assertIn("cat_audience", manage)

        admin = Path("app/bot/handlers/admin.py").read_text(encoding="utf-8")
        self.assertIn("adm:plan:newcat:", admin)
        self.assertIn("adm:plan:catpick:", admin)
        self.assertIn("adm:plan:setcat:", admin)
        self.assertIn("pending_category_id", admin)
        self.assertIn("resolve_category_for_plan_write", admin)
        self.assertIn("add_plan_category", admin)

        res_plans = Path("app/bot/handlers/reseller_plans.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("res:plan:newcat:", res_plans)
        self.assertIn("res:plan:catpick:", res_plans)
        self.assertIn("res:plan:setcat:", res_plans)
        self.assertIn("category_id", res_plans)
        self.assertIn("resolve_category_for_plan_write", res_plans)

        nav = Path("app/bot/handlers/reply_nav.py").read_text(encoding="utf-8")
        self.assertIn("REPLY_ACTION_ADM_PLANS_CATEGORIES", nav)
        self.assertIn("REPLY_ACTION_ADM_PLANS_ADDONS", nav)
        self.assertIn("REPLY_ACTION_RES_PLAN_CATEGORIES", nav)
        self.assertIn("open_categories_manage", nav)
        self.assertIn("open_addons_manage", nav)

        init = Path("app/bot/__init__.py").read_text(encoding="utf-8")
        self.assertIn("plan_catalog_manage", init)

        labels = Path("app/services/settings_button_labels.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("btn_svc_addon", labels)


class PlanModelFieldTests(unittest.TestCase):
    def test_plan_has_category_attr(self):
        p = Plan(name="t", price=1, duration_days=30)
        self.assertTrue(hasattr(p, "category_id"))


class ApplyServiceAddonUnpackTests(unittest.IsolatedAsyncioTestCase):
    async def test_apply_accepts_four_tuple_snapshot_note(self):
        """Regression: parse_addon_note returns 4 values — unpack must not crash."""
        from unittest.mock import AsyncMock, patch

        from app.db.models import OrderStatus
        from app.services.service_addons import apply_service_addon

        order = MagicMock()
        order.id = 99
        order.note = "svc_addon:1:2:volume:10"
        order.status = OrderStatus.PAID.value
        order.service_id = 2
        order.user_id = 7
        order.reseller_id = None

        session = AsyncMock()
        # Claim PAID → DELIVERING succeeds once
        claim_result = MagicMock()
        claim_result.rowcount = 1
        session.execute = AsyncMock(return_value=claim_result)
        session.commit = AsyncMock()
        session.refresh = AsyncMock()
        # Pack/service missing → ValueError after successful unpack
        session.get = AsyncMock(return_value=None)

        with patch(
            "app.services.service_addons.update", return_value=MagicMock()
        ):
            with self.assertRaises(ValueError) as ctx:
                await apply_service_addon(session, order)
        self.assertIn("بسته یا سرویس", str(ctx.exception))


class AddonNoteFormatTests(unittest.TestCase):
    def test_volume_amount_never_scientific(self):
        from app.services.service_addons import format_addon_note, parse_addon_note

        note = format_addon_note(1, 2, kind="volume", amount=1_000_000)
        self.assertNotIn("e+", note.lower())
        self.assertNotIn("e-", note.lower())
        parsed = parse_addon_note(note)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[3], 1_000_000.0)

    def test_fractional_volume_roundtrips(self):
        from app.services.service_addons import format_addon_note, parse_addon_note

        note = format_addon_note(3, 9, kind="volume", amount=2.5)
        self.assertEqual(parse_addon_note(note), (3, 9, "volume", 2.5))


class DeliverOrderMutationGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_deliver_order_rejects_addon_note(self):
        from unittest.mock import AsyncMock, MagicMock

        from app.db.models import OrderStatus
        from app.services.orders import deliver_order

        order = MagicMock()
        order.id = 11
        order.note = "svc_addon:1:2:volume:5"
        order.service_id = 2
        order.status = OrderStatus.PAID.value
        session = AsyncMock()
        with self.assertRaises(ValueError) as ctx:
            await deliver_order(session, order)
        self.assertIn("fulfill", str(ctx.exception))


class AddonQuotaCacheCallTests(unittest.IsolatedAsyncioTestCase):
    async def test_apply_uses_sync_cache_not_await_session(self):
        """Regression: await sync_service_quota_cache(session, …) crashed after PG
        mutate and released the claim → retry double-applied capacity."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.db.models import OrderStatus, ServiceAddonPack, UserService
        from app.services.service_addons import _apply_pack_to_service

        service = MagicMock(spec=UserService)
        service.id = 2
        service.pg_user_id = 99
        service.bot_user_id = 7
        session = AsyncMock()

        pg = MagicMock()
        pg.get_user_by_id = AsyncMock(
            return_value={"data_limit": 10 * (1024**3), "expire": 2_000_000_000}
        )
        pg.modify_user_by_id = AsyncMock()

        sync_calls: list[tuple] = []

        def _sync(svc, info=None, *, expire_ts=None, data_limit_bytes=None):
            sync_calls.append((svc, info, expire_ts, data_limit_bytes))

        with (
            patch(
                "app.services.pasarguard.get_pg",
                return_value=pg,
            ),
            patch(
                "app.services.users.current_shop_reseller_id",
                return_value=None,
            ),
            patch(
                "app.services.bot_user_admin.sync_service_quota_cache",
                side_effect=_sync,
            ),
        ):
            await _apply_pack_to_service(
                session,
                service,
                kind="volume",
                amount=5,
                order_reseller_id=None,
            )

        self.assertEqual(len(sync_calls), 1)
        self.assertIs(sync_calls[0][0], service)
        self.assertIsNone(sync_calls[0][1])
        self.assertEqual(sync_calls[0][3], 15 * (1024**3))
        pg.modify_user_by_id.assert_awaited_once()


class FulfillRoutesAddonTests(unittest.TestCase):
    def test_bulk_and_retry_use_fulfill(self):
        from pathlib import Path

        bulk = Path("app/services/table_bulk.py").read_text(encoding="utf-8")
        self.assertIn("fulfill_paid_order", bulk)
        self.assertNotIn("await deliver_order(session, order)", bulk)
        ux = Path("app/services/ux20.py").read_text(encoding="utf-8")
        self.assertIn("fulfill_paid_order", ux)
        self.assertIn("open_notify_bot_for_user", ux)
        addons = Path("app/services/service_addons.py").read_text(encoding="utf-8")
        # Must not await the sync cache helper with a session first arg
        self.assertNotIn("await sync_service_quota_cache(", addons)
        self.assertIn("sync_service_quota_cache(\n            service,", addons)


if __name__ == "__main__":
    unittest.main()
