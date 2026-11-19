"""Hub buttons whose screens live in other handlers: label key, fallback label and callback.

The label comes from the ``btn_*`` setting, so the owner (or a shop for its ``btn_res_*`` copy)
can rename and recolor it from the web settings or the menu editor like any other hub button.
"""

from __future__ import annotations

from dataclasses import dataclass

TRIAL_USERS_CALLBACK = "tru:l:all:0"
STAFF_AUDIT_CALLBACK = "saud:l:0"
SERVICE_SHOP_REVIEW_CALLBACK = "ssr:l:0"
QUICK_LINKS_CALLBACK = "qlk:v"
COMMAND_CENTER_CALLBACK = "cmd:h"
# The same search, opened from the settings hub: results start on the settings.
COMMAND_CENTER_SETTINGS_CALLBACK = "cmd:hs"
# «🔎 جستجو» beside the command center: the plain search, without the switches panel.
COMMAND_SEARCH_CALLBACK = "cmd:s"


@dataclass(frozen=True, slots=True)
class HubEntry:
    label_key: str
    callback: str
    # Shown when the setting is blank, so the button never loses its text.
    fallback: str

    @property
    def style_id(self) -> str:
        return self.label_key.removeprefix("btn_")


ADMIN_TRIAL_USERS = HubEntry("btn_adm_trial_users", TRIAL_USERS_CALLBACK, "🧪 کاربران تست")
SHOP_TRIAL_USERS = HubEntry("btn_res_trial_users", TRIAL_USERS_CALLBACK, "🧪 کاربران تست")
ADMIN_STAFF_AUDIT = HubEntry("btn_adm_staff_audit", STAFF_AUDIT_CALLBACK, "🧾 گزارش کارکنان")
SHOP_STAFF_AUDIT = HubEntry("btn_res_staff_audit", STAFF_AUDIT_CALLBACK, "🧾 گزارش کارکنان")
ADMIN_QUICK_LINKS = HubEntry("btn_adm_quick_links", QUICK_LINKS_CALLBACK, "🔗 لینک‌های سریع")
SHOP_QUICK_LINKS = HubEntry("btn_res_quick_links", QUICK_LINKS_CALLBACK, "🔗 لینک‌های سریع")
ADMIN_COMMAND_CENTER = HubEntry("btn_adm_command_center", COMMAND_CENTER_CALLBACK, "⚡ مرکز فرمان")
SHOP_COMMAND_CENTER = HubEntry("btn_res_command_center", COMMAND_CENTER_CALLBACK, "⚡ مرکز فرمان")
ADMIN_SEARCH = HubEntry("btn_adm_search", COMMAND_SEARCH_CALLBACK, "🔎 جستجو")
SHOP_SEARCH = HubEntry("btn_res_search", COMMAND_SEARCH_CALLBACK, "🔎 جستجو")
SERVICE_SHOP_REVIEW = HubEntry("btn_adm_service_shop_review", SERVICE_SHOP_REVIEW_CALLBACK, "🏷 تعیین فروشگاه سرویس")
