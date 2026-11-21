"""User list routes of the web panel: the users page and changing a user's role, block state or account.

Registered from ``create_api_app`` at the point its routes always had, so route order is unchanged.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import quote
from collections.abc import Callable

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Role
from app.services.bot_admins import assert_role_change_allowed, BotAdminRoleLocked, WEB_ASSIGNABLE_ROLES
from app.services.list_query import filter_by_search, normalize_search_q
from app.services.resellers import DEFAULT_FEATURE_PERMS
from app.services.user_search import clean_handle, with_search_matches as _with_search_matches

logger = logging.getLogger(__name__)


def register_user_list_pages(  # noqa: C901 - counts the nested routes; each is listed on its own
    app: object,
    *,
    get_db: Callable[..., object],
    redirect_msg: Callable[..., object],
    render: Callable[..., object],
    require_admin: Callable[..., object],
    require_perm: Callable[..., object],
) -> None:
    @app.get("/users", response_class=HTMLResponse)
    async def users_page(
        request: Request,
        staff: dict = Depends(require_perm("dashboard")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.platform_identity import is_explicit_owner_staff
        from app.services.shop_scope import ShopScopeError, resolve_shop_scope_id
        from app.api.user_link_search_pages import link_search_redirect
        from app.services.bot_user_admin import customer_service_actions
        from app.services.subscription_lookup import link_token
        from app.services.color_tags import (
            color_tag_meta,
            color_tags_for_ui,
            normalize_color_filter,
        )
        from app.services.customer_marks import color_filter_clause, marks_for
        from app.services.users_ops import (
            build_users_ops_page,
            parse_focus_uid,
            scoped_users_where,
            users_list_href,
        )
        from sqlalchemy import and_

        try:
            scope = resolve_shop_scope_id(staff)
        except ShopScopeError:
            return RedirectResponse("/home", status_code=303)

        search_q = normalize_search_q(request.query_params.get("q"))
        if search_q and link_token(search_q):
            # A link typed without the page script: answer it, but never echo the secret into the page.
            return await link_search_redirect(session, search_q, shop_id=scope)
        filter_raw = request.query_params.get("filter")
        focus_uid = parse_focus_uid(request.query_params.get("uid"))
        color_filter = normalize_color_filter(request.query_params.get("color"))
        fetch_limit = 500 if search_q else 200

        # Shop scope: Owner → platform users only; reseller → own customers.
        # Pure resellers (no shop services) stay on /resellers.
        where = scoped_users_where(scope)
        if color_filter:
            # This shop's own tags only (the platform's own on BotUser).
            where = and_(where, color_filter_clause(scope, color_filter))
        result = await session.execute(
            select(BotUser)
            .where(where)
            .order_by(BotUser.id.desc())
            .limit(fetch_limit)
        )
        users = list(result.scalars().all())
        if search_q:
            users = await _with_search_matches(session, users, where, search_q)
        from app.services.wallet import wallet_balances_for_scope

        # Scoped purse only — never surface another shop's ShopWallet / platform
        # balance to tenant staff (and Owner sees platform BotUser.wallet_balance).
        wallet_by_uid = await wallet_balances_for_scope(
            session, [int(u.id) for u in users], shop_id=scope
        )
        if search_q:
            users = filter_by_search(
                users,
                clean_handle(search_q),
                lambda u: (
                    u.id,
                    u.telegram_id,
                    u.username,
                    u.full_name,
                    u.role,
                    wallet_by_uid.get(int(u.id), 0),
                    "مسدود" if u.is_blocked else "فعال",
                ),
            )
        rows, counts, filter_key = await build_users_ops_page(
            session,
            users,
            filter_key=filter_raw,
            focus_uid=focus_uid,
            shop_id=scope,
            wallet_by_uid=wallet_by_uid,
        )
        # Deep-link uid outside this shop → ignore (no cross-tenant leak).
        if focus_uid and not any(int(r.user.id) == int(focus_uid) for r in rows):
            in_fetched = any(int(u.id) == int(focus_uid) for u in users)
            if not in_fetched:
                focus_uid = None
        pg_acts = customer_service_actions(staff)
        in_shop = is_explicit_owner_staff(staff) or scope is not None

        def _users_href(**kwargs):
            kwargs.setdefault("color", color_filter)
            return users_list_href(**kwargs)

        return render(
            request,
            "users.html",
            {
                "staff": staff,
                "rows": rows,
                "users": [r.user for r in rows],
                "counts": counts,
                "filter": filter_key,
                "filter_href": _users_href,
                "q": search_q,
                "color_filter": color_filter,
                "color_tags": color_tags_for_ui(),
                "color_tag_meta": color_tag_meta,
                "marks_by_uid": await marks_for(session, scope, [int(r.user.id) for r in rows]),
                "flash_ok": request.query_params.get("ok"),
                "flash_err": request.query_params.get("err"),
                "open_edit": request.query_params.get("edit"),
                "can_manage_users": is_explicit_owner_staff(staff),
                # Shop scope ∧ PasarGuard users.* ACL (same axis as /pg/users).
                "can_pg_create": bool(pg_acts.get("create")),
                "can_pg_update": bool(pg_acts.get("update")),
                "can_pg_delete": bool(pg_acts.get("delete")),
                # CRM identity create: shop scope only (no PG call yet).
                "can_provision_users": in_shop,
                # Service mutate UI: shop scope ∧ any relevant PG users.* action.
                "can_edit_user_services": in_shop and bool(
                    pg_acts.get("create") or pg_acts.get("update") or pg_acts.get("delete")
                ),
                "can_user_ops": True,
            },
        )

    @app.post("/users/{user_id}/role")
    async def users_set_role(
        request: Request,
        user_id: int,
        role: str = Form(...),
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        form = await request.form()
        reason = str(form.get("reason") or "").strip()
        user = await session.get(BotUser, user_id)
        if not user:
            return redirect_msg("/users", err="کاربر یافت نشد")
        from app.services.shop_scope import ShopScopeError, assert_bot_user_in_scope

        try:
            assert_bot_user_in_scope(staff, user)
        except ShopScopeError as e:
            return redirect_msg("/users", err=e.message)
        try:
            assert_role_change_allowed(user, role)
        except BotAdminRoleLocked as exc:
            return redirect_msg("/users", err=str(exc))
        if role not in WEB_ASSIGNABLE_ROLES:
            return redirect_msg("/users", err="نقش نامعتبر")

        from app.services.notifications import actor_label_from_staff, notify_account_edit

        old_role = user.role
        actor = actor_label_from_staff(staff)

        if role == Role.RESELLER.value:
            from app.services.resellers import (
                format_credentials_message,
                get_reseller_panel_base_url,
                provision_reseller,
                reseller_plan_from_form,
            )

            try:
                # A package brings its own PasarGuard role, limits and permissions.
                plan = await reseller_plan_from_form(session, staff, form.get("reseller_plan_id"))
                perms = None if plan is not None else DEFAULT_FEATURE_PERMS
                creds = await provision_reseller(
                    session,
                    user=user,
                    plan=plan,
                    web_permissions=perms,
                    bot_permissions=perms,
                    create_pg_admin=True,
                    panel_base_url=await get_reseller_panel_base_url(session),
                )
            except Exception as e:
                return redirect_msg("/users", err=str(e))
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
                logger.debug("users_set_role: ignored failure", exc_info=True)
            # Admin mirror + short role notice (credentials already sent to subject)
            await notify_account_edit(
                session,
                user=user,
                event="role",
                reason=reason,
                old_role=old_role,
                new_role=role,
                actor=actor,
                notify_subject=False,
            )
        else:
            # Demoting a reseller must tear down profile / PG admin / shop bot
            if user.role == Role.RESELLER.value and role != Role.RESELLER.value:
                from app.services.resellers import (
                    PanelAdminNotRemoved,
                    notify_reseller_revoked,
                    revoke_reseller,
                )

                try:
                    info = await revoke_reseller(
                        session,
                        user.id,
                        delete_pg_admin=True,
                        reason=reason,
                    )
                except PanelAdminNotRemoved as e:
                    # The shop and its role stay as they are while its admin is still in PasarGuard.
                    return redirect_msg("/users", err=str(e))
                except ValueError:
                    user.role = role
                    await session.commit()
                    info = {"telegram_id": user.telegram_id}
                except Exception as e:
                    from app.services.users import friendly_user_delete_error

                    return redirect_msg("/users", err=friendly_user_delete_error(e))
                user = await session.get(BotUser, user_id)
                if user:
                    await notify_reseller_revoked(
                        int(info.get("telegram_id") or user.telegram_id),
                        reason,
                        session=session,
                        user=user,
                        actor=actor,
                    )
            else:
                user.role = role
                await session.commit()
                await notify_account_edit(
                    session,
                    user=user,
                    event="role",
                    reason=reason,
                    old_role=old_role,
                    new_role=role,
                    actor=actor,
                )
        return RedirectResponse(
            f"/users?edit={int(user_id)}&ok={quote('نقش به‌روز شد')}&_={int(time.time())}",
            status_code=303,
        )

    @app.post("/users/{user_id}/block")
    async def users_toggle_block(
        request: Request,
        user_id: int,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.notifications import actor_label_from_staff, notify_account_edit
        from app.services.users import is_protected_admin

        form = await request.form()
        reason = str(form.get("reason") or "").strip()
        user = await session.get(BotUser, user_id)
        if not user:
            return redirect_msg("/users", err="کاربر یافت نشد")
        from app.services.shop_scope import ShopScopeError, assert_bot_user_in_scope

        try:
            assert_bot_user_in_scope(staff, user)
        except ShopScopeError as e:
            return redirect_msg("/users", err=e.message)
        if is_protected_admin(user):
            return redirect_msg("/users", err="مسدود کردن ادمین مجاز نیست")
        will_block = not user.is_blocked
        user.is_blocked = will_block
        await session.commit()
        await notify_account_edit(
            session,
            user=user,
            event="block" if will_block else "unblock",
            reason=reason or None,
            actor=actor_label_from_staff(staff),
        )
        return redirect_msg("/users", ok="وضعیت مسدودی تغییر کرد")

    @app.post("/users/{user_id}/delete")
    async def users_delete(
        request: Request,
        user_id: int,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.notifications import actor_label_from_staff, notify_account_edit
        from app.services.users import delete_bot_user, friendly_user_delete_error

        form = await request.form()
        from app.services.delete_reason import delete_reason_too_short, extract_delete_reason

        reason = extract_delete_reason(form)
        if delete_reason_too_short(reason):
            return redirect_msg("/users", err="علت حذف کاربر الزامی است (حداقل ۳ کاراکتر)")

        actor_id = None
        try:
            actor_id = int(staff.get("user_id") or 0) or None
        except (TypeError, ValueError):
            actor_id = None
        # Prefer matching by telegram if panel session stores it
        if not actor_id and staff.get("telegram_id"):
            try:
                tg = int(staff["telegram_id"])
                actor = (
                    await session.execute(select(BotUser).where(BotUser.telegram_id == tg))
                ).scalar_one_or_none()
                actor_id = actor.id if actor else None
            except Exception:
                actor_id = None

        user = await session.get(BotUser, user_id)
        if not user:
            return redirect_msg("/users", err="کاربر یافت نشد")
        from app.services.shop_scope import ShopScopeError, assert_bot_user_in_scope

        try:
            assert_bot_user_in_scope(staff, user)
        except ShopScopeError as e:
            return redirect_msg("/users", err=e.message)
        await notify_account_edit(
            session,
            user=user,
            event="user_delete",
            reason=reason,
            actor=actor_label_from_staff(staff),
        )
        try:
            info = await delete_bot_user(
                session,
                user_id,
                actor_user_id=actor_id,
            )
        except ValueError as e:
            return redirect_msg("/users", err=str(e))
        except Exception as e:
            return redirect_msg("/users", err=friendly_user_delete_error(e))
        label = info.get("name") or info.get("telegram_id")
        return redirect_msg("/users", ok=f"کاربر {label} حذف شد")
