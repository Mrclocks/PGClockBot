"""The first-run setup wizard (``/setup``): the admin account, the bot, the PasarGuard link and the finish step.

The wizard writes ``.env`` through ``update_env_keys`` and never echoes a saved secret back into the page. The
setup gate (one-time link or a local address) and the CSRF origin check run in the panel's middleware, before
any of these routes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from functools import partial

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.config import normalize_pg_base_url, normalize_pg_subscription_path
from app.services.pg_access import clear_platform_pg_capability_cache, resolve_platform_pg_capabilities
from app.services.security_policy import UnsafePgUrlError, assert_safe_pg_base_url, is_placeholder_bot_token
from app.services.service_control import schedule_panel_restart
from app.services.setup_wizard import (
    begin_setup,
    current_setup_values,
    ensure_web_secret,
    force_http_login_url,
    is_setup_complete,
    mark_setup_complete,
    parse_admin_ids,
    setup_finish_login_url,
    setup_pg_access_audit,
    trusted_finish_next,
    update_env_keys,
    wizard_panel_url_hint,
)
from app.services.ssl_certs import https_is_active
from app.services.web_auth import save_web_admin, validate_password_strength

logger = logging.getLogger(__name__)

SetupPage = Callable[..., Awaitable[Response]]
FINISH_STEP = 4
MIN_PG_API_KEY_LENGTH = 16
TRUE_WORDS = {"1", "on", "true", "yes"}


class SetupInputError(ValueError):
    """A wizard field that cannot be saved; the message is shown to the operator as it is."""


def _https_ready() -> bool:
    try:
        return bool(https_is_active())
    except Exception:  # noqa: BLE001 - the finish link falls back to plain HTTP when the TLS state is unknown
        logger.debug("setup: https state unreadable", exc_info=True)
        return False


def _wants_json(request: Request) -> bool:
    return (request.headers.get("x-requested-with") or "").strip() == "setup-probe"


def _requested_step(request: Request) -> int:
    try:
        return int(request.query_params.get("step") or "0")
    except ValueError:
        return 0


async def _setup_page(
    render: Callable,
    request: Request,
    *,
    step: int = 0,
    err: str | None = None,
    ok: str | None = None,
    show_done: bool = False,
    pg_audit: dict | None = None,
) -> Response:
    begin_setup()
    values = current_setup_values()
    has_bot_token = bool((values.get("BOT_TOKEN") or "").strip()) and not is_placeholder_bot_token(
        values.get("BOT_TOKEN")
    )
    # Never echo a saved secret into the page source. A re-run of the wizard keeps it when its field is left
    # blank (see _bot_values and _pg_credentials), so the page never needs it.
    display_values = dict(values)
    display_values["BOT_TOKEN"] = ""
    display_values["PG_PASSWORD"] = ""
    display_values["PG_API_KEY"] = ""
    if pg_audit is None and (show_done or step >= FINISH_STEP):
        try:
            pg_audit = await setup_pg_access_audit()
        except Exception:  # noqa: BLE001 - the finish step shows without the access summary
            logger.warning("setup: PasarGuard access audit failed", exc_info=True)
            pg_audit = None
    return render(
        request,
        "setup.html",
        {
            "values": display_values,
            "has_bot_token": has_bot_token,
            "has_pg_password": bool((values.get("PG_PASSWORD") or "").strip()),
            "has_pg_api_key": bool((values.get("PG_API_KEY") or "").strip()),
            "initial_step": step,
            "show_done": show_done,
            "flash_err": err or request.query_params.get("err"),
            "flash_ok": ok or request.query_params.get("ok"),
            "panel_url": wizard_panel_url_hint(values.get("WEB_PORT", "9000")),
            "finish_login_url": setup_finish_login_url(),
            "https_ready": _https_ready(),
            "bot_username": (values.get("BOT_USERNAME") or "").lstrip("@"),
            "pg_audit": pg_audit,
        },
    )


def _save_admin(username: str, password: str, password_confirm: str) -> None:
    user = (username or "").strip() or "admin"
    first = password or ""
    if first != (password_confirm or ""):
        raise SetupInputError("رمز عبور و تکرار آن یکسان نیستند.")
    ok, message = validate_password_strength(first)
    if not ok:
        raise SetupInputError(message)
    try:
        save_web_admin(user, first)
    except ValueError as exc:
        raise SetupInputError(str(exc)) from exc
    ensure_web_secret()
    update_env_keys({"WEB_ADMIN_USER": user})


def _bot_values(bot_token: str, bot_username: str, admin_ids: str) -> dict[str, str]:
    token = (bot_token or "").strip()
    username = (bot_username or "").strip().lstrip("@")
    if not token:
        # The page never shows the saved token, so an empty field means "keep it", not "clear it".
        existing = (current_setup_values().get("BOT_TOKEN") or "").strip()
        if not existing or is_placeholder_bot_token(existing):
            raise SetupInputError("توکن ربات الزامی است.")
        token = existing
    if is_placeholder_bot_token(token):
        raise SetupInputError("توکن ربات نامعتبر است — یک توکن واقعی از BotFather وارد کنید.")
    if not username:
        raise SetupInputError("نام کاربری ربات الزامی است.")
    try:
        ids = parse_admin_ids((admin_ids or "").strip())
    except ValueError:
        raise SetupInputError("آیدی ادمین‌ها باید عدد باشد (با کاما جدا کنید).") from None
    if not ids:
        raise SetupInputError("حداقل یک آیدی ادمین وارد کنید.")
    return {"BOT_TOKEN": token, "BOT_USERNAME": username, "ADMIN_IDS": ",".join(str(i) for i in ids)}


def _subscription_path(raw: str) -> str:
    try:
        return normalize_pg_subscription_path(raw)
    except ValueError as exc:
        # Its message is worded for the operator.
        raise SetupInputError(str(exc)) from exc


async def _pg_base_url(raw: str) -> str:
    base = normalize_pg_base_url((raw or "").strip())
    if not base:
        raise SetupInputError("آدرس پاسارگارد الزامی است.")
    try:
        return await asyncio.to_thread(assert_safe_pg_base_url, base)
    except UnsafePgUrlError as exc:
        raise SetupInputError(str(exc)) from exc


def _pg_credentials(pg_password: str, pg_api_key: str, clear_pg_api_key: str) -> tuple[str, str]:
    """The password and API key to save; an empty field keeps the saved value, which the page never shows."""
    saved = current_setup_values()
    raw_key = (pg_api_key or "").replace("\r", "").strip()
    if str(clear_pg_api_key or "").strip().lower() in TRUE_WORDS:
        api_key = ""
    elif raw_key:
        if len(raw_key) < MIN_PG_API_KEY_LENGTH:
            raise SetupInputError("کلید API پاسارگارد کوتاه است — مقدار کامل را از پنل پاسارگارد کپی کنید.")
        api_key = raw_key
    else:
        api_key = (saved.get("PG_API_KEY") or "").strip()
    password = pg_password
    if not (password or "").strip():
        password = (saved.get("PG_PASSWORD") or "").strip()
        if not password and not api_key:
            raise SetupInputError("رمز یا کلید API پاسارگارد الزامی است (حداقل یکی برای ادمین متصل).")
    return password, api_key


def _web_port(raw: str) -> int:
    try:
        port = int((raw or "9000").strip())
    except ValueError:
        raise SetupInputError("پورت وب نامعتبر است.") from None
    if port < 1 or port > 65535:
        raise SetupInputError("پورت وب نامعتبر است.")
    return port


async def _check_pg_login(*, username: str, password: str, api_key: str, base_url: str) -> None:
    """Log in with the account before saving it (fail closed on a bad login)."""
    caps = await resolve_platform_pg_capabilities(
        username=username, password=password.strip() or None, api_key=api_key or None, base_url=base_url,
        use_cache=False,
    )
    if not caps.get("ok"):
        detail = caps.get("error") or "نامعتبر"
        raise SetupInputError(
            f"ورود به پاسارگارد ناموفق بود: {detail}. "
            "علت محتمل: آدرس، نام کاربری، رمز یا کلید API اشتباه. "
            "راه حل: اعتبارنامه همان ادمین متصل در پاسارگارد را وارد کنید "
            "(لزوماً owner پاسارگارد نیست)."
        )


async def _pg_values(form: dict[str, str]) -> dict[str, str | int | None]:
    """The PasarGuard and web settings to save, checked in the order the operator fills them in."""
    sub_path = _subscription_path(form["pg_subscription_path"])
    base = await _pg_base_url(form["pg_base_url"])
    username = (form["pg_username"] or "").strip()
    if not username:
        raise SetupInputError("نام کاربری پاسارگارد الزامی است.")
    password, api_key = _pg_credentials(form["pg_password"], form["pg_api_key"], form["clear_pg_api_key"])
    port = _web_port(form["web_port"])
    await _check_pg_login(username=username, password=password, api_key=api_key, base_url=base)
    public = (form["public_base_url"] or "").strip().rstrip("/")
    if public.startswith("https://"):
        public = "http://" + public[len("https://"):]
    return {
        "PG_BASE_URL": base,
        "PG_SUBSCRIPTION_PATH": sub_path,
        "PG_USERNAME": username,
        "PG_PASSWORD": (password or "").strip(),
        "PG_API_KEY": api_key,
        "WEB_PORT": str(port),
        "PUBLIC_BASE_URL": public,
        "CURRENCY": (form["currency"] or "").strip() or "تومان",
    }


async def _finish_target(request: Request) -> str:
    # The wizard page sends the login link it showed; it is kept only when it points at this panel. TLS is not
    # live yet on most installs, so the link is plain HTTP unless HTTPS already serves.
    try:
        form = await request.form()
        next_url = str(form.get("next") or "").strip()
    except Exception:  # noqa: BLE001 - an unreadable form falls back to the default login link
        logger.debug("setup finish: form unreadable", exc_info=True)
        next_url = ""
    return force_http_login_url(trusted_finish_next(next_url, request_host=request.url.hostname or ""))


async def _field_error(page: SetupPage, request: Request, message: str, *, step: int) -> Response:
    """The step's form again with the message, or JSON for the page's own probe request."""
    if _wants_json(request):
        return JSONResponse({"ok": False, "error": message}, status_code=400)
    return await page(request, step=step, err=message)


def register_setup_pages(app: FastAPI, *, render: Callable) -> SetupPage:
    """Register the wizard's routes; returns the page renderer, which ``/`` also shows before setup is done."""
    page = partial(_setup_page, render)

    @app.get("/setup", response_class=HTMLResponse)
    async def setup_page(request: Request) -> Response:
        if is_setup_complete():
            return RedirectResponse("/", status_code=303)
        step = _requested_step(request)
        return await page(request, step=min(step, FINISH_STEP), show_done=step >= FINISH_STEP)

    @app.post("/setup/admin")
    async def setup_admin(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        password_confirm: str = Form(...),
    ) -> Response:
        if is_setup_complete():
            return RedirectResponse("/", status_code=303)
        begin_setup()
        try:
            _save_admin(username, password, password_confirm)
        except SetupInputError as exc:
            return await page(request, step=1, err=str(exc))
        return RedirectResponse("/setup?step=2", status_code=303)

    @app.post("/setup/bot")
    async def setup_bot(
        request: Request,
        bot_token: str = Form(...),
        bot_username: str = Form(""),
        admin_ids: str = Form(...),
    ) -> Response:
        if is_setup_complete():
            return RedirectResponse("/", status_code=303)
        begin_setup()
        try:
            values = _bot_values(bot_token, bot_username, admin_ids)
        except SetupInputError as exc:
            return await page(request, step=2, err=str(exc))
        update_env_keys(values)
        ensure_web_secret()
        return RedirectResponse("/setup?step=3", status_code=303)

    @app.post("/setup/other")
    async def setup_other(
        request: Request,
        pg_base_url: str = Form(...),
        pg_subscription_path: str = Form("/sub"),
        pg_username: str = Form(""),
        pg_password: str = Form(""),
        pg_api_key: str = Form(""),
        clear_pg_api_key: str = Form(""),
        web_port: str = Form("9000"),
        public_base_url: str = Form(""),
        currency: str = Form("تومان"),
    ) -> Response:
        if is_setup_complete():
            if _wants_json(request):
                return JSONResponse({"ok": False, "error": "راه‌اندازی قبلاً کامل شده است"}, status_code=400)
            return RedirectResponse("/", status_code=303)
        begin_setup()
        try:
            values = await _pg_values({
                "pg_base_url": pg_base_url, "pg_subscription_path": pg_subscription_path, "pg_username": pg_username,
                "pg_password": pg_password, "pg_api_key": pg_api_key, "clear_pg_api_key": clear_pg_api_key,
                "web_port": web_port, "public_base_url": public_base_url, "currency": currency,
            })
        except SetupInputError as exc:
            return await _field_error(page, request, str(exc), step=3)
        ensure_web_secret()
        update_env_keys(values)
        clear_platform_pg_capability_cache()
        if _wants_json(request):
            return JSONResponse({"ok": True, "next": "/setup?step=4"})
        return RedirectResponse("/setup?step=4", status_code=303)

    @app.post("/setup/finish")
    async def setup_finish(request: Request) -> Response:
        mark_setup_complete()
        ensure_web_secret()
        schedule_panel_restart(delay_sec=2.5, reason="setup wizard finished")
        return RedirectResponse(await _finish_target(request), status_code=303)

    return page
