"""Finance hub: orders + payments tabs and domain settings modal."""

from __future__ import annotations

import logging
from urllib.parse import quote, urlencode

import httpx
from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import String, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import BotUser, Order, Payment, Plan, ResellerProfile
from app.services.authz import authz_from_staff, can_shop
from app.services.list_query import (
    DEFAULT_LIST_PAGE_SIZE,
    build_list_pager,
    ilike_pattern,
    list_offset,
    normalize_search_q,
    parse_list_page,
)
from app.services.shop_scope import is_platform_admin, shop_owner_id
from app.services.users import SETTING_GROUPS, TAB_SETTING_GROUPS, get_all_settings
from app.services.secret_box import reveal_bot_token

logger = logging.getLogger(__name__)


def _sql_substr(col, pat: str):
    """Case-insensitive substring match (SQLite + Postgres)."""
    return func.lower(cast(col, String)).like(pat, escape="\\")


def _order_search_clause(pat: str):
    return or_(
        _sql_substr(Order.id, pat),
        _sql_substr(Order.status, pat),
        _sql_substr(Order.payment_method, pat),
        _sql_substr(Order.amount, pat),
        _sql_substr(Order.note, pat),
        _sql_substr(Order.user_id, pat),
        _sql_substr(Order.plan_id, pat),
        _sql_substr(BotUser.username, pat),
        _sql_substr(BotUser.full_name, pat),
        _sql_substr(BotUser.telegram_id, pat),
        _sql_substr(Plan.name, pat),
    )


def _payment_search_clause(pat: str, needle: str):
    parts = [
        _sql_substr(Payment.id, pat),
        _sql_substr(Payment.user_id, pat),
        _sql_substr(Payment.order_id, pat),
        _sql_substr(Payment.amount, pat),
        _sql_substr(Payment.status, pat),
        _sql_substr(Payment.method, pat),
        _sql_substr(Payment.review_note, pat),
        _sql_substr(BotUser.username, pat),
        _sql_substr(BotUser.full_name, pat),
        _sql_substr(BotUser.telegram_id, pat),
    ]
    if "شارژ" in needle:
        parts.append(Payment.is_wallet_topup.is_(True))
    if "خرید" in needle:
        parts.append(Payment.is_wallet_topup.is_(False))
    return or_(*parts)


async def _payment_bot_token(
    session: AsyncSession,
    payment: Payment,
    *,
    order: Order | None,
    payer_reseller_id: int | None,
) -> str:
    """Resolve Telegram bot token that owns this receipt file_id.

    Never cross tenants: reseller receipts use only that shop's bot token;
    platform receipts use only the platform bot token (no fallback either way).
    """
    from app.config import get_settings

    if order is not None and order.reseller_id:
        profile = (
            await session.execute(
                select(ResellerProfile).where(
                    ResellerProfile.user_id == int(order.reseller_id)
                )
            )
        ).scalar_one_or_none()
        return ((reveal_bot_token(profile.bot_token) if profile else None) or "").strip()

    if payer_reseller_id:
        profile = (
            await session.execute(
                select(ResellerProfile).where(
                    ResellerProfile.user_id == int(payer_reseller_id)
                )
            )
        ).scalar_one_or_none()
        return ((reveal_bot_token(profile.bot_token) if profile else None) or "").strip()

    return (get_settings().bot_token or "").strip()


def _safe_telegram_file_path(raw: str | None) -> str | None:
    """Normalize Telegram getFile path; reject traversal / absolute / non-file paths."""
    import re

    path = (raw or "").strip().lstrip("/")
    if not path:
        return None
    if ".." in path or "\\" in path:
        return None
    if path.startswith(("http:", "https:", "file:")):
        return None
    if any(ord(ch) < 32 for ch in path):
        return None
    if not re.match(r"^[A-Za-z0-9_./-]+$", path):
        return None
    return path


async def _fetch_telegram_file(token: str, file_id: str) -> tuple[bytes, str] | None:
    """Download a Telegram file. Returns image bytes only (receipt photos)."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        meta = await client.get(
            f"https://api.telegram.org/bot{token}/getFile",
            params={"file_id": file_id},
        )
        data = meta.json() if meta.status_code == 200 else {}
        if not data.get("ok"):
            return None
        path = _safe_telegram_file_path(((data.get("result") or {}).get("file_path") or ""))
        if not path:
            return None
        file_resp = await client.get(
            f"https://api.telegram.org/file/bot{token}/{path}"
        )
        if file_resp.status_code != 200 or not file_resp.content:
            return None
        # Cap size — receipts are photos, not arbitrary dumps.
        if len(file_resp.content) > 15 * 1024 * 1024:
            return None
        ctype = (file_resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        if not ctype or ctype == "application/octet-stream":
            lower = path.lower()
            if lower.endswith(".png"):
                ctype = "image/png"
            elif lower.endswith(".webp"):
                ctype = "image/webp"
            elif lower.endswith(".gif"):
                ctype = "image/gif"
            else:
                ctype = "image/jpeg"
        if not ctype.startswith("image/"):
            return None
        return file_resp.content, ctype


async def _authorize_receipt_access(
    session: AsyncSession,
    staff: dict,
    payment: Payment,
) -> tuple[Order | None, int | None] | None:
    """Return (order, payer_reseller_id) if staff may view this receipt, else None.

    Platform admins: only platform-scoped payments (no reseller order / no reseller payer).
    Shop staff: only payments for their shop (order.reseller_id or payer.reseller_id).
    """
    order: Order | None = None
    if payment.order_id:
        order = await session.get(Order, int(payment.order_id))

    payer_reseller_id: int | None = None
    if payment.user_id:
        payer = await session.get(BotUser, int(payment.user_id))
        if payer and payer.reseller_id:
            payer_reseller_id = int(payer.reseller_id)

    order_rid = int(order.reseller_id) if order and order.reseller_id else None
    tenant_rid = order_rid or payer_reseller_id

    if is_platform_admin(staff):
        if tenant_rid:
            return None
        return order, payer_reseller_id

    rid = shop_owner_id(staff)
    if not rid or not tenant_rid or int(tenant_rid) != int(rid):
        return None
    return order, payer_reseller_id


def _groups_for_tab(tab: str) -> tuple[list[str], dict]:
    tab_groups = TAB_SETTING_GROUPS.get(tab, [])
    groups = {name: SETTING_GROUPS[name] for name in tab_groups if name in SETTING_GROUPS}
    return tab_groups, groups


def register_finance_pages(app, *, render, require_staff, get_db):
    @app.get("/finance", response_class=HTMLResponse)
    async def finance_page(
        request: Request,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ):
        authz = authz_from_staff(staff)
        can_orders = can_shop(authz, "orders")
        can_payments = can_shop(authz, "payments")
        if not (can_orders or can_payments):
            return RedirectResponse("/home", status_code=303)

        can_finance_settings = staff.get("role") == "admin" or can_shop(authz, "shop_settings")
        can_billing_settings = staff.get("role") == "admin"

        tab = (request.query_params.get("tab") or "").strip()
        if tab not in {"reports", "behavior", "orders", "payments", "delivery"}:
            tab = "reports" if (can_orders or can_payments) else "payments"
        if tab == "reports" and not (can_orders or can_payments):
            tab = "payments"
        if tab == "behavior" and not (can_orders or can_payments):
            tab = "payments"
        if tab == "orders" and not can_orders:
            tab = "reports" if can_payments else "payments"
        if tab == "payments" and not can_payments:
            tab = "reports" if can_orders else "orders"
        if tab == "delivery" and not can_orders:
            tab = "reports" if (can_orders or can_payments) else "payments"

        report_period = (request.query_params.get("period") or "week").strip().lower()
        if report_period not in {"day", "week", "month"}:
            report_period = "week"

        search_q = normalize_search_q(request.query_params.get("q"))
        open_settings = (request.query_params.get("settings") or "").strip()
        if open_settings not in {"payment", "billing"}:
            open_settings = ""
        if open_settings == "billing" and not can_billing_settings:
            open_settings = "payment" if can_finance_settings else ""
        if open_settings and not can_finance_settings:
            open_settings = ""

        ctx: dict = {
            "staff": staff,
            "finance_tab": tab,
            "can_orders": can_orders,
            "can_payments": can_payments,
            "can_finance_settings": can_finance_settings,
            "can_billing_settings": can_billing_settings and can_finance_settings,
            "can_pending_orders": staff.get("role") == "admin",
            "open_finance_settings": open_settings,
            "q": search_q,
            "orders": [],
            "payments_by_order": {},
            "payments": [],
            "payers": {},
            "delivery_failures": [],
            "stuck_paid_orders": [],
            "stuck_order_ids": set(),
            "stuck_only_order_ids": [],
            "orders_by_id": {},
            "pager": None,
            "flash_ok": request.query_params.get("ok")
            or ("ذخیره شد." if request.query_params.get("saved") == "1" else None),
            "flash_err": request.query_params.get("err"),
            "values": {},
            "payment_tab_groups": [],
            "payment_groups": {},
            "billing_tab_groups": [],
            "billing_groups": {},
            "payment_settings_action": "",
            "billing_settings_action": "",
            "finance_pending_action": "",
            "receipt_matches": {},
            "funnel": {
                "shop_open": 0,
                "plan_view": 0,
                "pay_start": 0,
                "receipt": 0,
                "delivered": 0,
            },
            "report": {},
            "report_period": report_period,
        }

        if can_finance_settings:
            # Owner/platform settings only for real Owner. Scoped staff without
            # resolvable shop_owner_id must NOT fall through to Owner settings.
            pay_groups_names, pay_groups = _groups_for_tab("payment")
            bill_groups_names, bill_groups = _groups_for_tab("billing")
            ctx["payment_tab_groups"] = pay_groups_names
            ctx["payment_groups"] = pay_groups
            ctx["billing_tab_groups"] = bill_groups_names
            ctx["billing_groups"] = bill_groups
            next_base = f"/finance?tab={tab}"
            if is_platform_admin(staff):
                ctx["values"] = await get_all_settings(session)
                ctx["payment_settings_action"] = (
                    f"/settings?tab=payment&next={quote(next_base + '&settings=payment')}"
                )
                ctx["billing_settings_action"] = (
                    f"/settings?tab=billing&next={quote(next_base + '&settings=billing')}"
                )
                ctx["finance_pending_action"] = (
                    "/settings/cancel-pending-orders?next="
                    + quote(next_base + "&settings=payment")
                )
            else:
                rid = shop_owner_id(staff)
                if rid:
                    ctx["values"] = await get_all_settings(session, reseller_id=rid)
                    ctx["payment_settings_action"] = (
                        f"/shop-settings?tab=payment&next={quote(next_base + '&settings=payment')}"
                    )
                    ctx["can_billing_settings"] = False
                    ctx["can_pending_orders"] = True
                    ctx["finance_pending_action"] = (
                        "/shop-settings/cancel-pending-orders?next="
                        + quote(next_base + "&settings=payment")
                    )
                # else: leave values={} — fail-closed / degraded, no Owner fallback

        # Marker for scope tests: fetch_limit replaced by SQL pagination.
        fetch_limit = 0  # unused; kept as anchor for shop-settings scope tests
        page = parse_list_page(request.query_params.get("page"))
        page_size = DEFAULT_LIST_PAGE_SIZE

        if tab == "reports" and (can_orders or can_payments):
            from app.services.db_safe import recover_session
            from app.services.finance_reports import build_finance_report

            await recover_session(session)
            if is_platform_admin(staff):
                rid = None
            else:
                rid = shop_owner_id(staff)
                if not rid:
                    ctx["flash_err"] = ctx["flash_err"] or "محدوده فروشگاه مشخص نیست"
                    return render(request, "finance.html", ctx)
            ctx["report"] = await build_finance_report(
                session, reseller_id=rid, period=report_period
            )
            ctx["report_period"] = report_period

        elif tab == "behavior" and (can_orders or can_payments):
            from app.api.home_pages import _EMPTY_FUNNEL, _safe_funnel
            from app.services.db_safe import recover_session

            await recover_session(session)
            if is_platform_admin(staff):
                rid = None
            else:
                rid = shop_owner_id(staff)
                if not rid:
                    ctx["flash_err"] = ctx["flash_err"] or "محدوده فروشگاه مشخص نیست"
                    return render(request, "finance.html", ctx)
            ctx["funnel"] = await _safe_funnel(session, reseller_id=rid) or dict(
                _EMPTY_FUNNEL
            )

        elif tab == "orders" and can_orders:
            base_filters = []
            if is_platform_admin(staff):
                base_filters.append(Order.reseller_id.is_(None))
            else:
                rid = shop_owner_id(staff)
                if not rid:
                    ctx["flash_err"] = ctx["flash_err"] or "محدوده فروشگاه مشخص نیست"
                    return render(request, "finance.html", ctx)
                base_filters.append(Order.reseller_id == rid)

            search_pat = ilike_pattern(search_q.casefold()) if search_q else ""
            join_user_plan = bool(search_q)

            count_q = select(func.count()).select_from(Order)
            list_q = select(Order).options(
                selectinload(Order.plan), selectinload(Order.user)
            )
            if join_user_plan:
                count_q = count_q.outerjoin(BotUser, BotUser.id == Order.user_id).outerjoin(
                    Plan, Plan.id == Order.plan_id
                )
                list_q = list_q.outerjoin(BotUser, BotUser.id == Order.user_id).outerjoin(
                    Plan, Plan.id == Order.plan_id
                )
            for f in base_filters:
                count_q = count_q.where(f)
                list_q = list_q.where(f)
            if search_q:
                clause = _order_search_clause(search_pat)
                count_q = count_q.where(clause)
                list_q = list_q.where(clause)

            total = int((await session.scalar(count_q)) or 0)
            pager = build_list_pager(
                page=page, page_size=page_size, fetched=0, total=total
            )
            page = int(pager["page"])
            orders = list(
                (
                    await session.execute(
                        list_q.order_by(Order.id.desc())
                        .offset(list_offset(page, page_size))
                        .limit(page_size)
                    )
                )
                .scalars()
                .all()
            )
            pager = build_list_pager(
                page=page,
                page_size=page_size,
                fetched=len(orders),
                total=total,
            )
            payments_by_order: dict[int, Payment] = {}
            if orders:
                ids = [o.id for o in orders]
                pay_rows = (
                    await session.execute(
                        select(Payment)
                        .where(Payment.order_id.in_(ids))
                        .order_by(Payment.id.desc())
                    )
                ).scalars().all()
                for p in pay_rows:
                    if p.order_id is not None and p.order_id not in payments_by_order:
                        payments_by_order[p.order_id] = p
            ctx["orders"] = orders
            ctx["payments_by_order"] = payments_by_order
            ctx["pager"] = pager
            ctx["stuck_order_ids"] = {
                int(o.id)
                for o in orders
                if o.status in {"paid", "delivering"} and o.service_id is None
            }

        elif tab == "payments" and can_payments:
            search_pat = ilike_pattern(search_q.casefold()) if search_q else ""
            needle = search_q.casefold() if search_q else ""

            if is_platform_admin(staff):
                # Platform scope only — never list reseller-tenant wallet topups/orders.
                scope = or_(
                    and_(
                        Payment.is_wallet_topup.is_(True),
                        BotUser.reseller_id.is_(None),
                    ),
                    and_(
                        Payment.is_wallet_topup.is_(False),
                        Order.reseller_id.is_(None),
                    ),
                )
                count_q = (
                    select(func.count())
                    .select_from(Payment)
                    .outerjoin(Order, Order.id == Payment.order_id)
                    .outerjoin(BotUser, BotUser.id == Payment.user_id)
                    .where(scope)
                )
                list_q = (
                    select(Payment)
                    .outerjoin(Order, Order.id == Payment.order_id)
                    .outerjoin(BotUser, BotUser.id == Payment.user_id)
                    .where(scope)
                )
            else:
                rid = shop_owner_id(staff)
                if not rid:
                    ctx["flash_err"] = ctx["flash_err"] or "محدوده فروشگاه مشخص نیست"
                    return render(request, "finance.html", ctx)
                scope_filters = (
                    Order.reseller_id == rid,
                    Payment.is_wallet_topup.is_(False),
                )
                count_q = (
                    select(func.count())
                    .select_from(Payment)
                    .join(Order, Order.id == Payment.order_id)
                    .outerjoin(BotUser, BotUser.id == Payment.user_id)
                    .where(*scope_filters)
                )
                list_q = (
                    select(Payment)
                    .join(Order, Order.id == Payment.order_id)
                    .outerjoin(BotUser, BotUser.id == Payment.user_id)
                    .where(*scope_filters)
                )

            if search_q:
                clause = _payment_search_clause(search_pat, needle)
                count_q = count_q.where(clause)
                list_q = list_q.where(clause)

            total = int((await session.scalar(count_q)) or 0)
            pager = build_list_pager(
                page=page, page_size=page_size, fetched=0, total=total
            )
            page = int(pager["page"])
            payments = list(
                (
                    await session.execute(
                        list_q.order_by(Payment.id.desc())
                        .offset(list_offset(page, page_size))
                        .limit(page_size)
                    )
                )
                .scalars()
                .unique()
                .all()
            )
            pager = build_list_pager(
                page=page,
                page_size=page_size,
                fetched=len(payments),
                total=total,
            )
            payer_ids = {int(p.user_id) for p in payments if p.user_id}
            payers: dict[int, BotUser] = {}
            if payer_ids:
                payers = {
                    int(u.id): u
                    for u in (
                        await session.execute(select(BotUser).where(BotUser.id.in_(payer_ids)))
                    ).scalars().all()
                }
            ctx["payments"] = payments
            ctx["payers"] = payers
            ctx["pager"] = pager
            # Soft receipt match suggestions for pending payments
            from app.services.users import on as _on
            from app.services.ux20 import suggest_receipt_matches

            values = ctx.get("values") or {}
            if _on(values.get("receipt_auto_match_enabled", "1")):
                try:
                    window = int(values.get("receipt_match_window_minutes") or 120)
                except Exception:
                    window = 120
                rid_scope = None if is_platform_admin(staff) else shop_owner_id(staff)
                matches: dict[int, list] = {}
                for p in payments:
                    if p.status != "pending" or not p.receipt_file_id:
                        continue
                    try:
                        matches[int(p.id)] = await suggest_receipt_matches(
                            session,
                            payment=p,
                            window_minutes=window,
                            reseller_id=rid_scope,
                        )
                    except Exception:
                        from app.services.db_safe import rollback_quiet

                        await rollback_quiet(session)
                        continue
                ctx["receipt_matches"] = matches

        elif tab == "delivery" and can_orders:
            from app.services.db_safe import rollback_quiet
            from app.services.ux20 import (
                list_open_delivery_failures,
                list_stuck_paid_orders,
            )

            rid = None if is_platform_admin(staff) else shop_owner_id(staff)
            if not is_platform_admin(staff) and not rid:
                ctx["flash_err"] = ctx["flash_err"] or "محدوده فروشگاه مشخص نیست"
                return render(request, "finance.html", ctx)
            try:
                failures = await list_open_delivery_failures(session, reseller_id=rid)
                stuck = await list_stuck_paid_orders(session, reseller_id=rid, limit=100)
                failure_oids = {int(f.order_id) for f in failures}
                order_ids = list(failure_oids | {int(o.id) for o in stuck})
                orders_by_id: dict[int, Order] = {}
                if order_ids:
                    for o in (
                        await session.execute(
                            select(Order)
                            .options(selectinload(Order.user), selectinload(Order.plan))
                            .where(Order.id.in_(order_ids))
                        )
                    ).scalars().all():
                        orders_by_id[int(o.id)] = o
                for o in stuck:
                    orders_by_id.setdefault(int(o.id), o)
                ctx["delivery_failures"] = failures
                ctx["stuck_paid_orders"] = stuck
                ctx["stuck_order_ids"] = {int(o.id) for o in stuck}
                ctx["orders_by_id"] = orders_by_id
                ctx["stuck_only_order_ids"] = sorted(
                    {int(o.id) for o in stuck} - failure_oids, reverse=True
                )
            except Exception:
                logger.exception("delivery failures tab failed")
                await rollback_quiet(session)
                ctx["delivery_failures"] = []
                ctx["stuck_paid_orders"] = []
                ctx["stuck_order_ids"] = set()
                ctx["stuck_only_order_ids"] = []
                ctx["orders_by_id"] = {}

        return render(request, "finance.html", ctx)

    def _legacy_finance_redirect(tab: str):
        async def _redir(request: Request):
            params = {"tab": tab}
            for k in ("q", "ok", "err", "saved", "settings"):
                v = request.query_params.get(k)
                if v:
                    params[k] = v
            return RedirectResponse(f"/finance?{urlencode(params)}", status_code=303)

        return _redir

    app.add_api_route("/orders", _legacy_finance_redirect("orders"), methods=["GET"], response_class=HTMLResponse)
    app.add_api_route("/payments", _legacy_finance_redirect("payments"), methods=["GET"], response_class=HTMLResponse)

    @app.get("/payments/{payment_id}/receipt")
    async def payment_receipt(
        payment_id: int,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ):
        authz = authz_from_staff(staff)
        if not can_shop(authz, "payments"):
            return RedirectResponse("/home", status_code=303)
        payment = await session.get(Payment, int(payment_id))
        if not payment:
            return Response(status_code=404, content="رسید یافت نشد")
        file_id = (payment.receipt_file_id or "").strip()
        if not file_id or file_id.startswith("stars:"):
            return Response(status_code=404, content="رسید تصویری نیست")

        scoped = await _authorize_receipt_access(session, staff, payment)
        if scoped is None:
            return Response(status_code=403, content="دسترسی ندارید")
        order, payer_reseller_id = scoped

        # Mini App private uploads (local:) — never fetch from Telegram.
        if file_id.startswith("local:"):
            from app.services.receipt_uploads import resolve_local_receipt_path

            path = resolve_local_receipt_path(file_id)
            if not path:
                return Response(status_code=404, content="رسید یافت نشد")
            body = path.read_bytes()
            ctype = "image/jpeg"
            suf = path.suffix.lower()
            if suf == ".png":
                ctype = "image/png"
            elif suf == ".webp":
                ctype = "image/webp"
            elif suf == ".gif":
                ctype = "image/gif"
            return Response(
                content=body,
                media_type=ctype,
                headers={
                    "Cache-Control": "private, no-store",
                    "Content-Disposition": f'inline; filename="receipt-{payment_id}"',
                    "X-Content-Type-Options": "nosniff",
                },
            )

        token = await _payment_bot_token(
            session,
            payment,
            order=order,
            payer_reseller_id=payer_reseller_id,
        )
        if not token:
            return Response(status_code=503, content="توکن ربات تنظیم نشده")
        try:
            fetched = await _fetch_telegram_file(token, file_id)
        except Exception:
            logger.exception("receipt getFile failed payment_id=%s", payment_id)
            fetched = None
        if not fetched:
            return Response(status_code=502, content="دریافت رسید از تلگرام ناموفق بود")
        body, ctype = fetched
        return Response(
            content=body,
            media_type=ctype,
            headers={
                "Cache-Control": "private, no-store",
                "Content-Disposition": f'inline; filename="receipt-{payment_id}"',
                "X-Content-Type-Options": "nosniff",
            },
        )
