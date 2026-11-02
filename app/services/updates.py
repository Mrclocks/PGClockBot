from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from app.version import (
    GITHUB_RELEASES_API,
    GITHUB_REPO_URL,
    __version__,
)

logger = logging.getLogger(__name__)

_CACHE: dict[str, Any] = {"at": 0.0, "data": None, "ok": False, "channel": ""}
_CACHE_TTL_OK = 300.0  # successful checks
_CACHE_TTL_FAIL = 30.0  # failed checks — retry soon
_VER_RE = re.compile(r"^v?\d+(\.\d+)*([.-][A-Za-z0-9]+)*$")


def _active_channel() -> str:
    from app.services.update_channel import get_update_channel

    return get_update_channel()


def local_version() -> str:
    return (__version__ or "").strip()


def _parse_ver(value: str | None) -> tuple[int, ...]:
    if not value:
        return (0,)
    parts: list[int] = []
    for chunk in str(value).strip().lstrip("vV").split("."):
        num = ""
        for ch in chunk:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    return tuple(parts) or (0,)


def is_newer(remote: str | None, local: str | None) -> bool:
    """True when remote version is strictly newer than local."""
    if not remote or not local:
        return bool(remote and remote != local)
    return _parse_ver(remote) > _parse_ver(local)


def is_same_or_newer(candidate: str | None, baseline: str | None) -> bool:
    """True when candidate >= baseline."""
    if not candidate:
        return False
    if not baseline:
        return True
    return _parse_ver(candidate) >= _parse_ver(baseline)


def peek_update_cache() -> dict[str, Any] | None:
    """Return last cached GitHub check without network I/O (for sidebar badge)."""
    data = _CACHE.get("data")
    return dict(data) if isinstance(data, dict) else None


def _valid_version(value: str | None) -> bool:
    if not value:
        return False
    return bool(_VER_RE.match(value.strip()))


def _parse_version_body(text: str | None) -> str | None:
    remote = (text or "").strip().splitlines()[0].strip() if text else ""
    if _valid_version(remote):
        return remote.lstrip("vV")
    return None


async def _fetch_version_via_contents_api(
    client: httpx.AsyncClient, *, channel: str
) -> str | None:
    """Read VERSION via GitHub Contents API (bypasses raw.githubusercontent CDN)."""
    import base64

    from app.version import GITHUB_REPO

    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/VERSION"
    try:
        resp = await client.get(
            url, headers=_github_headers(), params={"ref": channel}
        )
        if resp.status_code != 200:
            return None
        payload = resp.json() or {}
        raw_b64 = str(payload.get("content") or "")
        if not raw_b64:
            return None
        decoded = base64.b64decode(raw_b64).decode("utf-8", errors="replace")
        return _parse_version_body(decoded)
    except Exception as e:
        logger.debug("github contents VERSION failed (%s): %s", channel, e)
        return None


async def _fetch_remote_version_candidates(
    client: httpx.AsyncClient,
    *,
    channel: str | None = None,
) -> list[str]:
    """Collect version strings for the active update channel.

    Sources (max wins):
    - raw.githubusercontent VERSION (cache-busted)
    - GitHub Contents API VERSION (same file, different CDN — helps when raw is stale)
    - main only: GitHub releases/latest
    - `dev` only: newest prerelease whose target is the `dev` branch
    """
    from app.services.update_channel import github_version_url, normalize_channel

    ch = normalize_channel(channel if channel is not None else _active_channel())
    found: list[str] = []
    bust = {"_": str(int(time.time()))}
    headers = {
        "User-Agent": "PGClockBot-Panel",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    version_url = github_version_url(channel=ch)
    try:
        resp = await client.get(version_url, headers=headers, params=bust)
        if resp.status_code == 200:
            ver = _parse_version_body(resp.text)
            if ver:
                found.append(ver)
    except Exception as e:
        logger.debug("github VERSION fetch failed (%s): %s", ch, e)

    contents_ver = await _fetch_version_via_contents_api(client, channel=ch)
    if contents_ver:
        found.append(contents_ver)

    if ch == "main":
        try:
            resp = await client.get(
                GITHUB_RELEASES_API + "/latest", headers=_github_headers()
            )
            if resp.status_code == 200:
                payload = resp.json()
                tag = normalize_version_tag(
                    (payload or {}).get("tag_name") or (payload or {}).get("name")
                )
                if tag:
                    found.append(tag)
        except Exception as e:
            logger.debug("github releases/latest fetch failed: %s", e)
    elif ch == "dev":
        # Dev tip is published as GitHub prerelease (Latest stays on main).
        try:
            resp = await client.get(
                GITHUB_RELEASES_API,
                headers=_github_headers(),
                params={"per_page": 20},
            )
            if resp.status_code == 200:
                for row in resp.json() or []:
                    if not isinstance(row, dict) or not row.get("prerelease"):
                        continue
                    target = str(row.get("target_commitish") or "").strip().lower()
                    if target in {"main", "master"}:
                        continue
                    tag = normalize_version_tag(
                        row.get("tag_name") or row.get("name")
                    )
                    if tag:
                        found.append(tag)
        except Exception as e:
            logger.debug("github prereleases fetch failed: %s", e)

    return found


async def check_github_update(
    *,
    timeout: float = 4.0,
    force: bool = False,
    channel: str | None = None,
) -> dict[str, Any]:
    """
    Compare local app version with the configured update channel on GitHub.
    Returns: version, remote_version, update_available, label, tone (ok|warn|err)
    """
    from app.services.update_channel import channel_label_fa, normalize_channel

    ch = normalize_channel(channel if channel is not None else _active_channel())
    now = time.monotonic()
    cached = _CACHE.get("data")
    if (
        not force
        and isinstance(cached, dict)
        and _CACHE.get("channel") == ch
        and (now - float(_CACHE["at"])) < (_CACHE_TTL_OK if _CACHE.get("ok") else _CACHE_TTL_FAIL)
    ):
        return dict(cached)

    local = local_version()
    result: dict[str, Any] = {
        "version": local,
        "remote_version": None,
        "update_available": False,
        "label": "آخرین نسخه",
        "tone": "ok",
        "repo_url": GITHUB_REPO_URL,
        "checked": False,
        "channel": ch,
        "channel_label": channel_label_fa(ch),
    }
    ok = False
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            candidates = await _fetch_remote_version_candidates(client, channel=ch)
        label_fa = channel_label_fa(ch)
        if not candidates:
            result["label"] = f"بررسی آپدیت کانال {label_fa} ناموفق"
            result["tone"] = "warn"
        else:
            remote = max(candidates, key=_parse_ver)
            result["remote_version"] = remote
            result["checked"] = True
            ok = True
            if is_newer(remote, local):
                result["update_available"] = True
                result["label"] = f"برای کانال {label_fa} آپدیت {remote} آماده است"
                result["tone"] = "err"
            else:
                result["label"] = f"برای کانال {label_fa} آپدیت جدیدی نیست"
                result["tone"] = "ok"
    except Exception as e:
        logger.debug("github version check failed: %s", e)
        result["label"] = f"بررسی آپدیت کانال {channel_label_fa(ch)} ناموفق"
        result["tone"] = "warn"
    _CACHE.update({"at": now, "data": dict(result), "ok": ok, "channel": ch})
    return result


def clear_update_cache() -> None:
    _CACHE["at"] = 0.0
    _CACHE["data"] = None
    _CACHE["ok"] = False
    _CACHE["channel"] = ""
    try:
        from app.services.update_channel import clear_alembic_tree_cache
        from app.services.release_notes import clear_remote_notes_cache

        clear_alembic_tree_cache()
        clear_remote_notes_cache()
    except Exception:
        logger.debug("clear_update_cache: ignored failure", exc_info=True)


def normalize_version_tag(value: str | None) -> str | None:
    """Return bare semver (no leading v) when value looks like a release tag."""
    if not value:
        return None
    raw = str(value).strip()
    if not _valid_version(raw):
        return None
    return raw.lstrip("vV")


def _github_headers() -> dict[str, str]:
    return {
        "User-Agent": "PGClockBot-Panel",
        "Accept": "application/vnd.github+json",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
