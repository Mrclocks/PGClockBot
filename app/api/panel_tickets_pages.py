"""Internal panel ticketing pages: reseller / pg_staff ↔ platform admin."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Ticket
from app.services.ticket_scope import staff_ticket_in_scope, ticket_shop_clause
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
import logging

logger = logging.getLogger(__name__)

_OK_FLASH = {
    "created": "تیکت ثبت شد",
    "replied": "پاسخ ثبت شد",
    "status": "وضعیت تیکت به‌روز شد",
    "tg_replied": "پاسخ به کاربر در تلگرام ارسال شد",
    "tg_closed": "تیکت کاربر بسته شد",
}


def _staff_can_manage_bot_tickets(staff: dict) -> bool:
    if is_platform_admin(staff):
        return True
    return staff.get("role") == "reseller" and "tickets" in (staff.get("permissions") or [])


async def _bot_ticket_in_scope(session: AsyncSession, staff: dict, ticket: Ticket) -> bool:
    """Tenant isolation for Telegram Ticket rows."""
    return staff_ticket_in_scope(staff, ticket)


def _bot_user_label(user: BotUser | None, user_id: int) -> str:
    from app.services.formatting import bot_user_panel_label

    return bot_user_panel_label(user, fallback_id=user_id)


async def _staff_sender_tg(session: AsyncSession, staff: dict) -> int:
    uid = staff.get("bot_user_id")
    if not uid:
        return 0
    try:
        row = await session.get(BotUser, int(uid))
    except Exception:
        return 0
    if row and row.telegram_id:
        return int(row.telegram_id)
    return 0


async def _notify_bot_ticket_staff_reply(
    session: AsyncSession,
    ticket: Ticket,
    body: str,
    *,
    actor_name: str | None = None,
) -> None:
    """Send staff reply to the Telegram user (same path as in-bot ticket reply)."""
    from app.bot import create_bot
    from app.services.notifications import notify_ticket_message

    bot = create_bot()
    try:
        await notify_ticket_message(
            bot,
            session,
            ticket_id=int(ticket.id),
            subject=ticket.subject,
            body=body,
            from_staff=True,
            ticket_user_id=int(ticket.user_id),
            actor_name=actor_name,
            ticket_reseller_id=ticket.reseller_id,
        )
    finally:
        try:
            await bot.session.close()
        except Exception:
            logger.debug("_notify_bot_ticket_staff_reply: ignored failure", exc_info=True)


def register_panel_tickets_pages(app: FastAPI, *, render, require_staff, get_db) -> None:
    @app.get("/tickets", response_class=HTMLResponse)
    async def tickets_page(
        request: Request,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
        view: int | None = None,
        tg: int | None = None,
        new: int | None = None,
    ):
        if not can_access_panel_tickets(staff):
            return RedirectResponse("/home", status_code=303)

        from app.services import tickets as bot_tickets

        panel_tickets = await list_tickets(session, staff, limit=150)
        tg_tickets: list[Ticket] = []
        tg_users: dict[int, BotUser] = {}
        show_tg = _staff_can_manage_bot_tickets(staff)
        if show_tg:
            if is_platform_admin(staff):
                tg_tickets = list(
                    (
                        await session.execute(
                            select(Ticket)
                            .where(ticket_shop_clause(None))
                            .order_by(Ticket.id.desc())
                            .limit(100)
                        )
                    )
                    .scalars()
                    .all()
                )
            else:
                rid = shop_owner_id(staff)
                if rid:
                    tg_tickets = list(
                        (
                            await session.execute(
                                select(Ticket)
                                .where(ticket_shop_clause(rid))
                                .order_by(Ticket.id.desc())
                                .limit(100)
                            )
                        )
                        .scalars()
                        .all()
                    )
            uids = {int(t.user_id) for t in tg_tickets if t.user_id}
            if uids:
                rows = (
                    await session.execute(select(BotUser).where(BotUser.id.in_(uids)))
                ).scalars().all()
                tg_users = {int(u.id): u for u in rows}

        active_ticket = None
        if view is not None:
            active_ticket = await get_ticket(session, staff, int(view))
            if active_ticket is not None:
                changed = await mark_viewed(session, staff, active_ticket)
                if changed:
                    for row in panel_tickets:
                        if row.id == active_ticket.id:
                            row.answered_unread = active_ticket.answered_unread
                            row.owner_unread = active_ticket.owner_unread
                            break

        active_tg_ticket = None
        active_tg_user = None
        if tg is not None and show_tg:
            active_tg_ticket = await bot_tickets.get_ticket(session, int(tg))
            if active_tg_ticket is not None:
                if not await _bot_ticket_in_scope(session, staff, active_tg_ticket):
                    active_tg_ticket = None
                else:
                    active_tg_user = await session.get(BotUser, int(active_tg_ticket.user_id))

        tickets_unread = unread_from_tickets(panel_tickets, staff)
        request.state.panel_tickets_unread = tickets_unread

        ok_key = (request.query_params.get("ok") or "").strip()
        flash_ok = _OK_FLASH.get(ok_key, ok_key or None)
        if request.query_params.get("saved") == "1" and not flash_ok:
            flash_ok = "ذخیره شد."

        open_supports = request.query_params.get("supports") in {"1", "true", "yes"}
        supports_modal_tab = (request.query_params.get("stab") or "contacts").strip()
        if supports_modal_tab not in {"contacts", "text"}:
            supports_modal_tab = "contacts"

        can_manage_supports = staff.get("role") == "admin" or (
            staff.get("role") == "reseller" and "shop_settings" in (staff.get("permissions") or [])
        )

        tg_rows = [
            {
                "ticket": t,
                "user_id": int(t.user_id),
                "user_label": _bot_user_label(tg_users.get(int(t.user_id)), int(t.user_id)),
            }
            for t in tg_tickets
        ]

        ctx = {
            "staff": staff,
            "panel_tickets": panel_tickets,
            "tg_tickets": tg_tickets,
            "tg_rows": tg_rows,
            "show_tg": show_tg,
            "active_ticket": active_ticket,
            "active_tg_ticket": active_tg_ticket,
            "active_tg_user": active_tg_user,
            "active_tg_user_label": _bot_user_label(
                active_tg_user, int(active_tg_ticket.user_id) if active_tg_ticket else 0
            ),
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
            "can_manage_supports": can_manage_supports,
            "open_supports_modal": open_supports and can_manage_supports,
            "supports_modal_tab": supports_modal_tab,
            "support_contacts": [],
            "support_contacts_json": "[]",
            "values": {},
            "support_text_tab_groups": [],
            "support_text_groups": {},
            "supports_save_action": "/supports/save",
            "supports_delete_action": "/supports/delete",
            "support_text_action": (
                "/settings?tab=supports&next="
                + quote("/tickets?supports=1&stab=text")
            ),
        }

        if can_manage_supports:
            from app.services.button_styles import item_button_style_options
            from app.services.support_contacts import dump_support_contacts, get_support_contacts
            from app.services.users import SETTING_GROUPS, TAB_SETTING_GROUPS, get_all_settings

            ctx["item_style_options"] = item_button_style_options(
                inherit_label="ارث از پشتیبانی"
            )

            # Owner settings only for real Owner. Missing shop scope → empty, never Owner fallback.
            if is_platform_admin(staff):
                ctx["support_contacts"] = await get_support_contacts(session, reseller_id=None)
                ctx["support_contacts_json"] = dump_support_contacts(ctx["support_contacts"])
                values = await get_all_settings(session)
                ctx["values"] = values
                names = TAB_SETTING_GROUPS.get("supports") or []
                ctx["support_text_tab_groups"] = names
                ctx["support_text_groups"] = {
                    n: SETTING_GROUPS[n] for n in names if n in SETTING_GROUPS
                }
            else:
                rid = shop_owner_id(staff)
                if rid:
                    ctx["support_contacts"] = await get_support_contacts(
                        session, reseller_id=rid
                    )
                    ctx["support_contacts_json"] = dump_support_contacts(ctx["support_contacts"])
                    values = await get_all_settings(session, reseller_id=rid)
                    ctx["values"] = values
                    names = TAB_SETTING_GROUPS.get("supports") or []
                    ctx["support_text_tab_groups"] = names
                    ctx["support_text_groups"] = {
                        n: SETTING_GROUPS[n] for n in names if n in SETTING_GROUPS
                    }
                    ctx["supports_save_action"] = "/shop-supports/save"
                    ctx["supports_delete_action"] = "/shop-supports/delete"
                    ctx["support_text_action"] = (
                        "/shop-settings?tab=supports&next="
                        + quote("/tickets?supports=1&stab=text")
                    )
                # else: leave empty defaults — fail-closed

        return render(request, "tickets.html", ctx)

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

    @app.post("/tickets/bot/{ticket_id}/reply")
    async def tickets_bot_reply(
        ticket_id: int,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
        body: str = Form(""),
    ):
        from app.services import tickets as bot_tickets

        if not _staff_can_manage_bot_tickets(staff):
            return RedirectResponse("/tickets?err=" + quote("دسترسی ندارید"), status_code=303)
        text = (body or "").strip()
        if not text:
            return RedirectResponse(
                f"/tickets?err={quote('متن پاسخ خالی است')}&tg={ticket_id}",
                status_code=303,
            )
        ticket = await bot_tickets.get_ticket(session, int(ticket_id))
        if not ticket or not await _bot_ticket_in_scope(session, staff, ticket):
            return RedirectResponse("/tickets?err=" + quote("تیکت یافت نشد"), status_code=303)
        try:
            sender_tg = await _staff_sender_tg(session, staff)
            await bot_tickets.reply_ticket(
                session, ticket, text, sender_tg, is_staff=True
            )
        except Exception as exc:
            return RedirectResponse(
                f"/tickets?err={quote(str(exc))}&tg={ticket_id}",
                status_code=303,
            )
        try:
            await _notify_bot_ticket_staff_reply(
                session,
                ticket,
                text,
                actor_name=staff.get("username") or staff.get("role"),
            )
        except Exception:
            # Reply is persisted; Telegram delivery failure should not roll back UX.
            pass
        return RedirectResponse(
            f"/tickets?ok=tg_replied&tg={ticket_id}", status_code=303
        )

    @app.post("/tickets/bot/{ticket_id}/close")
    async def tickets_bot_close(
        ticket_id: int,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services import tickets as bot_tickets

        if not _staff_can_manage_bot_tickets(staff):
            return RedirectResponse("/tickets?err=" + quote("دسترسی ندارید"), status_code=303)
        ticket = await bot_tickets.get_ticket(session, int(ticket_id))
        if not ticket or not await _bot_ticket_in_scope(session, staff, ticket):
            return RedirectResponse("/tickets?err=" + quote("تیکت یافت نشد"), status_code=303)
        try:
            await bot_tickets.close_ticket(session, ticket)
            return RedirectResponse(
                f"/tickets?ok=tg_closed&tg={ticket_id}", status_code=303
            )
        except Exception as exc:
            return RedirectResponse(
                f"/tickets?err={quote(str(exc))}&tg={ticket_id}",
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
