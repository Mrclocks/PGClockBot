"""Shared bot authorization helpers (Phase C4 / D4 / Hybrid Owner PG / Phase 1 / 4G)."""

from __future__ import annotations

import functools
import inspect
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, OrgPrincipal

# Single Owner-only denial copy (Web-equivalent explicit Owner Principal).
OWNER_REQUIRED_MESSAGE = "دسترسی مالک سیستم لازم است"

# Migrated 4B–4E families L1 may use on Bot (never overview/admins/backup).
MIGRATED_PG_PAGES = frozenset(
    {"pg_users", "pg_nodes", "pg_hosts", "pg_templates", "pg_groups"}
)

# reply_nav _soft_admin callback ids that use the shared PG gate (not Owner-only).
MIGRATED_PG_SOFT_CALLBACKS = frozenset(
    {
        "adm:pg",
        "adm:pg:users",
        "adm:pg:search",
        "adm:pg:create",
        "adm:pg:nodes",
        "adm:pg:group",
        "adm:pg:template",
    }
)

def is_platform_admin(user: BotUser | None) -> bool:
    """True when the Telegram user is a platform admin (role or ADMIN_IDS).

    Candidate check only — does **not** grant Org Owner authority by itself.
    Sensitive admin routers must use ``is_bot_owner_principal``.
    Independent of Web ``web_admin.json`` identity store (Phase D4).
    """
    from app.services.platform_identity import is_bot_platform_admin

    return is_bot_platform_admin(user)

async def is_bot_owner_principal(
    session: AsyncSession | None,
    user: BotUser | None,
    *,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
) -> bool:
    """True when Bot admin maps to the same explicit Owner Principal as Web.

    - Shop/reseller bots → always deny.
    - Requires Telegram id in ``ADMIN_IDS`` (explicit Owner operator).
    - Sticky ``BotUser.role=admin`` alone is legacy metadata — not Owner.
    - Resolves via shared Owner singleton (Phase 4A bridge).
    """
    from app.services.bot_principal_identity import resolve_bot_org_principal
    from app.services.org_principals import is_owner_principal

    principal = await resolve_bot_org_principal(
        session,
        db_user=user,
        is_reseller_bot=is_reseller_bot,
    )
    return principal is not None and is_owner_principal(principal)

def is_migrated_pg_soft_callback(data: str | None) -> bool:
    """True when reply-nav may invoke a 4B–4E PG family handler without Owner."""
    raw = (data or "").strip()
    if not raw:
        return False
    if raw in MIGRATED_PG_SOFT_CALLBACKS:
        return True
    return False

async def notify_owner_required(*, callback=None, message=None) -> None:
    if callback is not None:
        try:
            await callback.answer(OWNER_REQUIRED_MESSAGE, show_alert=True)
        except Exception:
            pass
        return
    if message is not None:
        try:
            await message.answer(OWNER_REQUIRED_MESSAGE + ".")
        except Exception:
            pass

async def require_bot_owner(
    session: AsyncSession | None,
    db_user: BotUser | None,
    *,
    is_reseller_bot: bool = False,
    callback=None,
    message=None,
    notify: bool = True,
) -> bool:
    """Final Owner-only gate. Uses Phase 4A resolver — never role=admin.

    Same explicit Owner Principal as Web ``require_admin`` / ``is_explicit_owner_staff``.
    """
    ok = await is_bot_owner_principal(
        session, db_user, is_reseller_bot=is_reseller_bot
    )
    if ok:
        return True
    if notify:
        await notify_owner_required(callback=callback, message=message)
    return False

async def bot_admin_settings_in_flow(event: Any, data: dict[str, Any]) -> bool:
    """True when a settings inline/FSM step continues owner-gated reply navigation.

    Reply-nav opens Settings after ``_deny_unless_owner`` and sets
    ``NAV_ADMIN_SETTINGS``. Re-running the Owner Principal gate on every
    ``adm:st:*`` callback or ``SettingsStates`` message falsely denied the real
    operator with «دسترسی مالک سیستم لازم است» even though they were already
    inside Settings.
    """
    state = data.get("state")
    if state is None:
        return False
    from app.bot import menu_nav as nav

    try:
        level = await nav.get_nav_level(state)
    except Exception:
        return False
    if level not in (
        nav.NAV_ADMIN_SETTINGS,
        nav.NAV_ADMIN,
        nav.NAV_ADMIN_SYSTEM,
    ):
        return False

    cb_data = getattr(event, "data", None) or ""
    if cb_data.startswith("adm:st:") or cb_data in ("adm:settings", "adm:st:hub"):
        return True

    try:
        cur = await state.get_state()
    except Exception:
        cur = None
    if cur and "SettingsStates" in str(cur):
        return True
    return False

def _owner_handler_signature(fn: Callable[..., Awaitable[Any]]) -> inspect.Signature:
    """Aiogram follows ``inspect.signature`` → ``__wrapped__``, so extra DI
    params on the wrapper are invisible unless ``__signature__`` is set.

    Handlers that omit ``session`` (e.g. ``settings_hub`` / inline «تنظیمات»)
    must still receive the request session or the Owner check fail-closes.
    """
    orig = inspect.signature(fn)
    names = set(orig.parameters)
    extras: list[inspect.Parameter] = []
    for name, annotation, default in (
        ("session", AsyncSession | None, None),
        ("db_user", BotUser | None, None),
        ("is_reseller_bot", bool, False),
    ):
        if name in names:
            continue
        extras.append(
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                default=default,
                annotation=annotation,
            )
        )
    if not extras:
        return orig
    new_params: list[inspect.Parameter] = []
    var_kw: inspect.Parameter | None = None
    for param in orig.parameters.values():
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            var_kw = param
            continue
        new_params.append(param)
    new_params.extend(extras)
    if var_kw is not None:
        new_params.append(var_kw)
    return orig.replace(parameters=new_params)

def require_bot_owner_handler(fn: Callable[..., Awaitable[Any]]):
    """Decorator: Owner Principal required before the wrapped Bot handler runs.

    ``role=admin`` / ``is_platform_admin`` are never sufficient. Shop bots deny.
    Injects ``session`` even when the original signature omitted it so reply-nav
    and the dispatcher share the same check.

    Do not set ``__wrapped__``: aiogram 3.x ``inspect.unwrap`` would reach the
    inner handler, drop ``session`` from DI, and false-deny the real Owner.
    """
    orig_sig = inspect.signature(fn)

    async def wrapper(*args: Any, **kwargs: Any):
        from aiogram.types import CallbackQuery, Message

        session = kwargs.get("session")
        db_user = kwargs.get("db_user")
        is_reseller_bot = bool(kwargs.get("is_reseller_bot", False))
        callback = None
        message = None
        for value in (*args, *kwargs.values()):
            if db_user is None and isinstance(value, BotUser):
                db_user = value
            if session is None and isinstance(value, AsyncSession):
                session = value
            if value is None or isinstance(value, (BotUser, AsyncSession)):
                continue
            if callback is None and (
                isinstance(value, CallbackQuery)
                or (
                    callable(getattr(value, "answer", None))
                    and hasattr(value, "data")
                )
            ):
                callback = value
            elif message is None and (
                isinstance(value, Message)
                or (
                    callable(getattr(value, "answer", None))
                    and hasattr(value, "text")
                    and not hasattr(value, "data")
                )
            ):
                message = value
        if not await require_bot_owner(
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            callback=callback,
            message=message,
        ):
            return None
        call_kwargs = {k: v for k, v in kwargs.items() if k in orig_sig.parameters}
        bound = orig_sig.bind_partial(*args, **call_kwargs)
        if "session" in orig_sig.parameters and "session" not in bound.arguments:
            call_kwargs["session"] = session
        if "db_user" in orig_sig.parameters and "db_user" not in bound.arguments:
            call_kwargs["db_user"] = db_user
        if (
            "is_reseller_bot" in orig_sig.parameters
            and "is_reseller_bot" not in bound.arguments
        ):
            call_kwargs["is_reseller_bot"] = is_reseller_bot
        return await fn(*args, **call_kwargs)

    wrapper.__signature__ = _owner_handler_signature(fn)
    wrapper.__name__ = getattr(fn, "__name__", "wrapper")
    wrapper.__doc__ = fn.__doc__
    wrapper.__module__ = fn.__module__
    wrapper.__qualname__ = getattr(fn, "__qualname__", wrapper.__name__)
    return wrapper

async def bot_migrated_pg_features(
    session: AsyncSession | None,
    db_user: BotUser | None,
    *,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
) -> frozenset[str]:
    """PG page keys the Bot actor may open from migrated 4B–4E families.

    Owner: env Hybrid features (may include overview). L1/L2: own Principal
    capabilities ∩ migrated pages (never pg_overview / pg_admins).
    Shop bot: same as L1/L2 when the Telegram user is that shop's operator.
    """
    from app.services.bot_principal_identity import (
        bot_pg_family_resolution_ok,
        resolve_bot_principal_bridge,
    )
    from app.services.org_principals import is_owner_principal
    from app.services.principal_pg_authz import (
        apply_level1_pg_local_safety,
        authorize_pg_page,
        is_level1_principal_staff,
    )

    if session is None or db_user is None:
        return frozenset()
    if is_reseller_bot:
        from app.services.bot_principal_identity import shop_bot_actor_is_operator

        if not await shop_bot_actor_is_operator(
            session,
            db_user,
            is_reseller_bot=True,
            reseller_owner_id=reseller_owner_id,
        ):
            return frozenset()
    resolution = await resolve_bot_principal_bridge(
        session,
        db_user=db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
    )
    if resolution is None:
        return frozenset()
    principal = resolution.principal
    if is_owner_principal(principal):
        return await platform_pg_features()
    if not bot_pg_family_resolution_ok(resolution):
        return frozenset()
    staff = dict(resolution.staff)
    if is_level1_principal_staff(staff):
        staff = apply_level1_pg_local_safety(staff)
    allowed: set[str] = set()
    for page in MIGRATED_PG_PAGES:
        if authorize_pg_page(staff, page).allowed:
            allowed.add(page)
    return frozenset(allowed)

async def bot_may_open_pg_hub(
    session: AsyncSession | None,
    db_user: BotUser | None,
    *,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
) -> bool:
    feats = await bot_migrated_pg_features(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
    )
    return bool(feats)

async def bot_pg_can_create_user(
    session: AsyncSession | None,
    db_user: BotUser | None,
    *,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
) -> bool:
    from app.services.bot_principal_identity import (
        bot_pg_family_resolution_ok,
        resolve_bot_principal_bridge,
    )
    from app.services.org_principals import is_owner_principal
    from app.services.principal_pg_authz import (
        apply_level1_pg_local_safety,
        authorize_pg_user_action,
        is_level1_principal_staff,
    )

    if session is None or db_user is None:
        return False
    if is_reseller_bot:
        from app.services.bot_principal_identity import shop_bot_actor_is_operator

        if not await shop_bot_actor_is_operator(
            session,
            db_user,
            is_reseller_bot=True,
            reseller_owner_id=reseller_owner_id,
        ):
            return False
    resolution = await resolve_bot_principal_bridge(
        session,
        db_user=db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
    )
    if resolution is None:
        return False
    if is_owner_principal(resolution.principal):
        return await can_platform_pg_action(db_user, "users", "create")
    if not bot_pg_family_resolution_ok(resolution):
        return False
    staff = dict(resolution.staff)
    if is_level1_principal_staff(staff):
        staff = apply_level1_pg_local_safety(staff)
    return bool(authorize_pg_user_action(staff, "create").allowed)

async def resolve_bot_owner_principal(
    session: AsyncSession | None,
    user: BotUser | None,
    *,
    is_reseller_bot: bool = False,
) -> OrgPrincipal | None:
    """Return the shared Owner principal when bot operator is authorized."""
    from app.services.bot_principal_identity import resolve_bot_org_principal
    from app.services.org_principals import is_owner_principal

    principal = await resolve_bot_org_principal(
        session,
        db_user=user,
        is_reseller_bot=is_reseller_bot,
    )
    if principal is None or not is_owner_principal(principal):
        return None
    return principal

async def resolve_bot_principal_bridge(
    session: AsyncSession | None,
    user: BotUser | None,
    *,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    """Phase 4A — Principal + staff + AuthzContext for Bot (same as Web)."""
    from app.services.bot_principal_identity import (
        resolve_bot_principal_bridge as _bridge,
    )

    return await _bridge(
        session,
        db_user=user,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
    )

def can_shop_feature(
    key: str,
    *,
    profile: Any = None,
    db_user: BotUser | None = None,
) -> bool:
    """Bot shop feature check — same source of truth as Web ``require_perm`` / ``can_shop``.

    Platform Owner/Admin → allow. Otherwise uses ``web_permissions`` via authz
    (never Owner fallback, never ``bot_permissions`` column).
    """
    from app.services.authz import shop_feature_allowed

    if is_platform_admin(db_user):
        return shop_feature_allowed(key=key, role="admin")
    role = None
    if db_user is not None:
        role = db_user.role
    return shop_feature_allowed(key=key, profile=profile, role=role)

async def platform_pg_features() -> frozenset[str]:
    """Live PG menu keys for the env installer account (Hybrid fail-closed)."""
    from app.services.pg_access import resolve_platform_pg_capabilities

    caps = await resolve_platform_pg_capabilities()
    return frozenset(caps.get("features") or [])

async def can_platform_pg_page(db_user: BotUser | None, key: str) -> bool:
    """True when platform bot admin may open a PasarGuard bot surface."""
    if not is_platform_admin(db_user):
        return False
    return key in await platform_pg_features()

async def can_platform_pg_action(db_user: BotUser | None, resource: str, action: str) -> bool:
    if not is_platform_admin(db_user):
        return False
    from app.services.authz import authz_from_staff, can_pg_action
    from app.services.pg_access import resolve_platform_pg_capabilities, staff_from_platform_caps

    caps = await resolve_platform_pg_capabilities()
    if not caps.get("ok"):
        return False
    return can_pg_action(authz_from_staff(staff_from_platform_caps(caps)), resource, action)

async def platform_pg_quota_staff() -> dict:
    """Staff-shaped dict for the env PG admin, usable with ``app.services.pg_quota``.

    Bot PG tools are platform-admin-only, so unlike the web panel (which
    always threads a full ``staff`` dict through ``pg_quota``), bot handlers
    had no way to run the same max_users / data-cap pre-check. This mirrors
    the same Hybrid-Owner-aware probe used for menu/action gating so a
    limited env PasarGuard account gets a friendly Persian quota message
    from the bot too, instead of only PasarGuard's raw rejection.
    """
    from app.services.pg_access import resolve_platform_pg_capabilities, staff_from_platform_caps

    caps = await resolve_platform_pg_capabilities()
    staff = staff_from_platform_caps(caps)
    return {
        "role": "admin",
        "pg_is_owner": bool(staff.get("pg_is_owner")),
        "pg_admin_username": staff.get("pg_admin_username"),
        "pg_role_id": staff.get("pg_role_id"),
    }

async def platform_can_manage_representatives() -> bool:
    """True when the env PG account may create admins / shop representatives."""
    from app.services.pg_access import resolve_platform_pg_capabilities, staff_from_platform_caps
    from app.services.principal_provisioning import owner_has_pg_admin_create_capability

    caps = await resolve_platform_pg_capabilities()
    if not caps.get("ok"):
        return False
    return owner_has_pg_admin_create_capability(staff_from_platform_caps(caps))

def require_platform_rep_mgmt(fn: Callable[..., Awaitable[Any]]):
    """Deny Hybrid Owner without ``admins.create`` from representative management."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any):
        from aiogram.types import CallbackQuery, Message

        if not await platform_can_manage_representatives():
            callback = None
            message = None
            for value in (*args, *kwargs.values()):
                if callback is None and isinstance(value, CallbackQuery):
                    callback = value
                elif message is None and isinstance(value, Message):
                    message = value
            text = "قابلیت ساخت نماینده برای این حساب فعال نیست"
            if callback is not None:
                try:
                    await callback.answer(text, show_alert=True)
                except Exception:
                    pass
            elif message is not None:
                try:
                    await message.answer(text)
                except Exception:
                    pass
            return None
        return await fn(*args, **kwargs)

    return wrapper

async def filtered_pg_reply_keyboard(
    db_user: BotUser | None = None,
    ui: dict | None = None,
    *,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    """Heal to the stable main ReplyKeyboard after PG FSM cancel/save (Option B).

    PG leaves live on the inline panel; this never swaps in submenu chrome.
    ``reseller_profile_id`` kept for call-site compatibility.
    """
    _ = reseller_profile_id
    from app.services.users import get_all_settings

    if ui is None and session is not None:
        ui = await get_all_settings(session)
    if session is not None and db_user is not None:
        from app.bot.menu_nav import build_main_reply_keyboard

        markup, _, _ = await build_main_reply_keyboard(
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
            ui=ui,
        )
        return markup
    # No session/user context — empty main-style fallback (should be rare)
    from app.bot import keyboards as kb

    return kb.persistent_reply_keyboard(ui)
