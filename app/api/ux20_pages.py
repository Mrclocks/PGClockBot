"""UX20 web routes: delivery queue, gift codes, export/import, notes, magic links."""

from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import BotUser, ChargeCode, DeliveryFailure, Order
from app.services.authz import authz_from_staff, can_shop
from app.services.shop_scope import is_platform_admin, shop_owner_id, ShopScopeError, assert_order_retry_in_scope, assert_bot_user_in_scope
from app.services.users import get_all_settings, get_setting, on, set_setting
from app.services.redact import user_safe_error
from app.services.ux20 import (
    create_charge_code,
    export_shop_bundle,
    import_shop_bundle,
    list_open_delivery_failures,
    retry_delivery,
    shop_bundle_to_json,
)


def register_ux20_pages(app, *, render, require_staff, require_admin, get_db):
    @app.post("/plans/{plan_id}/clone")
    async def plans_clone(
        plan_id: int,
        request: Request,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ):
        authz = authz_from_staff(staff)
        if not (is_platform_admin(staff) or can_shop(authz, "plans")):
            return RedirectResponse("/home", status_code=303)
        from app.services.ux20 import clone_plan

        rid = None if is_platform_admin(staff) else shop_owner_id(staff)
        try:
            copy = await clone_plan(session, plan_id, owner_reseller_id=rid)
            return RedirectResponse(
                f"/plans/{copy.id}/edit?ok={quote('کپی پلن ساخته شد — فعلاً خاموش است')}",
                status_code=303,
            )
        except Exception as exc:
            return RedirectResponse(
                f"/plans?err={quote(user_safe_error(exc, fallback='خطا در کپی پلن'))}",
                status_code=303,
            )

    @app.post("/orders/{order_id}/retry-delivery")
    async def orders_retry_delivery(
        order_id: int,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ):
        authz = authz_from_staff(staff)
        if not (is_platform_admin(staff) or can_shop(authz, "orders")):
            return RedirectResponse("/home", status_code=303)
        order = (
            await session.execute(select(Order).where(Order.id == int(order_id)))
        ).scalar_one_or_none()
        if not order:
            return RedirectResponse(
                f"/finance?tab=delivery&err={quote('سفارش پیدا نشد')}", status_code=303
            )
        try:
            assert_order_retry_in_scope(staff, order)
        except ShopScopeError as e:
            return RedirectResponse(
                f"/finance?tab=delivery&err={quote(e.message)}", status_code=303
            )
        try:
            from app.db.models import Payment
            from app.services.payment_review_diag import diagnose_order_delivery

            pay = (
                await session.execute(
                    select(Payment)
                    .where(Payment.order_id == int(order_id))
                    .order_by(Payment.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            diag = await diagnose_order_delivery(session, order, payment=pay)
            await retry_delivery(session, int(order_id))
            ok_msg = (
                "پیام تحویل دوباره ارسال شد"
                if diag.preferred_action == "resend"
                else "تحویل ادامه یافت"
            )
            return RedirectResponse(
                f"/finance?tab=delivery&ok={quote(ok_msg)}",
                status_code=303,
            )
        except Exception as exc:
            return RedirectResponse(
                f"/finance?tab=delivery&err={quote(user_safe_error(exc, fallback='تلاش مجدد ناموفق'))}",
                status_code=303,
            )

    @app.post("/users/{user_id}/staff-note")
    async def users_staff_note(
        user_id: int,
        note: str = Form(""),
        risk_manual: str = Form(""),
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.ux20 import parse_risk_flags, refresh_user_risk, serialize_risk_flags

        if not is_platform_admin(staff) and not can_shop(authz_from_staff(staff), "users"):
            return RedirectResponse("/home", status_code=303)
        user = (
            await session.execute(select(BotUser).where(BotUser.id == int(user_id)))
        ).scalar_one_or_none()
        if not user:
            return RedirectResponse("/users", status_code=303)
        try:
            assert_bot_user_in_scope(staff, user)
        except ShopScopeError:
            return RedirectResponse("/home", status_code=303)
        user.staff_note = (note or "").strip()[:2000] or None
        # Manual risk checkbox removed — staff risk level lives on color_tag.
        # Keep automatic risk_flags refresh; strip stale "manual" markers.
        flags = [f for f in parse_risk_flags(user.risk_flags) if f != "manual"]
        user.risk_flags = serialize_risk_flags(flags)
        await refresh_user_risk(session, user)
        await session.commit()
        # Users UI is list + edit modal — there is no GET /users/{id}
        from app.api.user_pages import _redirect_user

        return _redirect_user(user_id, ok="یادداشت ذخیره شد")

    @app.post("/users/{user_id}/color-tag")
    async def users_color_tag(
        user_id: int,
        color_tag: str = Form(""),
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.color_tags import apply_color_tag

        if not is_platform_admin(staff) and not can_shop(authz_from_staff(staff), "users"):
            return RedirectResponse("/home", status_code=303)
        user = (
            await session.execute(select(BotUser).where(BotUser.id == int(user_id)))
        ).scalar_one_or_none()
        if not user:
            return RedirectResponse("/users", status_code=303)
        try:
            assert_bot_user_in_scope(staff, user)
        except ShopScopeError:
            return RedirectResponse("/home", status_code=303)
        apply_color_tag(user, color_tag)
        await session.commit()
        from app.api.user_pages import _redirect_user

        return _redirect_user(user_id, ok="تگ ریسک ذخیره شد")

    @app.post("/plans/gift-codes")
    async def gift_codes_create(
        amount: int = Form(...),
        max_uses: int = Form(1),
        code: str = Form(""),
        note: str = Form(""),
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ):
        authz = authz_from_staff(staff)
        if not (
            is_platform_admin(staff)
            or can_shop(authz, "orders")
            or can_shop(authz, "plans")
        ):
            return RedirectResponse("/home", status_code=303)
        rid = None if is_platform_admin(staff) else shop_owner_id(staff)
        # Non-owner staff without a shop scope must never mint platform gift codes.
        if not is_platform_admin(staff) and rid is None:
            return RedirectResponse("/home", status_code=303)
        try:
            row = await create_charge_code(
                session,
                amount=int(amount),
                max_uses=int(max_uses) if max_uses else None,
                reseller_id=rid,
                note=note,
                code=code or None,
            )
            return RedirectResponse(
                f"/plans?gifts=1&ok={quote('کد ساخته شد: ' + row.code)}",
                status_code=303,
            )
        except Exception as exc:
            return RedirectResponse(
                f"/plans?gifts=1&err={quote(user_safe_error(exc, fallback='خطا'))}",
                status_code=303,
            )

    @app.post("/plans/gift-codes/{code_id}/toggle")
    async def gift_codes_toggle(
        code_id: int,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ):
        authz = authz_from_staff(staff)
        admin_scope = is_platform_admin(staff)
        if not (
            admin_scope or can_shop(authz, "orders") or can_shop(authz, "plans")
        ):
            return RedirectResponse("/home", status_code=303)
        rid = None if admin_scope else shop_owner_id(staff)
        if not admin_scope and rid is None:
            # No shop context (e.g. pg_staff) — must never toggle platform or
            # any other shop's gift/charge codes.
            return RedirectResponse("/home", status_code=303)
        row = (
            await session.execute(select(ChargeCode).where(ChargeCode.id == int(code_id)))
        ).scalar_one_or_none()
        if not row:
            return RedirectResponse("/plans?gifts=1", status_code=303)
        if rid is None and row.reseller_id is not None:
            return RedirectResponse("/plans?gifts=1", status_code=303)
        if rid is not None and int(row.reseller_id or 0) != int(rid):
            return RedirectResponse("/plans?gifts=1", status_code=303)
        row.is_active = not bool(row.is_active)
        await session.commit()
        return RedirectResponse("/plans?gifts=1&ok=1", status_code=303)

    @app.get("/settings/shop-export")
    async def shop_export(
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ):
        if not (is_platform_admin(staff) or can_shop(authz_from_staff(staff), "shop_settings")):
            return RedirectResponse("/home", status_code=303)
        rid = None if is_platform_admin(staff) else shop_owner_id(staff)
        data = await export_shop_bundle(session, reseller_id=rid)
        body = shop_bundle_to_json(data).encode("utf-8")
        return Response(
            content=body,
            media_type="application/json; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="pgclock-shop-bundle.json"'
            },
        )

    @app.post("/settings/shop-import")
    async def shop_import(
        request: Request,
        file: UploadFile = File(...),
        replace_plans: str = Form(""),
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ):
        if not (is_platform_admin(staff) or can_shop(authz_from_staff(staff), "shop_settings")):
            return RedirectResponse("/home", status_code=303)
        rid = None if is_platform_admin(staff) else shop_owner_id(staff)
        try:
            raw = await file.read()
            payload = json.loads(raw.decode("utf-8"))
            stats = await import_shop_bundle(
                session,
                payload,
                reseller_id=rid,
                replace_plans=(replace_plans or "").strip() in {"1", "on", "true"},
                staff=staff,
            )
            msg = f"وارد شد: {stats.get('settings', 0)} تنظیمات، {stats.get('plans', 0)} پلن"
            if is_platform_admin(staff):
                return RedirectResponse(
                    f"/settings?tab=backup&ok={quote(msg)}",
                    status_code=303,
                )
            return RedirectResponse(
                f"/shop-settings?ok={quote(msg)}",
                status_code=303,
            )
        except Exception as exc:
            safe = user_safe_error(exc, fallback="خطای ایمپورت")
            if is_platform_admin(staff):
                return RedirectResponse(
                    f"/settings?tab=backup&err={quote(safe)}",
                    status_code=303,
                )
            return RedirectResponse(
                f"/shop-settings?err={quote(safe)}",
                status_code=303,
            )

    @app.get("/home/pg-health")
    async def home_pg_health(
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.ux20 import check_pg_connection

        rid = None if is_platform_admin(staff) else shop_owner_id(staff)
        if is_platform_admin(staff):
            data = await check_pg_connection()
        else:
            data = await check_pg_connection(reseller_user_id=rid, session=session)
        return JSONResponse(data)

    @app.post("/backup/verify-last")
    async def backup_verify_last(staff: dict = Depends(require_admin), session: AsyncSession = Depends(get_db)):
        from pathlib import Path

        from app.services.backup import list_backups, validate_backup_archive

        backups = list_backups()
        if not backups:
            return RedirectResponse(
                f"/settings?tab=backup&err={quote('بکاپی نیست')}", status_code=303
            )
        path = Path(str(backups[0].get("path") or ""))
        ok, err, _ = validate_backup_archive(path)
        await set_setting(session, "backup_last_verify_ok", "1" if ok else "0")
        await session.commit()
        if ok:
            return RedirectResponse(
                f"/settings?tab=backup&ok={quote('آخرین بکاپ سالم است')}",
                status_code=303,
            )
        return RedirectResponse(
            f"/settings?tab=backup&err={quote(err or 'بکاپ معیوب')}",
            status_code=303,
        )
