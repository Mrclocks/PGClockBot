"""Buttons under usage alerts and cleanup or staff-action notices: a way out, the services and support."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.services.customer_action_notices import notice_markup_for
from app.services.customer_notice_buttons import RENEW_LABEL
from app.services.notice_links import opened_screen
from app.services.support_button import SUPPORT_NOTICE_CALLBACK
from app.services.usage_alerts import AlertExtras, time_alert_markup, traffic_alert_markup

ON = {"one_tap_renew_enabled": "1"}
OFF = {"one_tap_renew_enabled": "0"}


def callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def notice(action: str, service_id: int | None = 7) -> SimpleNamespace:
    return SimpleNamespace(action=action, service_id=service_id)


class UsageAlertButtonTests(unittest.TestCase):
    def test_time_alert_offers_renewal_services_and_support(self) -> None:
        self.assertEqual(
            callbacks(time_alert_markup(ON, 7, AlertExtras())),
            ["nto:svc:renew:7", "nto:svc:list", SUPPORT_NOTICE_CALLBACK],
        )

    def test_time_alert_keeps_services_and_support_when_renew_button_is_off(self) -> None:
        self.assertEqual(callbacks(time_alert_markup(OFF, 7, AlertExtras())), ["nto:svc:list", SUPPORT_NOTICE_CALLBACK])

    def test_traffic_alert_offers_pack_renewal_services_and_support(self) -> None:
        self.assertEqual(
            callbacks(traffic_alert_markup(ON, 7, AlertExtras(sells_volume_packs=True))),
            ["nto:svc:addon:7:volume", "nto:svc:renew:7", "nto:svc:list", SUPPORT_NOTICE_CALLBACK],
        )

    def test_renew_button_is_named_for_what_it_does(self) -> None:
        button = time_alert_markup(ON, 7, AlertExtras()).inline_keyboard[0][0]
        self.assertEqual(button.text, RENEW_LABEL)
        self.assertNotIn("ضرب", button.text)


class CleanupAndActionNoticeButtonTests(unittest.TestCase):
    def test_volume_warning_offers_pack_when_the_shop_sells_one(self) -> None:
        self.assertEqual(
            callbacks(notice_markup_for(notice("cleanup_volume_warning"), sells_volume_packs=True)),
            ["nto:svc:addon:7:volume", "nto:svc:renew:7", "nto:svc:list", SUPPORT_NOTICE_CALLBACK],
        )
        self.assertNotIn(
            "nto:svc:addon:7:volume", callbacks(notice_markup_for(notice("cleanup_volume_warning"), sells_volume_packs=False))
        )

    def test_expiry_warning_offers_renewal_and_help(self) -> None:
        self.assertEqual(
            callbacks(notice_markup_for(notice("cleanup_expired"))),
            ["nto:svc:renew:7", "nto:svc:list", SUPPORT_NOTICE_CALLBACK],
        )

    def test_deleted_service_offers_a_new_service_and_support(self) -> None:
        for action in ("cleanup_deleted", "cleanup_volume_deleted", "service_delete", "pg_user_delete"):
            with self.subTest(action=action):
                self.assertEqual(callbacks(notice_markup_for(notice(action))), ["nto:shop:list", SUPPORT_NOTICE_CALLBACK])

    def test_new_link_notice_points_to_the_service_list(self) -> None:
        self.assertEqual(callbacks(notice_markup_for(notice("pg_user_revoke_sub"))), ["nto:svc:list"])

    def test_disabled_service_notice_offers_support(self) -> None:
        self.assertIn(SUPPORT_NOTICE_CALLBACK, callbacks(notice_markup_for(notice("pg_user_disable"))))

    def test_notice_without_service_never_carries_a_service_button(self) -> None:
        self.assertEqual(
            callbacks(notice_markup_for(notice("cleanup_expired", service_id=None))), ["nto:svc:list", SUPPORT_NOTICE_CALLBACK]
        )


class NoticeScreenTests(unittest.TestCase):
    def test_renewal_pack_and_shop_open_under_the_notice(self) -> None:
        for screen in ("svc:renew:7", "svc:addon:7:volume", "shop:list"):
            with self.subTest(screen=screen):
                self.assertEqual(opened_screen(f"nto:{screen}"), screen)

    def test_unknown_screens_still_refused(self) -> None:
        self.assertIsNone(opened_screen("nto:svc:renewconfirm:7:1"))
        self.assertIsNone(opened_screen("nto:adm:wallet:credit:1"))


if __name__ == "__main__":
    unittest.main()
