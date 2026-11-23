"""Optional webhook delivery for shop bots.

Shop bots poll Telegram by default: one long-lived getUpdates connection per shop, open even when
nobody writes. When the operator turns on ``SHOP_BOT_WEBHOOK`` and the main bot already has a
public HTTPS ``WEBHOOK_URL``, each shop bot asks Telegram to post its updates to one shared path
on this server instead, so an idle shop costs nothing. Shop owners change nothing: the same token
is used, and a shop whose webhook cannot be registered keeps polling.

Every shop bot gets its own secret, derived from the server's webhook secret and the shop's
profile id, so one shop's secret never opens another shop's path. Nothing new is stored.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from app.config import get_settings
from app.services.setup_wizard import telegram_webhook_endpoint
from app.services.webhook_secret import ensure_webhook_secret

SHOP_WEBHOOK_PREFIX = "/telegram/shop"
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
_SECRET_CONTEXT = b"pgclock-shop-bot-webhook:"


@dataclass(frozen=True, slots=True)
class ShopWebhookConfig:
    """Where Telegram posts shop-bot updates and the key their per-shop secrets come from."""

    base_url: str
    master_secret: str

    def url_for(self, profile_id: int) -> str:
        return f"{self.base_url}{SHOP_WEBHOOK_PREFIX}/{int(profile_id)}"

    def secret_for(self, profile_id: int) -> str:
        """Hex digest: within Telegram's allowed secret alphabet and length."""
        message = _SECRET_CONTEXT + str(int(profile_id)).encode("ascii")
        return hmac.new(self.master_secret.encode("utf-8"), message, hashlib.sha256).hexdigest()

    def accepts(self, profile_id: int, header: str | None) -> bool:
        expected = hashlib.sha256(self.secret_for(profile_id).encode("ascii")).digest()
        got = hashlib.sha256((header or "").strip().encode("utf-8")).digest()
        return bool(header) and secrets.compare_digest(got, expected)


def shop_webhook_config() -> ShopWebhookConfig | None:
    """The config when the operator opted in and the main webhook has a public HTTPS origin."""
    settings = get_settings()
    if not settings.shop_bot_webhook:
        return None
    base, _path, _full = telegram_webhook_endpoint(settings.webhook_url, settings.webhook_path)
    if not base or not base.startswith("https://"):
        return None
    master = (settings.webhook_secret_token or "").strip() or ensure_webhook_secret()
    return ShopWebhookConfig(base_url=base.rstrip("/"), master_secret=master)
