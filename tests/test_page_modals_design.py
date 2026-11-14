"""Page modals share the design-language head (no caption) and a pinned save bar."""

from __future__ import annotations

import re
import unittest

from tests import test_addon_general_scope


class PlansPageModalsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = test_addon_general_scope.PlansPageTests()._html()

    def modal(self, modal_id: str) -> str:
        """The modal's markup from its root div to the end of its first form."""
        match = re.search(rf'<div class="ui-modal[^"]*" id="{modal_id}"', self.html)
        return self.html[match.start(): self.html.index("</form>", match.start()) + len("</form>")] + \
            self.html[self.html.index("</form>", match.start()) + 7:][:600]

    def test_category_and_pack_modals_use_the_design_scope(self) -> None:
        for modal_id in ("modal-plan-categories", "modal-service-addons"):
            modal = self.modal(modal_id)
            self.assertRegex(modal, r'^<div class="ui-modal ds[ "]', modal_id)
            head = modal.split('class="ds-head-row"', 1)[1].split("</h2>", 1)[0]
            self.assertNotIn("modal-title-caption", head, modal_id)
            self.assertIn('ui-modal-sticky-save ds-foot', modal, modal_id)

    def test_form_fields_stay_inside_the_form(self) -> None:
        modal = self.modal("modal-service-addons")
        form = re.search(r"<form\b.*?</form>", modal, re.S).group(0)
        for name in ('name="name"', 'name="kind"', 'name="price"'):
            self.assertIn(name, form)


if __name__ == "__main__":
    unittest.main()
