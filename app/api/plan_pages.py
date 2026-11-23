"""Plan catalog pages of the web panel: the plan list, trial, custom and wholesale offers, editing.

Registered from ``create_api_app`` at the same point its routes always had, so route order is unchanged.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.gift_code_pages import legacy_gift_modal_redirect
from app.db.models import Plan
from app.services.renewal_carry import set_plan_carry_from_form
from app.services.trial_offer import PLACEMENT_LABELS as TRIAL_PLACEMENT_LABELS
from app.services.trial_offer import (
    TRIAL_SERVICE_OWNERS_KEY,
    TRIAL_SHOW_CLAIMED_KEY,
    parse_trial_placement,
    save_trial_placement,
)
from app.services.users import get_all_settings, set_setting

logger = logging.getLogger(__name__)


def register_plan_pages(  # noqa: C901 - counts the nested routes; each is listed on its own
    app: FastAPI,
    *,
    render: Callable[..., HTMLResponse],
    redirect_msg: Callable[..., RedirectResponse],
    require_perm: Callable[[str], Callable[..., object]],
    get_db: Callable[..., object],
) -> None:
    @app.get("/plans", response_class=HTMLResponse)
    async def plans_page(  # noqa: C901 - moved unchanged from app.py, where it had this finding
        request: Request,
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        legacy_gifts = legacy_gift_modal_redirect(request.query_params)
        if legacy_gifts:
            return RedirectResponse(legacy_gifts, status_code=303)
        from app.services.plans_catalog import (
            catalog_owner_id,
            custom_range_limit_issue,
            group_plans_blocked_by_role,
            list_catalog_plans,
            load_pg_plan_options,
            plan_limit_issue,
            role_requires_template,
            staff_can_create_pg_template,
        )
        from app.services.pg_quota import limit_snapshot_cards, load_staff_limit_snapshot
        from app.services.shop_scope import is_platform_admin

        plans = await list_catalog_plans(session, staff, include_trial=True)
        trial = next((p for p in plans if p.is_trial), None)
        sale_plans = [p for p in plans if not p.is_trial]
        templates, groups, pg_error = await load_pg_plan_options(staff, session=session)
        pg_live_template_ids: set[int] = set()
        for t in templates or []:
            try:
                tid = t.get("id") if isinstance(t, dict) else None
                if tid is not None:
                    pg_live_template_ids.add(int(tid))
            except (TypeError, ValueError):
                continue
        rid = catalog_owner_id(staff)
        # Non-admin without shop id must never load platform (admin) settings.
        if not is_platform_admin(staff) and not rid:
            values = {}
        else:
            values = await get_all_settings(session, reseller_id=rid)
        limit_snapshot = await load_staff_limit_snapshot(staff, session=session)
        plan_limit_issues: dict[int, str] = {}
        for p in sale_plans:
            issue = await plan_limit_issue(staff, p, session=session)
            if issue:
                plan_limit_issues[int(p.id)] = issue
        trial_limit_issue = await plan_limit_issue(staff, trial, session=session) if trial else None
        try:
            custom_limit_msg = custom_range_limit_issue(
                min_gb=float(values.get("custom_plan_min_gb") or 1),
                max_gb=float(values.get("custom_plan_max_gb") or 500),
                min_days=int(float(values.get("custom_plan_min_days") or 1)),
                max_days=int(float(values.get("custom_plan_max_days") or 365)),
                snapshot=limit_snapshot,
            )
        except (TypeError, ValueError):
            custom_limit_msg = "تنظیمات پلن دلخواه نامعتبر است"
        trial_group_ids = set()
        if trial and trial.pg_group_ids:
            trial_group_ids = {x.strip() for x in trial.pg_group_ids.split(",") if x.strip()}
        custom_group_ids = {
            x.strip()
            for x in (values.get("custom_plan_group_ids") or "").split(",")
            if x.strip()
        }
        reseller_plans = []
        feature_perms = []
        pg_roles = []
        reseller_plans_err = None
        if is_platform_admin(staff):
            from app.services.resellers import (
                FEATURE_PERMS,
                ensure_current_capacity_reseller_plan,
                list_reseller_plans,
            )

            feature_perms = FEATURE_PERMS
            try:
                await ensure_current_capacity_reseller_plan(session)
                await session.commit()
            except Exception:
                import logging

                logging.getLogger(__name__).exception(
                    "ensure current_capacity plan failed on /plans"
                )
                try:
                    await session.rollback()
                except Exception:
                    logger.debug("plans_page: ignored failure", exc_info=True)
            try:
                reseller_plans = await list_reseller_plans(session)
            except Exception:
                # Missing additive columns (e.g. billing_mode before migrate) must
                # not 500 the whole plans page — user catalog still loads.
                import logging

                logging.getLogger(__name__).exception("list_reseller_plans failed on /plans")
                reseller_plans = []
                reseller_plans_err = (
                    "بارگذاری پلن‌های نمایندگی ناموفق بود — پس از به‌روزرسانی، سرویس را یک‌بار ری‌استارت کنید."
                )
            try:
                from app.services.pasarguard import get_pg

                pg_roles = await get_pg().get_admin_roles()
            except Exception:
                pg_roles = []

        plan_categories: list = []
        plan_board = None
        plan_category_tones: dict = {}
        plan_category_map: dict = {}
        reseller_plan_category_map: dict = {}
        category_linked_plans: dict = {}
        service_addon_packs: list = []
        try:
            from app.services.plan_categories import (
                category_map_for_plans,
                category_plans_map,
                list_categories,
            )
            from app.services.plan_category_board import build_plan_board, category_tones
            from app.services.service_addons import list_packs

            if is_platform_admin(staff) or rid:
                plan_categories = await list_categories(session, staff)
                plan_board = build_plan_board(sale_plans, plan_categories, ui=values)
                plan_category_tones = category_tones(plan_categories, values)
                plan_category_map = await category_map_for_plans(
                    session, staff, sale_plans
                )
                if reseller_plans:
                    reseller_plan_category_map = await category_map_for_plans(
                        session, staff, reseller_plans
                    )
                category_linked_plans = await category_plans_map(
                    session, staff, plan_categories
                )
                service_addon_packs = await list_packs(session, staff)
                if service_addon_packs:
                    plan_category_map.update(
                        await category_map_for_plans(
                            session, staff, service_addon_packs
                        )
                    )
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "plan categories/addons load failed on /plans"
            )

        return render(
            request,
            "plans.html",
            {
                "staff": staff,
                "plans": sale_plans,
                "trial": trial,
                "trial_group_ids": trial_group_ids,
                "custom_group_ids": custom_group_ids,
                "values": values,
                "templates": templates,
                "pg_live_template_ids": pg_live_template_ids,
                "groups": groups,
                "pg_error": pg_error,
                "pg_template_only": role_requires_template(staff),
                "role_blocked_plans": group_plans_blocked_by_role(
                    staff,
                    plans,
                    custom_plan_enabled=str(values.get("custom_plan_enabled") or "").lower()
                    in {"1", "true", "yes", "on"},
                    custom_plan_group_ids=values.get("custom_plan_group_ids") or "",
                ),
                "can_create_template": staff_can_create_pg_template(staff),
                "is_platform_admin": is_platform_admin(staff),
                "reseller_plans": reseller_plans,
                "reseller_plans_err": reseller_plans_err,
                "feature_perms": feature_perms,
                "pg_roles": pg_roles,
                "plan_categories": plan_categories,
                "plan_board": plan_board,
                "plan_category_tones": plan_category_tones,
                "plan_category_map": plan_category_map,
                "reseller_plan_category_map": reseller_plan_category_map,
                "category_linked_plans": category_linked_plans,
                "service_addon_packs": service_addon_packs,
                "pg_limit_snapshot": limit_snapshot,
                "pg_limit_cards": limit_snapshot_cards(limit_snapshot),
                "plan_limit_issues": plan_limit_issues,
                "trial_limit_issue": trial_limit_issue,
                "trial_placement_labels": TRIAL_PLACEMENT_LABELS,
                "custom_limit_issue": custom_limit_msg,
                "open_categories": request.query_params.get("categories")
                in {"1", "true", "yes"},
                "open_addons": request.query_params.get("addons") in {"1", "true", "yes"},
                "flash_err": request.query_params.get("err"),
                "flash_ok": request.query_params.get("ok"),
                "plan_style_options": __import__(
                    "app.services.button_styles", fromlist=["PLAN_BUTTON_STYLE_OPTIONS"]
                ).PLAN_BUTTON_STYLE_OPTIONS,
                "pack_style_options": __import__(
                    "app.services.button_styles", fromlist=["ITEM_BUTTON_STYLE_OPTIONS"]
                ).ITEM_BUTTON_STYLE_OPTIONS,
                "category_style_options": __import__(
                    "app.services.button_styles",
                    fromlist=["CATEGORY_BUTTON_STYLE_OPTIONS"],
                ).CATEGORY_BUTTON_STYLE_OPTIONS,
            },
        )

    @app.post("/plans")
    async def plans_create(  # noqa: C901 - moved unchanged from app.py, where it had this finding
        request: Request,
        name: str = Form(...),
        price: int = Form(...),
        duration_days: str = Form(""),
        data_limit_gb: str = Form(""),
        pg_template_id: str = Form(""),
        description: str = Form(""),
        mode: str = Form("custom"),
        also_create_template: str = Form(""),
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        from urllib.parse import quote

        from app.services.pg_quota import PgQuotaError, assert_user_plan_within_limits
        from app.services.plans_catalog import (
            parse_group_ids_from_form,
            parse_user_plan_duration_days,
            plan_groups_denial,
            plan_template_denial,
            require_catalog_owner_id,
            staff_can_create_pg_template,
        )
        from app.services.shop_scope import ShopScopeError

        form = await request.form()
        gb = float(data_limit_gb) if str(data_limit_gb).strip() else None
        try:
            days = parse_user_plan_duration_days(duration_days)
        except ValueError as e:
            return RedirectResponse(f"/plans?err={quote(str(e))}", status_code=303)
        try:
            await assert_user_plan_within_limits(
                staff,
                data_limit=int(gb * (1024**3)) if gb is not None else None,
                duration_days=days,
                label="پلن فروش",
                session=session,
            )
        except PgQuotaError as e:
            return RedirectResponse(f"/plans?err={quote(e.message)}", status_code=303)
        tpl = None
        group_csv = None
        try:
            owner_id = require_catalog_owner_id(staff)
        except ShopScopeError as e:
            return RedirectResponse(f"/plans?err={quote(e.message)}", status_code=303)

        if mode == "template":
            tpl = int(pg_template_id) if str(pg_template_id).strip() else None
            if not tpl:
                return RedirectResponse(
                    f"/plans?err={quote('تمپلیت پاسارگارد را انتخاب کنید')}",
                    status_code=303,
                )
            template_denial = plan_template_denial(staff, tpl)
            if template_denial:
                return RedirectResponse(
                    f"/plans?err={quote(template_denial)}",
                    status_code=303,
                )
        else:
            ids = parse_group_ids_from_form(form)
            if not ids:
                return RedirectResponse(
                    f"/plans?err={quote('حداقل یک گروه پاسارگارد انتخاب کنید')}",
                    status_code=303,
                )
            groups_denial = plan_groups_denial(staff, ids)
            if groups_denial:
                return RedirectResponse(
                    f"/plans?err={quote(groups_denial)}",
                    status_code=303,
                )
            group_csv = ",".join(str(i) for i in ids)
            if also_create_template:
                if not staff_can_create_pg_template(staff):
                    return RedirectResponse(
                        f"/plans?err={quote('اجازه ساخت تمپلیت در پاسارگارد را ندارید')}",
                        status_code=303,
                    )
                try:
                    # Must use staff PG credentials — never owner token for non-admin
                    from app.api.pg_pages import _staff_pg

                    pg_client, _as_owner = await _staff_pg(session, staff)
                    tpl_payload: dict = {
                        "name": name.strip(),
                        "group_ids": ids,
                        "data_limit": int(gb * (1024**3)) if gb is not None else None,
                        "status": "active",
                    }
                    if days > 0:
                        tpl_payload["expire_duration"] = days * 86400
                    created = await pg_client.create_user_template(tpl_payload)
                    if isinstance(created, dict) and created.get("id"):
                        tpl = int(created["id"])
                except Exception as e:
                    from app.services.credential_policy import template_create_error_message

                    logger.warning("PasarGuard template creation failed", exc_info=True)
                    return RedirectResponse(
                        f"/plans?err={quote('ساخت تمپلیت در پاسارگارد ناموفق: ' + template_create_error_message(e))}",
                        status_code=303,
                    )

        from app.services.orders import parse_naming_form
        from app.services.button_styles import parse_plan_button_style_form
        from app.services.plan_categories import resolve_category_for_plan_write

        uname_prefix, uname_suffix, uname_pattern = parse_naming_form(form)
        button_style = parse_plan_button_style_form(form)
        try:
            category_id = await resolve_category_for_plan_write(
                session, staff, form.get("category_id")
            )
        except (ShopScopeError, ValueError) as e:
            return RedirectResponse(f"/plans?err={quote(str(e))}", status_code=303)

        plan = Plan(
            name=name.strip(),
            price=price,
            duration_days=days,
            data_limit_gb=gb,
            pg_template_id=tpl,
            pg_group_ids=group_csv,
            pg_username_prefix=uname_prefix,
            pg_username_suffix=uname_suffix,
            pg_username_pattern=uname_pattern,
            button_style=button_style,
            category_id=category_id,
            owner_reseller_id=owner_id,
            description=description or None,
            is_active=True,
        )
        set_plan_carry_from_form(plan, form)
        from app.services.service_addons import (
            apply_plan_extra_rates_from_form,
            replace_plan_scoped_packs_from_form,
        )

        try:
            apply_plan_extra_rates_from_form(plan, form)
        except ValueError as e:
            return RedirectResponse(f"/plans?err={quote(str(e))}", status_code=303)
        session.add(plan)
        await session.flush()
        try:
            await replace_plan_scoped_packs_from_form(
                session, staff, plan_id=int(plan.id), form=form
            )
        except ValueError as e:
            await session.rollback()
            return RedirectResponse(f"/plans?err={quote(str(e))}", status_code=303)
        await session.commit()
        return RedirectResponse(
            f"/plans?ok={quote('پلن ذخیره شد')}",
            status_code=303,
        )

    @app.post("/plans/trial")
    async def plans_trial_save(
        request: Request,
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        from urllib.parse import quote

        from app.services.pg_quota import PgQuotaError, assert_user_plan_within_limits
        from app.services.plans_catalog import (
            parse_group_ids_from_form,
            plan_groups_denial,
            plan_template_denial,
            require_catalog_owner_id,
        )
        from app.services.shop_scope import ShopScopeError

        form = await request.form()
        try:
            owner_id = require_catalog_owner_id(staff)
        except ShopScopeError as e:
            return RedirectResponse(f"/plans?err={quote(e.message)}", status_code=303)
        try:
            placement = parse_trial_placement(form.get("trial_button_placement"))
        except ValueError as e:
            return RedirectResponse(f"/plans?err={quote(str(e))}", status_code=303)
        enabled = str(form.get("trial_enabled") or "") in {"1", "on", "true", "yes"}
        await set_setting(session, "trial_enabled", "1" if enabled else "0", reseller_id=owner_id)
        # Both fields came with the placement choice: a form without it (an older page) keeps both saved values,
        # since an unchecked switch and a missing one look the same.
        if placement is not None:
            for_service_owners = str(form.get(TRIAL_SERVICE_OWNERS_KEY) or "") in {"1", "on", "true", "yes"}
            await set_setting(
                session, TRIAL_SERVICE_OWNERS_KEY, "1" if for_service_owners else "0", reseller_id=owner_id
            )
            show_claimed = str(form.get(TRIAL_SHOW_CLAIMED_KEY) or "") in {"1", "on", "true", "yes"}
            await set_setting(session, TRIAL_SHOW_CLAIMED_KEY, "1" if show_claimed else "0", reseller_id=owner_id)
            await save_trial_placement(session, placement, reseller_id=owner_id)
        name = str(form.get("name") or "تست رایگان").strip() or "تست رایگان"
        try:
            days = max(1, int(str(form.get("duration_days") or "1")))
        except ValueError:
            days = 1
        gb_raw = str(form.get("data_limit_gb") or "").strip()
        gb = float(gb_raw) if gb_raw else None
        try:
            await assert_user_plan_within_limits(
                staff,
                data_limit=int(gb * (1024**3)) if gb is not None else None,
                duration_days=days,
                label="پلن تست",
                session=session,
            )
        except PgQuotaError as e:
            return RedirectResponse(f"/plans?err={quote(e.message)}", status_code=303)
        mode = str(form.get("mode") or "custom")
        tpl = None
        group_csv = None
        if mode == "template":
            tpl_raw = str(form.get("pg_template_id") or "").strip()
            tpl = int(tpl_raw) if tpl_raw.isdigit() else None
            if enabled and not tpl:
                return RedirectResponse(
                    f"/plans?err={quote('برای پلن تست، تمپلیت را انتخاب کنید')}",
                    status_code=303,
                )
            template_denial = plan_template_denial(staff, tpl) if tpl else None
            if template_denial:
                return RedirectResponse(
                    f"/plans?err={quote(template_denial)}",
                    status_code=303,
                )
        else:
            ids = parse_group_ids_from_form(form)
            if enabled and not ids:
                return RedirectResponse(
                    f"/plans?err={quote('برای پلن تست حداقل یک گروه انتخاب کنید')}",
                    status_code=303,
                )
            groups_denial = plan_groups_denial(staff, ids) if ids else None
            if groups_denial:
                return RedirectResponse(
                    f"/plans?err={quote(groups_denial)}",
                    status_code=303,
                )
            group_csv = ",".join(str(i) for i in ids) if ids else None

        from app.services.orders import parse_naming_form
        from app.services.button_styles import parse_plan_button_style_form

        uname_prefix, uname_suffix, uname_pattern = parse_naming_form(form)
        button_style = parse_plan_button_style_form(form)

        q = select(Plan).where(Plan.is_trial.is_(True))
        if owner_id:
            q = q.where(Plan.owner_reseller_id == owner_id)
        else:
            q = q.where(Plan.owner_reseller_id.is_(None))
        result = await session.execute(q)
        trial = result.scalar_one_or_none()
        if not trial:
            trial = Plan(
                name=name,
                price=0,
                duration_days=days,
                data_limit_gb=gb,
                pg_template_id=tpl,
                pg_group_ids=group_csv,
                pg_username_prefix=uname_prefix,
                pg_username_suffix=uname_suffix,
                pg_username_pattern=uname_pattern,
                button_style=button_style,
                owner_reseller_id=owner_id,
                is_trial=True,
                is_active=enabled,
                description="پلن تست رایگان",
            )
            session.add(trial)
        else:
            trial.name = name
            trial.price = 0
            trial.duration_days = days
            trial.data_limit_gb = gb
            trial.pg_template_id = tpl
            trial.pg_group_ids = group_csv
            trial.pg_username_prefix = uname_prefix
            trial.pg_username_suffix = uname_suffix
            trial.pg_username_pattern = uname_pattern
            trial.button_style = button_style
            trial.is_active = enabled
        await session.commit()
        return RedirectResponse(
            f"/plans?ok={quote('تنظیمات پلن تست ذخیره شد')}",
            status_code=303,
        )

    @app.post("/plans/custom")
    async def plans_custom_save(
        request: Request,
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        from urllib.parse import quote

        from app.services.pg_quota import PgQuotaError, assert_custom_plan_range_within_limits
        from app.services.plans_catalog import (
            parse_group_ids_from_form,
            plan_groups_denial,
            plan_template_denial,
            require_catalog_owner_id,
        )
        from app.services.shop_scope import ShopScopeError

        form = await request.form()
        try:
            owner_id = require_catalog_owner_id(staff)
        except ShopScopeError as e:
            return RedirectResponse(f"/plans?err={quote(e.message)}", status_code=303)
        enabled = str(form.get("custom_plan_enabled") or "") in {"1", "on", "true", "yes"}
        try:
            await assert_custom_plan_range_within_limits(
                staff,
                min_gb=float(str(form.get("custom_plan_min_gb") or "1") or "1"),
                max_gb=float(str(form.get("custom_plan_max_gb") or "500") or "500"),
                min_days=max(1, int(float(str(form.get("custom_plan_min_days") or "1") or "1"))),
                max_days=max(1, int(float(str(form.get("custom_plan_max_days") or "365") or "365"))),
                session=session,
            )
        except (PgQuotaError, ValueError) as e:
            return RedirectResponse(
                f"/plans?err={quote(getattr(e, 'message', str(e)))}",
                status_code=303,
            )
        await set_setting(
            session, "custom_plan_enabled", "1" if enabled else "0", reseller_id=owner_id
        )
        for key in (
            "custom_plan_price_per_gb",
            "custom_plan_price_per_day",
            "custom_plan_min_gb",
            "custom_plan_max_gb",
            "custom_plan_min_days",
            "custom_plan_max_days",
        ):
            raw = str(form.get(key) or "").strip()
            if raw:
                await set_setting(session, key, raw, reseller_id=owner_id)
        mode = str(form.get("mode") or "custom")
        if mode == "template":
            tpl = str(form.get("custom_plan_template_id") or "").strip()
            template_denial = plan_template_denial(staff, int(tpl)) if tpl.isdigit() else None
            if template_denial:
                return RedirectResponse(
                    f"/plans?err={quote(template_denial)}",
                    status_code=303,
                )
            await set_setting(session, "custom_plan_template_id", tpl, reseller_id=owner_id)
            await set_setting(session, "custom_plan_group_ids", "", reseller_id=owner_id)
        else:
            ids = parse_group_ids_from_form(form)
            groups_denial = plan_groups_denial(staff, ids) if ids else None
            if groups_denial:
                return RedirectResponse(
                    f"/plans?err={quote(groups_denial)}",
                    status_code=303,
                )
            await set_setting(
                session,
                "custom_plan_group_ids",
                ",".join(str(i) for i in ids),
                reseller_id=owner_id,
            )
            await set_setting(session, "custom_plan_template_id", "", reseller_id=owner_id)
        from app.services.orders import parse_naming_form
        from app.services.button_styles import parse_custom_plan_button_style_form

        c_prefix, c_suffix, c_pattern = parse_naming_form(
            form,
            prefix_key="custom_plan_username_prefix",
            suffix_key="custom_plan_username_suffix",
            pattern_key="custom_plan_username_pattern",
        )
        custom_btn_style = parse_custom_plan_button_style_form(form)
        await set_setting(
            session,
            "custom_plan_button_style",
            "" if custom_btn_style is None else custom_btn_style,
            reseller_id=owner_id,
        )
        await set_setting(
            session, "custom_plan_username_prefix", c_prefix or "", reseller_id=owner_id
        )
        await set_setting(
            session, "custom_plan_username_suffix", c_suffix or "", reseller_id=owner_id
        )
        await set_setting(
            session, "custom_plan_username_pattern", c_pattern or "", reseller_id=owner_id
        )
        return RedirectResponse(
            f"/plans?ok={quote('تنظیمات پلن دلخواه ذخیره شد')}",
            status_code=303,
        )

    @app.post("/plans/wholesale")
    async def plans_wholesale_save(
        request: Request,
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        from urllib.parse import quote

        from app.services.orders import parse_wholesale_tiers
        from app.services.plans_catalog import require_catalog_owner_id
        from app.services.shop_scope import ShopScopeError

        form = await request.form()
        try:
            owner_id = require_catalog_owner_id(staff)
        except ShopScopeError as e:
            return RedirectResponse(f"/plans?err={quote(e.message)}", status_code=303)
        enabled = str(form.get("wholesale_enabled") or "") in {"1", "on", "true", "yes"}
        await set_setting(
            session, "wholesale_enabled", "1" if enabled else "0", reseller_id=owner_id
        )
        try:
            mn = max(1, int(float(str(form.get("wholesale_min_qty") or "5"))))
        except (TypeError, ValueError):
            mn = 5
        try:
            mx = max(mn, int(float(str(form.get("wholesale_max_qty") or "20"))))
        except (TypeError, ValueError):
            mx = max(mn, 20)
        await set_setting(session, "wholesale_min_qty", str(mn), reseller_id=owner_id)
        await set_setting(session, "wholesale_max_qty", str(mx), reseller_id=owner_id)
        btn = str(form.get("btn_wholesale") or "").strip() or "📦 فروش عمده"
        await set_setting(session, "btn_wholesale", btn, reseller_id=owner_id)
        tiers = parse_wholesale_tiers(str(form.get("wholesale_tiers") or ""))

        await set_setting(
            session,
            "wholesale_tiers",
            json.dumps(tiers, ensure_ascii=False),
            reseller_id=owner_id,
        )
        return RedirectResponse(
            f"/plans?ok={quote('تنظیمات فروش عمده ذخیره شد')}",
            status_code=303,
        )

    async def _plans_context(session: AsyncSession, request: Request, staff: dict, extra: dict | None = None):
        from app.services.plans_catalog import (
            list_catalog_plans,
            load_pg_plan_options,
            role_requires_template,
        )
        from app.services.pg_quota import limit_snapshot_cards, load_staff_limit_snapshot

        plans = await list_catalog_plans(session, staff, include_trial=True)
        templates, groups, pg_error = await load_pg_plan_options(staff, session=session)
        snap = await load_staff_limit_snapshot(staff, session=session)
        ctx = {
            "staff": staff,
            "plans": plans,
            "templates": templates,
            "groups": groups,
            "pg_error": pg_error,
            "pg_template_only": role_requires_template(staff),
            "pg_limit_snapshot": snap,
            "pg_limit_cards": limit_snapshot_cards(snap),
            "flash_err": request.query_params.get("err"),
            "flash_ok": request.query_params.get("ok"),
        }
        if extra:
            ctx.update(extra)
        return ctx

    @app.get("/plans/{plan_id}/edit", response_class=HTMLResponse)
    async def plans_edit_page(
        plan_id: int,
        request: Request,
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.plans_catalog import catalog_owner_id, get_owned_plan
        from app.services.shop_scope import is_platform_admin

        plan = await get_owned_plan(session, plan_id, staff)
        if not plan:
            return RedirectResponse("/plans?err=" + quote("پلن یافت نشد"), status_code=303)
        rid = catalog_owner_id(staff)
        if not is_platform_admin(staff) and not rid:
            values = {}
        else:
            values = await get_all_settings(session, reseller_id=rid)
        from app.services.plan_categories import list_categories
        from app.services.plan_category_board import category_tones

        cats = []
        if is_platform_admin(staff) or rid:
            cats = await list_categories(session, staff, active_only=False)
        from app.services.button_styles import (
            CATEGORY_BUTTON_STYLE_OPTIONS,
            ITEM_BUTTON_STYLE_OPTIONS,
            PLAN_BUTTON_STYLE_OPTIONS,
        )
        from app.services.service_addons import (
            KIND_DURATION,
            KIND_VOLUME,
            list_packs_for_plan,
            plan_duration_months_for_form,
        )

        plan_packs = await list_packs_for_plan(
            session, plan_id=int(plan.id), reseller_id=rid, active_only=False
        )
        plan_vol_packs = [
            {"amount": int(p.amount), "button_style": p.button_style}
            for p in plan_packs
            if p.kind == KIND_VOLUME
        ]
        plan_dur_packs = [
            {
                "amount": plan_duration_months_for_form(p),
                "button_style": p.button_style,
            }
            for p in plan_packs
            if p.kind == KIND_DURATION
        ]
        ctx = await _plans_context(
            session,
            request,
            staff,
            {
                "plan": plan,
                "values": values,
                "plan_categories": cats,
                "plan_category_tones": category_tones(cats, values),
                "plan_vol_packs": plan_vol_packs,
                "plan_dur_packs": plan_dur_packs,
            },
        )
        ctx["plan_style_options"] = PLAN_BUTTON_STYLE_OPTIONS
        ctx["pack_style_options"] = ITEM_BUTTON_STYLE_OPTIONS
        ctx["category_style_options"] = CATEGORY_BUTTON_STYLE_OPTIONS
        return render(request, "plan_edit.html", ctx)

    @app.post("/plans/{plan_id}/edit")
    async def plans_edit_save(
        plan_id: int,
        request: Request,
        name: str = Form(...),
        price: int = Form(...),
        duration_days: str = Form(""),
        data_limit_gb: str = Form(""),
        pg_template_id: str = Form(""),
        description: str = Form(""),
        mode: str = Form("custom"),
        sort_order: int = Form(0),
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.pg_quota import PgQuotaError, assert_user_plan_within_limits
        from app.services.plans_catalog import (
            get_owned_plan,
            parse_group_ids_from_form,
            parse_user_plan_duration_days,
            plan_groups_denial,
            plan_template_denial,
        )

        plan = await get_owned_plan(session, plan_id, staff)
        if not plan:
            return RedirectResponse("/plans?err=" + quote("پلن یافت نشد"), status_code=303)

        form = await request.form()
        gb = float(data_limit_gb) if str(data_limit_gb).strip() else None
        try:
            days = parse_user_plan_duration_days(duration_days)
        except ValueError as e:
            return RedirectResponse(
                "/plans/%d/edit?err=%s" % (plan_id, quote(str(e))),
                status_code=303,
            )
        try:
            await assert_user_plan_within_limits(
                staff,
                data_limit=int(gb * (1024**3)) if gb is not None else None,
                duration_days=days,
                label="ویرایش پلن",
                session=session,
            )
        except PgQuotaError as e:
            return RedirectResponse(
                "/plans/%d/edit?err=%s" % (plan_id, quote(e.message)),
                status_code=303,
            )
        tpl = None
        group_csv = None

        if mode == "template":
            tpl = int(pg_template_id) if str(pg_template_id).strip() else None
            if not tpl:
                return RedirectResponse(
                    f"/plans/{plan_id}/edit?err={quote('تمپلیت پاسارگارد را انتخاب کنید')}",
                    status_code=303,
                )
            template_denial = plan_template_denial(staff, tpl)
            if template_denial:
                return RedirectResponse(
                    f"/plans/{plan_id}/edit?err={quote(template_denial)}",
                    status_code=303,
                )
        else:
            ids = parse_group_ids_from_form(form)
            if not ids:
                return RedirectResponse(
                    f"/plans/{plan_id}/edit?err={quote('حداقل یک گروه پاسارگارد انتخاب کنید')}",
                    status_code=303,
                )
            groups_denial = plan_groups_denial(staff, ids)
            if groups_denial:
                return RedirectResponse(
                    f"/plans/{plan_id}/edit?err={quote(groups_denial)}",
                    status_code=303,
                )
            group_csv = ",".join(str(i) for i in ids)

        plan.name = name.strip()
        plan.price = price
        plan.duration_days = days
        plan.data_limit_gb = gb
        plan.description = description or None
        plan.sort_order = sort_order
        plan.pg_template_id = tpl
        plan.pg_group_ids = group_csv
        set_plan_carry_from_form(plan, form)
        from app.services.orders import parse_naming_form
        from app.services.button_styles import parse_plan_button_style_form
        from app.services.plan_categories import resolve_category_for_plan_write
        from app.services.shop_scope import ShopScopeError

        uname_prefix, uname_suffix, uname_pattern = parse_naming_form(form)
        plan.pg_username_prefix = uname_prefix
        plan.pg_username_suffix = uname_suffix
        plan.pg_username_pattern = uname_pattern
        plan.button_style = parse_plan_button_style_form(form)
        try:
            plan.category_id = await resolve_category_for_plan_write(
                session,
                staff,
                form.get("category_id"),
                allow_inactive_id=plan.category_id,
            )
        except (ShopScopeError, ValueError) as e:
            return RedirectResponse(
                f"/plans/{plan_id}/edit?err={quote(str(e))}",
                status_code=303,
            )
        try:
            from app.services.service_addons import (
                apply_plan_extra_rates_from_form,
                replace_plan_scoped_packs_from_form,
            )

            apply_plan_extra_rates_from_form(plan, form)
            await replace_plan_scoped_packs_from_form(
                session, staff, plan_id=int(plan.id), form=form
            )
        except ValueError as e:
            await session.rollback()
            return RedirectResponse(
                f"/plans/{plan_id}/edit?err={quote(str(e))}",
                status_code=303,
            )
        await session.commit()
        return RedirectResponse(
            f"/plans?ok={quote('پلن به‌روزرسانی شد')}",
            status_code=303,
        )

    @app.post("/plans/{plan_id}/toggle")
    async def plans_toggle(
        plan_id: int,
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.plans_catalog import get_owned_plan

        plan = await get_owned_plan(session, plan_id, staff)
        if plan:
            plan.is_active = not plan.is_active
            await session.commit()
        return RedirectResponse("/plans", status_code=303)

    @app.post("/plans/{plan_id}/delete")
    async def plans_delete(
        plan_id: int,
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.plans_catalog import (
            PlanDeleteBlocked,
            delete_shop_plan,
            get_owned_plan,
        )

        plan = await get_owned_plan(session, plan_id, staff)
        if not plan or plan.is_trial:
            return RedirectResponse("/plans", status_code=303)
        try:
            await delete_shop_plan(session, plan)
            await session.commit()
        except PlanDeleteBlocked as e:
            try:
                await session.rollback()
            except Exception:
                logger.debug("plans_delete: ignored failure", exc_info=True)
            return redirect_msg("/plans", err=e.message)
        return redirect_msg("/plans", ok="پلن حذف شد")
