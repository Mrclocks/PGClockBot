"""Settings of «افزودن سرویس قبلی»: switch, mode, per-customer cap, allowed paths and panel admins.

The subscription paths and the extra panel admins describe the one PasarGuard panel, so only the
platform Owner can set them. A shop always imports from its own panel admin and never from a list.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from app.config import get_settings, normalize_pg_subscription_path

KEY_ENABLED = "service_import_enabled"
KEY_MODE = "service_import_mode"
KEY_MAX_PER_USER = "service_import_max_per_user"
KEY_PATHS = "service_import_paths"
KEY_ADMINS = "service_import_admins"

PLATFORM_ONLY_KEYS = frozenset({KEY_PATHS, KEY_ADMINS})

MODE_INSTANT = "instant"
MODE_APPROVAL = "approval"
MODES = (MODE_INSTANT, MODE_APPROVAL)

DEFAULT_MAX_PER_USER = 5
# A stored cap of 0 means a customer may add any number of earlier services.
UNLIMITED_PER_USER = 0
# Upper bound only to reject absurd input; any real shop wanting more uses UNLIMITED_PER_USER.
MAX_PER_USER_LIMIT = 100_000
MAX_LIST_ITEMS = 10
_ADMIN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_LIST_SEPARATORS = re.compile(r"[\s,;،]+")


@dataclass(frozen=True, slots=True)
class ImportSettings:
    enabled: bool
    mode: str
    max_per_user: int
    extra_paths: tuple[str, ...]
    extra_admins: tuple[str, ...]

    @property
    def needs_approval(self) -> bool:
        return self.mode == MODE_APPROVAL

    @property
    def is_unlimited(self) -> bool:
        return self.max_per_user == UNLIMITED_PER_USER


def split_list(raw: str | None) -> list[str]:
    return [part for part in _LIST_SEPARATORS.split(raw or "") if part]


def normalize_paths(raw: str | None) -> list[str]:
    """Valid, distinct subscription prefixes (``/sub``, ``/hamid``); anything else is dropped."""
    out: list[str] = []
    for part in split_list(raw):
        try:
            path = normalize_pg_subscription_path(part)
        except ValueError:
            continue
        if path not in out:
            out.append(path)
    return out[:MAX_LIST_ITEMS]


def normalize_admins(raw: str | None) -> list[str]:
    """Valid, distinct, lower-case panel admin usernames."""
    out: list[str] = []
    for part in split_list(raw):
        if _ADMIN_NAME.match(part) and part.lower() not in out:
            out.append(part.lower())
    return out[:MAX_LIST_ITEMS]


def normalize_max_per_user(raw: object) -> int:
    """Per-customer cap in ``[0, MAX_PER_USER_LIMIT]``; 0 means unlimited, junk means the default."""
    if raw is None or isinstance(raw, bool):
        return DEFAULT_MAX_PER_USER
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_MAX_PER_USER
    if value < UNLIMITED_PER_USER:
        # A typo such as "-5" must not silently lift the cap.
        return DEFAULT_MAX_PER_USER
    return min(value, MAX_PER_USER_LIMIT)


def normalize_setting_value(key: str, value: str) -> str:
    """Canonical stored form of one of this feature's settings (called by the save routes)."""
    if key == KEY_MODE:
        return value if value in MODES else MODE_INSTANT
    if key == KEY_MAX_PER_USER:
        return str(normalize_max_per_user(value))
    if key == KEY_PATHS:
        return "\n".join(normalize_paths(value))
    if key == KEY_ADMINS:
        return "\n".join(normalize_admins(value))
    return value


def read_settings(values: Mapping[str, str] | None) -> ImportSettings:
    data = values or {}
    mode = data.get(KEY_MODE) or MODE_INSTANT
    return ImportSettings(
        enabled=str(data.get(KEY_ENABLED, "0")).strip().lower() in {"1", "true", "yes", "on"},
        mode=mode if mode in MODES else MODE_INSTANT,
        max_per_user=normalize_max_per_user(data.get(KEY_MAX_PER_USER, DEFAULT_MAX_PER_USER)),
        extra_paths=tuple(normalize_paths(data.get(KEY_PATHS))),
        extra_admins=tuple(normalize_admins(data.get(KEY_ADMINS))),
    )


def allowed_paths(config: ImportSettings) -> tuple[str, ...]:
    """Subscription prefixes a link may use: the configured panel path first, then the listed ones."""
    configured = normalize_paths(get_settings().pg_subscription_path)
    merged = [*configured]
    for path in config.extra_paths:
        if path not in merged:
            merged.append(path)
    return tuple(merged[:MAX_LIST_ITEMS])
