"""Entries of the discount and customer-behaviour screens, shared by the hubs and their handler."""

from __future__ import annotations

from app.bot.hub_entries import HubEntry

DISCOUNTS_CALLBACK = "dsc:l"
FUNNEL_CALLBACK = "fnl:v"
ADMIN_DISCOUNTS = HubEntry("btn_adm_discounts", DISCOUNTS_CALLBACK, "🏷 تخفیف‌ها")
ADMIN_FUNNEL = HubEntry("btn_adm_funnel", FUNNEL_CALLBACK, "📊 رفتار کاربر")
