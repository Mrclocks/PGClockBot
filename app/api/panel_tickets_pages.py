"""Internal panel ticketing pages: reseller / pg_staff ↔ platform admin."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Ticket
from app.services.panel_tickets import (
    PRIORITY_BADGE,
    PRIORITY_LABELS,
    STATUS_BADGE,
    STATUS_LABELS,
    can_access_panel_tickets,
    create_ticket,
    get_ticket,
    list_tickets,
    mark_viewed,
    reply_ticket,
    resolve_ticket_attachment_path,
    save_ticket_attachment,
    set_status,
    sidebar_unread_count,
    unread_from_tickets,
)
from app.services.shop_scope import is_platform_admin, shop_owner_id

_OK_FLASH = {
    "created": "تیکت ثبت شد",
    "replied": "پاسخ ثبت شد",
    "status": "وضعیت تیکت به‌روز شد",
}


def register_panel_tickets_pages(app: FastAPI, *, render, require_staff, get_db) -> None:
    @app.get("/tickets", response_class=HTMLResponse)
    async def tickets_page(
        request: Request,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
        view: int | None = None,
        new: int | None = None,
    ):
        if not can_access_panel_tickets(staff):
            return RedirectResponse("/home", status_code=303)

        support_tab = (request.query_params.get("tab") or "tickets").strip()
        if support_tab not in {"tickets", "settings"}:
            support_tab = "tickets"

        # Bot support contacts config: admin or reseller with shop_settings
        perms = staff.get("permissions") or []
        can_support_settings = staff.get("role") == "admin" or (
            staff.get("role") == "reseller" and "shop_settings" in perms
        )
        if support_tab == "settings" and not can_support_settings:
            return RedirectResponse("/tickets?tab=tickets", status_code=303)

        if support_tab == "settings":
            from app.services.support_contacts import get_support_contacts
            from app.services.users import SETTING_GROUPS, get_all_settings

            rid = None
            if staff.get("role") == "reseller":
                rid = shop_owner_id(staff)
            values = await get_all_settings(session, reseller_id=rid)
            support_contacts = await get_support_contacts(session, reseller_id=rid)
            fields = SETTING_GROUPS.get("متن پشتیبانی") or []
            ok_key = (request.query_params.get("ok") or "").strip()
            flash_ok = _OK_FLASH.get(ok_key, ok_key or None)
            if request.query_params.get("saved") == "1":
                flash_ok = request.query_params.get("msg") or "ذخیره شد."
            return render(
                request,
                "tickets.html",
                {
                    "staff": staff,
                    "support_tab": "settings",
                    "can_support_settings": can_support_settings,
                    "support_contacts": support_contacts,
                    "values": values,
                    "support_text_fields": fields,
                    "supports_action": "/shop-supports/save"
                    if staff.get("role") == "reseller"
                    else "/supports/save",
                    "supports_delete_action": "/shop-supports/delete"
                    if staff.get("role") == "reseller"
                    else "/supports/delete",
                    "support_text_action": "/shop-settings?tab=supports"
                    if staff.get("role") == "reseller"
                    else "/settings?tab=supports",
                    "panel_tickets": [],
                    "tg_tickets": [],
                    "show_tg": False,
                    "active_ticket": None,
                    "open_new": False,
                    "status_labels": STATUS_LABELS,
                    "priority_labels": PRIORITY_LABELS,
                    "status_badge": STATUS_BADGE,
                    "priority_badge": PRIORITY_BADGE,
                    "is_owner": is_platform_admin(staff),
                    "can_create": False,
                    "flash_ok": flash_ok,
                    "flash_err": request.query_params.get("err"),
                    "tickets_unread": getattr(request.state, "panel_tickets_unread", 0) or 0,
                },
            )

        panel_tickets = await list_tickets(session, staff, limit=150)
        tg_tickets: list[Ticket] = []
        show_tg = False
        if is_platform_admin(staff):
            show_tg = True
            tg_tickets = list(
                (
                    await session.execute(
                        select(Ticket)
                        .join(BotUser, BotUser.id == Ticket.user_id)
                        .where(
                            Ticket.reseller_id.is_(None),
                            BotUser.reseller_id.is_(None),
                        )
                        .order_by(Ticket.id.desc())
                        .limit(100)
                    )
                )
                .scalars()
                .all()
            )
        elif staff.get("role") == "reseller" and "tickets" in (staff.get("permissions") or []):
            show_tg = True
            rid = shop_owner_id(staff)
            if rid:
                from sqlalchemy import or_

                tg_tickets = list(
                    (
                        await session.execute(
                            select(Ticket)
                            .outerjoin(BotUser, BotUser.id == Ticket.user_id)
                            .where(
                                or_(
                                    Ticket.reseller_id == rid,
                                    (Ticket.reseller_id.is_(None))
                                    & (BotUser.reseller_id == rid),
                                )
                            )
                            .order_by(Ticket.id.desc())
                            .limit(100)
                        )
                    )
                    .scalars()
                    .all()
                )

        active_ticket = None
        if view is not None:
            active_ticket = await get_ticket(session, staff, int(view))
            if active_ticket is not None:
                changed = await mark_viewed(session, staff, active_ticket)
                if changed:
                    # Sync list-row flags in memory — no second list query
                    for row in panel_tickets:
                        if row.id == active_ticket.id:
                            row.answered_unread = active_ticket.answered_unread
                            row.owner_unread = active_ticket.owner_unread
                            break

        # Derive from the list we already loaded (middleware skips COUNT on /tickets)
        tickets_unread = unread_from_tickets(panel_tickets, staff)
        request.state.panel_tickets_unread = tickets_unread

        ok_key = (request.query_params.get("ok") or "").strip()
        flash_ok = _OK_FLASH.get(ok_key, ok_key or None)

        return render(
            request,
            "tickets.html",
            {
                "staff": staff,
                "support_tab": "tickets",
                "can_support_settings": can_support_settings,
                "panel_tickets": panel_tickets,
                "tg_tickets": tg_tickets,
                "show_tg": show_tg,
                "active_ticket": active_ticket,
                "open_new": bool(new),
                "status_labels": STATUS_LABELS,
                "priority_labels": PRIORITY_LABELS,
                "status_badge": STATUS_BADGE,
                "priority_badge": PRIORITY_BADGE,
                "is_owner": is_platform_admin(staff),
                "can_create": staff.get("role") in {"reseller", "pg_staff"},
                "flash_ok": flash_ok,
                "flash_err": request.query_params.get("err"),
                "tickets_unread": tickets_unread,
            },
        )

    @app.post("/tickets/panel/create")
    async def tickets_panel_create(
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
        subject: str = Form(...),
        body: str = Form(""),
        priority: str = Form("normal"),
        attachment: UploadFile | None = File(None),
    ):
        if staff.get("role") not in {"reseller", "pg_staff"}:
            return RedirectResponse(
                "/tickets?err=" + quote("فقط نماینده / ادمین فرعی می‌تواند تیکت بسازد"),
                status_code=303,
            )
        try:
            att = await save_ticket_attachment(attachment)
            ticket = await create_ticket(
                session,
                staff,
                subject=subject,
                body=body,
                priority=priority,
                attachment=att,
            )
            return RedirectResponse(f"/tickets?ok=created&view={ticket.id}", status_code=303)
        except Exception as exc:
            return RedirectResponse(
                f"/tickets?err={quote(str(exc))}&new=1",
                status_code=303,
            )

    @app.get("/tickets/panel/{ticket_id}/attachment/{message_id}")
    async def tickets_panel_attachment(
        ticket_id: int,
        message_id: int,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ):
        if not can_access_panel_tickets(staff):
            raise HTTPException(403, "forbidden")
        ticket = await get_ticket(session, staff, ticket_id)
        if not ticket:
            raise HTTPException(404, "not found")
        msg = next((m for m in (ticket.messages or []) if int(m.id) == int(message_id)), None)
        if not msg or not msg.attachment_path:
            raise HTTPException(404, "not found")
        path = resolve_ticket_attachment_path(msg.attachment_path)
        if not path:
            raise HTTPException(404, "not found")
        return FileResponse(
            path,
            filename=msg.attachment_name or path.name,
            media_type=msg.attachment_mime or "application/octet-stream",
            content_disposition_type="attachment",
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/tickets/panel/{ticket_id}/reply")
    async def tickets_panel_reply(
        ticket_id: int,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
        body: str = Form(""),
        attachment: UploadFile | None = File(None),
    ):
        if not can_access_panel_tickets(staff):
            return RedirectResponse("/tickets?err=" + quote("دسترسی ندارید"), status_code=303)
        # Authorize ticket access before writing any bytes to disk
        existing = await get_ticket(session, staff, ticket_id)
        if not existing:
            return RedirectResponse("/tickets?err=" + quote("تیکت یافت نشد"), status_code=303)
        att = None
        try:
            att = await save_ticket_attachment(attachment, ticket_id=ticket_id)
            await reply_ticket(session, staff, ticket_id, body=body, attachment=att)
            return RedirectResponse(f"/tickets?ok=replied&view={ticket_id}", status_code=303)
        except Exception as exc:
            if att and att[0]:
                path = resolve_ticket_attachment_path(att[0])
                if path:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
            return RedirectResponse(
                f"/tickets?err={quote(str(exc))}&view={ticket_id}",
                status_code=303,
            )

    @app.post("/tickets/panel/{ticket_id}/status")
    async def tickets_panel_status(
        ticket_id: int,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
        status: str = Form(...),
    ):
        if not can_access_panel_tickets(staff):
            return RedirectResponse("/tickets?err=" + quote("دسترسی ندارید"), status_code=303)
        try:
            await set_status(session, staff, ticket_id, status=status)
            return RedirectResponse(f"/tickets?ok=status&view={ticket_id}", status_code=303)
        except Exception as exc:
            return RedirectResponse(
                f"/tickets?err={quote(str(exc))}&view={ticket_id}",
                status_code=303,
            )


async def panel_ticket_dashboard_alert(
    session: AsyncSession,
    staff: dict,
    *,
    unread: int | None = None,
) -> dict | None:
    """Banner data for dashboards — links to list only (no auto-open modal).

    Pass ``unread`` from ``request.state.panel_tickets_unread`` to avoid a second COUNT.
    """
    if unread is None:
        n = await sidebar_unread_count(session, staff)
    else:
        n = int(unread or 0)
    if n <= 0:
        return None

    if is_platform_admin(staff):
        return {
            "title": f"{n} تیکت خوانده‌نشده" if n > 1 else "یک تیکت خوانده‌نشده",
            "detail": "نماینده یا ادمین فرعی پیام جدیدی فرستاده است.",
            "href": "/tickets",
        }

    if n == 1:
        title = "پاسخ جدید برای تیکت پشتیبانی"
        detail = "ادمین اصلی به تیکت شما پاسخ داده است."
    else:
        title = f"{n} تیکت پاسخ‌داده‌شده دارید"
        detail = "پاسخ‌های جدید در صفحه پشتیبانی منتظر مشاهده‌اند."
    return {"title": title, "detail": detail, "href": "/tickets"}
