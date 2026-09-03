from __future__ import annotations

"""PasarGuard manager pages — admin + reseller (permission-gated)."""

import asyncio
from urllib.parse import quote

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.formatting import data_limit_to_gb, expire_remaining_days, format_stat_row
from app.services.numbers import normalize_number_text, parse_float, parse_int, parse_optional_float, parse_optional_int
from app.services.pasarguard import (
    PasarGuardError,
    as_list,
    build_user_create_payload,
    build_user_modify_payload,
    get_pg,
    get_pg_for_reseller,
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
from app.services.shop_scope import is_platform_admin, shop_owner_id


def _q(msg: str) -> str:
    return quote(str(msg), safe="")


async def _staff_pg(session: AsyncSession, staff: dict):
    """Return (client, as_owner) for mutations (Phase C2 + C5 + Hybrid + 2C).

    - Platform admin → env credentials; ``as_owner`` only when PG account is owner
    - Reseller → shop PG admin credentials (as_owner=False)
    - pg_staff → own PG credentials when stored (as_owner=False); else fail closed
    - principal (Level-1) → own OrgPrincipal PG credentials (as_owner=False)
    """
    if is_platform_admin(staff):
        return get_pg(), bool(staff.get("pg_is_owner"))
    rid = shop_owner_id(staff)
    if rid:
        return await get_pg_for_reseller(session, int(rid)), False
    if staff.get("role") == "pg_staff":
        from app.services.pasarguard import get_pg_for_staff

        return await get_pg_for_staff(
            session,
            pg_username=staff.get("pg_admin_username"),
            staff_id=staff.get("pg_staff_id"),
        ), False
    if staff.get("role") == "principal":
        from app.services.pasarguard import get_pg_for_principal

        return await get_pg_for_principal(
            session,
            principal_id=staff.get("org_principal_id"),
            pg_username=staff.get("pg_admin_username"),
        ), False
    raise PasarGuardError(
        "تغییر در پاسارگارد بدون اعتبارنامه اختصاصی ممکن نیست. "
        "علت محتمل: رمز ادمین فرعی هنوز با پاسارگارد همگام نشده. "
        "راه حل: ادمین اصلی از «ادمین‌ها» دسترسی ادمین فرعی را با رمز جدید ویرایش کند."
    )


async def _assert_owned_user(
    staff: dict, user_id: int, *, session: AsyncSession | None = None
) -> dict | None:
    """Fetch a PG user only when the staff principal is allowed to see/mutate it.

    Object-level check after load (H1). Non-owner without ``pg_admin_username``
    → deny (M6). Unknown ownership on the user payload → deny.
    Never uses Owner credentials for reseller/pg_staff.
    """
    from app.services.pg_user_scope import (
        is_full_pg_owner,
        pg_user_in_staff_scope,
        staff_pg_username,
    )

    info: dict | None = None

    if is_full_pg_owner(staff):
        raw = await get_pg().get_user_by_id(user_id)
        return raw if isinstance(raw, dict) else None

    # Limited / Hybrid / reseller / pg_staff — must have own PG identity
    if not staff_pg_username(staff):
        return None

    rid = shop_owner_id(staff)
    if rid and session is not None:
        try:
            pg = await get_pg_for_reseller(session, int(rid))
            raw = await pg.get_user_by_id(user_id)
            info = raw if isinstance(raw, dict) else None
        except Exception:
            return None
    elif staff.get("role") == "pg_staff" and session is not None:
        try:
            from app.services.pasarguard import get_pg_for_staff

            pg = await get_pg_for_staff(
                session,
                pg_username=staff.get("pg_admin_username"),
                staff_id=staff.get("pg_staff_id"),
            )
            raw = await pg.get_user_by_id(user_id)
            info = raw if isinstance(raw, dict) else None
        except Exception:
            return None
    elif staff.get("role") == "principal" and session is not None:
        try:
            from app.services.pasarguard import get_pg_for_principal

            pg = await get_pg_for_principal(
                session,
                principal_id=staff.get("org_principal_id"),
                pg_username=staff.get("pg_admin_username"),
            )
            raw = await pg.get_user_by_id(user_id)
            info = raw if isinstance(raw, dict) else None
        except Exception:
            return None
    elif _is_admin(staff):
        # Hybrid limited platform admin: env client fetch, then ownership assert
        try:
            raw = await get_pg().get_user_by_id(user_id)
            info = raw if isinstance(raw, dict) else None
        except Exception:
            return None
    else:
        return None

    if info is None:
        return None
    if not pg_user_in_staff_scope(info, staff):
        return None
    return info


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


def _is_pg_owner_principal(staff: dict) -> bool:
    """True PasarGuard owner (Hybrid) — not merely web Owner session."""
    return bool(staff.get("pg_is_owner"))


def _pg_owner(staff: dict) -> str:
    return str(staff.get("pg_admin_username") or "").strip()


def _owner_of(user: dict) -> str:
    from app.services.pg_user_scope import pg_user_owner_username

    return pg_user_owner_username(user)


def _filter_owned_users(users: list[dict], staff: dict) -> list[dict]:
    """Scope PG user lists. Missing pg_username for non-owner → [] (M6)."""
    from app.services.pg_user_scope import filter_pg_users_for_staff

    return filter_pg_users_for_staff(users, staff)


def _filter_templates(items: list[dict], staff: dict) -> list[dict]:
    from app.services.plans_catalog import filter_templates_for_staff

    return filter_templates_for_staff(items, staff)


def _filter_groups(items: list[dict], staff: dict) -> list[dict]:
    from app.services.plans_catalog import filter_groups_for_staff

    return filter_groups_for_staff(items, staff)


def _pg_ctx(staff: dict, **extra) -> dict:
    from app.services.pg_read import effective_pg_menu_keys

    writes = staff_pg_writes(staff)
    actions = staff_user_actions(staff)
    pg_perms = effective_pg_menu_keys(staff)
    # Sidebar reads staff.pg_permissions — keep menu/data aligned (C1 + Hybrid).
    staff_view = dict(staff)
    staff_view["pg_permissions"] = list(pg_perms)
    ctx = {
        "staff": staff_view,
        "is_admin": _is_admin(staff),
        "pg_perms": pg_perms,
        "pg_writes": writes,
        "pg_user_actions": actions,
        "pg_access": staff.get("pg_access") or {},
        "pg_is_owner": bool(staff.get("pg_is_owner")),
    }
    ctx.update(extra)
    return ctx


def register_pg_pages(
    app,
    *,
    render,
    require_pg_perm,
    get_db,
):
    def _wants_full_pg_widgets(request: Request) -> bool:
        return (request.query_params.get("full") or "").strip() == "1"

    async def _pg_chrome_context(request: Request, staff: dict, session: AsyncSession) -> dict:
        """Local/fast chrome after authz — ticket alert, remediation, open URL."""
        ticket_alert = None
        if not _is_admin(staff) and staff.get("role") in {"reseller", "pg_staff", "principal"}:
            from app.api.panel_tickets_pages import panel_ticket_dashboard_alert

            ticket_alert = await panel_ticket_dashboard_alert(
                session,
                staff,
                unread=getattr(request.state, "panel_tickets_unread", None),
            )

        staff_remediation = None
        if staff.get("role") == "pg_staff":
            from app.services.pg_staff_access import (
                access_by_web_username,
                staff_remediation_flags,
                staff_username_aligned,
            )

            login_u = (staff.get("username") or "").strip()
            staff_row = await access_by_web_username(session, login_u) if login_u else None
            if staff_row is not None:
                flags = staff_remediation_flags(staff_row)
                if flags["needs_remediation"]:
                    staff_remediation = {
                        **flags,
                        "can_self_serve": staff_username_aligned(staff_row),
                        "web_username": staff_row.web_username,
                        "pg_username": staff_row.pg_username,
                    }

        from app.services.ux20 import resolve_pg_open_url

        pg_external_url = await resolve_pg_open_url(
            session, is_admin=_is_pg_owner_principal(staff)
        )
        return {
            "ticket_alert": ticket_alert,
            "staff_remediation": staff_remediation,
            "pg_external_url": pg_external_url or None,
        }

    async def _pg_display_widgets(staff: dict, session: AsyncSession) -> dict:
        """Decorative PG overview data — never used for allow/deny."""
        from app.services.pg_overview import build_reseller_pg_overview, is_server_stat_key

        err = None
        stats_rows: list[tuple[str, str]] = []
        nodes: list = []
        nodes_overview = None
        counts = {"admins": 0, "groups": 0, "hosts": 0, "nodes": 0, "users": 0}
        reseller_overview = None
        host_gauges = None

        async def _load():
            nonlocal err, stats_rows, nodes, nodes_overview, counts
            nonlocal reseller_overview, host_gauges
            if _is_pg_owner_principal(staff):
                from app.services.host_gauges import gauges_from_pg_system_stats
                from app.services.node_traffic import build_nodes_overview

                pg = get_pg()
                raw, nodes_raw, admins, groups, hosts, realtime = await asyncio.gather(
                    pg.get_system_stats(),
                    pg.get_nodes(),
                    pg.get_admins_simple(),
                    pg.get_groups_simple(),
                    pg.get_hosts(),
                    pg.get_nodes_realtime(),
                    return_exceptions=True,
                )
                if isinstance(raw, dict):
                    host_gauges = gauges_from_pg_system_stats(raw)
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
                nodes = nodes_raw if isinstance(nodes_raw, list) else []
                rt = realtime if not isinstance(realtime, Exception) else None
                try:
                    nodes_overview = build_nodes_overview(nodes, rt)
                    nodes = nodes_overview.get("nodes") or []
                except Exception:
                    nodes_overview = build_nodes_overview(nodes, None)
                    nodes = nodes_overview.get("nodes") or []
                counts["nodes"] = len(nodes)
                counts["admins"] = len(admins) if isinstance(admins, list) else 0
                counts["groups"] = len(groups) if isinstance(groups, list) else 0
                counts["hosts"] = len(hosts) if isinstance(hosts, list) else 0
            else:
                reseller_overview = await build_reseller_pg_overview(staff, session=session)

        try:
            await asyncio.wait_for(_load(), timeout=3.0)
        except asyncio.TimeoutError:
            import logging

            logging.getLogger(__name__).info(
                "pg display widgets timed out after 3.0s — serving partial data"
            )
        except Exception as e:
            err = str(e)

        return {
            "err": err,
            "stats_rows": stats_rows,
            "nodes": nodes if _is_pg_owner_principal(staff) else [],
            "nodes_overview": nodes_overview,
            "counts": counts,
            "reseller_overview": reseller_overview,
            "host_gauges": host_gauges,
        }

    @app.get("/pg", response_class=HTMLResponse)
    async def pg_home(
        request: Request,
        staff: dict = Depends(require_pg_perm("pg_overview")),
        session: AsyncSession = Depends(get_db),
    ):
        """Authz via require_pg_perm before any HTML.

        Default: fast chrome shell, then /pg/body fills decorative widgets.
        ``?full=1`` keeps classic single-response behavior.
        Live-metrics script stays in the shell and re-queries DOM after swap.
        """
        from app.services.panel_timing import mark

        mark(request, "handler")
        chrome = await _pg_chrome_context(request, staff, session)
        want_full = _wants_full_pg_widgets(request)

        if not want_full:
            mark(request, "page_data")
            ctx = _pg_ctx(
                staff,
                stats_rows=[],
                nodes=[],
                counts={"admins": 0, "groups": 0, "hosts": 0, "nodes": 0, "users": 0},
                reseller_overview=None,
                flash_err=None,
                active="pg",
                host_gauges=None,
                nodes_overview=None,
                **chrome,
            )
            ctx["widgets_deferred"] = True
            ctx["widgets_body_url"] = "/pg/body"
            return render(request, "pg_home.html", ctx)

        widgets = await _pg_display_widgets(staff, session)
        mark(request, "page_data")
        ctx = _pg_ctx(
            staff,
            stats_rows=widgets["stats_rows"],
            nodes=widgets["nodes"],
            counts=widgets["counts"],
            reseller_overview=widgets["reseller_overview"],
            flash_err=widgets["err"],
            active="pg",
            host_gauges=widgets["host_gauges"],
            nodes_overview=widgets["nodes_overview"],
            **chrome,
        )
        ctx["widgets_deferred"] = False
        return render(request, "pg_home.html", ctx)

    @app.get("/pg/body", response_class=HTMLResponse)
    async def pg_home_body(
        request: Request,
        staff: dict = Depends(require_pg_perm("pg_overview")),
        session: AsyncSession = Depends(get_db),
    ):
        """Widget HTML only — re-runs the same require_pg_perm as /pg.

        Skips ticket/remediation chrome (already in the shell); only needs
        ``pg_external_url`` for the open-PasarGuard link inside the dash body.
        """
        from app.services.panel_timing import mark
        from app.services.ux20 import resolve_pg_open_url

        mark(request, "handler")
        widgets = await _pg_display_widgets(staff, session)
        pg_external_url = await resolve_pg_open_url(
            session, is_admin=_is_pg_owner_principal(staff)
        )
        mark(request, "page_data")
        ctx = _pg_ctx(
            staff,
            stats_rows=widgets["stats_rows"],
            nodes=widgets["nodes"],
            counts=widgets["counts"],
            reseller_overview=widgets["reseller_overview"],
            flash_err=widgets["err"],
            active="pg",
            host_gauges=widgets["host_gauges"],
            nodes_overview=widgets["nodes_overview"],
            ticket_alert=None,
            staff_remediation=None,
            pg_external_url=pg_external_url or None,
        )
        ctx["widgets_deferred"] = False
        return render(request, "_pg_dash_body.html", ctx)

    @app.get("/pg/metrics")
    async def pg_host_metrics_json(
        staff: dict = Depends(require_pg_perm("pg_overview")),
    ):
        """PG host CPU/RAM + node live rates — owner principal only (never reseller/pg_staff)."""
        from fastapi.responses import JSONResponse

        from app.services.host_gauges import empty_host_gauges, gauges_from_pg_system_stats, gauges_json
        from app.services.node_traffic import build_nodes_overview, nodes_overview_json

        if not _is_pg_owner_principal(staff):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        pg = get_pg()
        try:
            raw, nodes_raw, realtime = await asyncio.gather(
                pg.get_system_stats(),
                pg.get_nodes(),
                pg.get_nodes_realtime(),
                return_exceptions=True,
            )
            host = gauges_from_pg_system_stats(raw if isinstance(raw, dict) else None)
            nodes_list = nodes_raw if isinstance(nodes_raw, list) else []
            rt = realtime if not isinstance(realtime, Exception) else None
            overview = build_nodes_overview(nodes_list, rt)
        except Exception:
            host = empty_host_gauges()
            overview = build_nodes_overview([], None)
        payload = gauges_json(host)
        payload.update(nodes_overview_json(overview))
        return JSONResponse(payload)

    # ---- VPN users ----
    @app.get("/pg/users", response_class=HTMLResponse)
    async def pg_users(
        request: Request,
        staff: dict = Depends(require_pg_perm("pg_users")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.list_query import (
            DEFAULT_LIST_PAGE_SIZE,
            build_list_pager,
            extract_list_total,
            filter_by_search,
            list_offset,
            normalize_search_q,
            parse_list_page,
        )
        from app.services.pg_read import PgReadDenied, staff_pg_read_client

        err = request.query_params.get("err")
        ok = request.query_params.get("ok")
        form_err = request.query_params.get("form_err")
        form_modal = (request.query_params.get("modal") or "").strip()
        form_uid = (request.query_params.get("uid") or "").strip()
        q = (request.query_params.get("q") or "").strip()
        page = parse_list_page(request.query_params.get("page"))
        page_size = DEFAULT_LIST_PAGE_SIZE
        users: list[dict] = []
        users_total: int | None = None
        pager: dict = build_list_pager(
            page=page, page_size=page_size, fetched=0, total=None
        )
        templates: list[dict] = []
        groups: list[dict] = []
        access = staff.get("pg_access") or {}
        require_template = bool(access.get("require_template")) and not _is_pg_owner_principal(staff)
        try:
            search_q = normalize_search_q(q)
            pg = await staff_pg_read_client(session, staff)
            params: dict = {
                "offset": list_offset(page, page_size),
                "limit": page_size,
            }
            if search_q:
                # Remote API may be exact/prefix; keep hint + local casefold substring
                params["username"] = search_q
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
                users_total = extract_list_total(data)
            elif isinstance(data, list):
                users = data
            fetched = len(users)
            users = _filter_owned_users(users, staff)
            if search_q:

                def _user_search_parts(u: dict):
                    return (
                        u.get("username"),
                        u.get("note"),
                        u.get("id"),
                        u.get("status"),
                        (u.get("admin") or {}).get("username")
                        if isinstance(u.get("admin"), dict)
                        else u.get("admin"),
                        u.get("owner_username"),
                    )

                filtered = filter_by_search(users, search_q, _user_search_parts)
                if not filtered:
                    # Remote may be exact-only or case-sensitive — widen then filter locally
                    params_wide = {k: v for k, v in params.items() if k != "username"}
                    try:
                        data_wide = await pg.get_users(**params_wide)
                        if isinstance(data_wide, dict):
                            users = as_list(data_wide, "users") or []
                            users_total = extract_list_total(data_wide)
                        elif isinstance(data_wide, list):
                            users = data_wide
                            users_total = None
                        fetched = len(users)
                        users = _filter_owned_users(users, staff)
                        filtered = filter_by_search(users, search_q, _user_search_parts)
                    except Exception:
                        filtered = []
                        fetched = 0
                users = filtered
            pager = build_list_pager(
                page=page,
                page_size=page_size,
                fetched=fetched,
                total=users_total,
                scoped_len=len(users),
            )
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
        except PgReadDenied as e:
            err = e.message
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
                pager=pager,
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
        require_template = bool(access.get("require_template")) and not _is_pg_owner_principal(staff)
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
                try:
                    await assert_provision_create(
                        session, staff=staff, from_template=True
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
                hwid_raw = str(form.get("hwid_limit") or "").strip()
                data_limit = None
                expire_ts = None
                hwid_limit = None
                if gb_raw:
                    try:
                        gb = parse_float(gb_raw)
                        if gb < 0:
                            raise ValueError
                        if gb > 0:
                            data_limit = int(gb * (1024**3))
                    except ValueError:
                        return _pg_form_err("حجم نامعتبر است", modal="create")
                if days_raw:
                    try:
                        days = parse_int(days_raw)
                        if days < 0:
                            raise ValueError
                        if days > 0:
                            expire_ts = int(time.time()) + days * 86400
                    except ValueError:
                        return _pg_form_err("مدت نامعتبر است", modal="create")
                if hwid_raw:
                    try:
                        hwid_limit = parse_int(hwid_raw)
                        if hwid_limit < 0:
                            raise ValueError
                    except ValueError:
                        return _pg_form_err("سقف دستگاه (HWID) نامعتبر است", modal="create")

                try:
                    await assert_provision_create(
                        session,
                        staff=staff,
                        data_limit=data_limit,
                        expire_ts=expire_ts,
                        hwid_limit=hwid_limit,
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
                        hwid_limit=hwid_limit,
                        note=note,
                    )
                )

            # Ownership comes from `_staff_pg` credentials (admin→owner token,
            # reseller/pg_staff→own PG admin). Do not call get_pg() here for a
            # second owner-token transfer — that path bypassed staff ACL.
            _ = as_owner
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
            info = await _assert_owned_user(staff, user_id, session=session)
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
        hwid_raw = str(form.get("hwid_limit") or "").strip()
        data_limit = 0  # 0 = unlimited
        expire_ts = 0  # clear expire when empty
        hwid_limit = None
        hwid_changed = False
        if gb_raw:
            try:
                gb = parse_float(gb_raw)
                if gb < 0:
                    raise ValueError
                data_limit = int(gb * (1024**3)) if gb > 0 else 0
            except ValueError:
                return _pg_form_err("حجم نامعتبر است", modal="edit", uid=user_id)
        if days_raw:
            try:
                days = parse_int(days_raw)
                if days < 0:
                    raise ValueError
                expire_ts = int(time.time()) + days * 86400 if days > 0 else 0
            except ValueError:
                return _pg_form_err("مدت نامعتبر است", modal="edit", uid=user_id)
        if "hwid_limit" in form:
            hwid_changed = True
            if hwid_raw:
                try:
                    hwid_limit = parse_int(hwid_raw)
                    if hwid_limit < 0:
                        raise ValueError
                except ValueError:
                    return _pg_form_err("سقف دستگاه (HWID) نامعتبر است", modal="edit", uid=user_id)
            else:
                hwid_limit = 0  # unlimited

        try:
            current = await _assert_owned_user(staff, user_id, session=session)
            if current is None:
                return RedirectResponse(f"/pg/users?err={_q('دسترسی ندارید')}", status_code=303)
            try:
                await assert_provision_modify(
                    session,
                    staff,
                    data_limit=data_limit,
                    expire_ts=expire_ts,
                    hwid_limit=hwid_limit,
                    data_limit_changed=True,
                    expire_changed=True,
                    hwid_changed=hwid_changed,
                )
            except (ProvisionError, PgQuotaError) as qe:
                return _pg_form_err(getattr(qe, "message", str(qe)), modal="edit", uid=user_id)
            # PasarGuard does not allow changing username after create
            payload = build_user_modify_payload(
                username=None,
                group_ids=ids,
                data_limit=data_limit,
                expire_ts=expire_ts,
                hwid_limit=hwid_limit if hwid_changed else None,
                status=str(current.get("status") or "") or None,
            )
            pg, _as_owner = await _staff_pg(session, staff)
            await pg.modify_user_by_id(user_id, payload)
            uname = str(current.get("username") or user_id)
        except Exception as e:
            msg = e.user_message(fallback="خطا در ویرایش") if isinstance(e, PasarGuardError) else str(e)
            return _pg_form_err(msg, modal="edit", uid=user_id)
        return RedirectResponse(f"/pg/users?ok={_q(f'کاربر {uname} ویرایش شد')}", status_code=303)

    async def _guard_owned_mutation(
        staff: dict, user_id: int, *, session: AsyncSession
    ) -> RedirectResponse | None:
        """Ownership + limited-admin write gate. Returns redirect on failure."""
        if await _assert_owned_user(staff, user_id, session=session) is None:
            return RedirectResponse(f"/pg/users?err={_q('دسترسی ندارید')}", status_code=303)
        try:
            await assert_can_mutate_owned_users(staff, session=session)
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
            denied = await _guard_owned_mutation(staff, user_id, session=session)
            if denied is not None:
                return denied
            pg, _ = await _staff_pg(session, staff)
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
            denied = await _guard_owned_mutation(staff, user_id, session=session)
            if denied is not None:
                return denied
            pg, _ = await _staff_pg(session, staff)
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
            denied = await _guard_owned_mutation(staff, user_id, session=session)
            if denied is not None:
                return denied
            pg, _ = await _staff_pg(session, staff)
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
            denied = await _guard_owned_mutation(staff, user_id, session=session)
            if denied is not None:
                return denied
            pg, _ = await _staff_pg(session, staff)
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
            denied = await _guard_owned_mutation(staff, user_id, session=session)
            if denied is not None:
                return denied
            pg, _ = await _staff_pg(session, staff)
            await pg.delete_user_by_id(user_id)
            from app.services.bot_user_admin import detach_local_services_for_pg_user

            await detach_local_services_for_pg_user(session, user_id, commit=True)
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
        from app.services.pg_read import PgReadDenied, staff_pg_read_client

        err = request.query_params.get("err")
        ok = request.query_params.get("ok")
        templates, groups = [], []
        try:
            pg = await staff_pg_read_client(session, staff)
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
        except PgReadDenied as e:
            err = e.message
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
                can_create=staff_pg_action(staff, "templates", "create"),
                can_update=staff_pg_action(staff, "templates", "update"),
                can_delete=staff_pg_action(staff, "templates", "delete"),
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
        try:
            await assert_can_mutate_owned_users(staff, session=session)
        except PgQuotaError as qe:
            return RedirectResponse(f"/pg/templates?err={_q(qe.message)}", status_code=303)
        form = await request.form()
        group_ids = [
            parse_int(str(v))
            for k, v in form.items()
            if str(k).startswith("g_") and normalize_number_text(str(v)).isdigit()
        ]
        if not group_ids:
            return RedirectResponse(f"/pg/templates?err={_q('حداقل یک گروه انتخاب کنید')}", status_code=303)
        from app.services.plans_catalog import groups_allowed_for_staff

        if not groups_allowed_for_staff(staff, group_ids):
            return RedirectResponse(f"/pg/templates?err={_q('گروه خارج از دسترسی شماست')}", status_code=303)
        try:
            days = parse_int(expire_days or "30", default=30)
            gb = parse_optional_float(data_limit_gb)
            pg, _as_owner = await _staff_pg(session, staff)
            await pg.create_user_template(
                {
                    "name": name.strip(),
                    "group_ids": group_ids,
                    "expire_duration": days * 86400 if days else None,
                    "data_limit": int(gb * (1024**3)) if gb is not None else None,
                    "status": "active",
                }
            )
        except Exception as e:
            return RedirectResponse(f"/pg/templates?err={_q(e)}", status_code=303)
        return RedirectResponse(f"/pg/templates?ok={_q('تمپلیت ساخته شد')}", status_code=303)

    @app.post("/pg/templates/{template_id}/edit")
    async def pg_templates_edit(
        template_id: int,
        request: Request,
        name: str = Form(...),
        data_limit_gb: str = Form(""),
        expire_days: str = Form("30"),
        staff: dict = Depends(require_pg_perm("pg_templates")),
        session: AsyncSession = Depends(get_db),
    ):
        if not staff_pg_action(staff, "templates", "update"):
            return RedirectResponse(f"/pg/templates?err={_q('اجازه ویرایش ندارید')}", status_code=303)
        from app.services.plans_catalog import groups_allowed_for_staff, template_allowed_for_staff

        if not template_allowed_for_staff(staff, template_id):
            return RedirectResponse(f"/pg/templates?err={_q('تمپلیت خارج از دسترسی شماست')}", status_code=303)
        try:
            await assert_can_mutate_owned_users(staff, session=session)
        except PgQuotaError as qe:
            return RedirectResponse(f"/pg/templates?err={_q(qe.message)}", status_code=303)
        form = await request.form()
        group_ids = [
            parse_int(str(v))
            for k, v in form.items()
            if str(k).startswith("g_") and normalize_number_text(str(v)).isdigit()
        ]
        if not group_ids:
            return RedirectResponse(f"/pg/templates?err={_q('حداقل یک گروه انتخاب کنید')}", status_code=303)
        if not groups_allowed_for_staff(staff, group_ids):
            return RedirectResponse(f"/pg/templates?err={_q('گروه خارج از دسترسی شماست')}", status_code=303)
        try:
            days = parse_int(expire_days or "30", default=30)
            gb = parse_optional_float(data_limit_gb)
            pg, _as_owner = await _staff_pg(session, staff)
            await pg.modify_user_template(
                template_id,
                {
                    "name": name.strip(),
                    "group_ids": group_ids,
                    "expire_duration": days * 86400 if days else None,
                    "data_limit": int(gb * (1024**3)) if gb is not None else None,
                },
            )
        except Exception as e:
            return RedirectResponse(f"/pg/templates?err={_q(e)}", status_code=303)
        return RedirectResponse(f"/pg/templates?ok={_q('تمپلیت به‌روز شد')}", status_code=303)

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
            await assert_can_mutate_owned_users(staff, session=session)
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
        from app.services.pg_read import PgReadDenied, staff_pg_read_client

        err = request.query_params.get("err")
        ok = request.query_params.get("ok")
        groups, inbound_tags = [], []
        edit_id = request.query_params.get("edit")
        edit_group = None
        try:
            pg = await staff_pg_read_client(session, staff)
            full = await pg.get_groups()
            groups = full if isinstance(full, list) else as_list(full, "groups")
            if not groups:
                groups = await pg.get_groups_simple()
            groups = _filter_groups([g for g in groups if isinstance(g, dict)], staff)
            inbound_tags = _inbound_tags(await pg.get_inbounds())
            if edit_id and normalize_number_text(str(edit_id)).isdigit():
                eid = parse_int(edit_id)
                from app.services.plans_catalog import groups_allowed_for_staff

                if groups_allowed_for_staff(staff, [eid]):
                    edit_group = await pg.get_group(eid)
                else:
                    err = err or "گروه خارج از دسترسی شماست"
        except PgReadDenied as e:
            err = e.message
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
                can_create=staff_pg_action(staff, "groups", "create"),
                can_update=staff_pg_action(staff, "groups", "update"),
                can_delete=staff_pg_action(staff, "groups", "delete"),
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
            await assert_can_mutate_owned_users(staff, session=session)
        except PgQuotaError as qe:
            return RedirectResponse(f"/pg/groups?err={_q(qe.message)}", status_code=303)
        form = await request.form()
        tags = [str(v) for k, v in form.items() if str(k).startswith("tag_")]
        if not tags:
            return RedirectResponse(f"/pg/groups?err={_q('حداقل یک اینباند انتخاب کنید')}", status_code=303)
        try:
            pg, _as_owner = await _staff_pg(session, staff)
            # The form submits raw tag strings — never trust them as-is. Only
            # accept tags that actually exist on this admin's own inbounds
            # (see _inbound_tags docstring / audit item on inbound tag spoofing).
            valid_tags = set(_inbound_tags(await pg.get_inbounds()))
            tags = [t for t in tags if t in valid_tags]
            if not tags:
                return RedirectResponse(f"/pg/groups?err={_q('اینباند انتخاب‌شده نامعتبر است')}", status_code=303)
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
            await assert_can_mutate_owned_users(staff, session=session)
        except PgQuotaError as qe:
            return RedirectResponse(f"/pg/groups?err={_q(qe.message)}", status_code=303)
        form = await request.form()
        tags = [str(v) for k, v in form.items() if str(k).startswith("tag_")]
        disabled = bool(form.get("is_disabled"))
        try:
            pg, _as_owner = await _staff_pg(session, staff)
            # Same inbound-tag allow-list guard as group creation.
            valid_tags = set(_inbound_tags(await pg.get_inbounds()))
            tags = [t for t in tags if t in valid_tags]
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
            await assert_can_mutate_owned_users(staff, session=session)
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
        from app.services.pg_read import PgReadDenied, staff_pg_read_client

        err = request.query_params.get("err")
        ok = request.query_params.get("ok")
        hosts, inbound_tags = [], []
        try:
            pg = await staff_pg_read_client(session, staff)
            hosts = await pg.get_hosts()
            inbound_tags = _inbound_tags(await pg.get_inbounds())
        except PgReadDenied as e:
            err = e.message
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
                can_create=staff_pg_action(staff, "hosts", "create"),
                can_update=staff_pg_action(staff, "hosts", "update"),
                can_delete=staff_pg_action(staff, "hosts", "delete"),
                active="pg_hosts",
            ),
        )

    @app.post("/pg/hosts")
    async def pg_hosts_create(
        remark: str = Form(...),
        address: str = Form(...),
        port: str = Form(""),
        inbound_tag: str = Form(...),
        priority: str = Form("0"),
        staff: dict = Depends(require_pg_perm("pg_hosts")),
        session: AsyncSession = Depends(get_db),
    ):
        if not staff_pg_action(staff, "hosts", "create"):
            return RedirectResponse(f"/pg/hosts?err={_q('اجازه ساخت ندارید')}", status_code=303)
        try:
            await assert_can_mutate_owned_users(staff, session=session)
        except PgQuotaError as qe:
            return RedirectResponse(f"/pg/hosts?err={_q(qe.message)}", status_code=303)
        addrs = _addr_set(address)
        if not addrs:
            return RedirectResponse(f"/pg/hosts?err={_q('آدرس هاست الزامی است')}", status_code=303)
        try:
            prio = parse_int(priority or "0", default=0)
        except ValueError:
            return RedirectResponse(f"/pg/hosts?err={_q('اولویت نامعتبر است')}", status_code=303)
        payload = {
            "remark": remark.strip(),
            "address": addrs,
            "inbound_tag": inbound_tag.strip(),
            "priority": prio,
            "is_disabled": False,
        }
        port_n = parse_optional_int(port)
        if port_n is not None:
            payload["port"] = port_n
        try:
            pg, _as_owner = await _staff_pg(session, staff)
            from app.services.pg_object_scope import inbound_tag_allowed

            valid_tags = set(_inbound_tags(await pg.get_inbounds()))
            if not inbound_tag_allowed(payload["inbound_tag"], valid_tags):
                return RedirectResponse(
                    f"/pg/hosts?err={_q('اینباند انتخاب‌شده نامعتبر است')}",
                    status_code=303,
                )
            await pg.create_host(payload)
        except Exception as e:
            return RedirectResponse(f"/pg/hosts?err={_q(e)}", status_code=303)
        return RedirectResponse(f"/pg/hosts?ok={_q('هاست ساخته شد')}", status_code=303)

    @app.post("/pg/hosts/{host_id}/edit")
    async def pg_hosts_edit(
        host_id: int,
        remark: str = Form(...),
        address: str = Form(...),
        port: str = Form(""),
        inbound_tag: str = Form(...),
        priority: str = Form("0"),
        staff: dict = Depends(require_pg_perm("pg_hosts")),
        session: AsyncSession = Depends(get_db),
    ):
        if not staff_pg_action(staff, "hosts", "update"):
            return RedirectResponse(f"/pg/hosts?err={_q('اجازه ویرایش ندارید')}", status_code=303)
        try:
            await assert_can_mutate_owned_users(staff, session=session)
        except PgQuotaError as qe:
            return RedirectResponse(f"/pg/hosts?err={_q(qe.message)}", status_code=303)
        addrs = _addr_set(address)
        if not addrs:
            return RedirectResponse(f"/pg/hosts?err={_q('آدرس هاست الزامی است')}", status_code=303)
        try:
            prio = parse_int(priority or "0", default=0)
        except ValueError:
            return RedirectResponse(f"/pg/hosts?err={_q('اولویت نامعتبر است')}", status_code=303)
        payload = {
            "remark": remark.strip(),
            "address": addrs,
            "inbound_tag": inbound_tag.strip(),
            "priority": prio,
        }
        port_n = parse_optional_int(port)
        if port_n is not None:
            payload["port"] = port_n
        try:
            pg, _as_owner = await _staff_pg(session, staff)
            from app.services.pg_object_scope import assert_owned_host, inbound_tag_allowed

            if await assert_owned_host(pg, staff, host_id) is None:
                return RedirectResponse(
                    f"/pg/hosts?err={_q('هاست خارج از دسترسی شماست')}", status_code=303
                )
            valid_tags = set(_inbound_tags(await pg.get_inbounds()))
            if not inbound_tag_allowed(payload["inbound_tag"], valid_tags):
                return RedirectResponse(
                    f"/pg/hosts?err={_q('اینباند انتخاب‌شده نامعتبر است')}",
                    status_code=303,
                )
            await pg.modify_host(host_id, payload)
        except Exception as e:
            return RedirectResponse(f"/pg/hosts?err={_q(e)}", status_code=303)
        return RedirectResponse(f"/pg/hosts?ok={_q('هاست به‌روز شد')}", status_code=303)

    @app.post("/pg/hosts/{host_id}/toggle")
    async def pg_hosts_toggle(
        host_id: int,
        staff: dict = Depends(require_pg_perm("pg_hosts")),
        session: AsyncSession = Depends(get_db),
    ):
        if not staff_pg_action(staff, "hosts", "update"):
            return RedirectResponse(f"/pg/hosts?err={_q('اجازه ندارید')}", status_code=303)
        try:
            await assert_can_mutate_owned_users(staff, session=session)
        except PgQuotaError as qe:
            return RedirectResponse(f"/pg/hosts?err={_q(qe.message)}", status_code=303)
        try:
            pg, _as_owner = await _staff_pg(session, staff)
            from app.services.pg_object_scope import assert_owned_host

            host = await assert_owned_host(pg, staff, host_id)
            if host is None:
                return RedirectResponse(
                    f"/pg/hosts?err={_q('هاست خارج از دسترسی شماست')}", status_code=303
                )
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
        if not staff_pg_action(staff, "hosts", "delete"):
            return RedirectResponse(f"/pg/hosts?err={_q('اجازه حذف ندارید')}", status_code=303)
        try:
            await assert_can_mutate_owned_users(staff, session=session)
        except PgQuotaError as qe:
            return RedirectResponse(f"/pg/hosts?err={_q(qe.message)}", status_code=303)
        try:
            pg, _as_owner = await _staff_pg(session, staff)
            from app.services.pg_object_scope import assert_owned_host

            if await assert_owned_host(pg, staff, host_id) is None:
                return RedirectResponse(
                    f"/pg/hosts?err={_q('هاست خارج از دسترسی شماست')}", status_code=303
                )
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
        from app.services.pg_read import PgReadDenied, staff_pg_read_client

        err = request.query_params.get("err")
        ok = request.query_params.get("ok")
        nodes = []
        try:
            from app.services.node_traffic import enrich_nodes_with_traffic

            pg = await staff_pg_read_client(session, staff)
            nodes_raw = await pg.get_nodes()
            if not isinstance(nodes_raw, list):
                nodes_raw = []
            realtime = None
            try:
                realtime = await pg.get_nodes_realtime()
            except Exception:
                realtime = None
            nodes = enrich_nodes_with_traffic(nodes_raw, realtime)
        except PgReadDenied as e:
            err = e.message
        except Exception as e:
            err = str(e)
        node_acts = (staff.get("pg_actions") or {}).get("nodes") or {}
        return render(
            request,
            "pg_nodes.html",
            _pg_ctx(
                staff,
                nodes=nodes,
                flash_err=err,
                flash_ok=ok,
                can_reconnect=bool(node_acts.get("reconnect") or staff_pg_action(staff, "nodes", "reconnect")),
                can_create=bool(node_acts.get("create") or staff_pg_action(staff, "nodes", "create")),
                can_update=bool(node_acts.get("update") or staff_pg_action(staff, "nodes", "update")),
                can_delete=bool(node_acts.get("delete") or staff_pg_action(staff, "nodes", "delete")),
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
            return RedirectResponse(
                f"/pg/nodes?err={_q('اجازه اتصال مجدد ندارید. علت: نقش پاسارگارد این عمل را ندارد. راه حل: نقش ادمین را در پاسارگارد بررسی کنید.')}",
                status_code=303,
            )
        try:
            pg, _as_owner = await _staff_pg(session, staff)
            from app.services.pg_object_scope import assert_owned_node

            if await assert_owned_node(pg, staff, node_id) is None:
                return RedirectResponse(
                    f"/pg/nodes?err={_q('نود خارج از دسترسی شماست')}", status_code=303
                )
            await pg.reconnect_node(node_id)
        except Exception as e:
            msg = e.user_message(fallback=str(e)) if isinstance(e, PasarGuardError) else str(e)
            return RedirectResponse(
                f"/pg/nodes?err={_q(f'اتصال مجدد ناموفق: {msg}. علت محتمل: نود آفلاین یا API نود. راه حل: وضعیت نود را در پاسارگارد بررسی کنید.')}",
                status_code=303,
            )
        return RedirectResponse(f"/pg/nodes?ok={_q('درخواست اتصال مجدد ارسال شد')}", status_code=303)

    @app.post("/pg/nodes/reconnect-all")
    async def pg_nodes_reconnect_all(
        staff: dict = Depends(require_pg_perm("pg_nodes")),
        session: AsyncSession = Depends(get_db),
    ):
        if not staff_pg_action(staff, "nodes", "reconnect"):
            return RedirectResponse(f"/pg/nodes?err={_q('اجازه اتصال مجدد ندارید')}", status_code=303)
        try:
            pg, _ = await _staff_pg(session, staff)
            await pg.reconnect_all_nodes()
        except Exception as e:
            msg = e.user_message(fallback=str(e)) if isinstance(e, PasarGuardError) else str(e)
            return RedirectResponse(f"/pg/nodes?err={_q(msg)}", status_code=303)
        return RedirectResponse(f"/pg/nodes?ok={_q('اتصال مجدد همه نودها ارسال شد')}", status_code=303)

    @app.post("/pg/nodes/{node_id}/reset")
    async def pg_node_reset(
        node_id: int,
        staff: dict = Depends(require_pg_perm("pg_nodes")),
        session: AsyncSession = Depends(get_db),
    ):
        if not staff_pg_action(staff, "nodes", "update"):
            return RedirectResponse(f"/pg/nodes?err={_q('اجازه ریست مصرف نود ندارید')}", status_code=303)
        try:
            pg, _ = await _staff_pg(session, staff)
            from app.services.pg_object_scope import assert_owned_node

            if await assert_owned_node(pg, staff, node_id) is None:
                return RedirectResponse(
                    f"/pg/nodes?err={_q('نود خارج از دسترسی شماست')}", status_code=303
                )
            await pg.reset_node(node_id)
        except Exception as e:
            msg = e.user_message(fallback=str(e)) if isinstance(e, PasarGuardError) else str(e)
            return RedirectResponse(f"/pg/nodes?err={_q(msg)}", status_code=303)
        return RedirectResponse(f"/pg/nodes?ok={_q('مصرف نود ریست شد')}", status_code=303)

    @app.post("/pg/nodes/{node_id}/sync")
    async def pg_node_sync(
        node_id: int,
        staff: dict = Depends(require_pg_perm("pg_nodes")),
        session: AsyncSession = Depends(get_db),
    ):
        if not staff_pg_action(staff, "nodes", "update"):
            return RedirectResponse(f"/pg/nodes?err={_q('اجازه همگام‌سازی نود ندارید')}", status_code=303)
        try:
            pg, _ = await _staff_pg(session, staff)
            from app.services.pg_object_scope import assert_owned_node

            if await assert_owned_node(pg, staff, node_id) is None:
                return RedirectResponse(
                    f"/pg/nodes?err={_q('نود خارج از دسترسی شماست')}", status_code=303
                )
            await pg.sync_node(node_id)
        except Exception as e:
            msg = e.user_message(fallback=str(e)) if isinstance(e, PasarGuardError) else str(e)
            return RedirectResponse(f"/pg/nodes?err={_q(msg)}", status_code=303)
        return RedirectResponse(f"/pg/nodes?ok={_q('همگام‌سازی نود انجام شد')}", status_code=303)

    @app.post("/pg/nodes/{node_id}/toggle")
    async def pg_node_toggle(
        node_id: int,
        staff: dict = Depends(require_pg_perm("pg_nodes")),
        session: AsyncSession = Depends(get_db),
    ):
        if not staff_pg_action(staff, "nodes", "update"):
            return RedirectResponse(f"/pg/nodes?err={_q('اجازه تغییر وضعیت نود ندارید')}", status_code=303)
        try:
            pg, _ = await _staff_pg(session, staff)
            from app.services.pg_object_scope import assert_owned_node

            node = await assert_owned_node(pg, staff, node_id)
            if node is None:
                return RedirectResponse(
                    f"/pg/nodes?err={_q('نود خارج از دسترسی شماست')}", status_code=303
                )
            st = str(node.get("status") or "").lower()
            # Toggle disabled ↔ connected (PasarGuard uses status enum)
            new_status = "connected" if st in {"disabled", "error", "limited"} else "disabled"
            await pg.modify_node(node_id, {"status": new_status})
        except Exception as e:
            msg = e.user_message(fallback=str(e)) if isinstance(e, PasarGuardError) else str(e)
            return RedirectResponse(f"/pg/nodes?err={_q(msg)}", status_code=303)
        return RedirectResponse(f"/pg/nodes?ok={_q('وضعیت نود تغییر کرد')}", status_code=303)

    @app.post("/pg/nodes/{node_id}/delete")
    async def pg_node_delete(
        node_id: int,
        staff: dict = Depends(require_pg_perm("pg_nodes")),
        session: AsyncSession = Depends(get_db),
    ):
        if not staff_pg_action(staff, "nodes", "delete"):
            return RedirectResponse(f"/pg/nodes?err={_q('اجازه حذف نود ندارید')}", status_code=303)
        try:
            pg, _ = await _staff_pg(session, staff)
            from app.services.pg_object_scope import assert_owned_node

            if await assert_owned_node(pg, staff, node_id) is None:
                return RedirectResponse(
                    f"/pg/nodes?err={_q('نود خارج از دسترسی شماست')}", status_code=303
                )
            await pg.delete_node(node_id)
        except Exception as e:
            msg = e.user_message(fallback=str(e)) if isinstance(e, PasarGuardError) else str(e)
            return RedirectResponse(f"/pg/nodes?err={_q(msg)}", status_code=303)
        return RedirectResponse(f"/pg/nodes?ok={_q('نود حذف شد')}", status_code=303)

    @app.post("/pg/nodes")
    async def pg_nodes_create(
        request: Request,
        staff: dict = Depends(require_pg_perm("pg_nodes")),
        session: AsyncSession = Depends(get_db),
    ):
        if not staff_pg_action(staff, "nodes", "create"):
            return RedirectResponse(f"/pg/nodes?err={_q('اجازه ساخت نود ندارید')}", status_code=303)
        form = await request.form()
        name = str(form.get("name") or "").strip()
        address = str(form.get("address") or "").strip()
        port_raw = str(form.get("port") or "").strip()
        api_key = str(form.get("api_key") or "").strip()
        server_ca = str(form.get("server_ca") or "").strip()
        conn = str(form.get("connection_type") or "grpc").strip() or "grpc"
        core_raw = str(form.get("core_config_id") or "").strip()
        if not name or not address:
            return RedirectResponse(
                f"/pg/nodes?err={_q('نام و آدرس نود الزامی است')}",
                status_code=303,
            )
        payload: dict = {
            "name": name,
            "address": address,
            "connection_type": conn,
        }
        if port_raw.isdigit():
            payload["port"] = int(port_raw)
        if api_key:
            payload["api_key"] = api_key
        if server_ca:
            payload["server_ca"] = server_ca
        if core_raw.isdigit():
            payload["core_config_id"] = int(core_raw)
        try:
            pg, _ = await _staff_pg(session, staff)
            await pg.create_node(payload)
        except Exception as e:
            msg = e.user_message(fallback=str(e)) if isinstance(e, PasarGuardError) else str(e)
            return RedirectResponse(
                f"/pg/nodes?err={_q(f'ساخت نود ناموفق: {msg}. علت محتمل: فیلدهای الزامی ناقص (api_key/گواهی). راه حل: مقادیر را از پنل پاسارگارد کپی کنید.')}",
                status_code=303,
            )
        return RedirectResponse(f"/pg/nodes?ok={_q('نود ساخته شد')}", status_code=303)

    @app.get("/pg/inbounds", response_class=HTMLResponse)
    async def pg_inbounds(
        request: Request,
        staff: dict = Depends(require_pg_perm("pg_inbounds")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.pg_read import PgReadDenied, staff_pg_read_client

        err = request.query_params.get("err")
        inbounds, details = [], None
        try:
            pg = await staff_pg_read_client(session, staff)
            inbounds = await pg.get_inbounds()
            details = await pg.get_inbounds_details()
        except PgReadDenied as e:
            err = e.message
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
        staff: dict = Depends(require_pg_perm("pg_admins")),
        session=Depends(get_db),
    ):
        from app.services.pg_overview import admin_usage_snapshot
        from app.services.pg_staff_access import (
            purge_orphaned_staff_access,
            web_access_status_map,
        )
        from app.services.resellers import FEATURE_PERMS, list_reseller_plans

        err = request.query_params.get("err")
        ok = request.query_params.get("ok")
        admins = []
        roles = []
        web_status: dict = {}
        reseller_plans = []
        admin_usage: dict = {}
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
            roles_by_id = {
                int(r.get("id")): r
                for r in (roles or [])
                if isinstance(r, dict) and r.get("id") is not None
            }
            names = [
                str(a.get("username") or "").strip()
                for a in (admins or [])
                if a.get("username")
            ]
            web_status = await web_access_status_map(session, names)
            reseller_plans = [p for p in await list_reseller_plans(session) if p.is_active]
            for a in admins or []:
                if not isinstance(a, dict):
                    continue
                uname = str(a.get("username") or "").strip().lower()
                if not uname:
                    continue
                payload = a
                # Simple list often omits usage — fetch full admin when metrics missing
                has_usage = any(
                    payload.get(k) is not None
                    for k in (
                        "used_traffic",
                        "traffic_used",
                        "lifetime_used_traffic",
                        "total_users",
                        "users_count",
                        "data_limit",
                        "max_users",
                    )
                )
                if not has_usage:
                    try:
                        full = await pg.get_admin(uname)
                        if isinstance(full, dict):
                            payload = full
                    except Exception:
                        pass
                role = payload.get("role") if isinstance(payload.get("role"), dict) else None
                if role is None:
                    rid = payload.get("role_id") or a.get("role_id")
                    try:
                        role = roles_by_id.get(int(rid)) if rid is not None else None
                    except (TypeError, ValueError):
                        role = None
                admin_usage[uname] = admin_usage_snapshot(payload, role)
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
                admin_usage=admin_usage,
                reseller_plans=reseller_plans,
                feature_perms=FEATURE_PERMS,
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
        staff: dict = Depends(require_pg_perm("pg_admins")),
        session=Depends(get_db),
    ):
        form = await request.form()
        role_raw = str(form.get("role_id") or "").strip()
        note = str(form.get("note") or "").strip()
        # as_reseller: checkbox absent when OFF (default UI is OFF / fields hidden)
        as_reseller = str(form.get("as_reseller") or "").strip().lower() in {
            "1",
            "on",
            "true",
            "yes",
        }
        grant_web = str(form.get("grant_web") or "").strip().lower() in {
            "1",
            "on",
            "true",
            "yes",
        }
        plan_raw = str(form.get("plan_id") or "").strip()
        share_pg = str(form.get("share_pg_panel_url") or "").strip().lower() in {
            "1",
            "on",
            "true",
            "yes",
        }
        data_limit_gb = str(form.get("data_limit_gb") or "").strip()
        max_users = str(form.get("max_users") or "").strip()
        max_hwid = str(form.get("max_hwid_per_user") or "").strip()
        from app.services.credential_policy import validate_credentials
        from app.services.resellers import (
            DEFAULT_FEATURE_PERMS,
            FEATURE_PERMS,
            join_perms,
            normalize_feature_perms,
        )

        # Reject early with clear Persian causes (PasarGuard username+password rules)
        uname, cerr = validate_credentials(username, password, lowercase_username=False)
        if cerr:
            return RedirectResponse(f"/pg/admins?err={_q(cerr)}", status_code=303)
        if as_reseller and not plan_raw.isdigit():
            return RedirectResponse(
                f"/pg/admins?err={_q('برای ساخت به‌عنوان نماینده، انتخاب پلن نمایندگی الزامی است')}",
                status_code=303,
            )
        reseller_perms = None
        if as_reseller:
            selected = [key for key, _ in FEATURE_PERMS if form.get(f"perm_{key}")]
            reseller_perms = normalize_feature_perms(
                join_perms(selected) or DEFAULT_FEATURE_PERMS
            )
        payload: dict = {
            "username": uname,
            "password": password,
            "note": note or "created from PGClockBot",
        }
        if role_raw.isdigit():
            rid = int(role_raw)
            # Never assign an owner-equivalent PG role via panel create.
            # Capability matrix is role-driven; is_owner bypasses all action checks.
            try:
                roles = await get_pg().get_admin_roles()
            except Exception:
                roles = []
            chosen = next(
                (r for r in (roles or []) if isinstance(r, dict) and int(r.get("id") or 0) == rid),
                None,
            )
            if chosen and chosen.get("is_owner"):
                return RedirectResponse(
                    f"/pg/admins?err={_q('نمی‌توان نقش ادمین اصلی را به ادمین جدید داد')}",
                    status_code=303,
                )
            payload["role_id"] = rid
        else:
            # Legacy sudo flag must not mint owner-equivalent admins from the panel.
            if bool(form.get("is_sudo")):
                return RedirectResponse(
                    f"/pg/admins?err={_q('ساخت ادمین با دسترسی sudo از پنل مجاز نیست — یک نقش غیرمالک انتخاب کنید')}",
                    status_code=303,
                )
            payload["is_sudo"] = False
        if data_limit_gb:
            try:
                gb = parse_float(data_limit_gb)
                if gb > 0:
                    payload["data_limit"] = int(gb * (1024**3))
            except ValueError:
                return RedirectResponse(
                    f"/pg/admins?err={_q('سقف حجم ادمین نامعتبر است')}",
                    status_code=303,
                )
        overrides: dict = {}
        max_users_n = parse_optional_int(max_users)
        max_hwid_n = parse_optional_int(max_hwid)
        if max_users_n is not None:
            overrides["max_users"] = max_users_n
        if max_hwid_n is not None:
            overrides["max_hwid_per_user"] = max_hwid_n
        if overrides:
            payload["permission_overrides"] = overrides
        try:
            await get_pg().create_admin(payload)
        except Exception as e:
            # Do not retry without role_id / with elevated flags — fail closed.
            return RedirectResponse(f"/pg/admins?err={_pg_err(e)}", status_code=303)

        msg = f"ادمین «{uname}» در پاسارگارد ساخته شد"
        # One web path only: reseller XOR pg_staff (never both)
        if as_reseller:
            from app.services.resellers import provision_existing_pg_admin

            _profile, hint, werr = await provision_existing_pg_admin(
                session,
                pg_username=uname,
                web_username=uname,
                password=password,
                plan_id=int(plan_raw),
                note=note or "auto reseller from create admin",
                web_permissions=reseller_perms,
                share_pg_panel_url=share_pg,
            )
            if werr:
                return RedirectResponse(
                    f"/pg/admins?err={_q(f'{msg} — ولی نمایندگی ساخته نشد: {werr}')}",
                    status_code=303,
                )
            msg += " و به‌عنوان نماینده (پنل ربات + پاسارگارد) فعال شد"
            if hint:
                msg += " — ربات شخصی را از تنظیمات فروشگاه راه‌اندازی کنید"
        elif grant_web:
            from app.services.pg_staff_access import grant_web_access

            row, werr = await grant_web_access(
                session,
                pg_username=uname,
                web_username=uname,
                password=password,
                note=note or "auto from create admin",
                is_active=True,
            )
            if werr:
                return RedirectResponse(
                    f"/pg/admins?err={_q(f'{msg} — ولی دسترسی وب ساخته نشد: {werr}')}",
                    status_code=303,
                )
            msg += " و دسترسی وب ادمین فرعی (فقط پاسارگارد) فعال شد"
        return RedirectResponse(f"/pg/admins?ok={_q(msg)}", status_code=303)

    @app.post("/pg/admins/{username}/web-access")
    async def pg_admins_web_access_legacy(
        username: str,
        staff: dict = Depends(require_pg_perm("pg_admins")),
    ):
        """Phase D2 Q2: old single grant path hard-fails (no silent alias/conversion)."""
        return RedirectResponse(
            f"/pg/admins?err={_q('این مسیر منسوخ شده است — از «اعطای ادمین فرعی» یا «اعطای نماینده» استفاده کنید')}",
            status_code=303,
        )

    @app.post("/pg/admins/{username}/web-access/staff")
    async def pg_admins_web_access_staff(
        username: str,
        request: Request,
        staff: dict = Depends(require_pg_perm("pg_admins")),
        session=Depends(get_db),
    ):
        """PG-only secondary admin (pg_staff) — never creates a reseller."""
        from app.services.pg_staff_access import (
            access_by_pg_username,
            classify_pg_admin_dict,
            grant_web_access_with_plan,
            update_web_access,
        )

        form = await request.form()
        web_password = str(form.get("web_password") or "")
        note = str(form.get("note") or "").strip()
        plan_raw = str(form.get("plan_id") or "").strip()
        plan_id = int(plan_raw) if plan_raw.isdigit() else None
        # D3 Q1 A: explicit confirm-align checkbox (never silent rename)
        confirm_align = str(form.get("confirm_align") or "").strip().lower() in {
            "1",
            "on",
            "true",
            "yes",
        }
        pg_u = (username or "").strip()
        if not pg_u:
            return RedirectResponse(f"/pg/admins?err={_q('نام ادمین نامعتبر است')}", status_code=303)
        # D2: web username must match PG username (Q1: error-only — form locks to pg_u)
        web_username = str(form.get("web_username") or pg_u).strip() or pg_u
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

        existing = await access_by_pg_username(session, pg_u)
        if existing:
            # Preserve is_active on edit — use toggle for enable/disable
            row, err = await update_web_access(
                session,
                pg_username=pg_u,
                web_username=web_username,
                password=web_password,
                note=note,
                is_active=None,
                confirm_align=confirm_align,
            )
            if not err and plan_id and row:
                from app.db.models import ResellerPlan
                from app.services.pg_admin_subscription import (
                    is_subscription_plan,
                    start_or_refresh_subscription,
                )

                plan = await session.get(ResellerPlan, int(plan_id))
                if plan and is_subscription_plan(plan):
                    try:
                        await start_or_refresh_subscription(
                            session,
                            pg_username=pg_u,
                            plan=plan,
                            reset_extras=False,
                            apply_pg_limits=True,
                        )
                        await session.commit()
                    except Exception as e:
                        err = f"ذخیره شد ولی اشتراک زمانی اعمال نشد: {e}"
        else:
            row, err = await grant_web_access_with_plan(
                session,
                pg_username=pg_u,
                web_username=web_username,
                password=web_password,
                plan_id=plan_id,
                note=note,
                is_active=True,
            )
        if err:
            return RedirectResponse(f"/pg/admins?err={_q(err)}", status_code=303)
        uname = row.web_username if row else web_username
        msg = f"دسترسی ادمین فرعی «{uname}» ذخیره شد — فقط منوی پاسارگارد (بدون فروشگاه)"
        return RedirectResponse(f"/pg/admins?ok={_q(msg)}", status_code=303)

    @app.post("/pg/admins/{username}/subscription/renew")
    async def pg_admins_subscription_renew(
        username: str,
        request: Request,
        staff: dict = Depends(require_pg_perm("pg_admins")),
        session=Depends(get_db),
    ):
        """Owner extends / restores a time-limited PG admin (reseller or staff). No wallet charge."""
        from app.db.models import ResellerPlan
        from app.services.pg_admin_subscription import (
            get_or_create_subscription,
            get_subscription,
            is_subscription_plan,
            renew_subscription,
        )

        pg_u = (username or "").strip()
        if not pg_u:
            return RedirectResponse(f"/pg/admins?err={_q('نام ادمین نامعتبر است')}", status_code=303)
        form = await request.form()
        plan_raw = str(form.get("plan_id") or "").strip()
        sub = await get_subscription(session, pg_u)
        plan_id = int(plan_raw) if plan_raw.isdigit() else (sub.plan_id if sub else None)
        if not plan_id:
            return RedirectResponse(
                f"/pg/admins?err={_q('پلن اشتراک برای تمدید مشخص نیست')}",
                status_code=303,
            )
        plan = await session.get(ResellerPlan, int(plan_id))
        if not plan or not is_subscription_plan(plan):
            return RedirectResponse(
                f"/pg/admins?err={_q('پلن اشتراک معتبر نیست')}",
                status_code=303,
            )
        try:
            sub = await get_or_create_subscription(session, pg_u)
            result = await renew_subscription(
                session,
                sub=sub,
                plan=plan,
                payer=None,
                charge_wallet=False,
                reset_user_traffic=True,
            )
        except ValueError as e:
            return RedirectResponse(f"/pg/admins?err={_q(e)}", status_code=303)
        except Exception as e:
            return RedirectResponse(f"/pg/admins?err={_q(e)}", status_code=303)
        exp = result.get("expires_at")
        exp_s = exp.strftime("%Y-%m-%d") if exp is not None else "بدون انقضا"
        return RedirectResponse(
            f"/pg/admins?ok={_q(f'اشتراک «{pg_u}» تمدید شد — انقضا: {exp_s}')}",
            status_code=303,
        )

    @app.post("/pg/admins/{username}/web-access/reseller")
    async def pg_admins_web_access_reseller(
        username: str,
        request: Request,
        staff: dict = Depends(require_pg_perm("pg_admins")),
        session=Depends(get_db),
    ):
        """Shop reseller grant — refuses when pg_staff row exists (no conversion)."""
        from app.services.pg_staff_access import classify_pg_admin_dict
        from app.services.resellers import provision_existing_pg_admin

        form = await request.form()
        web_password = str(form.get("web_password") or "")
        note = str(form.get("note") or "").strip()
        plan_raw = str(form.get("plan_id") or "").strip()
        pg_u = (username or "").strip()
        if not pg_u:
            return RedirectResponse(f"/pg/admins?err={_q('نام ادمین نامعتبر است')}", status_code=303)
        web_username = str(form.get("web_username") or pg_u).strip() or pg_u
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

    @app.post("/pg/admins/{username}/convert-to-reseller")
    async def pg_admins_convert_to_reseller(
        username: str,
        request: Request,
        staff: dict = Depends(require_pg_perm("pg_admins")),
        session=Depends(get_db),
    ):
        """Explicit Owner action: pg_staff → reseller (never silent)."""
        from app.services.resellers import convert_staff_to_reseller

        form = await request.form()
        plan_raw = str(form.get("plan_id") or "").strip()
        web_password = str(form.get("web_password") or "")
        note = str(form.get("note") or "").strip()
        pg_u = (username or "").strip()
        if not pg_u:
            return RedirectResponse(f"/pg/admins?err={_q('نام ادمین نامعتبر است')}", status_code=303)
        if not plan_raw.isdigit():
            return RedirectResponse(
                f"/pg/admins?err={_q('انتخاب پلن نمایندگی الزامی است')}",
                status_code=303,
            )
        _profile, hint, err = await convert_staff_to_reseller(
            session,
            pg_username=pg_u,
            plan_id=int(plan_raw),
            password=web_password,
            note=note,
        )
        if err:
            return RedirectResponse(f"/pg/admins?err={_q(err)}", status_code=303)
        msg = (
            f"ادمین فرعی «{pg_u}» به نماینده تبدیل شد "
            "(دسترسی ادمین فرعی حذف و پنل ربات/فروشگاه فعال شد)"
        )
        if hint:
            msg += " — ربات شخصی را از تنظیمات فروشگاه راه‌اندازی کنید"
        return RedirectResponse(f"/pg/admins?ok={_q(msg)}", status_code=303)

    @app.post("/pg/admins/{username}/web-access/revoke")
    async def pg_admins_web_access_revoke(
        username: str,
        staff: dict = Depends(require_pg_perm("pg_admins")),
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
        staff: dict = Depends(require_pg_perm("pg_admins")),
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

    @app.post("/pg/admins/{username}/delete")
    async def pg_admins_delete(
        request: Request,
        username: str,
        staff: dict = Depends(require_pg_perm("pg_admins")),
        session=Depends(get_db),
    ):
        from app.services.pg_staff_access import (
            access_by_pg_username,
            reseller_by_pg_username,
            revoke_web_access,
        )
        from app.services.resellers import notify_reseller_revoked, revoke_reseller

        form = await request.form()
        reason = str(form.get("reason") or "").strip()
        if len(reason) < 3:
            return RedirectResponse(
                f"/pg/admins?err={_q('علت حذف ادمین الزامی است (حداقل ۳ کاراکتر)')}",
                status_code=303,
            )

        pg_u = (username or "").strip()
        if not pg_u:
            return RedirectResponse(f"/pg/admins?err={_q('نام ادمین نامعتبر است')}", status_code=303)

        reseller = await reseller_by_pg_username(session, pg_u)
        staff_row = await access_by_pg_username(session, pg_u)
        notes: list[str] = []

        if reseller is not None:
            # Cascade: removing PG admin removes linked shop/reseller path too
            try:
                info = await revoke_reseller(
                    session,
                    int(reseller.user_id),
                    delete_pg_admin=True,
                    reason=reason,
                )
                notes.append("نمایندگی/ربات فروشگاه هم حذف شد")
                if info.get("pg_admin_deleted"):
                    notes.append("ادمین پاسارگارد حذف شد")
                else:
                    # Profile removed; PG may already be gone — try direct delete
                    try:
                        await get_pg().delete_admin(pg_u)
                        notes.append("ادمین پاسارگارد حذف شد")
                    except Exception:
                        notes.append("ادمین پاسارگارد از قبل نبود یا حذف نشد")
                tg = info.get("telegram_id")
                if tg:
                    try:
                        await notify_reseller_revoked(int(tg), reason)
                    except Exception:
                        pass
            except Exception as e:
                return RedirectResponse(f"/pg/admins?err={_pg_err(e)}", status_code=303)
        else:
            try:
                await get_pg().delete_admin(pg_u)
                notes.append("ادمین پاسارگارد حذف شد")
            except Exception as e:
                return RedirectResponse(f"/pg/admins?err={_pg_err(e)}", status_code=303)

        if staff_row is not None or await access_by_pg_username(session, pg_u):
            try:
                await revoke_web_access(session, pg_u)
                notes.append("دسترسی ادمین فرعی وب حذف شد")
            except Exception:
                pass

        msg = "حذف شد"
        if notes:
            msg += " — " + "؛ ".join(notes)
        return RedirectResponse(f"/pg/admins?ok={_q(msg)}", status_code=303)
