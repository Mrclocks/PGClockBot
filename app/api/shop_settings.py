"""Reseller-facing shop bot settings (web panel subset)."""

from __future__ import annotations

import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from app.config import DATA_DIR
from app.db.models import ResellerProfile
from app.services.resellers import RESELLER_SETTINGS_TABS, complete_reseller_setup
from app.services.users import (
    IMAGE_KEYS,
    TAB_SETTING_GROUPS,
    TOGGLE_KEYS,
    SETTING_GROUPS,
    SETTINGS_TAB_ALIASES,
    get_all_settings,
    keys_for_tab,
)


def register_shop_settings(app, *, render, require_staff, get_db, require_shop_settings=None):
    allowed_tabs = {t[0] for t in RESELLER_SETTINGS_TABS}
    shop_dep = require_shop_settings or require_staff

    def _menu_tab_context(values: dict) -> dict:
        from app.bot.keyboards import DEFAULT_MENU_ORDER

        order_raw = values.get("menu_order") or ",".join(DEFAULT_MENU_ORDER)
        order = [p.strip() for p in order_raw.split(",") if p.strip()]
        order = [k for k in order if k in DEFAULT_MENU_ORDER and k != "reseller_apply"]
        if "shop" not in order:
            order.insert(0, "shop")
        catalog_meta = {
            "shop": {"label": "خرید سرویس", "required": True},
            "services": {"label": "سرویس‌های من", "required": False},
            "wallet": {"label": "کیف پول", "required": False},
            "support": {"label": "پشتیبانی", "required": False},
            "referral": {"label": "دعوت دوستان", "required": False},
        }
        items = []
        for key in order:
            meta = catalog_meta.get(key)
            if not meta:
                continue
            items.append(
                {
                    "key": key,
                    "label": meta["label"],
                    "btn": values.get(f"btn_{key}", meta["label"]),
                    "required": meta["required"],
                }
            )
        pool = []
        for key, meta in catalog_meta.items():
            if key not in order and key != "shop":
                pool.append(
                    {
                        "key": key,
                        "label": meta["label"],
                        "btn": values.get(f"btn_{key}", meta["label"]),
                        "required": False,
                    }
                )
        return {"items": items, "pool": pool, "order_csv": ",".join(order)}

    async def _bot_token_status(token: str) -> dict:
        token = (token or "").strip()
        if not token:
            return {"ok": False, "error": "توکن تنظیم نشده"}
        try:
            import httpx

            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(f"https://api.telegram.org/bot{token}/getMe")
                data = resp.json()
            if data.get("ok") and isinstance(data.get("result"), dict):
                me = data["result"]
                return {
                    "ok": True,
                    "username": me.get("username"),
                    "id": me.get("id"),
                    "name": me.get("first_name"),
                }
            return {"ok": False, "error": data.get("description") or "توکن نامعتبر"}
        except Exception as exc:
            return {"ok": False, "error": f"عدم اتصال به تلگرام: {exc}"}

    def _rid(staff: dict) -> int | None:
        from app.services.shop_scope import shop_owner_id

        return shop_owner_id(staff)

    def _deny_scope() -> RedirectResponse:
        return RedirectResponse(
            "/dashboard?err=" + quote("محدوده فروشگاه مشخص نیست — به داده ادمین اصلی دسترسی ندارید"),
            status_code=303,
        )

    async def _load_profile(session: AsyncSession, user_id: int) -> ResellerProfile | None:
        from sqlalchemy import select

        result = await session.execute(
            select(ResellerProfile).where(ResellerProfile.user_id == user_id)
        )
        return result.scalar_one_or_none()

    @app.get("/shop-settings", response_class=HTMLResponse)
    async def shop_settings_page(
        request: Request,
        staff: dict = Depends(shop_dep),
        session: AsyncSession = Depends(get_db),
    ):
        if staff.get("role") == "admin":
            return RedirectResponse("/settings", status_code=303)
        if staff.get("role") != "reseller":
            return RedirectResponse("/login", status_code=303)

        tab = (request.query_params.get("tab") or "messages").strip()
        if tab == "security":
            return RedirectResponse("/security", status_code=303)
        if tab in SETTINGS_TAB_ALIASES:
            return RedirectResponse(
                f"/shop-settings?tab={SETTINGS_TAB_ALIASES[tab]}", status_code=303
            )
        if tab not in allowed_tabs:
            tab = "messages"

        rid = _rid(staff)
        if not rid:
            return _deny_scope()
        values = await get_all_settings(session, reseller_id=rid)
        values["show_reseller_apply"] = "0"
        tab_groups = TAB_SETTING_GROUPS.get(tab, [])
        # Hide platform-only fields from reseller menu tab
        groups = {name: SETTING_GROUPS[name] for name in tab_groups if name in SETTING_GROUPS}
        if tab == "menu":
            for name, fields in list(groups.items()):
                groups[name] = [f for f in fields if f[0] != "show_reseller_apply"]
        if tab == "messages":
            # Shop bots don't need platform-admin / apply button labels
            _shop_btn_block = {
                "btn_admin",
                "btn_adm_orders",
                "btn_adm_payments",
                "btn_adm_tickets",
                "btn_adm_plans",
                "btn_adm_pg",
                "btn_adm_preview",
                "btn_reseller_apply",
                "btn_reseller_creds",
            }
            for name, fields in list(groups.items()):
                groups[name] = [f for f in fields if f[0] not in _shop_btn_block]

        profile = await _load_profile(session, rid)
        ctx: dict = {
            "staff": staff,
            "values": values,
            "groups": groups,
            "tab_groups": list(groups.keys()),
            "tabs": RESELLER_SETTINGS_TABS,
            "tab": tab,
            "saved": request.query_params.get("saved") == "1",
            "saved_msg": request.query_params.get("msg") or "",
            "shop_mode": True,
            "settings_base": "/shop-settings",
            "profile": profile,
        }
        # Single flash above page title (base.html)
        if request.query_params.get("saved") == "1":
            ctx["flash_ok"] = request.query_params.get("msg") or "ذخیره شد."
        if request.query_params.get("err"):
            ctx["flash_err"] = request.query_params.get("err")

        if tab == "menu":
            ctx.update(_menu_tab_context(values))
        elif tab == "bot":
            token = (profile.bot_token if profile else "") or ""
            ctx["bot_status"] = await _bot_token_status(token)
            ctx["bot_token_masked"] = ("••••" + token[-6:]) if len(token) > 8 else ("••••" if token else "")
            ctx["bot_username"] = (profile.bot_username if profile else "") or ""
        elif tab == "appearance":
            from app.services.bot_appearance import load_appearance_context

            token = (profile.bot_token if profile else "") or ""
            uname = (profile.bot_username if profile else "") or ""
            ctx["bot_username"] = uname
            ctx.update(
                await load_appearance_context(
                    session,
                    token=token,
                    reseller_id=rid,
                    fallback_username=uname,
                )
            )
        elif tab == "supports":
            from app.services.support_contacts import get_support_contacts

            ctx["support_contacts"] = await get_support_contacts(session, reseller_id=rid)
        elif tab == "notifications":
            from app.services.authz import resolve_shop_permissions_from_profile
            from app.services.notifications import (
                get_shop_notify_prefs,
                shop_notify_catalog,
            )

            perms = list(staff.get("permissions") or []) or list(
                resolve_shop_permissions_from_profile(profile) or []
            )
            ctx["notify_items"] = shop_notify_catalog(perms)
            ctx["notify_prefs"] = await get_shop_notify_prefs(session, rid)

        return render(request, "shop_settings.html", ctx)

    @app.post("/shop-settings")
    async def shop_settings_save(
        request: Request,
        staff: dict = Depends(shop_dep),
        session: AsyncSession = Depends(get_db),
    ):
        if staff.get("role") != "reseller":
            return RedirectResponse("/settings", status_code=303)

        rid = _rid(staff)
        if not rid:
            return _deny_scope()
        tab = (request.query_params.get("tab") or "messages").strip()
        tab = SETTINGS_TAB_ALIASES.get(tab, tab)
        if tab not in allowed_tabs:
            return RedirectResponse("/shop-settings", status_code=303)

        form = await request.form()
        profile = await _load_profile(session, rid)
        if not profile:
            return RedirectResponse("/logout", status_code=303)

        if tab == "bot":
            token = str(form.get("BOT_TOKEN") or "").strip()
            if not token:
                return RedirectResponse(
                    "/shop-settings?tab=bot&err=" + quote("توکن ربات الزامی است"),
                    status_code=303,
                )
            try:
                from aiogram import Bot

                bot = Bot(token=token)
                try:
                    me = await bot.get_me()
                    uname = me.username or str(me.id)
                    tg_id = int(me.id)
                finally:
                    await bot.session.close()
                await complete_reseller_setup(
                    session,
                    profile,
                    bot_token=token,
                    bot_username=uname,
                    bot_telegram_id=tg_id,
                    bot_only=True,
                )
                from app.services.reseller_bots import start_reseller_bot_for_profile

                await start_reseller_bot_for_profile(profile.id)
            except ValueError as e:
                return RedirectResponse(
                    "/shop-settings?tab=bot&err=" + quote(str(e)),
                    status_code=303,
                )
            except Exception:
                return RedirectResponse(
                    "/shop-settings?tab=bot&err=" + quote("توکن ربات نامعتبر است"),
                    status_code=303,
                )
            return RedirectResponse("/shop-settings?tab=bot&saved=1", status_code=303)

        if tab == "appearance":
            from app.services.bot_appearance import save_appearance_from_form

            token = (profile.bot_token or "").strip()
            if not token:
                return RedirectResponse(
                    "/shop-settings?tab=appearance&err="
                    + quote("ابتدا توکن ربات اختصاصی را در تب «ربات اختصاصی» ذخیره کنید"),
                    status_code=303,
                )
            ok, msg = await save_appearance_from_form(
                session,
                form,
                token=token,
                reseller_id=rid,
                upload_prefix=f"r{rid}",
            )
            if not ok:
                return RedirectResponse(
                    "/shop-settings?tab=appearance&err=" + quote(msg),
                    status_code=303,
                )
            return RedirectResponse(
                "/shop-settings?tab=appearance&saved=1&msg=" + quote(msg),
                status_code=303,
            )

        known = keys_for_tab(tab)
        if tab == "menu":
            known = known | {"menu_order"}
            known.discard("show_reseller_apply")

        payload: dict[str, str] = {}
        for key in TOGGLE_KEYS:
            if key in known:
                payload[key] = "1" if form.get(f"s_{key}") else "0"
        for key in known:
            if key in TOGGLE_KEYS or key in IMAGE_KEYS:
                continue
            raw = form.get(f"s_{key}")
            if raw is not None and not isinstance(raw, UploadFile):
                val = str(raw)
                if key in ("user_alert_low_traffic_pct", "user_alert_low_time_pct"):
                    from app.services.users import clamp_alert_percent

                    val = clamp_alert_percent(val)
                if key == "force_join_channel":
                    from app.services.users import normalize_force_join_channel_value

                    val = normalize_force_join_channel_value(val)
                payload[key] = val

        uploads = DATA_DIR / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        max_image_bytes = 5 * 1024 * 1024
        for key in IMAGE_KEYS:
            if key not in known:
                continue
            if form.get(f"s_{key}_clear"):
                payload[key] = ""
                continue
            upload = form.get(f"s_{key}")
            if isinstance(upload, UploadFile) and upload.filename:
                name = upload.filename.lower()
                ext = Path(name).suffix
                if ext not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                    continue
                dest_name = f"r{rid}_{key}_{uuid.uuid4().hex[:10]}{ext}"
                dest = uploads / dest_name
                content = await upload.read(max_image_bytes + 1)
                if len(content) > max_image_bytes:
                    continue
                if content:
                    dest.write_bytes(content)
                    payload[key] = f"uploads/{dest_name}"

        # Always keep apply button off on reseller shops
        payload["show_reseller_apply"] = "0"
        # Never allow notify_* smuggling into ResellerSetting via generic settings save
        # (shop notify prefs go through /shop-notifications with an ACL allowlist).
        for k in list(payload.keys()):
            if str(k).startswith("notify_"):
                payload.pop(k, None)
        from app.services.users import set_settings_bulk

        await set_settings_bulk(session, payload, reseller_id=rid)
        return RedirectResponse(f"/shop-settings?tab={tab}&saved=1", status_code=303)

    @app.post("/shop-settings/cancel-pending-orders")
    async def shop_cancel_pending_orders(
        request: Request,
        staff: dict = Depends(shop_dep),
        session: AsyncSession = Depends(get_db),
    ):
        if staff.get("role") != "reseller":
            return RedirectResponse("/settings?tab=payment", status_code=303)
        rid = _rid(staff)
        if not rid:
            return _deny_scope()
        from app.services.orders import cancel_stale_pending_for_settings

        try:
            n = await cancel_stale_pending_for_settings(
                session, reseller_id=int(rid), force=True
            )
        except Exception as e:
            return RedirectResponse(
                f"/shop-settings?tab=payment&err={quote(str(e)[:200])}",
                status_code=303,
            )
        msg = f"{n} سفارش معلق/تأییدنشده لغو شد" if n else "سفارش معلقی برای لغو نبود"
        return RedirectResponse(
            f"/shop-settings?tab=payment&saved=1&msg={quote(msg)}",
            status_code=303,
        )

    @app.post("/shop-notifications")
    async def shop_notifications_save(
        request: Request,
        staff: dict = Depends(shop_dep),
        session: AsyncSession = Depends(get_db),
    ):
        """Save shop Telegram notify prefs — ResellerSetting only, ACL-filtered."""
        if staff.get("role") != "reseller":
            return RedirectResponse("/settings", status_code=303)
        rid = _rid(staff)
        if not rid:
            return _deny_scope()
        profile = await _load_profile(session, rid)
        if not profile:
            return RedirectResponse("/logout", status_code=303)

        from app.services.authz import resolve_shop_permissions_from_profile
        from app.services.notifications import (
            PLATFORM_ONLY_NOTIFY_KEYS,
            save_shop_notify_prefs,
            shop_notify_allowed_keys,
        )

        perms = list(staff.get("permissions") or []) or list(
            resolve_shop_permissions_from_profile(profile) or []
        )
        allowed = shop_notify_allowed_keys(perms)
        # Belt-and-suspenders: never accept platform-only keys from the form
        allowed -= set(PLATFORM_ONLY_NOTIFY_KEYS)

        form = await request.form()
        await save_shop_notify_prefs(
            session,
            rid,
            dict(form),
            allowed_keys=allowed,
        )
        return RedirectResponse(
            "/shop-settings?tab=notifications&saved=1&msg="
            + quote("نوتیفیکیشن‌های فروشگاه ذخیره شد"),
            status_code=303,
        )

    @app.post("/shop-settings/menu-layout")
    async def shop_menu_layout_save(
        request: Request,
        staff: dict = Depends(shop_dep),
        session: AsyncSession = Depends(get_db),
    ):
        if staff.get("role") != "reseller":
            return RedirectResponse("/settings", status_code=303)
        from app.bot.keyboards import DEFAULT_MENU_ORDER

        rid = _rid(staff)
        if not rid:
            return _deny_scope()
        form = await request.form()
        order = [p.strip() for p in str(form.get("menu_order") or "").split(",") if p.strip()]
        order = [
            k
            for k in order
            if k in DEFAULT_MENU_ORDER and k not in {"reseller_apply", "miniapp"}
        ]
        if "shop" not in order:
            order.insert(0, "shop")
        layout = str(form.get("menu_layout") or "compact").strip()
        from app.bot.keyboards import sync_show_flags_for_order
        from app.services.users import set_settings_bulk

        payload = {"menu_order": ",".join(order), **sync_show_flags_for_order(order)}
        payload["show_reseller_apply"] = "0"
        payload["show_miniapp"] = "0"
        if layout in {"classic", "compact"}:
            payload["menu_layout"] = layout
        await set_settings_bulk(session, payload, reseller_id=rid)
        return RedirectResponse("/shop-settings?tab=menu&saved=1", status_code=303)

    @app.post("/shop-supports/save")
    async def shop_supports_save(
        request: Request,
        staff: dict = Depends(shop_dep),
        session: AsyncSession = Depends(get_db),
    ):
        if staff.get("role") != "reseller":
            return RedirectResponse("/settings?tab=supports", status_code=303)
        from app.services.support_contacts import upsert_support_contact

        rid = _rid(staff)
        if not rid:
            return _deny_scope()
        form = await request.form()
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
        _, err = await upsert_support_contact(
            session,
            contact_id=contact_id,
            title=title,
            telegram=telegram,
            sort=sort,
            enabled=enabled,
            reseller_id=rid,
        )
        if err:
            return RedirectResponse(
                f"/shop-settings?tab=supports&err={quote(err)}",
                status_code=303,
            )
        return RedirectResponse("/shop-settings?tab=supports&saved=1", status_code=303)

    @app.post("/shop-supports/delete")
    async def shop_supports_delete(
        request: Request,
        staff: dict = Depends(shop_dep),
        session: AsyncSession = Depends(get_db),
    ):
        if staff.get("role") != "reseller":
            return RedirectResponse("/settings?tab=supports", status_code=303)
        from app.services.support_contacts import delete_support_contact

        rid = _rid(staff)
        if not rid:
            return _deny_scope()
        form = await request.form()
        contact_id = str(form.get("id") or "").strip()
        if contact_id:
            await delete_support_contact(session, contact_id, reseller_id=rid)
        return RedirectResponse("/shop-settings?tab=supports&saved=1", status_code=303)
