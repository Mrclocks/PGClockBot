"""Empty lists say why they are empty and offer the page's create action only to who may use it (DESIGN_SYSTEM.md §6-3)."""

from __future__ import annotations

import re
import unittest
from types import SimpleNamespace


from tests.test_marketing_nav import OWNER, _page
from tests.test_page_header import ENV


def render(body: str) -> str:
    return ENV.from_string('{% from "macros.html" import empty_state %}' + body).render().strip()


def empty_rows(html: str) -> list[str]:
    return re.findall(r'<tr class="table-empty">.*?</tr>', html, re.S)


class EmptyStateMacroTests(unittest.TestCase):
    def test_a_table_row_spans_the_columns_and_carries_the_marker(self) -> None:
        html = render("{{ empty_state('سفارشی نیست', 'card', colspan=9) }}")
        self.assertRegex(html, r'^<tr class="table-empty"><td colspan="9">')
        self.assertIn('class="empty-state-text">سفارشی نیست<', html)
        self.assertIn("<svg", html)

    def test_outside_a_table_it_is_a_block(self) -> None:
        html = render("{{ empty_state('خالی') }}")
        self.assertTrue(html.startswith('<div class="empty-state">'))

    def test_the_action_needs_a_label_and_a_target(self) -> None:
        self.assertNotIn("<button", render("{{ empty_state('x', action_modal='m') }}"))
        self.assertNotIn("<button", render("{{ empty_state('x', action_label=None, action_modal='m') }}"))
        self.assertIn('data-modal-open="m">ساخت</button>', render("{{ empty_state('x', action_label='ساخت', action_modal='m') }}"))
        self.assertIn('<a class="btn" href="/new">ساخت</a>', render("{{ empty_state('x', action_label='ساخت', action_href='/new') }}"))

    def test_text_and_labels_are_escaped(self) -> None:
        html = render("{{ empty_state('<b>x</b>', action_label='<i>y</i>', action_modal='m') }}")
        self.assertNotIn("<b>x</b>", html)
        self.assertNotIn("<i>y</i>", html)


class TicketListTests(unittest.TestCase):
    def tickets(self, **ctx: object) -> str:
        base = {"panel_tickets": [], "tg_rows": [], "show_tg": False, "can_create": False, "is_owner": True,
                "status_labels": {}, "priority_labels": {}, "priority_badge": {}, "status_badge": {}}
        return _page("tickets.html", "/tickets", OWNER, **{**base, **ctx})

    def test_an_empty_panel_list_is_one_empty_row(self) -> None:
        rows = empty_rows(self.tickets())
        self.assertEqual(len(rows), 1)
        self.assertIn('colspan="8"', rows[0])

    def test_the_create_action_follows_the_create_permission(self) -> None:
        self.assertNotIn('data-modal-open="modal-ticket-new"', empty_rows(self.tickets(can_create=False))[0])
        self.assertIn('data-modal-open="modal-ticket-new"', empty_rows(self.tickets(can_create=True))[0])

    def test_two_lists_each_have_their_own_heading(self) -> None:
        both = self.tickets(show_tg=True)
        self.assertIn(">تیکت‌های پنل</h2>", both)
        self.assertIn(">تیکت‌های کاربران ربات</h2>", both)
        self.assertNotIn(">تیکت‌های پنل</h2>", self.tickets(show_tg=False))
        self.assertEqual(len(empty_rows(both)), 2)

    def test_a_list_with_rows_has_no_empty_row(self) -> None:
        ticket = SimpleNamespace(id=1, subject="s", priority="normal", status="open", opener_label="x",
                                 updated_at=None, created_at=None, unread=False)
        self.assertEqual(empty_rows(self.tickets(panel_tickets=[ticket])), [])


if __name__ == "__main__":
    unittest.main()


class PlansCountTests(unittest.TestCase):
    """The fixed-plan counter and the empty message come from the same list; the built-in rows are not counted."""

    def plans_page(self, plans: list) -> str:
        return _page("plans.html", "/plans", OWNER, plans=plans, values={})

    def test_no_fixed_plan_reads_zero_and_offers_to_add_one(self) -> None:
        html = self.plans_page([])
        self.assertIn('<span class="li-count">۰ پلن ثابت</span>', html)
        row = empty_rows(html)[0]
        self.assertIn("هنوز پلن ثابتی نساخته‌اید.", row)
        self.assertIn('data-modal-open="modal-plan-unified"', row)
