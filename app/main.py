from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

import uvicorn
from aiogram.types import Update
from fastapi import Request
from sqlalchemy import select

from app.api.integration_transport import IntegrationPeerCapture
from app.api.app import create_api_app
from app.bot import create_bot, create_dispatcher
from app.config import get_settings
from app.db.models import Plan
from app.db.session import SessionLocal, init_db
from app.jobs.scheduler import start_background_scheduler, start_scheduler, stop_scheduler
from app.services.pasarguard import get_pg
from app.services.integration_runtime import load_integration_runtime
from app.services.users import ensure_default_settings
from app.services.web_auth import load_web_admin

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("pgclock")

# httpx logs full request URLs at INFO — bot tokens in api.telegram.org/bot{token}/…
# must never land in journald.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


async def seed_demo_plan() -> None:
    async with SessionLocal() as session:
        await ensure_default_settings(session)
        await load_integration_runtime(session)
        result = await session.execute(select(Plan).limit(1))
        if result.scalar_one_or_none() is None:
            session.add(
                Plan(
                    name="ماهانه ۳۰ گیگ",
                    description="پلن شروع — تمپلیت را از وب‌پنل تنظیم کنید",
                    price=150000,
                    duration_days=30,
                    data_limit_gb=30,
                    pg_template_id=None,
                    is_active=True,
                    sort_order=1,
                )
            )
            session.add(
                Plan(
                    name="ماهانه نامحدود",
                    description="حجم نامحدود یک‌ماهه",
                    price=250000,
                    duration_days=30,
                    data_limit_gb=None,
                    is_active=True,
                    sort_order=2,
                )
            )
            await session.commit()
            logger.info("Seeded demo plans")
        try:
            from app.services.resellers import ensure_current_capacity_reseller_plan

            await ensure_current_capacity_reseller_plan(session)
            await session.commit()
        except Exception:
            logger.exception("ensure current_capacity reseller plan failed")
            try:
                await session.rollback()
            except Exception:
                pass


def main() -> None:
    # Always reload settings from project .env
    get_settings.cache_clear()
    settings = get_settings()
    has_token = bool((settings.bot_token or "").strip())
    bot = None
    dp = None
    if has_token:
        try:
            bot = create_bot()
            dp = create_dispatcher()
        except Exception:
            logger.exception("Bot init failed — starting web panel only")
            bot = None
            dp = None

    @asynccontextmanager
    async def lifespan(app):
        # uvicorn binds ONLY after this startup finishes. A hung Postgres
        # connect previously left systemd "active" with nothing on WEB_PORT.
        try:
            await asyncio.wait_for(init_db(), timeout=120)
        except asyncio.TimeoutError:
            logger.error(
                "Database init timed out after 120s — check PostgreSQL is up, "
                "DATABASE_URL password/HBA, and listen_addresses"
            )
            raise SystemExit("Database init timed out (PostgreSQL unreachable?)")
        except Exception:
            logger.exception(
                "Database init failed — panel will not listen until DATABASE_URL works"
            )
            raise SystemExit("Database init failed — see journalctl -u pgclockbot")

        from app.services.setup_wizard import ensure_web_secret

        try:
            ensure_web_secret()
        except Exception:
            logger.exception("ensure_web_secret failed")
            raise SystemExit("WEB_SECRET could not be persisted — check .env permissions")

        try:
            from app.services.service_control import ensure_restart_helper

            ok_h, msg_h = ensure_restart_helper()
            logger.info("Restart helper: %s", msg_h if ok_h else f"skip — {msg_h}")
        except Exception:
            logger.debug("restart helper bootstrap skipped", exc_info=True)
        try:
            await seed_demo_plan()
        except Exception:
            logger.exception("Demo plan seed failed — continuing")

        try:
            start_background_scheduler()
        except Exception:
            logger.exception("Background scheduler failed to start")

        poll_task = None
        pg_warmup_task = None
        reseller_mgr = None
        panel_only = bot is None or dp is None

        if panel_only:
            logger.warning(
                "Web panel only mode (no bot yet). Open http://%s:%s/ to finish setup.",
                settings.web_host,
                settings.web_port,
            )
        else:
            try:
                me = await bot.get_me()
                logger.info(
                    "Bot online as @%s (id=%s) · admins=%s",
                    me.username,
                    me.id,
                    settings.admin_ids,
                )
                try:
                    from app.bot.chat_menu import sync_telegram_menu_button

                    await sync_telegram_menu_button(bot, allow_miniapp=True)
                except Exception:
                    logger.debug("sync telegram menu button failed", exc_info=True)
            except Exception:
                logger.exception(
                    "Cannot connect to Telegram — web panel stays up. "
                    "Fix BOT_TOKEN in /setup or .env and restart."
                )
                # Do not raise — panel must remain reachable
                panel_only = True

            if not panel_only:
                try:
                    start_scheduler(bot)
                except Exception:
                    logger.exception("Scheduler failed to start")

                async def _pg_warmup() -> None:
                    try:
                        await get_pg().ensure_token()
                        logger.info("PasarGuard panel login OK · %s", get_settings().pg_base_url)
                    except Exception:
                        logger.exception(
                            "PasarGuard login FAILED — fix PG_BASE_URL / PG_USERNAME / PG_PASSWORD "
                            "(use https://host only, no path)"
                        )

                pg_warmup_task = asyncio.create_task(_pg_warmup())

                from app.services.setup_wizard import (
                    normalize_webhook_base_url,
                    telegram_webhook_endpoint,
                )
                from urllib.parse import urlparse

                wh_base, wh_path, wh_full = telegram_webhook_endpoint(
                    settings.webhook_url, settings.webhook_path
                )
                raw_base = normalize_webhook_base_url(settings.webhook_url)
                if raw_base:
                    raw_path = urlparse(raw_base).path
                    if raw_path not in {"", "/"}:
                        logger.warning(
                            "WEBHOOK_URL had a path component %r; using origin %s with path %s",
                            raw_path,
                            wh_base,
                            wh_path,
                        )

                use_webhook = bool(wh_base)
                webhook_ok = False
                if use_webhook:
                    secret = (settings.webhook_secret_token or "").strip()
                    if not secret:
                        from app.services.webhook_secret import ensure_webhook_secret

                        secret = ensure_webhook_secret()
                    try:
                        await bot.set_webhook(
                            wh_full,
                            drop_pending_updates=True,
                            secret_token=secret,
                        )
                        webhook_ok = True
                        logger.info("Webhook set: %s (secret token enabled)", wh_full)
                    except Exception:
                        logger.exception(
                            "set_webhook failed for %s — falling back to long-polling "
                            "(check WEBHOOK_URL is https origin-only and reachable)",
                            wh_full,
                        )
                        try:
                            await bot.delete_webhook(drop_pending_updates=True)
                        except Exception:
                            logger.exception("delete_webhook after set_webhook failure failed")

                if not webhook_ok:
                    if not use_webhook:
                        try:
                            await bot.delete_webhook(drop_pending_updates=True)
                        except Exception:
                            logger.exception("delete_webhook failed")

                    async def _poll():
                        logger.info("Starting long-polling…")
                        try:
                            await dp.start_polling(bot)
                        except Exception:
                            logger.exception("Polling crashed")
                            raise

                    poll_task = asyncio.create_task(_poll())

                    def _on_done(task: asyncio.Task) -> None:
                        if task.cancelled():
                            return
                        exc = task.exception()
                        if exc:
                            logger.error("Polling task failed: %s", exc)

                    poll_task.add_done_callback(_on_done)

                # Reseller-owned bots always use getUpdates (independent of main webhook)
                try:
                    from app.services.reseller_bots import init_reseller_bot_manager

                    reseller_mgr = init_reseller_bot_manager(dp)
                    await reseller_mgr.start_all()
                except Exception:
                    logger.exception("Reseller bots failed to start")

        try:
            yield
        finally:
            stop_scheduler()
            if reseller_mgr is not None:
                try:
                    await reseller_mgr.stop_all()
                except Exception:
                    logger.exception("Reseller bots shutdown failed")
            if poll_task:
                poll_task.cancel()
                try:
                    await poll_task
                except asyncio.CancelledError:
                    pass
            if pg_warmup_task:
                pg_warmup_task.cancel()
                try:
                    await pg_warmup_task
                except asyncio.CancelledError:
                    pass
            if bot is not None and settings.webhook_url.strip():
                try:
                    await bot.delete_webhook(drop_pending_updates=False)
                except Exception:
                    pass
            try:
                await get_pg().close()
            except Exception:
                pass
            if bot is not None:
                try:
                    await bot.session.close()
                except Exception:
                    pass

    api = create_api_app(lifespan=lifespan)

    from app.services.setup_wizard import telegram_webhook_endpoint

    _wh_base, _wh_path, _wh_full = telegram_webhook_endpoint(
        settings.webhook_url, settings.webhook_path
    )
    if bot is not None and dp is not None and _wh_base:
        from fastapi.responses import JSONResponse

        from app.services.webhook_secret import ensure_webhook_secret

        webhook_secret = (settings.webhook_secret_token or "").strip() or ensure_webhook_secret()

        @api.post(_wh_path)
        async def telegram_webhook(request: Request):
            import hashlib
            import secrets as _secrets

            from app.services.security_policy import WEBHOOK_MAX_BODY_BYTES, content_length_ok

            header = (request.headers.get("X-Telegram-Bot-Api-Secret-Token") or "").strip()
            # Hash both sides so compare_digest never raises on length mismatch
            expected = hashlib.sha256(webhook_secret.encode("utf-8")).digest()
            got = hashlib.sha256(header.encode("utf-8")).digest()
            if not header or not _secrets.compare_digest(got, expected):
                logger.warning(
                    "Telegram webhook rejected (missing/invalid secret token) — "
                    "if behind nginx, enable underscores_in_headers"
                )
                return JSONResponse({"ok": False}, status_code=403)
            if not content_length_ok(request.headers.get("content-length"), WEBHOOK_MAX_BODY_BYTES):
                return JSONResponse({"ok": False}, status_code=413)
            body = await request.body()
            if len(body) > WEBHOOK_MAX_BODY_BYTES:
                return JSONResponse({"ok": False}, status_code=413)
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return JSONResponse({"ok": False}, status_code=400)
            update = Update.model_validate(data, context={"bot": bot})
            await dp.feed_update(bot, update)
            return {"ok": True}

    from app.services.setup_wizard import is_setup_complete, persist_setup_entry_url
    from app.services.web_auth import repair_web_admin_from_env

    # Migrate from .env only when web_admin.json is missing — never reset panel password
    try:
        creds = repair_web_admin_from_env() if is_setup_complete() else load_web_admin()
    except Exception:
        creds = load_web_admin()

    from app.services.ssl_certs import uvicorn_ssl_kwargs, verify_tls_material

    ssl_kwargs = uvicorn_ssl_kwargs() or {}
    if ssl_kwargs:
        check = verify_tls_material()
        if not check.get("ok"):
            # Do NOT silently fall back to HTTP — browsers get ERR_CONNECTION_CLOSED
            # when clients speak TLS to a plain-HTTP listener (and install health
            # would keep probing https:// while the process looks "active").
            logger.error("TLS enabled but cert/key not loadable: %s", check.get("error"))
            raise SystemExit(f"TLS cert not loadable: {check.get('error')}")
        logger.info("TLS enabled · cert=%s", ssl_kwargs.get("ssl_certfile"))

    # Public URL for setup gate (never 0.0.0.0; prefer real IP / HTTPS host).
    try:
        from app.services.setup_wizard import default_http_panel_url
        from app.services.ssl_certs import public_panel_base_url

        if ssl_kwargs:
            entry = (public_panel_base_url() or "").rstrip("/") + "/"
        else:
            entry = default_http_panel_url(web_port=settings.web_port).rstrip("/") + "/"
    except Exception:
        scheme = "https" if ssl_kwargs else "http"
        host_hint = settings.web_host if settings.web_host not in {"0.0.0.0", "::"} else "127.0.0.1"
        entry = f"{scheme}://{host_hint}:{settings.web_port}/"
    if not entry or entry == "/":
        scheme = "https" if ssl_kwargs else "http"
        entry = f"{scheme}://127.0.0.1:{settings.web_port}/"

    if not creds.get("password") or not is_setup_complete():
        setup_url = persist_setup_entry_url(entry.rstrip("/"))
        if setup_url:
            from app.services.setup_wizard import SETUP_ENTRY_FILE

            # Never write the one-time gate token itself into logs — logs are
            # often shipped to aggregators / broader-read files than the
            # 0600 hint file persist_setup_entry_url() already wrote.
            logger.warning(
                "First-run wizard ready (valid 15 min) — read the one-time "
                "setup URL from %s (root/owner-only) or open %s and use the "
                "printed gate token.",
                SETUP_ENTRY_FILE,
                entry,
            )
    else:
        logger.info(
            "Web panel ready · user=%s · %s",
            creds.get("username") or "admin",
            entry,
        )

    uvicorn.run(
        IntegrationPeerCapture(api),
        proxy_headers=False,
        host=settings.web_host,
        port=settings.web_port,
        log_level="info",
        **ssl_kwargs,
    )


if __name__ == "__main__":
    main()
