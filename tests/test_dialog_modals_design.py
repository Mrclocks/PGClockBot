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

    def test_save_bar_is_pinned_and_has_cancel(self) -> None:
        html = render_macro("{{ ds_foot('ذخیره') }}")
        self.assertIn('class="ui-modal-sticky-save ds-foot"', html)
        self.assertIn('type="submit"', html)
        self.assertIn("data-modal-close", html)


if __name__ == "__main__":
    unittest.main()
