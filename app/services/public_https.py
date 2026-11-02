"""Public https origin for payment gateway callbacks.

TLS enabled from the panel is stored with the certificate, not in ``.env``, so the
live panel address wins over ``PUBLIC_BASE_URL`` / ``WEBHOOK_URL``.

Do not strip a non-443 listen port from that address: many installs terminate TLS
on the app port (e.g. ``:9000``) and have nothing healthy on ``:443``. Publishing
a bare ``https://host`` then yields ``ERR_CONNECTION_CLOSED`` in the browser.
"""

from __future__ import annotations

from app.config import get_settings

# Imported as module attrs so tests can patch ``public_https.https_is_active``.
from app.services.ssl_certs import https_is_active, public_panel_base_url


def env_public_base() -> str:
    """Configured PUBLIC_BASE_URL or WEBHOOK_URL (any scheme), stripped."""
    cfg = get_settings()
    return (cfg.public_base_url or cfg.webhook_url or "").rstrip("/")


def https_public_base() -> str:
    """Https origin for callbacks, or empty when none is available.

    Order: live panel https (when TLS is active) → env base only if it is https.
    """
    if https_is_active():
        live = (public_panel_base_url() or "").strip().rstrip("/")
        if live.startswith("https://"):
            return live
    configured = env_public_base()
    return configured if configured.startswith("https://") else ""
