"""Hub buttons whose screens live in other handlers: label key, fallback label and callback.

The label comes from the ``btn_*`` setting, so the owner (or a shop for its ``btn_res_*`` copy)
can rename and recolor it from the web settings or the menu editor like any other hub button.
"""

from __future__ import annotations

from dataclasses import dataclass

TRIAL_USERS_CALLBACK = "tru:l:all:0"
STAFF_AUDIT_CALLBACK = "saud:l:0"
SERVICE_SHOP_REVIEW_CALLBACK = "ssr:l:0"


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
SERVICE_SHOP_REVIEW = HubEntry("btn_adm_service_shop_review", SERVICE_SHOP_REVIEW_CALLBACK, "🏷 تعیین فروشگاه سرویس")
