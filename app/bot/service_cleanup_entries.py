"""Entries of the service cleanup screen, shared by the hubs and its handler."""

from __future__ import annotations

from app.bot.hub_entries import HubEntry

SERVICE_CLEANUP_CALLBACK = "scl:h"
ADMIN_SERVICE_CLEANUP = HubEntry("btn_adm_service_cleanup", SERVICE_CLEANUP_CALLBACK, "🗑 پاکسازی سرویس‌ها")
SHOP_SERVICE_CLEANUP = HubEntry("btn_res_service_cleanup", SERVICE_CLEANUP_CALLBACK, "🗑 پاکسازی سرویس‌ها")
