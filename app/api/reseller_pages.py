from __future__ import annotations

"""Reseller plans, applications, and staff management pages."""

from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings, normalize_pg_base_url
from app.db.models import BotUser, ResellerPlan, ResellerProfile, Role
from app.services.pasarguard import get_pg
from app.services.resellers import (
    DEFAULT_FEATURE_PERMS,
    FEATURE_PERMS,
    approve_application,
    format_credentials_message,
    get_application,
    get_reseller_panel_base_url,
    get_reseller_pg_panel_base_url,
    join_perms,
    list_applications,
    list_reseller_plans,
    normalize_feature_perms,
    notify_reseller_revoked,
    parse_perms,
    provision_reseller,
    reject_application,
    revoke_reseller,
    with_shop_settings,
)


def _q(msg: str) -> str:
    return quote(str(msg), safe="")


def _feature_perms_from_form(form) -> str:
    selected = []
    for key, _ in FEATURE_PERMS:
        if form.get(f"perm_{key}"):
            selected.append(key)
    return normalize_feature_perms(join_perms(selected) or DEFAULT_FEATURE_PERMS)


def register_reseller_pages(app, *, render, require_admin, get_db):
    def _tabs(active: str) -> list[dict]:
        return [
            {"href": "/resellers", "label": "لیست", "id": "list"},
            {"href": "/resellers/plans", "label": "پلن‌ها", "id": "plans"},
            {"href": "/resellers/applications", "label": "درخواست‌ها", "id": "apps"},
        ]

    @app.get("/resellers", response_class=HTMLResponse)
    async def resellers_page(
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.setup_wizard import default_panel_base_url
        from app.services.users import get_setting

        result = await session.execute(
            select(BotUser, ResellerProfile)
            .join(ResellerProfile, ResellerProfile.user_id == BotUser.id)
            .order_by(ResellerProfile.id.desc())
        )
        rows = result.all()
        roles = []
        try:
            roles = await get_pg().get_admin_roles()
        except Exception:
            roles = []
        panel_url = await get_reseller_panel_base_url(session)
        custom_url = (await get_setting(session, "reseller_panel_base_url") or "").strip()
        default_url = default_panel_base_url()
        pg_panel_url = await get_reseller_pg_panel_base_url(session)
        custom_pg_url = (await get_setting(session, "reseller_pg_panel_base_url") or "").strip()
        default_pg_url = normalize_pg_base_url(get_settings().pg_base_url or "")
        return render(
            request,
            "resellers.html",
            {
                "staff": staff,
                "rows": rows,
                "tabs": _tabs("list"),
                "tab": "list",
                "feature_perms": FEATURE_PERMS,
                "pg_roles": roles,
                "panel_url": panel_url,
                "custom_panel_url": custom_url,
                "default_panel_url": default_url,
                "using_custom_panel_url": bool(custom_url),
                "pg_panel_url": pg_panel_url,
                "custom_pg_panel_url": custom_pg_url,
                "default_pg_panel_url": default_pg_url,
                "using_custom_pg_panel_url": bool(custom_pg_url),
                "flash_ok": request.query_params.get("ok"),
                "flash_err": request.query_params.get("err"),
            },
        )

    @app.post("/resellers/panel-url")
    async def reseller_panel_url_save(
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.users import set_setting

        form = await request.form()
        bot_url = str(form.get("reseller_panel_base_url") or "").strip().rstrip("/")
        pg_url = normalize_pg_base_url(str(form.get("reseller_pg_panel_base_url") or ""))
        await set_setting(session, "reseller_panel_base_url", bot_url)
        await set_setting(session, "reseller_pg_panel_base_url", pg_url)
        return RedirectResponse(
            f"/resellers?ok={_q('آدرس‌های پنل نماینده ذخیره شد')}",
            status_code=303,
        )

    @app.post("/resellers")
    async def reseller_create(
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        form = await request.form()
        try:
            telegram_id = int(str(form.get("telegram_id") or "0"))
        except ValueError:
            return RedirectResponse(f"/resellers?err={_q('آیدی تلگرام نامعتبر')}", status_code=303)
        result = await session.execute(select(BotUser).where(BotUser.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if not user:
            return RedirectResponse(
                f"/resellers?err={_q('کاربر با این آیدی در ربات پیدا نشد — اول باید استارت زده باشد')}",
                status_code=303,
            )
        try:
            commission = int(str(form.get("commission_percent") or "10"))
        except ValueError:
            commission = 10
        perms = _feature_perms_from_form(form)
        create_pg = bool(form.get("create_pg_admin"))
        share_pg = bool(form.get("share_pg_panel_url"))
        pg_role_raw = str(form.get("pg_role_id") or "").strip()
        pg_role_id = int(pg_role_raw) if pg_role_raw.isdigit() else None
        panel_url = await get_reseller_panel_base_url(session)
        try:
            creds = await provision_reseller(
                session,
                user=user,
                commission_percent=commission,
                web_permissions=perms,
                bot_permissions=perms,
                create_pg_admin=create_pg,
                share_pg_panel_url=share_pg,
                pg_role_id=pg_role_id,
                panel_base_url=panel_url,
            )
        except Exception as e:
            return RedirectResponse(f"/resellers?err={_q(str(e))}", status_code=303)

        try:
            from app.bot import create_bot

            bot = create_bot()
            try:
                await bot.send_message(
                    user.telegram_id,
                    format_credentials_message(creds),
                    parse_mode="HTML",
                )
            finally:
                await bot.session.close()
        except Exception:
            pass
        return RedirectResponse(
            f"/resellers?ok={_q('نماینده فعال شد — اطلاعات ورود (پاسارگارد + وب‌پنل) ارسال شد')}",
            status_code=303,
        )

    @app.get("/resellers/{user_id}/edit", response_class=HTMLResponse)
    async def reseller_edit_page(
        user_id: int,
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        user = await session.get(BotUser, user_id)
        profile = (
            await session.execute(select(ResellerProfile).where(ResellerProfile.user_id == user_id))
        ).scalar_one_or_none()
        if not user or not profile:
            return RedirectResponse(f"/resellers?err={_q('نماینده یافت نشد')}", status_code=303)
        roles = []
        try:
            roles = await get_pg().get_admin_roles()
        except Exception:
            roles = []
        perms = with_shop_settings(
            parse_perms(profile.web_permissions) or parse_perms(DEFAULT_FEATURE_PERMS)
        )
        from app.services.billing import is_payg, list_billing_transactions
        from app.services.formatting import format_toman
        import secrets

        billing_txs = []
        if is_payg(profile):
            billing_txs = await list_billing_transactions(session, int(user_id), limit=15)
        return render(
            request,
            "reseller_edit.html",
            {
                "staff": staff,
                "user": user,
                "profile": profile,
                "feature_perms": FEATURE_PERMS,
                "selected_perms": perms,
                "pg_roles": roles,
                "is_payg": is_payg(profile),
                "billing_txs": billing_txs,
                "format_toman": format_toman,
                "topup_nonce": secrets.token_hex(8),
                "flash_ok": request.query_params.get("ok"),
                "flash_err": request.query_params.get("err"),
            },
        )

    @app.post("/resellers/{user_id}/edit")
    async def reseller_edit_save(
        user_id: int,
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        form = await request.form()
        user = await session.get(BotUser, user_id)
        profile = (
            await session.execute(select(ResellerProfile).where(ResellerProfile.user_id == user_id))
        ).scalar_one_or_none()
        if not user or not profile:
            return RedirectResponse(f"/resellers?err={_q('نماینده یافت نشد')}", status_code=303)
        try:
            profile.commission_percent = int(str(form.get("commission_percent") or "10"))
        except ValueError:
            pass
        mode = str(form.get("billing_mode") or "").strip().lower()
        if mode in {"fixed", "payg"}:
            profile.billing_mode = mode
        perms = _feature_perms_from_form(form)
        profile.web_permissions = perms
        profile.bot_permissions = perms  # must stay identical
        profile.can_approve_receipts = "payments" in parse_perms(perms)
        profile.is_active = bool(form.get("is_active"))
        profile.share_pg_panel_url = bool(form.get("share_pg_panel_url"))
        pg_role_raw = str(form.get("pg_role_id") or "").strip()
        profile.pg_role_id = int(pg_role_raw) if pg_role_raw.isdigit() else None
        pg_user = str(form.get("pg_admin_username") or "").strip()
        if pg_user:
            from app.services.pg_staff_access import conflict_message_for_reseller_link

            link_err = await conflict_message_for_reseller_link(
                session, pg_user, exclude_profile_id=int(profile.id)
            )
            if link_err:
                return RedirectResponse(
                    f"/resellers/{user_id}/edit?err={_q(link_err)}",
                    status_code=303,
                )
        profile.pg_admin_username = pg_user or None
        from app.services.reseller_access import normalize_telegram_ids_csv

        profile.bot_admin_ids = normalize_telegram_ids_csv(
            str(form.get("bot_admin_ids") or "").strip() or None
        )
        new_pass = str(form.get("web_password") or "").strip()
        if new_pass:
            from app.services.resellers import apply_reseller_panel_password
            from app.services.web_auth import validate_password_strength

            ok, perr = validate_password_strength(new_pass)
            if not ok:
                return RedirectResponse(
                    f"/resellers/{user_id}/edit?err={_q(perr)}",
                    status_code=303,
                )
            try:
                await apply_reseller_panel_password(session, profile, new_pass, sync_pg=True)
            except ValueError as e:
                return RedirectResponse(
                    f"/resellers/{user_id}/edit?err={_q(str(e))}",
                    status_code=303,
                )
            if profile.web_username and not profile.setup_completed_at:
                profile.setup_completed_at = datetime.now(timezone.utc)
        if bool(form.get("reissue_setup")):
            from app.services.resellers import new_setup_token

            token, expires = new_setup_token()
            profile.setup_token = token
            profile.setup_token_expires = expires
            # Keep web login if creds already exist — link is for bot-token only
            if profile.web_username and profile.web_password_hash:
                if not profile.setup_completed_at:
                    profile.setup_completed_at = datetime.now(timezone.utc)
            else:
                profile.setup_completed_at = None
            base = await get_reseller_panel_base_url(session)
            pg_panel = await get_reseller_pg_panel_base_url(session) if profile.share_pg_panel_url else ""
            if base:
                try:
                    from app.bot import create_bot

                    bot = create_bot()
                    try:
                        from app.services.formatting import copyable

                        parts = [
                            "🔗 لینک جدید راه‌اندازی نماینده:",
                            copyable(f"{base}/rsetup/{token}"),
                            "",
                            f"آدرس وب‌پنل ربات: {copyable(base)}",
                            f"ورود: {copyable(f'{base}/login')}",
                        ]
                        if pg_panel:
                            parts += ["", f"آدرس پنل پاسارگارد: {copyable(pg_panel)}"]
                        if profile.web_username:
                            parts += [
                                "",
                                f"یوزر وب فعلی: {copyable(profile.web_username)}",
                                "در لینک فقط توکن ربات اختصاصی را ثبت کنید.",
                            ]
                        else:
                            parts += ["", "۴۸ ساعت اعتبار — یوزر/رمز وب را در همان صفحه بسازید."]
                        await bot.send_message(
                            user.telegram_id,
                            "\n".join(parts),
                            parse_mode="HTML",
                        )
                    finally:
                        await bot.session.close()
                except Exception:
                    pass
        user.role = Role.RESELLER.value if profile.is_active else Role.USER.value
        await session.commit()
        return RedirectResponse(f"/resellers/{user_id}/edit?ok={_q('ذخیره شد')}", status_code=303)

    @app.post("/resellers/{user_id}/billing-topup")
    async def reseller_billing_topup(
        user_id: int,
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        """MVP: manual Billing credit by Super Admin only."""
        form = await request.form()
        raw = str(form.get("amount") or "").strip().replace(",", "")
        note = str(form.get("note") or "").strip()
        try:
            amount = int(float(raw))
        except (TypeError, ValueError):
            return RedirectResponse(
                f"/resellers/{user_id}/edit?err={_q('مبلغ نامعتبر است')}",
                status_code=303,
            )
        if amount < 1000:
            return RedirectResponse(
                f"/resellers/{user_id}/edit?err={_q('حداقل شارژ ۱۰۰۰ تومان است')}",
                status_code=303,
            )
        from app.services.billing import credit_topup

        actor = str(staff.get("username") or staff.get("role") or "admin")
        try:
            await credit_topup(
                session,
                int(user_id),
                amount,
                created_by=actor,
                note=note or "شارژ دستی ادمین",
                idempotency_key=f"manual:{user_id}:{amount}:{note}:{actor}:{form.get('nonce') or ''}",
            )
        except ValueError as e:
            return RedirectResponse(
                f"/resellers/{user_id}/edit?err={_q(str(e))}",
                status_code=303,
            )
        return RedirectResponse(
            f"/resellers/{user_id}/edit?ok={_q(f'شارژ Billing به مبلغ {amount:,} تومان ثبت شد')}",
            status_code=303,
        )

    @app.post("/resellers/{user_id}/delete")
    async def reseller_delete(
        user_id: int,
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        form = await request.form()
        reason = str(form.get("reason") or "").strip()
        if len(reason) < 3:
            return RedirectResponse(
                f"/resellers?err={_q('علت حذف نمایندگی الزامی است (حداقل ۳ کاراکتر)')}",
                status_code=303,
            )
        try:
            info = await revoke_reseller(
                session, user_id, delete_pg_admin=True, reason=reason
            )
        except ValueError as e:
            return RedirectResponse(f"/resellers?err={_q(str(e))}", status_code=303)
        except Exception as e:
            return RedirectResponse(f"/resellers?err={_q(str(e))}", status_code=303)
        from app.services.notifications import actor_label_from_staff

        user = await session.get(BotUser, user_id)
        notified = False
        if user:
            notified = await notify_reseller_revoked(
                int(info["telegram_id"]),
                reason,
                session=session,
                user=user,
                actor=actor_label_from_staff(staff),
            )
        else:
            notified = await notify_reseller_revoked(int(info["telegram_id"]), reason)
        label = info.get("telegram_id") or user_id
        note = " — پیام علت ارسال شد" if notified else " — پیام تلگرام ارسال نشد"
        return RedirectResponse(
            f"/resellers?ok={_q(f'نمایندگی {label} حذف شد{note}')}",
            status_code=303,
        )

    @app.post("/resellers/{user_id}/role")
    async def reseller_set_role(
        user_id: int,
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        form = await request.form()
        role = str(form.get("role") or "").strip()
        user = await session.get(BotUser, user_id)
        profile = (
            await session.execute(select(ResellerProfile).where(ResellerProfile.user_id == user_id))
        ).scalar_one_or_none()
        if not user or not profile:
            return RedirectResponse(f"/resellers?err={_q('نماینده یافت نشد')}", status_code=303)
        if role not in {Role.USER.value, Role.RESELLER.value, Role.ADMIN.value}:
            return RedirectResponse(f"/resellers?err={_q('نقش نامعتبر')}", status_code=303)

        reason = str(form.get("reason") or "").strip()
        if len(reason) < 3:
            return RedirectResponse(
                f"/resellers?err={_q('علت تغییر نقش الزامی است (حداقل ۳ کاراکتر)')}",
                status_code=303,
            )

        if role == Role.USER.value:
            try:
                info = await revoke_reseller(
                    session, user_id, delete_pg_admin=True, reason=reason
                )
            except ValueError as e:
                return RedirectResponse(f"/resellers?err={_q(str(e))}", status_code=303)
            from app.services.notifications import actor_label_from_staff

            user = await session.get(BotUser, user_id)
            if user:
                await notify_reseller_revoked(
                    int(info["telegram_id"]),
                    reason,
                    session=session,
                    user=user,
                    actor=actor_label_from_staff(staff),
                )
            else:
                await notify_reseller_revoked(int(info["telegram_id"]), reason)
            return RedirectResponse(
                f"/resellers?ok={_q('نقش به کاربر عادی تغییر کرد و اطلاع داده شد')}",
                status_code=303,
            )

        if role == Role.ADMIN.value:
            from app.services.notifications import actor_label_from_staff, notify_account_edit

            old_role = user.role
            # Full teardown — do not leave an inactive ResellerProfile that can be reactivated.
            try:
                await revoke_reseller(
                    session,
                    user_id,
                    delete_pg_admin=True,
                    reason=reason or "ارتقا به مدیر",
                    commit=False,
                )
            except ValueError as e:
                return RedirectResponse(f"/resellers?err={_q(str(e))}", status_code=303)
            user.role = Role.ADMIN.value
            await session.commit()
            await notify_account_edit(
                session,
                user=user,
                event="role",
                reason=reason,
                old_role=old_role,
                new_role=Role.ADMIN.value,
                actor=actor_label_from_staff(staff),
            )
            return RedirectResponse(
                f"/resellers?ok={_q('نقش به مدیر تغییر کرد و پروفایل نماینده حذف شد')}",
                status_code=303,
            )

        # reseller
        from app.services.notifications import actor_label_from_staff, notify_account_edit

        old_role = user.role
        profile.is_active = True
        user.role = Role.RESELLER.value
        await session.commit()
        await notify_account_edit(
            session,
            user=user,
            event="role",
            reason=reason,
            old_role=old_role,
            new_role=Role.RESELLER.value,
            actor=actor_label_from_staff(staff),
        )
        return RedirectResponse(f"/resellers?ok={_q('نقش نماینده فعال شد')}", status_code=303)

    @app.post("/resellers/{user_id}/delete-user")
    async def reseller_delete_user(
        user_id: int,
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        """Full bot-user delete from resellers tab (same cascade as /users delete)."""
        form = await request.form()
        reason = str(form.get("reason") or "").strip()
        if len(reason) < 3:
            return RedirectResponse(
                f"/resellers?err={_q('علت حذف کاربر الزامی است')}",
                status_code=303,
            )
        from app.services.users import delete_bot_user
        from app.services.notifications import actor_label_from_staff, notify_account_edit

        # Notify before delete while telegram_id still available
        user = await session.get(BotUser, user_id)
        if not user:
            return RedirectResponse(f"/resellers?err={_q('کاربر یافت نشد')}", status_code=303)
        try:
            await notify_account_edit(
                session,
                user=user,
                event="user_delete",
                reason=reason,
                actor=actor_label_from_staff(staff),
            )
            info = await delete_bot_user(
                session,
                user_id,
                actor_user_id=staff.get("bot_user_id"),
            )
        except ValueError as e:
            return RedirectResponse(f"/resellers?err={_q(str(e))}", status_code=303)
        except Exception as e:
            return RedirectResponse(f"/resellers?err={_q(str(e))}", status_code=303)
        label = info.get("name") or info.get("telegram_id")
        return RedirectResponse(
            f"/resellers?ok={_q(f'کاربر {label} کامل حذف شد')}",
            status_code=303,
        )

    # ---- plans ----
    @app.get("/resellers/plans", response_class=HTMLResponse)
    async def reseller_plans_page(
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        plans = await list_reseller_plans(session)
        roles = []
        try:
            roles = await get_pg().get_admin_roles()
        except Exception:
            roles = []
        return render(
            request,
            "reseller_plans.html",
            {
                "staff": staff,
                "plans": plans,
                "tabs": _tabs("plans"),
                "tab": "plans",
                "feature_perms": FEATURE_PERMS,
                "pg_roles": roles,
                "flash_ok": request.query_params.get("ok"),
                "flash_err": request.query_params.get("err"),
            },
        )

    @app.post("/resellers/plans")
    async def reseller_plan_create(
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        form = await request.form()
        name = str(form.get("name") or "").strip()
        if not name:
            return RedirectResponse(f"/resellers/plans?err={_q('نام الزامی است')}", status_code=303)
        try:
            price = int(str(form.get("price") or "0").replace(",", "").replace("٬", ""))
        except ValueError:
            price = 0
        try:
            commission = int(str(form.get("commission_percent") or "10"))
        except ValueError:
            commission = 10
        perms = _feature_perms_from_form(form)
        pg_role_raw = str(form.get("pg_role_id") or "").strip()
        plan = ResellerPlan(
            name=name,
            description=str(form.get("description") or "").strip() or None,
            price=max(0, price),
            commission_percent=commission,
            can_approve_receipts="payments" in parse_perms(perms),
            web_permissions=perms,
            bot_permissions=perms,
            create_pg_admin=bool(form.get("create_pg_admin")),
            create_web_access=True,
            share_pg_panel_url=bool(form.get("share_pg_panel_url")),
            pg_role_id=int(pg_role_raw) if pg_role_raw.isdigit() else None,
            is_active=bool(form.get("is_active", "1")),
            sort_order=int(str(form.get("sort_order") or "0") or "0"),
        )
        session.add(plan)
        await session.commit()
        return RedirectResponse(f"/resellers/plans?ok={_q('پلن ذخیره شد')}", status_code=303)

    @app.get("/resellers/plans/{plan_id}/edit", response_class=HTMLResponse)
    async def reseller_plan_edit_page(
        plan_id: int,
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        plan = await session.get(ResellerPlan, plan_id)
        if not plan:
            return RedirectResponse(f"/resellers/plans?err={_q('یافت نشد')}", status_code=303)
        roles = []
        try:
            roles = await get_pg().get_admin_roles()
        except Exception:
            roles = []
        return render(
            request,
            "reseller_plan_edit.html",
            {
                "staff": staff,
                "plan": plan,
                "feature_perms": FEATURE_PERMS,
                "selected_perms": with_shop_settings(
                    parse_perms(plan.web_permissions) or parse_perms(DEFAULT_FEATURE_PERMS)
                ),
                "pg_roles": roles,
                "flash_ok": request.query_params.get("ok"),
                "flash_err": request.query_params.get("err"),
            },
        )

    @app.post("/resellers/plans/{plan_id}/edit")
    async def reseller_plan_edit_save(
        plan_id: int,
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        plan = await session.get(ResellerPlan, plan_id)
        if not plan:
            return RedirectResponse(f"/resellers/plans?err={_q('یافت نشد')}", status_code=303)
        form = await request.form()
        name = str(form.get("name") or "").strip()
        if not name:
            return RedirectResponse(
                f"/resellers/plans/{plan_id}/edit?err={_q('نام الزامی است')}", status_code=303
            )
        plan.name = name
        plan.description = str(form.get("description") or "").strip() or None
        try:
            plan.price = max(0, int(str(form.get("price") or "0").replace(",", "").replace("٬", "")))
        except ValueError:
            pass
        try:
            plan.commission_percent = int(str(form.get("commission_percent") or "10"))
        except ValueError:
            pass
        try:
            plan.sort_order = int(str(form.get("sort_order") or "0") or "0")
        except ValueError:
            pass
        perms = _feature_perms_from_form(form)
        plan.web_permissions = perms
        plan.bot_permissions = perms
        plan.can_approve_receipts = "payments" in parse_perms(perms)
        plan.create_pg_admin = bool(form.get("create_pg_admin"))
        plan.share_pg_panel_url = bool(form.get("share_pg_panel_url"))
        plan.is_active = bool(form.get("is_active"))
        pg_role_raw = str(form.get("pg_role_id") or "").strip()
        plan.pg_role_id = int(pg_role_raw) if pg_role_raw.isdigit() else None
        await session.commit()
        return RedirectResponse(
            f"/resellers/plans/{plan_id}/edit?ok={_q('ذخیره شد')}", status_code=303
        )

    @app.post("/resellers/plans/{plan_id}/toggle")
    async def reseller_plan_toggle(
        plan_id: int,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        plan = await session.get(ResellerPlan, plan_id)
        if plan:
            plan.is_active = not plan.is_active
            await session.commit()
        return RedirectResponse("/resellers/plans", status_code=303)

    @app.post("/resellers/plans/{plan_id}/delete")
    async def reseller_plan_delete(
        plan_id: int,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        plan = await session.get(ResellerPlan, plan_id)
        if plan:
            await session.delete(plan)
            await session.commit()
        return RedirectResponse(f"/resellers/plans?ok={_q('حذف شد')}", status_code=303)

    # ---- applications ----
    @app.get("/resellers/applications", response_class=HTMLResponse)
    async def reseller_apps_page(
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        apps = await list_applications(session)
        return render(
            request,
            "reseller_applications.html",
            {
                "staff": staff,
                "apps": apps,
                "tabs": _tabs("apps"),
                "tab": "apps",
                "flash_ok": request.query_params.get("ok"),
                "flash_err": request.query_params.get("err"),
            },
        )

    @app.post("/resellers/applications/{app_id}/approve")
    async def reseller_app_approve(
        app_id: int,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        app = await get_application(session, app_id)
        if not app:
            return RedirectResponse(f"/resellers/applications?err={_q('یافت نشد')}", status_code=303)
        try:
            creds = await approve_application(
                session,
                app,
                reviewer_tg=0,
                panel_base_url=await get_reseller_panel_base_url(session),
            )
            user = await session.get(BotUser, app.user_id)
            if user:
                from app.bot import create_bot

                bot = create_bot()
                try:
                    await bot.send_message(
                        user.telegram_id,
                        format_credentials_message(creds),
                        parse_mode="HTML",
                    )
                finally:
                    await bot.session.close()
        except Exception as e:
            return RedirectResponse(
                f"/resellers/applications?err={_q(str(e))}", status_code=303
            )
        return RedirectResponse(
            f"/resellers/applications?ok={_q('تأیید شد — اطلاعات ورود ارسال شد')}",
            status_code=303,
        )

    @app.post("/resellers/applications/{app_id}/reject")
    async def reseller_app_reject(
        app_id: int,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        app = await get_application(session, app_id)
        if not app:
            return RedirectResponse(f"/resellers/applications?err={_q('یافت نشد')}", status_code=303)
        try:
            await reject_application(session, app, reviewer_tg=0)
            user = await session.get(BotUser, app.user_id)
            if user:
                from app.bot import create_bot

                bot = create_bot()
                try:
                    await bot.send_message(
                        user.telegram_id,
                        "❌ درخواست نمایندگی شما رد شد.\nدر صورت نیاز با پشتیبانی در ارتباط باشید.",
                    )
                finally:
                    await bot.session.close()
        except Exception as e:
            return RedirectResponse(
                f"/resellers/applications?err={_q(str(e))}", status_code=303
            )
        return RedirectResponse(f"/resellers/applications?ok={_q('رد شد')}", status_code=303)
