from __future__ import annotations

"""PasarGuard manager pages — admin + reseller (permission-gated)."""

import asyncio
from urllib.parse import quote

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.formatting import data_limit_to_gb, expire_remaining_days, format_stat_row
from app.services.pasarguard import (
    PasarGuardError,
    as_list,
    build_user_create_payload,
    build_user_modify_payload,
    get_pg,
    get_pg_for_staff,
    user_group_ids,
    user_subscription_url,
)
from app.services.pg_access import staff_pg_action, staff_pg_writes, staff_user_actions
from app.services.pg_quota import (
    PgQuotaError,
    assert_can_mutate_owned_users,
)
from app.services.provision_gate import (
    ProvisionError,
    assert_provision_create,
    assert_provision_modify,
)


def _q(msg: str) -> str:
    return quote(str(msg), safe="")


async def _staff_pg(session: AsyncSession, staff: dict):
    """Return (client, as_owner). Shop staff always use their PG admin credentials."""
    return await get_pg_for_staff(session, staff)


async def _list_pg(session: AsyncSession, staff: dict):
    """PG client for inventory lists — Owner only for platform admin.

    Non-admin staff use their own credentials so PasarGuard scopes the view.
    Returns ``None`` when staff credentials are missing (fail closed → empty UI).
    """
    if _is_admin(staff):
        return get_pg()
    try:
        pg, _as_owner = await _staff_pg(session, staff)
        return pg
    except PasarGuardError:
        return None


def _pg_form_err(msg: str, *, modal: str, uid: str | int | None = None) -> RedirectResponse:
    url = f"/pg/users?form_err={_q(msg)}&modal={_q(modal)}"
    if uid is not None and str(uid).strip():
        url += f"&uid={_q(str(uid))}"
    return RedirectResponse(url, status_code=303)


def _pg_err(exc: Exception) -> str:
    if isinstance(exc, PasarGuardError):
        return _q(exc.user_message(fallback="خطا در ارتباط با پاسارگارد"))
    return _q(exc)


def _inbound_tags(raw) -> list[str]:
    tags = []
    for item in raw or []:
        if isinstance(item, str):
            tags.append(item)
        elif isinstance(item, dict):
            tags.append(str(item.get("tag") or item.get("name") or item.get("id") or ""))
    return [t for t in tags if t]


def _addr_set(raw: str) -> list[str]:
    return [p.strip() for p in (raw or "").replace("؛", ",").split(",") if p.strip()]


def _is_admin(staff: dict) -> bool:
    return staff.get("role") == "admin"


def _pg_owner(staff: dict) -> str:
    return str(staff.get("pg_admin_username") or "").strip()


def _owner_of(user: dict) -> str:
    admin = user.get("admin") or user.get("owner_username") or ""
    if isinstance(admin, dict):
        admin = admin.get("username") or ""
    return str(admin or "").strip().lower()


def _filter_owned_users(users: list[dict], staff: dict) -> list[dict]:
    if _is_admin(staff):
        return users
    mine = _pg_owner(staff).lower()
    if not mine:
        return []
    return [u for u in users if isinstance(u, dict) and _owner_of(u) == mine]


def _filter_templates(items: list[dict], staff: dict) -> list[dict]:
    from app.services.plans_catalog import filter_templates_for_staff

    return filter_templates_for_staff(items, staff)


def _filter_groups(items: list[dict], staff: dict) -> list[dict]:
    from app.services.plans_catalog import filter_groups_for_staff

    return filter_groups_for_staff(items, staff)


async def _assert_owned_user(
    staff: dict,
    user_id: int,
    session: AsyncSession | None = None,
) -> dict | None:
    """Fetch user via the staff's own PG client when possible (never owner for resellers)."""
    if _is_admin(staff) or session is None:
        info = await get_pg().get_user_by_id(user_id)
    else:
        try:
            pg, _as_owner = await _staff_pg(session, staff)
            info = await pg.get_user_by_id(user_id)
        except PasarGuardError:
            return None
    if not isinstance(info, dict):
        return None
    if _is_admin(staff):
        return info
    mine = _pg_owner(staff).lower()
    if not mine or _owner_of(info) != mine:
        return None
    return info


def _pg_ctx(staff: dict, **extra) -> dict:
    writes = staff_pg_writes(staff)
    actions = staff_user_actions(staff)
    pg_perms = list(staff.get("pg_permissions") or [])
    if _is_admin(staff):
        pg_perms = [
            "pg_overview",
            "pg_users",
            "pg_templates",
            "pg_groups",
            "pg_hosts",
            "pg_inbounds",
            "pg_nodes",
            "pg_admins",
        ]
    ctx = {
        "staff": staff,
        "is_admin": _is_admin(staff),
        "pg_perms": pg_perms,
        "pg_writes": writes,
        "pg_user_actions": actions,
        "pg_access": staff.get("pg_access") or {},
    }
    ctx.update(extra)
    return ctx


def register_pg_pages(
    app,
    *,
    render,
    require_admin,
    require_pg_perm,
    get_db,
    require_staff=None,
):
    staff_dep = require_staff or require_admin

    @app.get("/pg/credentials-required", response_class=HTMLResponse)
    async def pg_credentials_required(
        request: Request,
        staff: dict = Depends(staff_dep),
    ):
        from app.services.pg_credentials import PG_CREDENTIAL_MISSING_MSG

        err = request.query_params.get("err") or PG_CREDENTIAL_MISSING_MSG
        return render(
            request,
            "pg_credentials_required.html",
            _pg_ctx(
                staff,
                flash_err=err,
                active="pg",
                pg_client_ready=bool(staff.get("pg_client_ready")),
            ),
        )

    @app.get("/pg", response_class=HTMLResponse)
    async def pg_home(
        request: Request,
        staff: dict = Depends(require_pg_perm("pg_overview")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.pg_overview import build_reseller_pg_overview, is_server_stat_key

        err = None
        stats_rows: list[tuple[str, str]] = []
        nodes = []
        counts = {"admins": 0, "groups": 0, "hosts": 0, "nodes": 0, "users": 0}
        reseller_overview = None
        try:
            if _is_admin(staff):
                pg = get_pg()
                raw, nodes, admins, groups, hosts = await asyncio.gather(
                    pg.get_system_stats(),
                    pg.get_nodes_simple(),
                    pg.get_admins_simple(),
                    pg.get_groups_simple(),
                    pg.get_hosts(),
                    return_exceptions=True,
                )
                if isinstance(raw, dict):
                    # Keys already shown in the merged overview counts — skip duplicates
                    _count_dup_keys = {
                        "total_user",
                        "total_users",
                        "users",
                        "users_total",
                        "total_admin",
                        "admins_total",
                        "total_admins",
                        "admins",
                        "total_node",
                        "nodes_total",
                        "total_nodes",
                        "nodes",
                        "total_group",
                        "groups_total",
                        "total_groups",
                        "groups",
                        "total_host",
                        "hosts_total",
                        "total_hosts",
                        "hosts",
                    }
                    for key, val in raw.items():
                        if isinstance(val, (dict, list)):
                            continue
                        k = str(key)
                        if is_server_stat_key(k):
                            continue
                        if k.lower() in _count_dup_keys:
                            continue
                        stats_rows.append(format_stat_row(k, val))
                    for key in ("total_user", "users_active", "users", "total_users"):
                        if key in raw and isinstance(raw[key], (int, float)):
                            counts["users"] = int(raw[key])
                            break
                elif isinstance(raw, Exception):
                    err = str(raw)
                nodes = nodes if isinstance(nodes, list) else []
                counts["nodes"] = len(nodes)
                counts["admins"] = len(admins) if isinstance(admins, list) else 0
                counts["groups"] = len(groups) if isinstance(groups, list) else 0
                counts["hosts"] = len(hosts) if isinstance(hosts, list) else 0
            else:
                # Reseller: only own users/usage/limits — never server/hardware stats
                reseller_overview = await build_reseller_pg_overview(staff, session=session)
                # Keep overview.error in template; don't blank the page via flash_err
        except Exception as e:
            err = str(e)
        ticket_alert = None
        if not _is_admin(staff) and staff.get("role") in {"reseller", "pg_staff"}:
            from app.api.panel_tickets_pages import panel_ticket_dashboard_alert

            ticket_alert = await panel_ticket_dashboard_alert(
                session,
                staff,
                unread=getattr(request.state, "panel_tickets_unread", None),
            )

        return render(
            request,
            "pg_home.html",
            _pg_ctx(
                staff,
                stats_rows=stats_rows,
                nodes=nodes if _is_admin(staff) else [],
                counts=counts,
                reseller_overview=reseller_overview,
                flash_err=err,
                active="pg",
                ticket_alert=ticket_alert,
            ),
        )

    # ---- VPN users ----
    @app.get("/pg/users", response_class=HTMLResponse)
    async def pg_users(
        request: Request,
        staff: dict = Depends(require_pg_perm("pg_users")),
        session: AsyncSession = Depends(get_db),
    ):
        err = request.query_params.get("err")
        ok = request.query_params.get("ok")
        form_err = request.query_params.get("form_err")
        form_modal = (request.query_params.get("modal") or "").strip()
        form_uid = (request.query_params.get("uid") or "").strip()
        q = (request.query_params.get("q") or "").strip()
        users: list[dict] = []
        templates: list[dict] = []
        groups: list[dict] = []
        access = staff.get("pg_access") or {}
        require_template = bool(access.get("require_template")) and not _is_admin(staff)
        try:
            pg = await _list_pg(session, staff)
            if pg is None:
                raise PasarGuardError(
                    "رمز پاسارگارد این ادمین ذخیره نشده — دسترسی وب را دوباره با رمز تنظیم کنید"
                )
            params: dict = {"offset": 0, "limit": 200}
            if q:
                params["username"] = q
            owner = _pg_owner(staff)
            if not _is_admin(staff) and owner:
                params["admin"] = owner
            data, templates_raw, groups_raw = await asyncio.gather(
                pg.get_users(**params),
                pg.get_user_templates(),
                pg.get_groups(),
                return_exceptions=True,
            )
            if isinstance(data, Exception):
                raise data
            if isinstance(data, dict):
                users = as_list(data, "users") or []
            elif isinstance(data, list):
                users = data
            users = _filter_owned_users(users, staff)
            for u in users:
                if not isinstance(u, dict):
                    continue
                u["_sub_url"] = user_subscription_url(u)
                u["_days_left"] = expire_remaining_days(u.get("expire") or u.get("expire_date"))
                u["_data_gb"] = data_limit_to_gb(u.get("data_limit"))
                u["_group_ids"] = user_group_ids(u)

            if isinstance(templates_raw, list):
                templates = templates_raw
            elif isinstance(templates_raw, dict):
                templates = as_list(templates_raw, "templates") or []
            elif not isinstance(templates_raw, Exception):
                templates = []
            templates = _filter_templates(templates, staff)

            if isinstance(groups_raw, list):
                groups = groups_raw
            elif isinstance(groups_raw, dict):
                groups = as_list(groups_raw, "groups") or []
            elif not isinstance(groups_raw, Exception):
                groups = []
            groups = _filter_groups(groups, staff)
        except Exception as e:
            err = str(e)
        actions = staff_user_actions(staff)
        can_custom = bool(actions["create"]) and (not require_template) and bool(groups)
        can_template = bool(actions["create"]) and bool(templates)
        return render(
            request,
            "pg_users.html",
            _pg_ctx(
                staff,
                users=users,
                templates=templates,
                groups=groups,
                q=q,
                flash_err=err,
                flash_ok=ok,
                form_err=form_err,
                form_modal=form_modal,
                form_uid=form_uid,
                can_create=actions["create"],
                can_custom_create=can_custom,
                can_template_create=can_template,
                require_template=require_template,
                can_modify=actions["update"],
                can_delete=actions["delete"],
                can_reset=actions["reset_usage"],
                can_revoke=actions["revoke_sub"],
                can_disable=actions["disable"],
                can_enable=actions["enable"],
                active="pg_users",
            ),
        )

    @app.post("/pg/users")
    async def pg_users_create(
        request: Request,
        staff: dict = Depends(require_pg_perm("pg_users")),
        session: AsyncSession = Depends(get_db),
    ):
        import time

        from app.services.pasarguard import parse_group_ids
        from app.services.plans_catalog import (
            groups_allowed_for_staff,
            parse_group_ids_from_form,
            template_allowed_for_staff,
        )

        actions = staff_user_actions(staff)
        if not actions["create"]:
            return RedirectResponse(f"/pg/users?err={_q('اجازه ساخت کاربر ندارید')}", status_code=303)

        form = await request.form()
        uname = str(form.get("username") or "").strip()
        if not uname:
            return _pg_form_err("نام کاربری الزامی است", modal="create")

        access = staff.get("pg_access") or {}
        require_template = bool(access.get("require_template")) and not _is_admin(staff)
        mode = str(form.get("mode") or "").strip().lower()
        if require_template:
            mode = "template"
        if mode not in {"template", "custom"}:
            # Back-compat: old forms only sent template_id
            mode = "template" if str(form.get("template_id") or "").strip() else "custom"

        note = f"web panel · {staff.get('username') or 'staff'}"
        import re

        if not re.fullmatch(r"[A-Za-z0-9_]{3,32}", uname):
            return _pg_form_err(
                "نام کاربری باید ۳ تا ۳۲ کاراکتر انگلیسی، عدد یا _ باشد (بدون فاصله و فارسی)",
                modal="create",
            )
        # Non-admin must have a PG admin identity — otherwise create-as-owner
        # bypasses every role quota (max_users / volume / expire).
        if not _is_admin(staff) and not _pg_owner(staff):
            return _pg_form_err(
                "ادمین پاسارگارد برای این حساب تنظیم نشده است",
                modal="create",
            )
        try:
            if mode == "template":
                raw_tid = str(form.get("template_id") or form.get("pg_template_id") or "").strip()
                if not raw_tid.isdigit():
                    return _pg_form_err("تمپلیت نامعتبر", modal="create")
                tid = int(raw_tid)
                if not template_allowed_for_staff(staff, tid):
                    return _pg_form_err("این تمپلیت مجاز نیست", modal="create")
                # Resolve template limits before quota check (blocks oversized bypass).
                tpl_data_limit = None
                tpl_expire_ts = None
                try:
                    pg_peek, _ = await _staff_pg(session, staff)
                    tpl = await pg_peek.get_user_template(tid)
                    if isinstance(tpl, dict):
                        raw_dl = tpl.get("data_limit")
                        if raw_dl is not None and int(raw_dl or 0) > 0:
                            tpl_data_limit = int(raw_dl)
                        raw_exp = tpl.get("expire_duration")
                        if raw_exp is not None and int(raw_exp or 0) > 0:
                            import time as _time

                            tpl_expire_ts = int(_time.time()) + int(raw_exp)
                except Exception:
                    tpl_data_limit = None
                    tpl_expire_ts = None
                try:
                    await assert_provision_create(
                        session,
                        staff=staff,
                        data_limit=tpl_data_limit,
                        expire_ts=tpl_expire_ts,
                        from_template=True,
                    )
                except (ProvisionError, PgQuotaError) as qe:
                    return _pg_form_err(getattr(qe, "message", str(qe)), modal="create")
                pg, as_owner = await _staff_pg(session, staff)
                created = await pg.create_user_from_template(
                    {
                        "username": uname,
                        "user_template_id": tid,
                        "note": note,
                    }
                )
            else:
                if require_template:
                    return _pg_form_err(
                        "نقش شما فقط ساخت از تمپلیت را مجاز می‌داند",
                        modal="create",
                    )
                ids = parse_group_ids_from_form(form)
                if not ids:
                    # also accept comma field if present
                    ids = parse_group_ids(str(form.get("group_ids") or ""))
                if not ids:
                    return _pg_form_err("حداقل یک گروه انتخاب کنید", modal="create")
                if not groups_allowed_for_staff(staff, ids):
                    return _pg_form_err("یکی از گروه‌های انتخاب‌شده مجاز نیست", modal="create")

                gb_raw = str(form.get("data_limit_gb") or "").strip()
                days_raw = str(form.get("duration_days") or "").strip()
                data_limit = None
                expire_ts = None
                if gb_raw:
                    try:
                        gb = float(gb_raw.replace(",", "."))
                        if gb < 0:
                            raise ValueError
                        if gb > 0:
                            data_limit = int(gb * (1024**3))
                    except ValueError:
                        return _pg_form_err("حجم نامعتبر است", modal="create")
                if days_raw:
                    try:
                        days = int(float(days_raw))
                        if days < 0:
                            raise ValueError
                        if days > 0:
                            expire_ts = int(time.time()) + days * 86400
                    except ValueError:
                        return _pg_form_err("مدت نامعتبر است", modal="create")

                try:
                    await assert_provision_create(
                        session,
                        staff=staff,
                        data_limit=data_limit,
                        expire_ts=expire_ts,
                        from_template=False,
                    )
                except (ProvisionError, PgQuotaError) as qe:
                    return _pg_form_err(getattr(qe, "message", str(qe)), modal="create")

                pg, as_owner = await _staff_pg(session, staff)
                created = await pg.create_user(
                    build_user_create_payload(
                        username=uname,
                        group_ids=ids,
                        data_limit=data_limit,
                        expire_ts=expire_ts,
                        note=note,
                    )
                )

            if not as_owner:
                # Created as the shop PG admin — already owned; no owner-token transfer.
                pass
            elif not _is_admin(staff):
                owner = _pg_owner(staff)
                uid = created.get("id") if isinstance(created, dict) else None
                if not owner:
                    if uid:
                        try:
                            await get_pg().delete_user_by_id(int(uid))
                        except Exception:
                            pass
                    return _pg_form_err(
                        "ادمین پاسارگارد برای این حساب تنظیم نشده است",
                        modal="create",
                    )
                if not uid:
                    return _pg_form_err(
                        "کاربر ساخته شد ولی شناسه برگشت داده نشد — مالکیت قابل تنظیم نیست",
                        modal="create",
                    )
                try:
                    await get_pg().set_owner_by_id(int(uid), owner)
                except Exception as e:
                    try:
                        await get_pg().delete_user_by_id(int(uid))
                    except Exception:
                        pass
                    msg = (
                        e.user_message(fallback="خطا در تخصیص مالکیت کاربر")
                        if isinstance(e, PasarGuardError)
                        else str(e)
                    )
                    return _pg_form_err(
                        f"کاربر ساخته شد ولی مالکیت ست نشد و حذف شد: {msg}",
                        modal="create",
                    )
        except Exception as e:
            msg = e.user_message(fallback="خطا در ساخت کاربر") if isinstance(e, PasarGuardError) else str(e)
            return _pg_form_err(msg, modal="create")
        return RedirectResponse(f"/pg/users?ok={_q(f'کاربر {uname} ساخته شد')}", status_code=303)

    @app.get("/pg/users/{user_id}/link")
    async def pg_users_link(
        user_id: int,
        staff: dict = Depends(require_pg_perm("pg_users")),
        session: AsyncSession = Depends(get_db),
    ):
        from fastapi.responses import JSONResponse

        try:
            info = await _assert_owned_user(staff, user_id, session)
            if info is None:
                return JSONResponse({"ok": False, "error": "دسترسی ندارید"}, status_code=403)
            url = user_subscription_url(info)
            if not url:
                return JSONResponse({"ok": False, "error": "لینک اشتراک برای این کاربر یافت نشد"}, status_code=404)
            return JSONResponse(
                {
                    "ok": True,
                    "url": url,
                    "username": info.get("username") or "",
                }
            )
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)[:300]}, status_code=502)

    @app.post("/pg/users/{user_id}/edit")
    async def pg_users_edit(
        user_id: int,
        request: Request,
        staff: dict = Depends(require_pg_perm("pg_users")),
        session: AsyncSession = Depends(get_db),
    ):
        import time

        from app.services.plans_catalog import groups_allowed_for_staff, parse_group_ids_from_form

        if not staff_user_actions(staff)["update"]:
            return RedirectResponse(f"/pg/users?err={_q('اجازه ویرایش ندارید')}", status_code=303)
        form = await request.form()
        ids = parse_group_ids_from_form(form, prefix="edit_group_")
        if not ids:
            return _pg_form_err("حداقل یک گروه انتخاب کنید", modal="edit", uid=user_id)
        if not groups_allowed_for_staff(staff, ids):
            return _pg_form_err("یکی از گروه‌های انتخاب‌شده مجاز نیست", modal="edit", uid=user_id)

        gb_raw = str(form.get("data_limit_gb") or "").strip()
        days_raw = str(form.get("duration_days") or "").strip()
        data_limit = 0  # 0 = unlimited
        expire_ts = 0  # clear expire when empty
        if gb_raw:
            try:
                gb = float(gb_raw.replace(",", "."))
                if gb < 0:
                    raise ValueError
                data_limit = int(gb * (1024**3)) if gb > 0 else 0
            except ValueError:
                return _pg_form_err("حجم نامعتبر است", modal="edit", uid=user_id)
        if days_raw:
            try:
                days = int(float(days_raw))
                if days < 0:
                    raise ValueError
                expire_ts = int(time.time()) + days * 86400 if days > 0 else 0
            except ValueError:
                return _pg_form_err("مدت نامعتبر است", modal="edit", uid=user_id)

        try:
            current = await _assert_owned_user(staff, user_id, session)
            if current is None:
                return RedirectResponse(f"/pg/users?err={_q('دسترسی ندارید')}", status_code=303)
            try:
                await assert_provision_modify(
                    session,
                    staff,
                    data_limit=data_limit,
                    expire_ts=expire_ts,
                    data_limit_changed=True,
                    expire_changed=True,
                )
            except (ProvisionError, PgQuotaError) as qe:
                return _pg_form_err(getattr(qe, "message", str(qe)), modal="edit", uid=user_id)
            # PasarGuard does not allow changing username after create
            payload = build_user_modify_payload(
                username=None,
                group_ids=ids,
                data_limit=data_limit,
                expire_ts=expire_ts,
                status=str(current.get("status") or "") or None,
            )
            # Never mutate via owner token for shop staff — use their PG admin client.
            pg, _as_owner = await _staff_pg(session, staff)
            await pg.modify_user_by_id(user_id, payload)
            uname = str(current.get("username") or user_id)
        except Exception as e:
            msg = e.user_message(fallback="خطا در ویرایش") if isinstance(e, PasarGuardError) else str(e)
            return _pg_form_err(msg, modal="edit", uid=user_id)
        return RedirectResponse(f"/pg/users?ok={_q(f'کاربر {uname} ویرایش شد')}", status_code=303)

    async def _guard_owned_mutation(
        staff: dict, user_id: int, session: AsyncSession
    ) -> RedirectResponse | None:
        """Ownership + limited-admin write gate. Returns redirect on failure."""
        if await _assert_owned_user(staff, user_id, session) is None:
            return RedirectResponse(f"/pg/users?err={_q('دسترسی ندارید')}", status_code=303)
        try:
            await assert_can_mutate_owned_users(staff)
        except PgQuotaError as qe:
            return RedirectResponse(f"/pg/users?err={_q(qe.message)}", status_code=303)
        return None

    @app.post("/pg/users/{user_id}/disable")
    async def pg_users_disable(
        user_id: int,
        staff: dict = Depends(require_pg_perm("pg_users")),
        session: AsyncSession = Depends(get_db),
    ):
        if not staff_user_actions(staff)["disable"]:
            return RedirectResponse(f"/pg/users?err={_q('اجازه ندارید')}", status_code=303)
        try:
            denied = await _guard_owned_mutation(staff, user_id, session)
            if denied is not None:
                return denied
            pg, _as_owner = await _staff_pg(session, staff)
            await pg.set_disabled_by_id(user_id, True)
        except Exception as e:
            return RedirectResponse(f"/pg/users?err={_pg_err(e)}", status_code=303)
        return RedirectResponse(f"/pg/users?ok={_q('کاربر غیرفعال شد')}", status_code=303)

    @app.post("/pg/users/{user_id}/enable")
    async def pg_users_enable(
        user_id: int,
        staff: dict = Depends(require_pg_perm("pg_users")),
        session: AsyncSession = Depends(get_db),
    ):
        if not staff_user_actions(staff)["enable"]:
            return RedirectResponse(f"/pg/users?err={_q('اجازه ندارید')}", status_code=303)
        try:
            denied = await _guard_owned_mutation(staff, user_id, session)
            if denied is not None:
                return denied
            pg, _as_owner = await _staff_pg(session, staff)
            await pg.set_disabled_by_id(user_id, False)
        except Exception as e:
            return RedirectResponse(f"/pg/users?err={_pg_err(e)}", status_code=303)
        return RedirectResponse(f"/pg/users?ok={_q('کاربر فعال شد')}", status_code=303)

    @app.post("/pg/users/{user_id}/reset")
    async def pg_users_reset(
        user_id: int,
        staff: dict = Depends(require_pg_perm("pg_users")),
        session: AsyncSession = Depends(get_db),
    ):
        acts = staff_user_actions(staff)
        if not acts["reset_usage"]:
            return RedirectResponse(f"/pg/users?err={_q('اجازه ندارید')}", status_code=303)
        try:
            denied = await _guard_owned_mutation(staff, user_id, session)
            if denied is not None:
                return denied
            pg, _as_owner = await _staff_pg(session, staff)
            await pg.reset_user_by_id(user_id)
        except Exception as e:
            return RedirectResponse(f"/pg/users?err={_pg_err(e)}", status_code=303)
        return RedirectResponse(f"/pg/users?ok={_q('مصرف ریست شد')}", status_code=303)

    @app.post("/pg/users/{user_id}/revoke")
    async def pg_users_revoke(
        user_id: int,
        staff: dict = Depends(require_pg_perm("pg_users")),
        session: AsyncSession = Depends(get_db),
    ):
        acts = staff_user_actions(staff)
        if not acts["revoke_sub"]:
            return RedirectResponse(f"/pg/users?err={_q('اجازه ندارید')}", status_code=303)
        try:
            denied = await _guard_owned_mutation(staff, user_id, session)
            if denied is not None:
                return denied
            pg, _as_owner = await _staff_pg(session, staff)
            await pg.revoke_sub_by_id(user_id)
        except Exception as e:
            return RedirectResponse(f"/pg/users?err={_pg_err(e)}", status_code=303)
        return RedirectResponse(f"/pg/users?ok={_q('سابسکرایب ابطال شد')}", status_code=303)

    @app.post("/pg/users/{user_id}/delete")
    async def pg_users_delete(
        user_id: int,
        staff: dict = Depends(require_pg_perm("pg_users")),
        session: AsyncSession = Depends(get_db),
    ):
        if not staff_user_actions(staff)["delete"]:
            return RedirectResponse(f"/pg/users?err={_q('اجازه حذف ندارید')}", status_code=303)
        try:
            denied = await _guard_owned_mutation(staff, user_id, session)
            if denied is not None:
                return denied
            pg, _as_owner = await _staff_pg(session, staff)
            await pg.delete_user_by_id(user_id)
        except Exception as e:
            return RedirectResponse(f"/pg/users?err={_pg_err(e)}", status_code=303)
        return RedirectResponse(f"/pg/users?ok={_q('کاربر حذف شد')}", status_code=303)

    # ---- templates ----
    @app.get("/pg/templates", response_class=HTMLResponse)
    async def pg_templates(
        request: Request,
        staff: dict = Depends(require_pg_perm("pg_templates")),
        session: AsyncSession = Depends(get_db),
    ):
        err = request.query_params.get("err")
        ok = request.query_params.get("ok")
        templates, groups = [], []
        try:
            pg = await _list_pg(session, staff)
            if pg is None:
                raise PasarGuardError(
                    "رمز پاسارگارد این ادمین ذخیره نشده — دسترسی وب را دوباره با رمز تنظیم کنید"
                )
            templates_raw, groups_raw = await asyncio.gather(
                pg.get_user_templates(),
                pg.get_groups_simple(),
                return_exceptions=True,
            )
            if isinstance(templates_raw, list):
                templates = templates_raw
            elif isinstance(templates_raw, dict):
                templates = as_list(templates_raw, "templates") or []
            elif isinstance(templates_raw, Exception):
                raise templates_raw
            templates = _filter_templates(templates, staff)
            groups = _filter_groups(groups_raw if isinstance(groups_raw, list) else [], staff)
        except Exception as e:
            err = str(e)
        return render(
            request,
            "pg_templates.html",
            _pg_ctx(
                staff,
                templates=templates,
                groups=groups,
                flash_err=err,
                flash_ok=ok,
                can_write=staff_pg_writes(staff)["templates"],
                active="pg_templates",
            ),
        )

    @app.post("/pg/templates")
    async def pg_templates_create(
        request: Request,
        name: str = Form(...),
        data_limit_gb: str = Form(""),
        expire_days: str = Form("30"),
        staff: dict = Depends(require_pg_perm("pg_templates")),
        session: AsyncSession = Depends(get_db),
    ):
        if not staff_pg_action(staff, "templates", "create"):
            return RedirectResponse(f"/pg/templates?err={_q('اجازه ساخت ندارید')}", status_code=303)
        form = await request.form()
        group_ids = [int(v) for k, v in form.items() if str(k).startswith("g_") and str(v).isdigit()]
        if not group_ids:
            return RedirectResponse(f"/pg/templates?err={_q('حداقل یک گروه انتخاب کنید')}", status_code=303)
        from app.services.plans_catalog import groups_allowed_for_staff
        from app.services.pg_quota import assert_can_create_user

        if not groups_allowed_for_staff(staff, group_ids):
            return RedirectResponse(f"/pg/templates?err={_q('گروه خارج از دسترسی شماست')}", status_code=303)
        try:
            days = int(expire_days or "30")
            gb = float(data_limit_gb) if str(data_limit_gb).strip() else None
            data_limit = int(gb * (1024**3)) if gb is not None else None
            expire_ts = None
            if days:
                import time as _time

                expire_ts = int(_time.time()) + days * 86400
            # Enforce role volume/expire bounds on template create (quota bypass fix).
            await assert_can_create_user(
                staff,
                data_limit=data_limit,
                expire_ts=expire_ts,
                from_template=False,
            )
            pg, _as_owner = await _staff_pg(session, staff)
            await pg.create_user_template(
                {
                    "name": name.strip(),
                    "group_ids": group_ids,
                    "expire_duration": days * 86400 if days else None,
                    "data_limit": data_limit,
                    "status": "active",
                }
            )
        except PgQuotaError as qe:
            return RedirectResponse(f"/pg/templates?err={_q(qe.message)}", status_code=303)
        except Exception as e:
            return RedirectResponse(f"/pg/templates?err={_q(e)}", status_code=303)
        return RedirectResponse(f"/pg/templates?ok={_q('تمپلیت ساخته شد')}", status_code=303)

    @app.post("/pg/templates/{template_id}/delete")
    async def pg_templates_delete(
        template_id: int,
        staff: dict = Depends(require_pg_perm("pg_templates")),
        session: AsyncSession = Depends(get_db),
    ):
        if not staff_pg_action(staff, "templates", "delete"):
            return RedirectResponse(f"/pg/templates?err={_q('اجازه حذف ندارید')}", status_code=303)
        from app.services.plans_catalog import template_allowed_for_staff

        if not template_allowed_for_staff(staff, template_id):
            return RedirectResponse(f"/pg/templates?err={_q('تمپلیت خارج از دسترسی شماست')}", status_code=303)
        try:
            await assert_can_mutate_owned_users(staff)
        except PgQuotaError as qe:
            return RedirectResponse(f"/pg/templates?err={_q(qe.message)}", status_code=303)
        try:
            pg, _as_owner = await _staff_pg(session, staff)
            await pg.delete_user_template(template_id)
        except Exception as e:
            return RedirectResponse(f"/pg/templates?err={_q(e)}", status_code=303)
        return RedirectResponse(f"/pg/templates?ok={_q('تمپلیت حذف شد')}", status_code=303)

    # ---- groups ----
    @app.get("/pg/groups", response_class=HTMLResponse)
    async def pg_groups(
        request: Request,
        staff: dict = Depends(require_pg_perm("pg_groups")),
        session: AsyncSession = Depends(get_db),
    ):
        err = request.query_params.get("err")
        ok = request.query_params.get("ok")
        groups, inbound_tags = [], []
        edit_id = request.query_params.get("edit")
        edit_group = None
        try:
            pg = await _list_pg(session, staff)
            if pg is None:
                raise PasarGuardError(
                    "رمز پاسارگارد این ادمین ذخیره نشده — دسترسی وب را دوباره با رمز تنظیم کنید"
                )
            full = await pg.get_groups()
            groups = full if isinstance(full, list) else as_list(full, "groups")
            if not groups:
                groups = await pg.get_groups_simple()
            groups = _filter_groups([g for g in groups if isinstance(g, dict)], staff)
            inbound_tags = _inbound_tags(await pg.get_inbounds())
            if edit_id and str(edit_id).isdigit():
                eid = int(edit_id)
                from app.services.plans_catalog import groups_allowed_for_staff

                if groups_allowed_for_staff(staff, [eid]):
                    edit_group = await pg.get_group(eid)
                else:
                    err = err or "گروه خارج از دسترسی شماست"
        except Exception as e:
            err = str(e)
        return render(
            request,
            "pg_groups.html",
            _pg_ctx(
                staff,
                groups=groups,
                inbound_tags=inbound_tags,
                edit_group=edit_group,
                flash_err=err,
                flash_ok=ok,
                can_write=staff_pg_writes(staff)["groups"],
                active="pg_groups",
            ),
        )

    @app.post("/pg/groups")
    async def pg_groups_create(
        request: Request,
        name: str = Form(...),
        staff: dict = Depends(require_pg_perm("pg_groups")),
        session: AsyncSession = Depends(get_db),
    ):
        if not staff_pg_action(staff, "groups", "create"):
            return RedirectResponse(f"/pg/groups?err={_q('اجازه ساخت ندارید')}", status_code=303)
        try:
            await assert_can_mutate_owned_users(staff)
        except PgQuotaError as qe:
            return RedirectResponse(f"/pg/groups?err={_q(qe.message)}", status_code=303)
        form = await request.form()
        tags = [str(v) for k, v in form.items() if str(k).startswith("tag_")]
        if not tags:
            return RedirectResponse(f"/pg/groups?err={_q('حداقل یک اینباند انتخاب کنید')}", status_code=303)
        try:
            pg, _as_owner = await _staff_pg(session, staff)
            await pg.create_group({"name": name.strip(), "inbound_tags": tags})
        except Exception as e:
            return RedirectResponse(f"/pg/groups?err={_q(e)}", status_code=303)
        return RedirectResponse(f"/pg/groups?ok={_q('گروه ساخته شد')}", status_code=303)

    @app.post("/pg/groups/{group_id}/edit")
    async def pg_groups_edit(
        request: Request,
        group_id: int,
        name: str = Form(...),
        staff: dict = Depends(require_pg_perm("pg_groups")),
        session: AsyncSession = Depends(get_db),
    ):
        if not staff_pg_action(staff, "groups", "update"):
            return RedirectResponse(f"/pg/groups?err={_q('اجازه ویرایش ندارید')}", status_code=303)
        from app.services.plans_catalog import groups_allowed_for_staff

        if not groups_allowed_for_staff(staff, [group_id]):
            return RedirectResponse(f"/pg/groups?err={_q('گروه خارج از دسترسی شماست')}", status_code=303)
        try:
            await assert_can_mutate_owned_users(staff)
        except PgQuotaError as qe:
            return RedirectResponse(f"/pg/groups?err={_q(qe.message)}", status_code=303)
        form = await request.form()
        tags = [str(v) for k, v in form.items() if str(k).startswith("tag_")]
        disabled = bool(form.get("is_disabled"))
        try:
            pg, _as_owner = await _staff_pg(session, staff)
            await pg.modify_group(
                group_id,
                {"name": name.strip(), "inbound_tags": tags, "is_disabled": disabled},
            )
        except Exception as e:
            return RedirectResponse(f"/pg/groups?err={_q(e)}", status_code=303)
        return RedirectResponse(f"/pg/groups?ok={_q('گروه به‌روز شد')}", status_code=303)

    @app.post("/pg/groups/{group_id}/delete")
    async def pg_groups_delete(
        group_id: int,
        staff: dict = Depends(require_pg_perm("pg_groups")),
        session: AsyncSession = Depends(get_db),
    ):
        if not staff_pg_action(staff, "groups", "delete"):
            return RedirectResponse(f"/pg/groups?err={_q('اجازه حذف ندارید')}", status_code=303)
        from app.services.plans_catalog import groups_allowed_for_staff

        if not groups_allowed_for_staff(staff, [group_id]):
            return RedirectResponse(f"/pg/groups?err={_q('گروه خارج از دسترسی شماست')}", status_code=303)
        try:
            await assert_can_mutate_owned_users(staff)
        except PgQuotaError as qe:
            return RedirectResponse(f"/pg/groups?err={_q(qe.message)}", status_code=303)
        try:
            pg, _as_owner = await _staff_pg(session, staff)
            await pg.delete_group(group_id)
        except Exception as e:
            return RedirectResponse(f"/pg/groups?err={_q(e)}", status_code=303)
        return RedirectResponse(f"/pg/groups?ok={_q('گروه حذف شد')}", status_code=303)

    # ---- hosts ----
    @app.get("/pg/hosts", response_class=HTMLResponse)
    async def pg_hosts(
        request: Request,
        staff: dict = Depends(require_pg_perm("pg_hosts")),
        session: AsyncSession = Depends(get_db),
    ):
        err = request.query_params.get("err")
        ok = request.query_params.get("ok")
        hosts, inbound_tags = [], []
        try:
            pg = await _list_pg(session, staff)
            if pg is None:
                raise PasarGuardError(
                    "رمز پاسارگارد این ادمین ذخیره نشده — دسترسی وب را دوباره با رمز تنظیم کنید"
                )
            hosts = await pg.get_hosts()
            inbound_tags = _inbound_tags(await pg.get_inbounds())
        except Exception as e:
            err = str(e)
        return render(
            request,
            "pg_hosts.html",
            _pg_ctx(
                staff,
                hosts=hosts,
                inbound_tags=inbound_tags,
                flash_err=err,
                flash_ok=ok,
                can_write=staff_pg_writes(staff)["hosts"],
                active="pg_hosts",
            ),
        )

    @app.post("/pg/hosts")
    async def pg_hosts_create(
        remark: str = Form(...),
        address: str = Form(...),
        port: str = Form(""),
        inbound_tag: str = Form(...),
        priority: int = Form(0),
        staff: dict = Depends(require_pg_perm("pg_hosts")),
        session: AsyncSession = Depends(get_db),
    ):
        if not staff_pg_action(staff, "hosts", "create"):
            return RedirectResponse(f"/pg/hosts?err={_q('اجازه ساخت ندارید')}", status_code=303)
        try:
            await assert_can_mutate_owned_users(staff)
        except PgQuotaError as qe:
            return RedirectResponse(f"/pg/hosts?err={_q(qe.message)}", status_code=303)
        addrs = _addr_set(address)
        if not addrs:
            return RedirectResponse(f"/pg/hosts?err={_q('آدرس هاست الزامی است')}", status_code=303)
        payload = {
            "remark": remark.strip(),
            "address": addrs,
            "inbound_tag": inbound_tag.strip(),
            "priority": priority,
            "is_disabled": False,
        }
        if str(port).strip().isdigit():
            payload["port"] = int(port)
        try:
            pg, _as_owner = await _staff_pg(session, staff)
            await pg.create_host(payload)
        except Exception as e:
            return RedirectResponse(f"/pg/hosts?err={_q(e)}", status_code=303)
        return RedirectResponse(f"/pg/hosts?ok={_q('هاست ساخته شد')}", status_code=303)

    @app.post("/pg/hosts/{host_id}/toggle")
    async def pg_hosts_toggle(
        host_id: int,
        staff: dict = Depends(require_pg_perm("pg_hosts")),
        session: AsyncSession = Depends(get_db),
    ):
        if not staff_pg_action(staff, "hosts", "update"):
            return RedirectResponse(f"/pg/hosts?err={_q('اجازه ندارید')}", status_code=303)
        try:
            await assert_can_mutate_owned_users(staff)
        except PgQuotaError as qe:
            return RedirectResponse(f"/pg/hosts?err={_q(qe.message)}", status_code=303)
        try:
            pg, _as_owner = await _staff_pg(session, staff)
            host = await pg.get_host(host_id)
            disabled = bool(host.get("is_disabled"))
            await pg.set_host_disabled(host_id, not disabled)
        except Exception as e:
            return RedirectResponse(f"/pg/hosts?err={_q(e)}", status_code=303)
        return RedirectResponse(f"/pg/hosts?ok={_q('وضعیت هاست تغییر کرد')}", status_code=303)

    @app.post("/pg/hosts/{host_id}/delete")
    async def pg_hosts_delete(
        host_id: int,
        staff: dict = Depends(require_pg_perm("pg_hosts")),
        session: AsyncSession = Depends(get_db),
    ):
        # Require explicit delete — update alone must not widen to destroy hosts.
        if not staff_pg_action(staff, "hosts", "delete"):
            return RedirectResponse(f"/pg/hosts?err={_q('اجازه حذف ندارید')}", status_code=303)
        try:
            await assert_can_mutate_owned_users(staff)
        except PgQuotaError as qe:
            return RedirectResponse(f"/pg/hosts?err={_q(qe.message)}", status_code=303)
        try:
            pg, _as_owner = await _staff_pg(session, staff)
            await pg.delete_host(host_id)
        except Exception as e:
            return RedirectResponse(f"/pg/hosts?err={_q(e)}", status_code=303)
        return RedirectResponse(f"/pg/hosts?ok={_q('هاست حذف شد')}", status_code=303)

    # ---- nodes / inbounds / admins ----
    @app.get("/pg/nodes", response_class=HTMLResponse)
    async def pg_nodes(
        request: Request,
        staff: dict = Depends(require_pg_perm("pg_nodes")),
        session: AsyncSession = Depends(get_db),
    ):
        err = request.query_params.get("err")
        ok = request.query_params.get("ok")
        nodes = []
        try:
            pg = await _list_pg(session, staff)
            if pg is None:
                raise PasarGuardError(
                    "رمز پاسارگارد این ادمین ذخیره نشده — دسترسی وب را دوباره با رمز تنظیم کنید"
                )
            nodes = await pg.get_nodes()
        except Exception as e:
            err = str(e)
        return render(
            request,
            "pg_nodes.html",
            _pg_ctx(
                staff,
                nodes=nodes,
                flash_err=err,
                flash_ok=ok,
                can_reconnect=staff_pg_action(staff, "nodes", "reconnect"),
                active="pg_nodes",
            ),
        )

    @app.post("/pg/nodes/{node_id}/reconnect")
    async def pg_node_reconnect(
        node_id: int,
        staff: dict = Depends(require_pg_perm("pg_nodes")),
        session: AsyncSession = Depends(get_db),
    ):
        if not staff_pg_action(staff, "nodes", "reconnect"):
            return RedirectResponse(f"/pg/nodes?err={_q('اجازه اتصال مجدد ندارید')}", status_code=303)
        try:
            await assert_can_mutate_owned_users(staff)
        except PgQuotaError as qe:
            return RedirectResponse(f"/pg/nodes?err={_q(qe.message)}", status_code=303)
        try:
            pg, _as_owner = await _staff_pg(session, staff)
            await pg.reconnect_node(node_id)
        except Exception as e:
            return RedirectResponse(f"/pg/nodes?err={_q(e)}", status_code=303)
        return RedirectResponse(f"/pg/nodes?ok={_q('درخواست اتصال مجدد ارسال شد')}", status_code=303)

    @app.get("/pg/inbounds", response_class=HTMLResponse)
    async def pg_inbounds(
        request: Request,
        staff: dict = Depends(require_pg_perm("pg_inbounds")),
        session: AsyncSession = Depends(get_db),
    ):
        err = request.query_params.get("err")
        inbounds, details = [], None
        try:
            pg = await _list_pg(session, staff)
            if pg is None:
                raise PasarGuardError(
                    "رمز پاسارگارد این ادمین ذخیره نشده — دسترسی وب را دوباره با رمز تنظیم کنید"
                )
            inbounds = await pg.get_inbounds()
            details = await pg.get_inbounds_details()
        except Exception as e:
            err = str(e)
        return render(
            request,
            "pg_inbounds.html",
            _pg_ctx(staff, inbounds=inbounds, details=details, flash_err=err, active="pg_inbounds"),
        )

    @app.get("/pg/admins", response_class=HTMLResponse)
    async def pg_admins(
        request: Request,
        staff: dict = Depends(require_admin),
        session=Depends(get_db),
    ):
        from app.services.pg_staff_access import (
            purge_orphaned_staff_access,
            web_access_status_map,
        )
        from app.services.resellers import list_reseller_plans

        err = request.query_params.get("err")
        ok = request.query_params.get("ok")
        admins = []
        roles = []
        web_status: dict = {}
        reseller_plans = []
        try:
            # Drop web rows for PG admins that no longer exist in PasarGuard
            try:
                await purge_orphaned_staff_access(session)
            except Exception:
                pass
            pg = get_pg()
            admins = await pg.get_admins()
            if not admins:
                admins = await pg.get_admins_simple()
            roles = await pg.get_admin_roles()
            names = [
                str(a.get("username") or "").strip()
                for a in (admins or [])
                if a.get("username")
            ]
            web_status = await web_access_status_map(session, names)
            reseller_plans = [p for p in await list_reseller_plans(session) if p.is_active]
        except Exception as e:
            err = str(e)
        return render(
            request,
            "pg_admins.html",
            _pg_ctx(
                staff,
                admins=admins,
                pg_roles=roles,
                web_status=web_status,
                reseller_plans=reseller_plans,
                flash_err=err,
                flash_ok=ok,
                active="pg_admins",
            ),
        )

    @app.post("/pg/admins")
    async def pg_admins_create(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        staff: dict = Depends(require_admin),
    ):
        form = await request.form()
        role_raw = str(form.get("role_id") or "").strip()
        note = str(form.get("note") or "").strip()
        payload: dict = {
            "username": username.strip(),
            "password": password,
            "note": note or "created from PGClockBot",
        }
        if role_raw.isdigit():
            payload["role_id"] = int(role_raw)
        else:
            payload["is_sudo"] = bool(form.get("is_sudo"))
        try:
            await get_pg().create_admin(payload)
        except Exception as e:
            if "role_id" in payload:
                payload.pop("role_id", None)
                payload["is_sudo"] = False
                try:
                    await get_pg().create_admin(payload)
                except Exception as e2:
                    return RedirectResponse(f"/pg/admins?err={_q(e2)}", status_code=303)
            else:
                return RedirectResponse(f"/pg/admins?err={_q(e)}", status_code=303)
        return RedirectResponse(f"/pg/admins?ok={_q('ادمین پنل ساخته شد')}", status_code=303)

    @app.post("/pg/admins/{username}/web-access")
    async def pg_admins_web_access(
        username: str,
        request: Request,
        staff: dict = Depends(require_admin),
        session=Depends(get_db),
    ):
        from app.services.pg_staff_access import classify_pg_admin_dict
        from app.services.resellers import provision_existing_pg_admin

        form = await request.form()
        web_username = str(form.get("web_username") or "").strip()
        web_password = str(form.get("web_password") or "")
        note = str(form.get("note") or "").strip()
        plan_raw = str(form.get("plan_id") or "").strip()
        pg_u = (username or "").strip()
        if not pg_u:
            return RedirectResponse(f"/pg/admins?err={_q('نام ادمین نامعتبر است')}", status_code=303)
        if not plan_raw.isdigit():
            return RedirectResponse(
                f"/pg/admins?err={_q('انتخاب پلن نمایندگی الزامی است')}",
                status_code=303,
            )
        try:
            admin = await get_pg().get_admin(pg_u)
        except Exception as e:
            return RedirectResponse(f"/pg/admins?err={_q(e)}", status_code=303)
        if not admin:
            return RedirectResponse(
                f"/pg/admins?err={_q('این ادمین در پاسارگارد یافت نشد')}",
                status_code=303,
            )
        if classify_pg_admin_dict(admin) == "disabled":
            return RedirectResponse(
                f"/pg/admins?err={_q('این ادمین در پاسارگارد غیرفعال است — ابتدا فعالش کنید')}",
                status_code=303,
            )

        profile, _hint, err = await provision_existing_pg_admin(
            session,
            pg_username=pg_u,
            web_username=web_username,
            password=web_password,
            plan_id=int(plan_raw),
            note=note,
        )
        if err:
            return RedirectResponse(f"/pg/admins?err={_q(err)}", status_code=303)
        uname = profile.web_username if profile else web_username
        msg = (
            f"دسترسی وب‌پنل «{uname}» با پلن نمایندگی فعال شد — "
            "مثل نماینده می‌تواند ربات و فروشگاه را تنظیم کند"
        )
        return RedirectResponse(f"/pg/admins?ok={_q(msg)}", status_code=303)

    @app.post("/pg/admins/{username}/web-access/revoke")
    async def pg_admins_web_access_revoke(
        username: str,
        staff: dict = Depends(require_admin),
        session=Depends(get_db),
    ):
        from app.services.pg_staff_access import revoke_web_access

        ok = await revoke_web_access(session, username)
        if not ok:
            return RedirectResponse(
                f"/pg/admins?err={_q('دسترسی وب برای این ادمین وجود نداشت')}",
                status_code=303,
            )
        return RedirectResponse(f"/pg/admins?ok={_q('دسترسی وب‌پنل حذف شد')}", status_code=303)

    @app.post("/pg/admins/{username}/web-access/toggle")
    async def pg_admins_web_access_toggle(
        username: str,
        staff: dict = Depends(require_admin),
        session=Depends(get_db),
    ):
        from app.services.pg_staff_access import (
            access_by_pg_username,
            reseller_by_pg_username,
            set_active,
        )

        row = await access_by_pg_username(session, username)
        if not row:
            return RedirectResponse(
                f"/pg/admins?err={_q('دسترسی وب برای این ادمین وجود ندارد')}",
                status_code=303,
            )
        if await reseller_by_pg_username(session, username):
            return RedirectResponse(
                f"/pg/admins?err={_q('این ادمین به نماینده متصل است؛ از بخش نمایندگان مدیریت کنید')}",
                status_code=303,
            )
        was_active = bool(row.is_active)
        ok = await set_active(session, username, not was_active)
        if not ok:
            return RedirectResponse(
                f"/pg/admins?err={_q('تغییر وضعیت ممکن نشد')}",
                status_code=303,
            )
        msg = "دسترسی وب غیرفعال شد" if was_active else "دسترسی وب فعال شد"
        return RedirectResponse(f"/pg/admins?ok={_q(msg)}", status_code=303)

    @app.post("/pg/admins/{username}/repair-pg-credentials")
    async def pg_admins_repair_credentials(
        username: str,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        """Admin-initiated: reset PG password + store enc. Never Owner-token for staff ops."""
        from app.services.pg_credentials import (
            repair_pg_staff_credentials,
            repair_reseller_pg_credentials,
        )
        from app.services.pg_staff_access import access_by_pg_username, reseller_by_pg_username

        reseller = await reseller_by_pg_username(session, username)
        if reseller is not None:
            pwd, err = await repair_reseller_pg_credentials(session, reseller)
            if err:
                return RedirectResponse(f"/pg/admins?err={_q(err)}", status_code=303)
            return RedirectResponse(
                f"/pg/admins?ok={_q('رمز پاسارگارد نماینده همگام شد — رمز جدید در اعلان ادمین ثبت نشد؛ از ویرایش نماینده قابل تنظیم است')}",
                status_code=303,
            )
        row = await access_by_pg_username(session, username)
        if not row:
            return RedirectResponse(
                f"/pg/admins?err={_q('دسترسی وب برای این ادمین وجود ندارد')}",
                status_code=303,
            )
        pwd, err = await repair_pg_staff_credentials(session, row)
        if err:
            return RedirectResponse(f"/pg/admins?err={_q(err)}", status_code=303)
        return RedirectResponse(
            f"/pg/admins?ok={_q('رمز پاسارگارد ادمین همگام و ذخیره شد')}",
            status_code=303,
        )

    @app.post("/pg/admins/{username}/delete")
    async def pg_admins_delete(
        username: str,
        staff: dict = Depends(require_admin),
        session=Depends(get_db),
    ):
        from app.services.pg_staff_access import revoke_web_access

        try:
            await get_pg().delete_admin(username)
        except Exception as e:
            return RedirectResponse(f"/pg/admins?err={_q(e)}", status_code=303)
        try:
            await revoke_web_access(session, username)
        except Exception:
            pass
        return RedirectResponse(f"/pg/admins?ok={_q('حذف شد')}", status_code=303)
