from __future__ import annotations

"""Reseller plans, applications, and staff management pages."""

import logging
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings, normalize_pg_base_url
from app.db.models import BotUser, ResellerPlan, ResellerProfile, Role
from app.services.pasarguard import get_pg
from app.services.pg_quota import limit_snapshot_cards, load_staff_limit_snapshot
from app.services.resellers import (
    DEFAULT_FEATURE_PERMS,
    FEATURE_PERMS,
    approve_application,
    format_credentials_message,
    get_application,
    get_reseller_panel_base_url,
    get_reseller_pg_panel_base_url,
    join_perms,
    list_active_reseller_plans,
    list_applications,
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


def _require_rep_mgmt(staff: dict) -> None:
    """Server deny for Hybrid Owner / L1 without live PG ``admins.create``."""
    from fastapi import HTTPException

    from app.services.representative_unification import (
        RepresentativeUnifyError,
        assert_staff_can_manage_representatives,
    )

    try:
        assert_staff_can_manage_representatives(staff)
    except RepresentativeUnifyError:
        raise HTTPException(status_code=403, detail="forbidden")


async def _validate_reseller_capacity_inputs(
    staff: dict,
    *,
    included_gb: int = 0,
    included_users: int = 0,
    addon_gb: int = 0,
    addon_users: int = 0,
    session: AsyncSession | None = None,
) -> None:
    """Single-package guard for limited independent installs before save."""
    snapshot = await load_staff_limit_snapshot(staff, session=session)
    if not snapshot.get("restricted"):
        return
    from app.services.formatting import format_bytes

    total_gb = max(0, int(included_gb or 0)) + max(0, int(addon_gb or 0))
    total_users = max(0, int(included_users or 0)) + max(0, int(addon_users or 0))
    cap_gb = snapshot.get("account_data_limit")
    cap_users = snapshot.get("max_users")
    if cap_gb is not None and cap_gb > 0 and total_gb * (1024**3) > int(cap_gb):
        raise ValueError(
            f"ظرفیت این پلن از سقف حجم ادمین اصلی بیشتر است (حداکثر {format_bytes(cap_gb)})"
        )
    if cap_users is not None and cap_users > 0 and total_users > int(cap_users):
        raise ValueError(
            f"ظرفیت این پلن از سقف کاربر ادمین اصلی بیشتر است (حداکثر {int(cap_users)})"
        )


def _redirect_reseller_edit(user_id: int, *, ok: str | None = None, err: str | None = None):
    """Return to list and reopen edit modal."""
    qs = [f"edit={int(user_id)}"]
    if err:
        qs.append(f"err={_q(err)}")
    elif ok:
        qs.append(f"ok={_q(ok)}")
    return RedirectResponse(f"/resellers?{'&'.join(qs)}", status_code=303)




def _feature_perms_from_form(form) -> str:
    selected = []
    for key, _ in FEATURE_PERMS:
        if form.get(f"perm_{key}"):
            selected.append(key)
    return normalize_feature_perms(join_perms(selected) or DEFAULT_FEATURE_PERMS)


def _parse_price_per_gb(form) -> int:
    from app.services.numbers import parse_int

    try:
        return max(0, parse_int(str(form.get("price_per_gb") or "0"), default=0))
    except ValueError:
        return 0


def _parse_nonneg_int(form, key: str, default: int = 0) -> int:
    from app.services.numbers import parse_int

    try:
        return max(0, parse_int(str(form.get(key) or default), default=default))
    except ValueError:
        return default


def _parse_plan_kind(form) -> str:
    kind = str(form.get("plan_kind") or "subscription").strip().lower()
    if kind in {"subscription", "addon_volume", "addon_users"}:
        return kind
    return "subscription"


def _is_addon_plan_kind(kind: str | None) -> bool:
    return str(kind or "").strip().lower() in {"addon_volume", "addon_users"}


def _parse_renew_pricing_mode(form, *, allow_buy_extra: bool = False) -> str:
    """Parse renew mode; buy-extra subscriptions must use from_capacity."""
    if allow_buy_extra:
        return "from_capacity"
    mode = str(form.get("renew_pricing_mode") or "fixed").strip().lower()
    return mode if mode in {"fixed", "from_capacity"} else "fixed"


def _addon_fields_from_form(form, plan_kind: str) -> tuple[int, int]:
    """Validate pack size for addon plans. Raises ValueError on bad size."""
    addon_gb = _parse_nonneg_int(form, "addon_gb")
    addon_users = _parse_nonneg_int(form, "addon_users")
    if plan_kind == "addon_volume":
        if addon_gb <= 0:
            raise ValueError("حجم بسته باید بیشتر از صفر باشد")
        return addon_gb, 0
    if plan_kind == "addon_users":
        if addon_users <= 0:
            raise ValueError("تعداد کاربر بسته باید بیشتر از صفر باشد")
        return 0, addon_users
    return 0, 0


def _parse_pg_group_ids(form) -> str | None:
    ids: list[str] = []
    for k, v in form.items():
        key = str(k)
        if key.startswith("group_") and str(v).strip():
            ids.append(str(v).strip())
        elif key == "pg_group_ids" and str(v).strip():
            # comma-separated fallback
            ids.extend(x.strip() for x in str(v).split(",") if x.strip())
    # Preserve order, unique
    seen: list[str] = []
    for x in ids:
        if x not in seen:
            seen.append(x)
    return ",".join(seen) if seen else None


def register_reseller_pages(app, *, render, require_admin, get_db, require_staff=None):
    staff_dep = require_staff or require_admin

    def _tabs(active: str) -> list[dict]:
        # Plans live under unified /plans — no duplicate «پلن‌ها» tab here.
        return [
            {"href": "/resellers", "label": "لیست", "id": "list"},
            {"href": "/resellers/applications", "label": "درخواست‌ها", "id": "apps"},
        ]

    @app.get("/resellers", response_class=HTMLResponse)
    async def resellers_page(
        request: Request,
        staff: dict = Depends(staff_dep),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.pg_overview import admin_usage_snapshot
        from app.services.platform_identity import is_explicit_owner_staff
        from app.services.representative_unification import (
            RepresentativeUnifyError,
            assert_live_parent_for_child,
            descendant_shop_profile_ids,
        )
        from app.services.setup_wizard import default_panel_base_url
        from app.services.users import get_setting
        from fastapi import HTTPException

        try:
            await assert_live_parent_for_child(session, staff)
        except RepresentativeUnifyError:
            raise HTTPException(status_code=403, detail="forbidden")

        owner_view = is_explicit_owner_staff(staff)
        l2_pg_roles: list[dict] = []
        if not owner_view:
            try:
                from app.services.pasarguard import get_pg_for_principal

                pid = int(staff.get("org_principal_id") or 0)
                if pid > 0:
                    pg = await get_pg_for_principal(session, principal_id=pid)
                    raw_roles = await pg.get_admin_roles()
                    l2_pg_roles = [
                        r
                        for r in (raw_roles or [])
                        if isinstance(r, dict)
                        and r.get("id") is not None
                        and not bool(r.get("is_owner"))
                    ]
            except Exception:
                l2_pg_roles = []

        result = await session.execute(
            select(BotUser, ResellerProfile)
            .join(ResellerProfile, ResellerProfile.user_id == BotUser.id)
            .order_by(ResellerProfile.id.desc())
        )
        rows = result.all()
        if not owner_view:
            allowed = await descendant_shop_profile_ids(session, staff)
            rows = [pair for pair in rows if int(pair[1].id) in allowed]
        from app.services.color_tags import (
            color_tag_meta,
            color_tags_for_ui,
            effective_color_tag,
            normalize_color_filter,
        )

        color_filter = normalize_color_filter(request.query_params.get("color"))
        if color_filter:
            rows = [
                pair
                for pair in rows
                if effective_color_tag(pair[0]) == color_filter
            ]

        search_q = ""
        try:
            from app.services.list_query import filter_by_search, normalize_search_q

            search_q = normalize_search_q(request.query_params.get("q"))
            if search_q:
                rows = filter_by_search(
                    rows,
                    search_q,
                    lambda pair: (
                        pair[0].id,
                        pair[0].telegram_id,
                        pair[0].username,
                        pair[0].full_name,
                        pair[0].role,
                        pair[1].web_username,
                        pair[1].bot_username,
                        pair[1].pg_admin_username,
                        pair[1].billing_mode,
                    ),
                )
        except Exception:
            search_q = (request.query_params.get("q") or "").strip()
        # One-time PAYG→wallet link; only auto-restore wrongful legacy suspend on first merge
        try:
            from app.services.billing import ensure_payg_shop_wallet, is_payg
            from app.services.billing_suspend import restore_payg_reseller

            touched = False
            for _user, profile in rows:
                if not is_payg(profile):
                    continue
                was_linked = bool(getattr(profile, "payg_wallet_linked", False))
                _u, bal = await ensure_payg_shop_wallet(session, profile)
                touched = True
                if (
                    (not was_linked)
                    and bal > 0
                    and profile.billing_suspended_at is not None
                ):
                    try:
                        await restore_payg_reseller(session, profile, commit=False)
                    except Exception:
                        logging.getLogger(__name__).debug(
                            "list auto-restore failed", exc_info=True
                        )
            if touched:
                await session.commit()
        except Exception:
            try:
                await session.rollback()
            except Exception:
                pass
        roles = []
        reseller_usage: dict = {}
        if owner_view:
            try:
                roles = await get_pg().get_admin_roles()
            except Exception:
                roles = []
            try:
                pg = get_pg()
                admins = await pg.get_admins()
                if not admins:
                    admins = await pg.get_admins_simple()
                roles_by_id = {
                    int(r.get("id")): r
                    for r in (roles or [])
                    if isinstance(r, dict) and r.get("id") is not None
                }
                by_name = {
                    str(a.get("username") or "").strip().lower(): a
                    for a in (admins or [])
                    if isinstance(a, dict) and a.get("username")
                }
                for _user, profile in rows:
                    uname = str(profile.pg_admin_username or "").strip().lower()
                    if not uname:
                        continue
                    admin = by_name.get(uname)
                    if not isinstance(admin, dict):
                        continue
                    role = admin.get("role") if isinstance(admin.get("role"), dict) else None
                    if role is None:
                        rid = admin.get("role_id") or profile.pg_role_id
                        try:
                            role = roles_by_id.get(int(rid)) if rid is not None else None
                        except (TypeError, ValueError):
                            role = None
                    reseller_usage[int(profile.user_id)] = admin_usage_snapshot(admin, role)
            except Exception:
                reseller_usage = {}
        panel_url = await get_reseller_panel_base_url(session)
        custom_url = (await get_setting(session, "reseller_panel_base_url") or "").strip()
        default_url = default_panel_base_url()
        pg_panel_url = await get_reseller_pg_panel_base_url(session)
        custom_pg_url = (await get_setting(session, "reseller_pg_panel_base_url") or "").strip()
        default_pg_url = normalize_pg_base_url(get_settings().pg_base_url or "")
        show_reseller_apply = await get_setting(session, "show_reseller_apply", "1")
        try:
            reseller_plans = await list_active_reseller_plans(session)
        except Exception:
            reseller_plans = []
        return render(
            request,
            "resellers.html",
            {
                "staff": staff,
                "rows": rows,
                "reseller_usage": reseller_usage,
                "tabs": _tabs("list"),
                "tab": "list",
                "feature_perms": FEATURE_PERMS,
                "pg_roles": roles,
                "reseller_plans": reseller_plans,
                "panel_url": panel_url,
                "custom_panel_url": custom_url,
                "default_panel_url": default_url,
                "using_custom_panel_url": bool(custom_url),
                "pg_panel_url": pg_panel_url,
                "custom_pg_panel_url": custom_pg_url,
                "default_pg_panel_url": default_pg_url,
                "using_custom_pg_panel_url": bool(custom_pg_url),
                "show_reseller_apply": show_reseller_apply,
                "flash_ok": request.query_params.get("ok"),
                "flash_err": request.query_params.get("err"),
                "open_edit": request.query_params.get("edit"),
                "q": search_q,
                "owner_view": owner_view,
                "can_create_l2": not owner_view,
                "l2_pg_roles": l2_pg_roles,
                "color_filter": color_filter,
                "color_tags": color_tags_for_ui(),
                "color_tag_meta": color_tag_meta,
            },
        )

    @app.post("/resellers/panel-url")
    async def reseller_panel_url_save(
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        _require_rep_mgmt(staff)
        from app.services.users import set_setting

        form = await request.form()
        bot_url = str(form.get("reseller_panel_base_url") or "").strip().rstrip("/")
        pg_url = normalize_pg_base_url(str(form.get("reseller_pg_panel_base_url") or ""))
        show_apply = "1" if str(form.get("show_reseller_apply") or "") in {
            "1", "on", "true", "yes"
        } else "0"
        await set_setting(session, "reseller_panel_base_url", bot_url)
        await set_setting(session, "reseller_pg_panel_base_url", pg_url)
        await set_setting(session, "show_reseller_apply", show_apply)
        return RedirectResponse(
            f"/resellers?ok={_q('تنظیمات نمایندگی ذخیره شد')}",
            status_code=303,
        )

    @app.post("/resellers")
    async def reseller_create(
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        _require_rep_mgmt(staff)
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
        plan_raw = str(form.get("plan_id") or "").strip()
        if not plan_raw.isdigit():
            return RedirectResponse(
                f"/resellers?err={_q('انتخاب پلن نمایندگی الزامی است')}",
                status_code=303,
            )
        plan = await session.get(ResellerPlan, int(plan_raw))
        if not plan or not plan.is_active:
            return RedirectResponse(
                f"/resellers?err={_q('پلن نمایندگی نامعتبر یا غیرفعال است')}",
                status_code=303,
            )
        perms = _feature_perms_from_form(form)
        create_pg = bool(form.get("create_pg_admin"))
        share_pg = bool(form.get("share_pg_panel_url"))
        pg_role_raw = str(form.get("pg_role_id") or "").strip()
        pg_role_id = int(pg_role_raw) if pg_role_raw.isdigit() else None
        from app.services.color_tags import apply_color_tag

        # Whitelist-only; form omission / junk → green (مطمئن).
        apply_color_tag(user, str(form.get("color_tag") or ""))
        panel_url = await get_reseller_panel_base_url(session)
        try:
            creds = await provision_reseller(
                session,
                user=user,
                plan=plan,
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

    @app.post("/resellers/create-child")
    async def reseller_create_child(
        request: Request,
        staff: dict = Depends(staff_dep),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.platform_identity import is_explicit_owner_staff
        from app.services.principal_child_provisioning import (
            ChildProvisionError,
            Level2ProvisionRequest,
            provision_level2_child,
        )
        from app.services.representative_unification import (
            RepresentativeUnifyError,
            assert_live_parent_for_child,
        )
        from fastapi import HTTPException

        if is_explicit_owner_staff(staff):
            raise HTTPException(status_code=403, detail="forbidden")
        try:
            await assert_live_parent_for_child(session, staff)
        except RepresentativeUnifyError:
            raise HTTPException(status_code=403, detail="forbidden")
        form = await request.form()
        _ = form.get("parent_id")
        _ = form.get("depth")
        _ = form.get("principal_id")
        _ = form.get("reseller_profile_id")
        try:
            role_raw = str(form.get("pg_role_id") or "").strip()
            result = await provision_level2_child(
                session,
                staff,
                Level2ProvisionRequest(
                    pg_username=str(form.get("pg_username") or ""),
                    pg_password=str(form.get("pg_password") or ""),
                    pg_role_id=int(role_raw) if role_raw.isdigit() else None,
                    idempotency_key=str(form.get("idempotency_key") or "")
                    or f"web-child-{uuid.uuid4()}",
                    note=str(form.get("note") or "") or None,
                    parent_id=form.get("parent_id"),
                    depth=form.get("depth"),
                ),
            )
        except ChildProvisionError as exc:
            return RedirectResponse(f"/resellers?err={_q(exc.message)}", status_code=303)
        except Exception:
            return RedirectResponse(
                f"/resellers?err={_q('ساخت نماینده ناموفق بود')}",
                status_code=303,
            )
        return RedirectResponse(
            f"/resellers?ok={_q('نماینده ساخته شد')}",
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
        from app.services.authz import resolve_shop_permissions_from_profile

        perms = list(resolve_shop_permissions_from_profile(profile) or [])
        from app.services.billing import (
            is_payg,
            list_billing_transactions,
            rate_context_for_profile,
            resolve_price_per_gb,
        )
        from app.services.formatting import format_toman
        import secrets

        billing_txs = []
        billing_rate = 0
        pg_limits = None
        min_unsuspend = 0
        wallet_balance = int(user.wallet_balance or 0)
        wallet_txs = []
        if is_payg(profile):
            from app.services.billing import ensure_payg_shop_wallet, list_billing_transactions
            from app.services.billing_suspend import restore_payg_reseller
            from app.services.bot_user_admin import list_wallet_txs

            was_linked = bool(getattr(profile, "payg_wallet_linked", False))
            _u, wallet_balance = await ensure_payg_shop_wallet(session, profile)
            await session.refresh(user)
            wallet_balance = int(user.wallet_balance or 0)
            # First-link only: lift legacy suspend caused by empty billing pot
            if (
                (not was_linked)
                and wallet_balance > 0
                and profile.billing_suspended_at is not None
            ):
                try:
                    await restore_payg_reseller(session, profile, commit=True)
                except Exception:
                    logging.getLogger(__name__).debug(
                        "edit auto-restore failed", exc_info=True
                    )
            else:
                await session.commit()
            billing_txs = await list_billing_transactions(session, int(user_id), limit=15)
            wallet_txs = await list_wallet_txs(session, int(user_id), limit=15)
            billing_rate = await resolve_price_per_gb(session, rate_context_for_profile(profile))
            if profile.billing_suspended_at is not None:
                from app.services.billing_suspend import min_topup_to_unsuspend

                min_unsuspend = await min_topup_to_unsuspend(session)
        if profile.pg_admin_username:
            from app.services.pg_overview import build_reseller_pg_overview

            # Owner session + target reseller PG username — use platform client.
            staff_view = dict(staff)
            staff_view["pg_admin_username"] = profile.pg_admin_username
            staff_view["pg_role_id"] = profile.pg_role_id
            try:
                ov = await build_reseller_pg_overview(staff_view, session=session)
                if ov.get("ready"):
                    pg_limits = ov
            except Exception:
                pg_limits = None
        pg_subscription = None
        if profile.pg_admin_username:
            from app.services.formatting import format_expire_short
            from app.services.pg_admin_subscription import get_subscription

            sub = await get_subscription(session, profile.pg_admin_username)
            if sub is not None:
                extra = int(sub.extra_gb_purchased or 0)
                base = int(sub.base_gb or 0)
                exp = sub.expires_at
                pg_subscription = {
                    "expires_at": format_expire_short(exp) if exp else None,
                    "extra_gb": extra,
                    "base_gb": base,
                    "total_gb": base + extra,
                }
        from app.services.color_tags import color_tags_for_ui

        ctx = {
            "staff": staff,
            "user": user,
            "profile": profile,
            "feature_perms": FEATURE_PERMS,
            "selected_perms": perms,
            "pg_roles": roles,
            "is_payg": is_payg(profile),
            "billing_txs": billing_txs,
            "billing_rate": billing_rate,
            "wallet_balance": wallet_balance,
            "wallet_txs": wallet_txs,
            "pg_limits": pg_limits,
            "pg_subscription": pg_subscription,
            "min_unsuspend": min_unsuspend,
            "format_toman": format_toman,
            "topup_nonce": secrets.token_hex(8),
            "flash_ok": request.query_params.get("ok"),
            "flash_err": request.query_params.get("err"),
            "color_tags": color_tags_for_ui(),
        }
        if request.query_params.get("fragment") == "1":
            return render(request, "_reseller_edit_body.html", ctx)
        return render(request, "reseller_edit.html", ctx)

    async def _load_scoped_reseller(
        session: AsyncSession, staff: dict, user_id: int
    ):
        from app.services.platform_identity import is_explicit_owner_staff
        from app.services.representative_unification import descendant_shop_profile_ids

        user = await session.get(BotUser, int(user_id))
        profile = (
            await session.execute(
                select(ResellerProfile).where(ResellerProfile.user_id == int(user_id))
            )
        ).scalar_one_or_none()
        if not user or not profile:
            return None, None, "missing"
        if not is_explicit_owner_staff(staff):
            allowed = await descendant_shop_profile_ids(session, staff)
            if int(profile.id) not in allowed:
                return None, None, "forbidden"
        return user, profile, "ok"

    @app.get("/resellers/{user_id}/color-tag", response_class=HTMLResponse)
    async def reseller_color_tag_page(
        user_id: int,
        request: Request,
        staff: dict = Depends(staff_dep),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.color_tags import color_tags_for_ui
        from app.services.representative_unification import (
            RepresentativeUnifyError,
            assert_live_parent_for_child,
        )
        from fastapi import HTTPException

        # Same live-parent gate as GET /resellers (no weaker color-tag bypass).
        try:
            await assert_live_parent_for_child(session, staff)
        except RepresentativeUnifyError:
            raise HTTPException(status_code=403, detail="forbidden")

        user, _profile, status = await _load_scoped_reseller(session, staff, user_id)
        if status == "forbidden":
            raise HTTPException(status_code=403, detail="forbidden")
        if status != "ok" or user is None:
            return RedirectResponse(f"/resellers?err={_q('نماینده یافت نشد')}", status_code=303)
        ctx = {
            "staff": staff,
            "user": user,
            "color_tags": color_tags_for_ui(),
            "flash_ok": request.query_params.get("ok"),
            "flash_err": request.query_params.get("err"),
        }
        if request.query_params.get("fragment") == "1":
            return render(request, "_reseller_color_tag_body.html", ctx)
        return render(request, "_reseller_color_tag_body.html", ctx)

    @app.post("/resellers/{user_id}/color-tag")
    async def reseller_color_tag_save(
        user_id: int,
        request: Request,
        staff: dict = Depends(staff_dep),
        session: AsyncSession = Depends(get_db),
    ):
        """Set color tag on a reseller BotUser within actor scope."""
        from app.services.color_tags import apply_color_tag
        from app.services.platform_identity import is_explicit_owner_staff
        from app.services.representative_unification import (
            RepresentativeUnifyError,
            assert_live_parent_for_child,
        )
        from fastapi import HTTPException

        # Same live-parent gate as GET /resellers (no weaker color-tag bypass).
        try:
            await assert_live_parent_for_child(session, staff)
        except RepresentativeUnifyError:
            raise HTTPException(status_code=403, detail="forbidden")

        form = await request.form()
        user, _profile, status = await _load_scoped_reseller(session, staff, user_id)
        if status == "forbidden":
            return RedirectResponse("/home", status_code=303)
        if status != "ok" or user is None:
            return RedirectResponse(f"/resellers?err={_q('نماینده یافت نشد')}", status_code=303)
        apply_color_tag(user, str(form.get("color_tag") or ""))
        await session.commit()
        referer = str(request.headers.get("referer") or "")
        if is_explicit_owner_staff(staff) and "/edit" in referer:
            return _redirect_reseller_edit(user_id, ok="تگ ریسک ذخیره شد")
        return RedirectResponse(
            f"/resellers?ok={_q('تگ ریسک ذخیره شد')}",
            status_code=303,
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
        new_pg_role_id = int(pg_role_raw) if pg_role_raw.isdigit() else None
        profile.pg_role_id = new_pg_role_id
        pg_user = str(form.get("pg_admin_username") or "").strip()
        if pg_user:
            from app.services.pg_staff_access import conflict_message_for_reseller_link

            link_err = await conflict_message_for_reseller_link(
                session, pg_user, exclude_profile_id=int(profile.id)
            )
            if link_err:
                return _redirect_reseller_edit(user_id, err=link_err)
        profile.pg_admin_username = pg_user or None
        # Push role to PasarGuard so edit is not local-only (limited roles depend on live role).
        if profile.pg_admin_username and new_pg_role_id is not None:
            from app.services.pasarguard import PasarGuardError, get_pg

            try:
                await get_pg().modify_admin(
                    profile.pg_admin_username,
                    {"role_id": int(new_pg_role_id)},
                )
            except PasarGuardError as e:
                return _redirect_reseller_edit(user_id, err=e.user_message(fallback=str(e)))
            except Exception as e:
                return _redirect_reseller_edit(user_id, err=f'همگام‌سازی نقش پاسارگارد ناموفق: {e}')
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
                return _redirect_reseller_edit(user_id, err=perr)
            try:
                await apply_reseller_panel_password(session, profile, new_pass, sync_pg=True)
            except ValueError as e:
                return _redirect_reseller_edit(user_id, err=str(e))
            if profile.web_username and not profile.setup_completed_at:
                profile.setup_completed_at = datetime.now(timezone.utc)
        user.role = Role.RESELLER.value if profile.is_active else Role.USER.value
        await session.commit()
        return _redirect_reseller_edit(user_id, ok='ذخیره شد')

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
            return _redirect_reseller_edit(user_id, err='مبلغ نامعتبر است')
        if amount < 1000:
            return _redirect_reseller_edit(user_id, err='حداقل شارژ ۱۰۰۰ تومان است')
        from app.services.billing import credit_topup

        actor = str(staff.get("username") or staff.get("role") or "admin")
        nonce = str(form.get("nonce") or "").strip()
        if len(nonce) < 8:
            return _redirect_reseller_edit(user_id, err='فرم شارژ منقضی شده — صفحه را تازه کنید')
        try:
            await credit_topup(
                session,
                int(user_id),
                amount,
                created_by=actor,
                note=note or "شارژ دستی ادمین",
                idempotency_key=f"manual:{user_id}:{amount}:{note}:{actor}:{nonce}",
            )
        except ValueError as e:
            return _redirect_reseller_edit(user_id, err=str(e))
        return _redirect_reseller_edit(user_id, ok=f'شارژ کیف پول به مبلغ {amount:,} تومان ثبت شد')

    @app.post("/resellers/{user_id}/subscription/adjust")
    async def reseller_subscription_adjust(
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
        form = await request.form()
        days_raw = str(form.get("extra_days") or "0").strip() or "0"
        gb_raw = str(form.get("extra_gb") or "0").strip() or "0"
        try:
            from app.services.numbers import parse_int

            extra_days = parse_int(days_raw)
            extra_gb = parse_int(gb_raw)
        except ValueError:
            return _redirect_reseller_edit(user_id, err="مقادیر تغییر ظرفیت نامعتبر است")
        from app.services.reseller_capacity import admin_adjust_reseller_subscription

        try:
            await admin_adjust_reseller_subscription(
                session, profile, extra_days=extra_days, extra_gb=extra_gb
            )
        except ValueError as e:
            return _redirect_reseller_edit(user_id, err=str(e))
        except Exception as e:
            return _redirect_reseller_edit(user_id, err=f"تغییر ظرفیت ناموفق: {e}")
        return _redirect_reseller_edit(user_id, ok="ظرفیت اشتراک به‌روز شد")

    @app.post("/resellers/{user_id}/delete")
    async def reseller_delete(
        user_id: int,
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        form = await request.form()
        from app.services.delete_reason import delete_reason_too_short, extract_delete_reason

        reason = extract_delete_reason(form)
        if delete_reason_too_short(reason):
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
            from app.services.users import friendly_user_delete_error

            return RedirectResponse(
                f"/resellers?err={_q(friendly_user_delete_error(e))}", status_code=303
            )
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
        pg_note = ""
        if info.get("pg_admin_username"):
            if info.get("pg_admin_deleted"):
                pg_note = f" — ادمین پاسارگارد «{info['pg_admin_username']}» هم حذف شد"
            else:
                pg_note = (
                    f" — ادمین پاسارگارد «{info['pg_admin_username']}» حذف نشد "
                    "(ممکن است از قبل نبوده باشد)"
                )
        return RedirectResponse(
            f"/resellers?ok={_q(f'نمایندگی {label} حذف شد{note}{pg_note}')}",
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
        # Reason is optional for role demotion; required only on explicit delete endpoints.

        if role == Role.USER.value:
            try:
                info = await revoke_reseller(
                    session, user_id, delete_pg_admin=True, reason=reason
                )
            except ValueError as e:
                return RedirectResponse(f"/resellers?err={_q(str(e))}", status_code=303)
            except Exception as e:
                from app.services.users import friendly_user_delete_error

                return RedirectResponse(
                    f"/resellers?err={_q(friendly_user_delete_error(e))}", status_code=303
                )
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
            profile.is_active = False
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
                f"/resellers?ok={_q('نقش به مدیر تغییر کرد (پروفایل نماینده غیرفعال شد)')}",
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
        return RedirectResponse(
            f"/resellers?edit={int(user_id)}&ok={_q('نقش نماینده فعال شد')}",
            status_code=303,
        )

    @app.post("/resellers/{user_id}/delete-user")
    async def reseller_delete_user(
        user_id: int,
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        """Full bot-user delete from resellers tab (same cascade as /users delete)."""
        form = await request.form()
        from app.services.delete_reason import delete_reason_too_short, extract_delete_reason

        reason = extract_delete_reason(form)
        if delete_reason_too_short(reason):
            return RedirectResponse(
                f"/resellers?err={_q('علت حذف کاربر الزامی است')}",
                status_code=303,
            )
        from app.services.users import delete_bot_user, friendly_user_delete_error
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
            return RedirectResponse(
                f"/resellers?err={_q(friendly_user_delete_error(e))}", status_code=303
            )
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
    ):
        # Unified under /plans (audience=resellers section)
        return RedirectResponse("/plans#reseller-plans", status_code=303)

    @app.post("/resellers/plans")
    async def reseller_plan_create(
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        form = await request.form()
        from app.services.button_styles import parse_plan_button_style_form

        button_style = parse_plan_button_style_form(form)
        name = str(form.get("name") or "").strip()
        if not name:
            return RedirectResponse(f"/plans?err={_q('نام الزامی است')}#reseller-plans", status_code=303)
        try:
            price = int(str(form.get("price") or "0").replace(",", "").replace("٬", ""))
        except ValueError:
            price = 0
        plan_kind = _parse_plan_kind(form)
        try:
            sort_order = int(str(form.get("sort_order") or "0") or "0")
        except ValueError:
            sort_order = 0

        # Addon packs: capacity-only — no PG groups/role/perms/admin/share/extras.
        if _is_addon_plan_kind(plan_kind):
            try:
                addon_gb, addon_users = _addon_fields_from_form(form, plan_kind)
                await _validate_reseller_capacity_inputs(
                    staff, addon_gb=addon_gb, addon_users=addon_users, session=session
                )
            except ValueError as e:
                return RedirectResponse(
                    f"/plans?err={_q(str(e))}#reseller-plans", status_code=303
                )
            if max(0, price) <= 0:
                return RedirectResponse(
                    f"/plans?err={_q('قیمت بسته باید بیشتر از صفر باشد')}#reseller-plans",
                    status_code=303,
                )
            plan = ResellerPlan(
                name=name,
                description=str(form.get("description") or "").strip() or None,
                price=max(0, price),
                billing_mode="fixed",
                price_per_gb=0,
                pg_group_ids=None,
                plan_kind=plan_kind,
                duration_days=0,
                included_gb=0,
                included_users=0,
                addon_gb=addon_gb,
                addon_users=addon_users,
                renew_pricing_mode="fixed",
                allow_buy_extra=False,
                extra_gb_price=0,
                extra_user_price=0,
                renew_price=0,
                can_approve_receipts=False,
                web_permissions="",
                bot_permissions="",
                create_pg_admin=False,
                create_web_access=False,
                share_pg_panel_url=False,
                pg_role_id=None,
                button_style=button_style,
                category_id=None,
                is_active=bool(form.get("is_active", "1")),
                sort_order=sort_order,
            )
            session.add(plan)
            await session.flush()
            from app.services.billing import sync_plan_billing_rate

            await sync_plan_billing_rate(session, plan)
            await session.commit()
            return RedirectResponse(
                f"/plans?ok={_q('بسته اضافه ذخیره شد')}#reseller-plans-addons",
                status_code=303,
            )

        billing_mode = str(form.get("billing_mode") or "fixed").strip().lower()
        if billing_mode not in {"fixed", "payg"}:
            billing_mode = "fixed"
        price_per_gb = _parse_price_per_gb(form) if billing_mode == "payg" else 0
        pg_group_ids = _parse_pg_group_ids(form)
        if not pg_group_ids:
            return RedirectResponse(
                f"/plans?err={_q('حداقل یک گروه پاسارگارد الزامی است')}#reseller-plans",
                status_code=303,
            )
        perms = _feature_perms_from_form(form)
        pg_role_raw = str(form.get("pg_role_id") or "").strip()
        if not pg_role_raw.isdigit():
            return RedirectResponse(
                f"/plans?err={_q('نقش پاسارگارد الزامی است')}#reseller-plans",
                status_code=303,
            )
        try:
            await _validate_reseller_capacity_inputs(
                staff,
                included_gb=_parse_nonneg_int(form, "included_gb"),
                included_users=_parse_nonneg_int(form, "included_users"),
                session=session,
            )
        except ValueError as e:
            return RedirectResponse(
                f"/plans?err={_q(str(e))}#reseller-plans",
                status_code=303,
            )
        category_id = None
        try:
            from app.services.plan_categories import (
                AUDIENCE_RESELLERS,
                resolve_category_for_plan_write,
            )
            from app.services.shop_scope import ShopScopeError

            category_id = await resolve_category_for_plan_write(
                session,
                staff,
                form.get("category_id"),
                expected_audience=AUDIENCE_RESELLERS,
            )
        except (ShopScopeError, ValueError) as e:
            return RedirectResponse(
                f"/plans?err={_q(str(e))}#reseller-plans",
                status_code=303,
            )
        plan = ResellerPlan(
            name=name,
            description=str(form.get("description") or "").strip() or None,
            price=max(0, price),
            billing_mode=billing_mode,
            price_per_gb=price_per_gb,
            pg_group_ids=pg_group_ids,
            plan_kind="subscription",
            duration_days=_parse_nonneg_int(form, "duration_days"),
            included_gb=_parse_nonneg_int(form, "included_gb"),
            included_users=_parse_nonneg_int(form, "included_users"),
            addon_gb=0,
            addon_users=0,
            allow_buy_extra=bool(form.get("allow_buy_extra")) if billing_mode == "fixed" else False,
            extra_gb_price=_parse_nonneg_int(form, "extra_gb_price") if billing_mode == "fixed" else 0,
            extra_user_price=_parse_nonneg_int(form, "extra_user_price")
            if billing_mode == "fixed"
            else 0,
            renew_pricing_mode=_parse_renew_pricing_mode(
                form,
                allow_buy_extra=bool(form.get("allow_buy_extra")) if billing_mode == "fixed" else False,
            ),
            renew_price=_parse_nonneg_int(form, "renew_price"),
            can_approve_receipts="payments" in parse_perms(perms),
            web_permissions=perms,
            bot_permissions=perms,
            create_pg_admin=bool(form.get("create_pg_admin")),
            create_web_access=True,
            share_pg_panel_url=bool(form.get("share_pg_panel_url")),
            pg_role_id=int(pg_role_raw) if pg_role_raw.isdigit() else None,
            button_style=button_style,
            category_id=category_id,
            is_active=bool(form.get("is_active", "1")),
            sort_order=sort_order,
        )
        session.add(plan)
        await session.flush()
        from app.services.billing import sync_plan_billing_rate

        await sync_plan_billing_rate(session, plan)
        await session.commit()
        return RedirectResponse(f"/plans?ok={_q('پلن نمایندگی ذخیره شد')}#reseller-plans", status_code=303)

    @app.get("/resellers/plans/{plan_id}/edit", response_class=HTMLResponse)
    async def reseller_plan_edit_page(
        plan_id: int,
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        plan = await session.get(ResellerPlan, plan_id)
        if not plan:
            return RedirectResponse(f"/plans?err={_q('یافت نشد')}#reseller-plans", status_code=303)
        roles = []
        try:
            roles = await get_pg().get_admin_roles()
        except Exception:
            roles = []
        groups = []
        try:
            groups = await get_pg().get_groups_simple()
            if not isinstance(groups, list):
                groups = []
            groups = [g for g in groups if isinstance(g, dict)]
        except Exception:
            groups = []
        plan_group_ids = {
            x.strip()
            for x in (plan.pg_group_ids or "").split(",")
            if x.strip()
        }
        snap = await load_staff_limit_snapshot(staff, session=session)
        from app.services.button_styles import PLAN_BUTTON_STYLE_OPTIONS
        from app.services.plan_categories import (
            AUDIENCE_RESELLERS,
            list_categories,
        )

        plan_categories = await list_categories(
            session, staff, audience=AUDIENCE_RESELLERS
        )

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
                "pg_limit_snapshot": snap,
                "pg_limit_cards": limit_snapshot_cards(snap),
                "pg_roles": roles,
                "groups": groups,
                "plan_group_ids": plan_group_ids,
                "plan_style_options": PLAN_BUTTON_STYLE_OPTIONS,
                "plan_categories": plan_categories,
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
            return RedirectResponse(f"/plans?err={_q('یافت نشد')}#reseller-plans", status_code=303)
        form = await request.form()
        from app.services.button_styles import parse_plan_button_style_form

        button_style = parse_plan_button_style_form(form)
        name = str(form.get("name") or "").strip()
        if not name:
            return RedirectResponse(
                f"/resellers/plans/{plan_id}/edit?err={_q('نام الزامی است')}", status_code=303
            )
        plan.name = name
        plan.button_style = button_style
        plan.description = str(form.get("description") or "").strip() or None
        try:
            plan.price = max(0, int(str(form.get("price") or "0").replace(",", "").replace("٬", "")))
        except ValueError:
            pass
        try:
            plan.sort_order = int(str(form.get("sort_order") or "0") or "0")
        except ValueError:
            pass
        plan.is_active = bool(form.get("is_active"))
        plan_kind = _parse_plan_kind(form)

        if _is_addon_plan_kind(plan_kind):
            try:
                addon_gb, addon_users = _addon_fields_from_form(form, plan_kind)
                await _validate_reseller_capacity_inputs(
                    staff, addon_gb=addon_gb, addon_users=addon_users, session=session
                )
            except ValueError as e:
                return RedirectResponse(
                    f"/resellers/plans/{plan_id}/edit?err={_q(str(e))}", status_code=303
                )
            if int(plan.price or 0) <= 0:
                return RedirectResponse(
                    f"/resellers/plans/{plan_id}/edit?err={_q('قیمت بسته باید بیشتر از صفر باشد')}",
                    status_code=303,
                )
            # Strip subscription-only knobs — addons never provision admins/groups.
            plan.plan_kind = plan_kind
            plan.billing_mode = "fixed"
            plan.price_per_gb = 0
            plan.pg_group_ids = None
            plan.duration_days = 0
            plan.included_gb = 0
            plan.included_users = 0
            plan.addon_gb = addon_gb
            plan.addon_users = addon_users
            plan.renew_pricing_mode = "fixed"
            plan.allow_buy_extra = False
            plan.extra_gb_price = 0
            plan.extra_user_price = 0
            plan.renew_price = 0
            plan.can_approve_receipts = False
            plan.web_permissions = ""
            plan.bot_permissions = ""
            plan.create_pg_admin = False
            plan.create_web_access = False
            plan.share_pg_panel_url = False
            plan.pg_role_id = None
            from app.services.billing import sync_plan_billing_rate

            await sync_plan_billing_rate(session, plan)
            await session.commit()
            return RedirectResponse(
                f"/plans?ok={_q('بسته اضافه ذخیره شد')}#reseller-plans-addons",
                status_code=303,
            )

        billing_mode = str(form.get("billing_mode") or plan.billing_mode or "fixed").strip().lower()
        if billing_mode not in {"fixed", "payg"}:
            billing_mode = "fixed"
        plan.billing_mode = billing_mode
        pg_group_ids = _parse_pg_group_ids(form)
        if not pg_group_ids:
            return RedirectResponse(
                f"/resellers/plans/{plan_id}/edit?err={_q('حداقل یک گروه پاسارگارد الزامی است')}",
                status_code=303,
            )
        try:
            await _validate_reseller_capacity_inputs(
                staff,
                included_gb=_parse_nonneg_int(form, "included_gb"),
                included_users=_parse_nonneg_int(form, "included_users"),
                session=session,
            )
        except ValueError as e:
            return RedirectResponse(
                f"/resellers/plans/{plan_id}/edit?err={_q(str(e))}",
                status_code=303,
            )
        if billing_mode == "payg":
            plan.price_per_gb = _parse_price_per_gb(form)
            plan.pg_group_ids = pg_group_ids
            plan.allow_buy_extra = False
            plan.extra_gb_price = 0
            plan.extra_user_price = 0
        else:
            plan.price_per_gb = 0
            plan.pg_group_ids = pg_group_ids
            plan.allow_buy_extra = bool(form.get("allow_buy_extra"))
            plan.extra_gb_price = _parse_nonneg_int(form, "extra_gb_price")
            plan.extra_user_price = _parse_nonneg_int(form, "extra_user_price")
        perms = _feature_perms_from_form(form)
        plan.web_permissions = perms
        plan.bot_permissions = perms
        plan.can_approve_receipts = "payments" in parse_perms(perms)
        plan.create_pg_admin = bool(form.get("create_pg_admin"))
        plan.share_pg_panel_url = bool(form.get("share_pg_panel_url"))
        plan.renew_price = _parse_nonneg_int(form, "renew_price")
        plan.plan_kind = "subscription"
        plan.duration_days = _parse_nonneg_int(form, "duration_days")
        plan.included_gb = _parse_nonneg_int(form, "included_gb")
        plan.included_users = _parse_nonneg_int(form, "included_users")
        plan.addon_gb = 0
        plan.addon_users = 0
        plan.renew_pricing_mode = _parse_renew_pricing_mode(
            form, allow_buy_extra=bool(plan.allow_buy_extra)
        )
        plan.create_web_access = True
        pg_role_raw = str(form.get("pg_role_id") or "").strip()
        if not pg_role_raw.isdigit():
            return RedirectResponse(
                f"/resellers/plans/{plan_id}/edit?err={_q('نقش پاسارگارد الزامی است')}",
                status_code=303,
            )
        plan.pg_role_id = int(pg_role_raw)
        try:
            from app.services.plan_categories import (
                AUDIENCE_RESELLERS,
                resolve_category_for_plan_write,
            )
            from app.services.shop_scope import ShopScopeError

            plan.category_id = await resolve_category_for_plan_write(
                session,
                staff,
                form.get("category_id"),
                expected_audience=AUDIENCE_RESELLERS,
                allow_inactive_id=plan.category_id,
            )
        except (ShopScopeError, ValueError) as e:
            return RedirectResponse(
                f"/resellers/plans/{plan_id}/edit?err={_q(str(e))}",
                status_code=303,
            )
        from app.services.billing import sync_plan_billing_rate

        await sync_plan_billing_rate(session, plan)
        await session.commit()
        return RedirectResponse(
            f"/plans?ok={_q('پلن نمایندگی ذخیره شد')}#reseller-plans", status_code=303
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
            from app.services.billing import sync_plan_billing_rate

            await sync_plan_billing_rate(session, plan)
            await session.commit()
        return RedirectResponse("/plans#reseller-plans", status_code=303)

    @app.post("/resellers/plans/{plan_id}/delete")
    async def reseller_plan_delete(
        plan_id: int,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.plans_catalog import PlanDeleteBlocked, delete_reseller_plan

        plan = await session.get(ResellerPlan, plan_id)
        if not plan:
            return RedirectResponse("/plans#reseller-plans", status_code=303)
        try:
            await delete_reseller_plan(session, plan)
            await session.commit()
        except PlanDeleteBlocked as e:
            try:
                await session.rollback()
            except Exception:
                pass
            return RedirectResponse(
                f"/plans?err={_q(e.message)}#reseller-plans", status_code=303
            )
        return RedirectResponse(f"/plans?ok={_q('حذف شد')}#reseller-plans", status_code=303)

    # ---- applications ----
    @app.get("/resellers/applications", response_class=HTMLResponse)
    async def reseller_apps_page(
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        _require_rep_mgmt(staff)
        from app.services.resellers import release_stale_pending_payment_apps

        released = await release_stale_pending_payment_apps(session)
        if released:
            await session.commit()
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
        _require_rep_mgmt(staff)
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
        _require_rep_mgmt(staff)
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
