"""Owner settings saved from the panel: notification preferences and the bot's support contacts.

Registered from ``create_api_app`` at the point its routes always had, so route order is unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def register_support_settings_pages(  # noqa: C901 - counts the nested routes; each is listed on its own
    app: object,
    *,
    get_db: Callable[..., object],
    require_admin: Callable[..., object],
) -> None:
    @app.get("/notifications", response_class=HTMLResponse)
    async def notifications_page(staff: dict = Depends(require_admin)):
        return RedirectResponse("/settings?tab=notifications", status_code=303)

    @app.post("/notifications")
    async def notifications_save(
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.notifications import save_notify_prefs

        form = await request.form()
        await save_notify_prefs(session, dict(form))
        return RedirectResponse("/settings?tab=notifications&saved=1", status_code=303)

    @app.post("/supports/save")
    async def supports_save(
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        from urllib.parse import quote

        from app.services.support_contacts import contacts_from_form_json, save_support_contacts

        form = await request.form()
        raw = form.get("support_contacts")
        if raw is not None:
            contacts, err = contacts_from_form_json(str(raw))
            if err:
                return RedirectResponse(
                    f"/tickets?supports=1&stab=contacts&err={quote(err)}",
                    status_code=303,
                )
            await save_support_contacts(session, contacts or [])
            return RedirectResponse("/tickets?supports=1&stab=contacts&saved=1", status_code=303)

        # Legacy single-row upsert (bot / older clients)
        from app.services.button_styles import parse_item_button_style_form
        from app.services.support_contacts import upsert_support_contact

        contact_id = str(form.get("id") or "").strip() or None
        title = str(form.get("title") or "").strip()
        telegram = str(form.get("telegram") or "").strip()
        try:
            sort = int(str(form.get("sort") or "0").strip() or "0")
        except ValueError:
            sort = 0
        enabled = str(form.get("enabled") or "") in {"1", "on", "true", "yes"}
        if contact_id and "enabled" not in form:
            enabled = False
        button_style = parse_item_button_style_form(form)
        _, err = await upsert_support_contact(
            session,
            contact_id=contact_id,
            title=title,
            telegram=telegram,
            sort=sort,
            enabled=enabled,
            button_style=button_style,
        )
        if err:
            return RedirectResponse(
                f"/tickets?supports=1&stab=contacts&err={quote(err)}",
                status_code=303,
            )
        return RedirectResponse("/tickets?supports=1&stab=contacts&saved=1", status_code=303)

    @app.post("/supports/delete")
    async def supports_delete(
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.support_contacts import delete_support_contact

        form = await request.form()
        contact_id = str(form.get("id") or "").strip()
        if contact_id:
            await delete_support_contact(session, contact_id)
        return RedirectResponse("/tickets?supports=1&stab=contacts&saved=1", status_code=303)
