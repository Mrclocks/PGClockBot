"""Shared dialogs in the design language keep every hook panel.js and the routes rely on."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.api.app import templates


def render_base() -> str:
    request = SimpleNamespace(query_params={}, url=SimpleNamespace(path="/home"), state=SimpleNamespace())
    return templates.env.get_template("base.html").render(
        request=request, app_version="t", staff={"role": "admin", "permissions": []},
        identity={"kind": "owner"}, is_owner=True, csrf_token="tok")


def render_macro(source: str, **ctx) -> str:
    return templates.env.from_string('{% from "_ds.html" import ds_switch, ds_foot, ds_modal_head %}' + source).render(**ctx)


class ConfirmDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = render_base()

    def test_confirm_keeps_the_ids_the_script_fills(self) -> None:
        confirm = self.html.split('id="modal-confirm"', 1)[1].split('id="confirm-form"', 1)[1].split("</form>", 1)[0]
        for hook in ('id="confirm-message"', 'id="confirm-reason-slot"', 'id="confirm-submit"', 'name="csrf_token"'):
            self.assertIn(hook, confirm)
        self.assertIn('id="confirm-title"', self.html)

    def test_confirmations_open_as_short_sheets_without_a_caption(self) -> None:
        self.assertIn('class="ui-modal ds ds-dialog" id="modal-confirm"', self.html)
        self.assertIn('class="ui-modal ds ds-dialog" id="modal-inbox-dismiss"', self.html)
        head = self.html.split('id="modal-confirm"', 1)[1].split("</h2>", 1)[0]
        self.assertNotIn("modal-title-caption", head)

    def test_inbox_dismiss_still_posts_its_fields(self) -> None:
        form = self.html.split('id="form-inbox-dismiss"', 1)[1].split("</form>", 1)[0]
        for name in ("alert_key", "entity_id", "return_to", "mode", "csrf_token"):
            self.assertIn(f'name="{name}"', form)
        self.assertIn('id="inbox-dismiss-submit"', form)


class DesignMacroTests(unittest.TestCase):
    def test_a_switch_posts_one_when_on(self) -> None:
        html = render_macro("{{ ds_switch('refund_to_wallet', 'برگشت به کیف پول') }}")
        self.assertIn('name="refund_to_wallet" value="1"', html)
        self.assertNotIn("checked", html)

    def test_labels_are_escaped(self) -> None:
        html = render_macro("{{ ds_switch('x', label) }}{{ ds_foot(label) }}", label="<script>1</script>")
        self.assertNotIn("<script>", html)

    def test_form_save_bar_is_pinned_without_cancel(self) -> None:
        html = render_macro("{{ ds_foot('ذخیره') }}")
        self.assertIn('class="ui-modal-sticky-save ds-foot"', html)
        self.assertIn('type="submit"', html)
        self.assertNotIn("data-modal-close", html)

    def test_dialog_save_bar_keeps_cancel_when_asked(self) -> None:
        html = render_macro("{{ ds_foot('باطل کن', cancel=true) }}")
        self.assertIn("data-modal-close", html)
        self.assertIn("انصراف", html)


class GatedSettingsSectionTests(unittest.TestCase):
    """A payment section whose first field is its on/off switch shows that switch in its header."""

    def render(self, enabled: str) -> str:
        groups = {"کارت به کارت": [
            ("pay_card_enabled", "کارت به کارت", "toggle", ""),
            ("pay_card_random", "نمایش کارت تصادفی", "toggle", ""),
        ]}
        return templates.env.get_template("_domain_settings_form.html").render(
            settings_form_action="/settings", tab_groups=list(groups), groups=groups,
            values={"pay_card_enabled": enabled}, app_version="t")

    def test_section_title_is_not_repeated_as_switch_label(self) -> None:
        html = self.render("1")
        self.assertEqual(html.count("کارت به کارت"), 2)  # header title and the switch's aria-label
        self.assertIn('name="s_pay_card_enabled" value="1" aria-label="کارت به کارت" checked', html)
        self.assertEqual(html.count('name="s_pay_card_enabled"'), 1)

    def test_section_body_is_hidden_while_switched_off(self) -> None:
        html = self.render("0")
        body = html.split('data-pay-settings-for="pay_card_enabled"', 1)[1].split(">", 1)[0]
        self.assertIn("hidden", body)
        self.assertNotIn('aria-label="کارت به کارت" checked', html)


if __name__ == "__main__":
    unittest.main()
