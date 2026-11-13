"""PasarGuard screens that the PG hub links to: editable label keys and callbacks."""

from __future__ import annotations

from app.bot.hub_entries import HubEntry

PG_HOSTS_CALLBACK = "adm:pg:hosts"
PG_INBOUNDS_CALLBACK = "adm:pg:inbounds"
PG_ADMINS_CALLBACK = "adm:pg:admins"

PG_HOSTS = HubEntry("btn_pg_hosts", PG_HOSTS_CALLBACK, "🌐 هاست‌ها")
PG_INBOUNDS = HubEntry("btn_pg_inbounds", PG_INBOUNDS_CALLBACK, "📡 اینباندها")
PG_ADMINS = HubEntry("btn_pg_admins", PG_ADMINS_CALLBACK, "🛡 ادمین‌های پنل")
