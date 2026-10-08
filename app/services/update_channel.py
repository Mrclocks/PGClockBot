"""Update channel (main=stable / dev) — env-backed, used by panel update + header badge."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from app.services.migration_metadata import clear_revision_metadata_cache, fetch_revision_ids
from app.services.migration_metadata import parse_revision_assignment as parse_revision_assignment
from app.version import GITHUB_REPO

logger = logging.getLogger(__name__)

CHANNELS: tuple[str, ...] = ("main", "dev")
DEFAULT_CHANNEL = "main"

CHANNEL_LABELS_FA: dict[str, str] = {
    "main": "پایدار",
    "dev": "توسعه",
}

_ALEMBIC_TREE_CACHE: dict[str, Any] = {"at": 0.0, "channel": "", "ids": None, "ok": False}
_ALEMBIC_TREE_TTL_OK = 120.0
_ALEMBIC_TREE_TTL_FAIL = 30.0


def normalize_channel(value: object | None) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"dev", "develop", "development", "unstable"}:
        return "dev"
    if raw in {"main", "master", "stable", "prod", "production"}:
        return "main"
    return DEFAULT_CHANNEL


def channel_label_fa(channel: object | None = None) -> str:
    ch = normalize_channel(channel if channel is not None else get_update_channel())
    return CHANNEL_LABELS_FA.get(ch, CHANNEL_LABELS_FA[DEFAULT_CHANNEL])


def get_update_channel() -> str:
    """Selected channel for update checks / next deploy (dropdown)."""
    try:
        from app.config import get_settings

        return normalize_channel(getattr(get_settings(), "update_channel", None))
    except Exception:
        return DEFAULT_CHANNEL


def get_deployed_channel() -> str:
    """Channel tip currently installed — header badge source of truth."""
    try:
        from app.config import get_settings

        return normalize_channel(getattr(get_settings(), "deployed_channel", None))
    except Exception:
        return DEFAULT_CHANNEL


def set_update_channel(channel: object | None) -> str:
    """Persist UPDATE_CHANNEL in .env and clear settings + update caches.

    Does not change the header badge (see ``set_deployed_channel``).
    """
    ch = normalize_channel(channel)
    from app.services.setup_wizard import update_env_keys
    from app.services.updates import clear_update_cache

    update_env_keys({"UPDATE_CHANNEL": ch})
    clear_update_cache()
    clear_alembic_tree_cache()
    return ch


def set_deployed_channel(channel: object | None) -> str:
    """Persist DEPLOYED_CHANNEL after a successful channel deploy/rollback."""
    ch = normalize_channel(channel)
    from app.config import get_settings
    from app.services.setup_wizard import update_env_keys

    update_env_keys({"DEPLOYED_CHANNEL": ch})
    get_settings.cache_clear()
    return ch


def channel_branch(channel: object | None = None) -> str:
    return normalize_channel(channel if channel is not None else get_update_channel())


def github_raw_url(path: str, *, channel: object | None = None) -> str:
    ref = channel_branch(channel)
    clean = str(path or "").lstrip("/")
    return f"https://raw.githubusercontent.com/{GITHUB_REPO}/{ref}/{clean}"


def github_version_url(*, channel: object | None = None) -> str:
    return github_raw_url("VERSION", channel=channel)


def github_release_notes_url(*, channel: object | None = None) -> str:
    return github_raw_url("app/services/release_notes.py", channel=channel)


def channel_context(channel: object | None = None) -> dict[str, Any]:
    """Dropdown / update-check channel (preferred tip), not the header badge."""
    ch = normalize_channel(channel if channel is not None else get_update_channel())
    return {
        "channel": ch,
        "channel_label": channel_label_fa(ch),
        "channels": [
            {"id": "main", "label": CHANNEL_LABELS_FA["main"]},
            {"id": "dev", "label": CHANNEL_LABELS_FA["dev"]},
        ],
    }


def badge_context(channel: object | None = None) -> dict[str, Any]:
    """Header badge — reflects the actually deployed/running channel tip."""
    ch = normalize_channel(channel if channel is not None else get_deployed_channel())
    return {
        "deployed_channel": ch,
        "deployed_channel_label": channel_label_fa(ch),
    }


def clear_alembic_tree_cache() -> None:
    _ALEMBIC_TREE_CACHE.update({"at": 0.0, "channel": "", "ids": None, "ok": False})
    clear_revision_metadata_cache()


def _github_headers() -> dict[str, str]:
    return {
        "User-Agent": "PGClockBot-Panel",
        "Accept": "application/vnd.github+json",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }


async def fetch_remote_alembic_revision_ids(
    channel: object | None = None,
    *,
    timeout: float = 6.0,
    force: bool = False,
) -> set[str] | None:
    """Revision ids present under alembic/versions on the channel branch.

    Returns None when complete remote metadata could not be loaded.
    """
    ch = channel_branch(channel)
    now = time.monotonic()
    same_channel = _ALEMBIC_TREE_CACHE.get("channel") == ch
    cached_ids = _ALEMBIC_TREE_CACHE.get("ids") if same_channel else None
    ttl = _ALEMBIC_TREE_TTL_OK if _ALEMBIC_TREE_CACHE.get("ok") else _ALEMBIC_TREE_TTL_FAIL
    if not force and same_channel and (now - float(_ALEMBIC_TREE_CACHE["at"])) < ttl:
        return set(cached_ids) if isinstance(cached_ids, set) else None
    try:
        async with asyncio.timeout(timeout), httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            ids = await fetch_revision_ids(
                client, repository=GITHUB_REPO, channel=ch, headers=_github_headers(),
            )
        _ALEMBIC_TREE_CACHE.update({"at": now, "channel": ch, "ids": set(ids), "ok": True})
        return set(ids)
    except (httpx.HTTPError, ValueError, TimeoutError) as e:
        logger.debug("remote alembic tree fetch failed (%s): %s", ch, e)
        _ALEMBIC_TREE_CACHE.update(
            {
                "at": now,
                "channel": ch,
                "ids": set(cached_ids) if isinstance(cached_ids, set) else None,
                "ok": False,
            }
        )
        return set(cached_ids) if isinstance(cached_ids, set) else None


def local_alembic_revision_ids() -> set[str]:
    """Revision ids from the installed alembic/versions tree."""
    try:
        from alembic.script import ScriptDirectory

        from app.db.alembic_runner import alembic_config

        script = ScriptDirectory.from_config(alembic_config())
        return {str(rev.revision) for rev in script.walk_revisions()}
    except Exception as e:
        logger.debug("local alembic revisions failed: %s", e)
        return set()


def evaluate_migration_preflight(
    *,
    db_revision: str | None,
    remote_revision_ids: set[str] | None,
) -> dict[str, Any]:
    """Gate deploy when DB revision is absent from the target channel.

    Panel never downgrades Alembic — switching to an older channel tip that
    lacks the current DB revision must be blocked.
    """
    db_rev = (db_revision or "").strip() or None
    result: dict[str, Any] = {
        "checked": False,
        "blocked": False,
        "db_revision": db_rev,
        "tone": "ok",
        "label": "مایگریشن سازگار است",
        "message": "",
    }
    if not db_rev:
        result["checked"] = True
        result["label"] = "نسخه مایگریشن دیتابیس مشخص نیست"
        result["tone"] = "warn"
        result["message"] = "قبل از آپدیت، در صورت امکان pgclock migrate را بررسی کنید."
        return result
    if remote_revision_ids is None:
        result["label"] = "بررسی مایگریشن کانال ناموفق"
        result["tone"] = "warn"
        result["message"] = (
            "نتوانستیم لیست مایگریشن کانال مقصد را از گیت‌هاب بخوانیم. "
            "اگر اخیراً روی کانال جدیدتری بوده‌اید، با احتیاط ادامه دهید."
        )
        return result
    result["checked"] = True
    if db_rev in remote_revision_ids:
        result["label"] = "مایگریشن سازگار است"
        result["tone"] = "ok"
        result["message"] = ""
        return result
    result["blocked"] = True
    result["tone"] = "err"
    result["label"] = "آپدیت این کانال بلاک شد"
    result["message"] = (
        f"دیتابیس روی مایگریشن «{db_rev}» است که در کانال مقصد وجود ندارد. "
        "پنل مایگریشن را پایین نمی‌آورد؛ ابتدا به کانالی بروید که این revision را دارد "
        "یا دیتابیس را جداگانه هم‌تراز کنید."
    )
    return result


async def migration_preflight(
    channel: object | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    ch = channel_branch(channel)
    db_rev: str | None = None
    try:
        from app.config import get_settings
        from app.db.alembic_runner import current_revision

        db_rev = await asyncio.to_thread(current_revision, get_settings().database_url)
    except Exception as e:
        logger.debug("current_revision failed: %s", e)
    remote_ids = await fetch_remote_alembic_revision_ids(ch, force=force)
    out = evaluate_migration_preflight(
        db_revision=db_rev,
        remote_revision_ids=remote_ids,
    )
    out["channel"] = ch
    return out
