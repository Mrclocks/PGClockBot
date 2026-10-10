"""Bot-user management pages (platform admin)."""

from __future__ import annotations

import html
from urllib.parse import quote

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Plan
from app.services.authz import authz_from_staff, can_shop
from app.services.demo_users import DEMO_USERS_PERMISSION, set_demo_user
from app.services.shop_scope import ShopScopeError, assert_bot_user_in_scope


def _q(msg: str) -> str:
    return quote(str(msg), safe="")


def _redirect_user(user_id: int, *, ok: str | None = None, err: str | None = None):
    """After modal edits, return to users list and reopen the edit modal."""
    qs = [f"edit={int(user_id)}"]
    if err:
        qs.append(f"err={_q(err)}")
    elif ok:
        qs.append(f"ok={_q(ok)}")
    return RedirectResponse(f"/users?{'&'.join(qs)}", status_code=303)


def _list_return_from_form(form) -> tuple[str, str | None]:
    from app.services.users_ops import normalize_users_filter

    fk = normalize_users_filter(str(form.get("return_filter") or form.get("filter") or "all"))
    q = str(form.get("return_q") or form.get("q") or "").strip() or None
    return fk, q


def _redirect_list_form(
    form,
    *,
    ok: str | None = None,
    err: str | None = None,
    uid: int | None = None,
):
    from app.services.users_ops import users_list_href

    fk, q = _list_return_from_form(form)
    href = users_list_href(filter_key=fk, uid=uid, q=q)
    bits = []
    if err:
        bits.append(f"err={_q(err)}")
    elif ok:
        bits.append(f"ok={_q(ok)}")
    if bits:
        href += ("&" if "?" in href else "?") + "&".join(bits)
    return RedirectResponse(href, status_code=303)


async def _require_scoped_user(
    session: AsyncSession,
    staff: dict,
    user_id: int,
    *,
    fragment: bool = False,
) -> BotUser | RedirectResponse | HTMLResponse:
    user = await session.get(BotUser, int(user_id))
    if not user:
        if fragment:
            return HTMLResponse(
                '<div class="flash err">کاربر یافت نشد</div>', status_code=404
            )
        return RedirectResponse(f"/users?err={_q('کاربر یافت نشد')}", status_code=303)
    try:
        assert_bot_user_in_scope(staff, user)
    except ShopScopeError as e:
        if fragment:
            return HTMLResponse(
                f'<div class="flash err">{html.escape(e.message)}</div>',
                status_code=403,
            )
        return RedirectResponse(f"/users?err={_q(e.message)}", status_code=303)
    return user


def register_user_pages(app, *, render, require_admin, get_db, require_perm=None) -> None:
    require_ops = require_perm("dashboard") if require_perm else require_admin

    @app.post("/users/{user_id}/demo")
    async def user_demo(
        user_id: int,
        request: Request,
        staff: dict = Depends(require_ops),
        session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        form = await request.form()
        state = form.get("is_demo")
        if state not in {"0", "1"}:
            return _redirect_user(user_id, err="وضعیت کاربر دمو نامعتبر است")
        try:
            await set_demo_user(session, staff, user_id, is_demo=state == "1")
        except (PermissionError, ValueError, ShopScopeError) as exc:
            return _redirect_user(user_id, err=str(exc))
        return _redirect_user(user_id, ok="وضعیت کاربر دمو ذخیره شد")

    @app.post("/users/create")
    async def user_create(
        request: Request,
        staff: dict = Depends(require_ops),
        session: AsyncSession = Depends(get_db),
    ):
        """Owner or shop-scoped reseller may create BotUser in their own shop only."""
        from app.services.bot_user_admin import admin_create_bot_user
        from app.services.platform_identity import is_explicit_owner_staff
        from app.services.shop_scope import ShopScopeError, resolve_shop_scope_id
        from app.services.users_ops import users_list_href

        try:
            scope = resolve_shop_scope_id(staff)
        except ShopScopeError as e:
            return RedirectResponse(f"/users?err={_q(e.message)}", status_code=303)
        # Defense-in-depth: never let reseller create platform (NULL) users.
        if not is_explicit_owner_staff(staff):
            if not scope:
                return RedirectResponse(
                    f"/users?err={_q('محدوده فروشگاه مشخص نیست')}", status_code=303
                )

        form = await request.form()
        raw_tid = str(form.get("telegram_id") or "").strip()
        username = str(form.get("username") or "").strip() or None
        full_name = str(form.get("full_name") or "").strip() or None
        color_tag = str(form.get("color_tag") or "").strip() or None
        try:
            telegram_id = int(raw_tid)
        except (TypeError, ValueError):
            return _redirect_list_form(form, err="شناسه تلگرام نامعتبر است")

        try:
            user = await admin_create_bot_user(
                session,
                telegram_id=telegram_id,
                username=username,
                full_name=full_name,
                # Owner → None (platform shop); reseller → forced own scope id.
                reseller_id=None if is_explicit_owner_staff(staff) else int(scope),
                staff=staff,
                color_tag=color_tag,
            )
        except ValueError as e:
            return _redirect_list_form(form, err=str(e), uid=None)
        except Exception:
            return _redirect_list_form(form, err="ساخت کاربر ناموفق بود")

        fk, q = _list_return_from_form(form)
        href = users_list_href(filter_key=fk, uid=int(user.id), q=q)
        href += ("&" if "?" in href else "?") + f"ok={_q('کاربر جدید ساخته شد')}&edit={int(user.id)}"
        return RedirectResponse(href, status_code=303)

    @app.post("/users/{user_id}/services")
    async def user_provision_service(
        user_id: int,
        request: Request,
        staff: dict = Depends(require_ops),
        session: AsyncSession = Depends(get_db),
    ):
        """Owner/reseller assign plan — scoped user + owned catalog plan + provision gate."""
        from app.services.bot_user_admin import admin_provision_service
        from app.services.notifications import actor_label_from_staff
        from app.services.plans_catalog import get_owned_plan

        loaded = await _require_scoped_user(session, staff, user_id)
        if isinstance(loaded, RedirectResponse):
            return loaded
        user = loaded

        form = await request.form()
        plan_raw = str(form.get("plan_id") or "").strip()
        if not plan_raw.isdigit():
            return _redirect_user(user_id, err="پلن را انتخاب کنید")
        plan = await get_owned_plan(session, int(plan_raw), staff)
        if plan is None:
            return _redirect_user(user_id, err="پلن یافت نشد")

        try:
            svc = await admin_provision_service(
                session,
                user,
                plan,
                staff=staff,
                actor=actor_label_from_staff(staff),
            )
        except ValueError as e:
            return _redirect_user(user_id, err=str(e))
        except Exception as e:
            return _redirect_user(user_id, err=f"اختصاص پلن ناموفق: {e}")
        label = plan.name or f"#{svc.id}"
        return _redirect_user(user_id, ok=f"پلن «{label}» اختصاص داده شد (سرویس #{svc.id})")

    @app.get("/users/{user_id}/edit", response_class=HTMLResponse)
    async def user_edit_page(
        user_id: int,
        request: Request,
        staff: dict = Depends(require_ops),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.bot_user_admin import list_service_snapshots, list_wallet_txs
        from app.services.formatting import format_toman
        from app.services.pg_access import staff_user_actions
        from app.services.plans_catalog import list_catalog_plans
        from app.services.platform_identity import is_explicit_owner_staff

        as_fragment = request.query_params.get("fragment") == "1"
        loaded = await _require_scoped_user(
            session, staff, user_id, fragment=as_fragment
        )
        if isinstance(loaded, (RedirectResponse, HTMLResponse)):
            return loaded
        user = loaded

        snaps = await list_service_snapshots(session, int(user_id))
        wallet_txs = await list_wallet_txs(session, int(user_id), limit=20)
        plans = await list_catalog_plans(session, staff, include_trial=False)
        pg_acts = staff_user_actions(staff)
        can_pg_create = bool(pg_acts.get("create"))
        can_pg_update = bool(pg_acts.get("update"))
        can_pg_delete = bool(pg_acts.get("delete"))
        from app.services.color_tags import color_tag_meta, color_tags_for_ui

        ctx = {
            "staff": staff,
            "user": user,
            "services": snaps,
            "wallet_txs": wallet_txs,
            "plans": plans,
            "format_toman": format_toman,
            "flash_ok": request.query_params.get("ok"),
            "flash_err": request.query_params.get("err"),
            # CRM identity ops stay Owner-only (not PG ACL).
            "can_manage_users": is_explicit_owner_staff(staff),
            "can_manage_demo_users": can_shop(authz_from_staff(staff), DEMO_USERS_PERMISSION),
            # Plan assign / service mutate follow PasarGuard users.* ACL.
            "can_provision_users": can_pg_create,
            "can_pg_create": can_pg_create,
            "can_pg_update": can_pg_update,
            "can_pg_delete": can_pg_delete,
            "color_tags": color_tags_for_ui(),
            "color_tag_meta": color_tag_meta,
        }
        if as_fragment:
            return render(request, "_user_edit_body.html", ctx)
        return render(request, "user_edit.html", ctx)

    @app.post("/users/{user_id}/wallet-credit")
    async def user_wallet_credit(
        user_id: int,
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.bot_user_admin import (
            admin_adjust_user_wallet,
            parse_admin_wallet_amount,
        )
        from app.services.notifications import actor_label_from_staff

        loaded = await _require_scoped_user(session, staff, user_id)
        if isinstance(loaded, RedirectResponse):
            return loaded
        user = loaded
        form = await request.form()

        raw = str(form.get("amount") or "").strip()
        note = str(form.get("note") or "").strip()
        try:
            amount = parse_admin_wallet_amount(raw)
        except (TypeError, ValueError):
            return _redirect_user(user_id, err="مبلغ نامعتبر است")
        try:
            await admin_adjust_user_wallet(
                session,
                user,
                amount,
                actor=actor_label_from_staff(staff),
                note=note or None,
            )
        except ValueError as e:
            return _redirect_user(user_id, err=str(e))
        if amount > 0:
            ok = f"کیف پول {amount:,} تومان شارژ شد"
        else:
            ok = f"از کیف پول {abs(amount):,} تومان کسر شد"
        return _redirect_user(user_id, ok=ok)

    @app.post("/users/{user_id}/message")
    async def user_staff_message(
        user_id: int,
        request: Request,
        staff: dict = Depends(require_ops),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.notifications import actor_label_from_staff
        from app.services.users_quick import send_staff_dm

        loaded = await _require_scoped_user(session, staff, user_id)
        if isinstance(loaded, RedirectResponse):
            return loaded
        user = loaded
        form = await request.form()
        body = str(form.get("body") or form.get("text") or "")
        try:
            await send_staff_dm(
                session, user, body, actor=actor_label_from_staff(staff)
            )
            await session.commit()
        except ValueError as e:
            return _redirect_list_form(form, err=str(e), uid=user_id)
        except Exception:
            return _redirect_list_form(form, err="ارسال پیام ناموفق بود", uid=user_id)
        return _redirect_list_form(form, ok="پیام ارسال شد", uid=user_id)

    @app.post("/users/{user_id}/quick-renew")
    async def user_quick_renew(
        user_id: int,
        request: Request,
        staff: dict = Depends(require_ops),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.pg_access import staff_user_actions
        from app.services.users_quick import quick_renew_user

        form = await request.form()
        if not staff_user_actions(staff).get("update"):
            return _redirect_list_form(
                form, err="نقش پاسارگارد شما اجازه تمدید ندارد", uid=user_id
            )

        loaded = await _require_scoped_user(session, staff, user_id)
        if isinstance(loaded, RedirectResponse):
            return loaded
        user = loaded
        sid_raw = str(form.get("service_id") or "").strip()
        service_id = int(sid_raw) if sid_raw.isdigit() else None
        try:
            svc, label = await quick_renew_user(session, user, service_id=service_id, staff=staff)
            await session.commit()
        except ValueError as e:
            return _redirect_list_form(form, err=str(e), uid=user_id)
        except Exception as e:
            return _redirect_list_form(form, err=f"تمدید ناموفق: {e}", uid=user_id)
        return _redirect_list_form(
            form, ok=f"سرویس {label} تمدید شد", uid=user_id
        )

    @app.post("/users/bulk-message")
    async def users_bulk_message(
        request: Request,
        staff: dict = Depends(require_ops),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.notifications import actor_label_from_staff
        from app.services.platform_identity import is_explicit_owner_staff
        from app.services.shop_scope import ShopScopeError, resolve_shop_scope_id
        from app.services.users_ops import (
            build_users_ops_page,
            normalize_users_filter,
            scoped_users_where,
        )
        from app.services.users_quick import MAX_BULK_RECIPIENTS, bulk_staff_dm

        # Bulk is Owner-only (cross-customer blast) or reseller shop-scoped.
        try:
            scope = resolve_shop_scope_id(staff)
        except ShopScopeError as e:
            return RedirectResponse(f"/users?err={_q(e.message)}", status_code=303)
        if scope is None and not is_explicit_owner_staff(staff):
            return RedirectResponse(
                f"/users?err={_q('دسترسی پیام گروهی ندارید')}", status_code=303
            )

        form = await request.form()
        fk = normalize_users_filter(str(form.get("return_filter") or form.get("filter") or "all"))
        if fk == "all":
            return _redirect_list_form(
                form, err="برای پیام گروهی یک فیلتر (مثلاً نزدیک انقضا) انتخاب کنید"
            )
        body = str(form.get("body") or form.get("text") or "")

        result = await session.execute(
            select(BotUser).where(scoped_users_where(scope)).order_by(BotUser.id.desc()).limit(200)
        )
        users = list(result.scalars().all())
        rows, _counts, _fk = await build_users_ops_page(
            session, users, filter_key=fk
        )
        targets = [r.user for r in rows if r.user and not r.user.is_blocked][
            :MAX_BULK_RECIPIENTS
        ]
        if not targets:
            return _redirect_list_form(form, err="گیرنده‌ای در این فیلتر نیست")
        try:
            stats = await bulk_staff_dm(
                session, targets, body, actor=actor_label_from_staff(staff)
            )
            await session.commit()
        except ValueError as e:
            return _redirect_list_form(form, err=str(e))
        except Exception:
            return _redirect_list_form(form, err="ارسال گروهی ناموفق بود")
        return _redirect_list_form(
            form,
            ok=f"پیام گروهی: {stats['ok']} موفق، {stats['fail']} ناموفق از {stats['total']}",
        )

    @app.post("/users/{user_id}/services/{service_id}/renew")
    async def user_service_renew(
        user_id: int,
        service_id: int,
        request: Request,
        staff: dict = Depends(require_ops),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.bot_user_admin import admin_renew_service, get_owned_service

        loaded = await _require_scoped_user(session, staff, user_id)
        if isinstance(loaded, RedirectResponse):
            return loaded

        form = await request.form()
        try:
            svc = await get_owned_service(
                session, bot_user_id=user_id, service_id=service_id
            )
        except ValueError as e:
            return _redirect_user(user_id, err=str(e))

        from app.services.plans_catalog import get_owned_plan

        plan = None
        plan_raw = str(form.get("plan_id") or "").strip()
        if plan_raw.isdigit():
            plan = await get_owned_plan(session, int(plan_raw), staff)
            if plan is None or not plan.is_active:
                return _redirect_user(user_id, err="پلن یافت نشد")
        if plan is None and svc.plan_id:
            plan = await session.get(Plan, int(svc.plan_id))
        if plan is None:
            return _redirect_user(user_id, err="پلن تمدید را انتخاب کنید")

        try:
            await admin_renew_service(
                session,
                svc,
                days=None,
                data_limit_gb=None,
                plan=plan,
                reset_traffic=True,
                staff=staff,
            )
        except ValueError as e:
            return _redirect_user(user_id, err=str(e))
        except Exception as e:
            return _redirect_user(user_id, err=f"تمدید ناموفق: {e}")
        return _redirect_user(user_id, ok=f"سرویس #{service_id} تمدید شد")

    @app.post("/users/{user_id}/services/{service_id}/extend")
    async def user_service_extend(
        user_id: int,
        service_id: int,
        request: Request,
        staff: dict = Depends(require_ops),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.bot_user_admin import admin_extend_service, get_owned_service

        loaded = await _require_scoped_user(session, staff, user_id)
        if isinstance(loaded, RedirectResponse):
            return loaded

        form = await request.form()
        try:
            svc = await get_owned_service(
                session, bot_user_id=user_id, service_id=service_id
            )
        except ValueError as e:
            return _redirect_user(user_id, err=str(e))
        days_raw = str(form.get("extra_days") or "0").strip() or "0"
        gb_raw = str(form.get("extra_gb") or "0").strip() or "0"
        try:
            from app.services.numbers import parse_float, parse_int

            extra_days = parse_int(days_raw)
            extra_gb = parse_float(gb_raw)
        except ValueError:
            return _redirect_user(user_id, err="مقادیر تغییر مانده نامعتبر است")
        try:
            await admin_extend_service(
                session,
                svc,
                extra_days=extra_days,
                extra_gb=extra_gb,
                staff=staff,
            )
        except ValueError as e:
            return _redirect_user(user_id, err=str(e))
        except Exception as e:
            return _redirect_user(user_id, err=f"تغییر مانده ناموفق: {e}")
        return _redirect_user(user_id, ok=f"مانده سرویس #{service_id} به‌روز شد")

    @app.post("/users/{user_id}/services/{service_id}/delete")
    async def user_service_delete(
        request: Request,
        user_id: int,
        service_id: int,
        staff: dict = Depends(require_ops),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.bot_user_admin import admin_delete_service, get_owned_service

        loaded = await _require_scoped_user(session, staff, user_id)
        if isinstance(loaded, RedirectResponse):
            return loaded

        try:
            svc = await get_owned_service(
                session, bot_user_id=user_id, service_id=service_id
            )
        except ValueError as e:
            return _redirect_user(user_id, err=str(e))


        try:
            info = await admin_delete_service(
                session, svc, delete_pg=True, staff=staff
            )
        except ValueError as e:
            return _redirect_user(user_id, err=str(e))
        except Exception as e:
            return _redirect_user(user_id, err=f"حذف سرویس ناموفق: {e}")
        note = " (پاسارگارد حذف شد)" if info.get("pg_deleted") else (
            " (پاسارگارد غیرفعال شد)" if info.get("pg_disabled") else ""
        )
        return _redirect_user(user_id, ok=f"سرویس #{service_id} حذف شد{note}")

    @app.get("/users/{user_id}/services/{service_id}/link")
    async def user_service_link(
        user_id: int,
        service_id: int,
        staff: dict = Depends(require_ops),
        session: AsyncSession = Depends(get_db),
    ):
        from fastapi.responses import JSONResponse

        from app.services.bot_user_admin import get_owned_service, service_snapshot

        user = await session.get(BotUser, int(user_id))
        if not user:
            return JSONResponse({"ok": False, "error": "کاربر یافت نشد"}, status_code=404)
        try:
            assert_bot_user_in_scope(staff, user)
        except ShopScopeError as e:
            return JSONResponse({"ok": False, "error": e.message}, status_code=403)

        try:
            svc = await get_owned_service(
                session, bot_user_id=user_id, service_id=service_id
            )
            snap = await service_snapshot(session, svc)
            await session.commit()
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=404)
        url = snap.subscription_url
        if not url:
            return JSONResponse({"ok": False, "error": "لینک موجود نیست"}, status_code=404)
        return JSONResponse(
            {
                "ok": True,
                "url": url,
                "service_id": service_id,
                "username": (snap.pg or {}).get("username") or svc.pg_username,
            }
        )
