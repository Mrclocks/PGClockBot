"""Rules for the public address Telegram posts the main bot's updates to.

Kept free of other imports so the SSL and setup code can use them: ``telegram_port_error`` refuses a
port Telegram will not deliver to, and ``webhook_after_domain_change`` lets the webhook follow the
panel when HTTPS is enabled on a new domain (issuing that certificate has just proved the domain
reaches this server).
"""

from __future__ import annotations

from urllib.parse import urlparse

# The only ports Telegram delivers webhooks to (Bot API, setWebhook).
TELEGRAM_WEBHOOK_PORTS = frozenset({443, 80, 88, 8443})
PORT_NOT_ALLOWED = "تلگرام Webhook را فقط روی پورت‌های ۴۴۳، ۸۰، ۸۸ و ۸۴۴۳ می‌فرستد."


def telegram_port_error(url: str) -> str | None:
    """``PORT_NOT_ALLOWED`` when ``url``'s port is one Telegram will not deliver to."""
    try:
        parsed = urlparse(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return PORT_NOT_ALLOWED
    return None if port in TELEGRAM_WEBHOOK_PORTS else PORT_NOT_ALLOWED


def _origin(url: str | None) -> str:
    parsed = urlparse((url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}".lower()


def webhook_after_domain_change(*, old_public: str, old_webhook: str, new_public: str) -> str | None:
    """The new ``WEBHOOK_URL`` when the webhook was riding the panel's old address, else ``None``.

    A webhook set to some other origin (a separate proxy or domain) is the operator's choice and is
    left alone, as is a new address Telegram could not deliver to.
    """
    old_hook, new_origin = _origin(old_webhook), _origin(new_public)
    if not old_hook or not new_origin or old_hook != _origin(old_public) or old_hook == new_origin:
        return None
    if not new_origin.startswith("https://") or telegram_port_error(new_origin):
        return None
    return new_origin
