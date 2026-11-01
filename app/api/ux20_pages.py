"""UX20 web routes: delivery queue, gift codes, export/import, notes, magic links."""

from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, ChargeCode, Order
from app.services.authz import authz_from_staff, can_shop
from app.services.customer_marks import has_customer_notes_permission
from app.services.gift_codes import gift_code_scope
from app.services.staff_audit import actor_from_staff, add_staff_action
from app.services.shop_scope import is_platform_admin, shop_owner_id, ShopScopeError, assert_order_retry_in_scope, assert_bot_user_in_scope
from app.services.users import set_setting
from app.services.ux20 import (
    create_charge_code,
    export_shop_bundle,
    import_shop_bundle,
    retry_delivery,
    shop_bundle_to_json,
)


def _gift_rules(form) -> dict:
    from app.services.gift_codes import parse_expiry

    def number(name, default=None):
        raw = str(form.get(name) or "").strip().replace(",", "").replace("٬", "")
        try:
            return int(raw) if raw else default
        except ValueError as exc:
            raise ValueError("مبلغ، درصد و تعداد استفاده باید عدد صحیح باشند") from exc

    return dict(
        kind=str(form.get("kind") or "wallet"), amount=number("amount", 0),
        percent=number("percent", 0), max_discount_toman=number("max_discount_toman"),
        max_uses=number("max_uses", 1 if "max_uses" not in form else None),
        max_uses_per_user=number("max_uses_per_user"),
        expires_at=parse_expiry(str(form.get("expires_at") or "")),
        first_purchase_only=str(form.get("first_purchase_only") or "") in {"1", "on", "true"},
        purchase_types=form.getlist("purchase_types"),
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
                f"/plans?err={quote(str(exc) or 'خطا در کپی پلن')}",
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
                f"/finance?tab=delivery&err={quote(str(exc) or 'تلاش مجدد ناموفق')}",
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

        if not has_customer_notes_permission(staff):
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

        if not has_customer_notes_permission(staff):
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
        request: Request,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ):
        try:
            rid = gift_code_scope(staff)
        except PermissionError:
            return RedirectResponse("/home", status_code=303)
        try:
            form = await request.form()
            row = await create_charge_code(
                session, reseller_id=rid, note=str(form.get("note") or ""),
                code=str(form.get("code") or "") or None,
                audit=actor_from_staff(staff, shop_id=rid), **_gift_rules(form),
            )
            return RedirectResponse(
                f"/gift-codes?ok={quote('کد ساخته شد: ' + row.code)}",
                status_code=303,
            )
        except Exception as exc:
            await session.rollback()
            return RedirectResponse(
                f"/gift-codes?err={quote(str(exc) or 'خطا')}",
                status_code=303,
            )

    @app.post("/plans/gift-codes/{code_id}/edit")
    async def gift_codes_edit(
        code_id: int, request: Request,
        staff: dict = Depends(require_staff), session: AsyncSession = Depends(get_db),
    ):
        from app.services.gift_codes import validate_rules

        try:
            rid = gift_code_scope(staff)
        except PermissionError:
            return RedirectResponse("/home", status_code=303)
        try:
            # Serialize edits with usage claims; never reset the usage history.
            row = (await session.execute(select(ChargeCode).where(
                ChargeCode.id == code_id,
                ChargeCode.reseller_id == rid if rid else ChargeCode.reseller_id.is_(None),
            ).with_for_update())).scalar_one_or_none()
            if not row:
                return RedirectResponse("/gift-codes", status_code=303)
            form = await request.form()
            rules = validate_rules(**_gift_rules(form))
            if rules["kind"] != row.kind:
                raise ValueError("نوع کد پس از ساخت قابل تغییر نیست؛ یک کد جدید بسازید")
            for name, value in rules.items():
                setattr(row, name, value)
            row.note = str(form.get("note") or "").strip()[:255] or None
            add_staff_action(
                session, actor_from_staff(staff, shop_id=rid), action="gift_code_edit",
                target_type="gift_code", target_id=int(row.id),
            )
            await session.commit()
            return RedirectResponse(f"/gift-codes?ok={quote('تنظیمات کد ذخیره شد')}", status_code=303)
        except Exception as exc:
            await session.rollback()
            return RedirectResponse(f"/gift-codes?err={quote(str(exc) or 'خطا')}", status_code=303)

    @app.post("/plans/gift-codes/{code_id}/toggle")
    async def gift_codes_toggle(
        code_id: int,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ):
        try:
            rid = gift_code_scope(staff)
        except PermissionError:
            return RedirectResponse("/home", status_code=303)
        row = (
            await session.execute(select(ChargeCode).where(ChargeCode.id == int(code_id)))
        ).scalar_one_or_none()
        if not row:
            return RedirectResponse("/gift-codes", status_code=303)
        if rid is None and row.reseller_id is not None:
            return RedirectResponse("/gift-codes", status_code=303)
        if rid is not None and int(row.reseller_id or 0) != int(rid):
            return RedirectResponse("/gift-codes", status_code=303)
        row.is_active = not bool(row.is_active)
        add_staff_action(
            session, actor_from_staff(staff, shop_id=rid), action="gift_code_toggle",
            target_type="gift_code", target_id=int(row.id),
        )
        await session.commit()
        return RedirectResponse("/gift-codes?ok=1", status_code=303)

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
            if is_platform_admin(staff):
                return RedirectResponse(
                    f"/settings?tab=backup&err={quote(str(exc) or 'خطای ایمپورت')}",
                    status_code=303,
                )
            return RedirectResponse(
                f"/shop-settings?err={quote(str(exc) or 'خطای ایمپورت')}",
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
