"""Entry of the broadcast history screen, shared by the hub and its handler."""

from __future__ import annotations

from app.bot.hub_entries import HubEntry

BROADCAST_HISTORY_CALLBACK = "bch:l"
BROADCAST_HISTORY = HubEntry("btn_adm_broadcast_history", BROADCAST_HISTORY_CALLBACK, "📜 تاریخچهٔ ارسال‌ها")
