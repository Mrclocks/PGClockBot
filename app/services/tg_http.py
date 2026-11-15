"""Proxy-aware httpx client for direct Telegram Bot API calls.

The aiogram Bot instance uses ``TG_PROXY`` for its session, but several places
in the codebase make one-off ``httpx`` calls to ``api.telegram.org`` (status
checks, file downloads, doctor probes) that bypassed the proxy entirely.  On
servers that cannot reach ``api.telegram.org`` directly those calls failed with
"All connection attempts failed" while the bot itself kept working through the
proxy.

``tg_client()`` returns an ``httpx.AsyncClient`` wired to the configured
outbound proxy (empty ``TG_PROXY`` keeps the historical direct-connection
behaviour).  All callers must use it instead of a bare ``httpx.AsyncClient``.
"""

from __future__ import annotations

import httpx

from app.config import get_settings

_DEFAULT_TIMEOUT = 8.0


def tg_client(*, timeout: float = _DEFAULT_TIMEOUT) -> httpx.AsyncClient:
    """Build an ``httpx.AsyncClient`` for ``api.telegram.org`` that honours ``TG_PROXY``."""
    settings = get_settings()
    proxy = (settings.tg_proxy or "").strip()
    kwargs: dict[str, object] = {"timeout": timeout}
    if proxy:
        kwargs["proxy"] = proxy
    return httpx.AsyncClient(**kwargs)
