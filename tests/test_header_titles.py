"""Page and modal headers carry only their title: no caption or path line under it (DESIGN_SYSTEM.md §3, §7)."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from jinja2 import TemplateRuntimeError

from tests.test_marketing_nav import OWNER, _page
from tests.test_page_header import ENV


def render(body: str) -> str:
    return ENV.from_string('{% from "macros.html" import page_header, modal_title %}' + body).render()


class HeaderTitleTests(unittest.TestCase):
    def test_page_header_takes_no_subtitle(self) -> None:
        self.assertIn("هویت ربات", render("{{ page_header('bot', 'هویت ربات', back='/settings') }}"))
        with self.assertRaises((TypeError, TemplateRuntimeError)):
            render("{{ page_header('bot', 'هویت ربات', back='/settings', subtitle='مسیر') }}")

    def test_modal_title_takes_no_caption(self) -> None:
        self.assertIn(">تیکت</h2>", render("{{ modal_title('tickets', 'تیکت') }}"))
        with self.assertRaises((TypeError, TemplateRuntimeError)):
            render("{{ modal_title('tickets', 'تیکت', caption='موضوع') }}")

    def test_a_detail_page_header_is_its_title_and_way_back_only(self) -> None:
        html = render("{{ page_header('bot', 'هویت ربات', back='/settings', back_label='تنظیمات ربات') }}")
        self.assertNotIn("page-head-sub", html)
        self.assertNotIn(" · ", html)

    def test_a_marketing_page_has_no_path_line(self) -> None:
        html = _page("broadcast.html", "/broadcast", OWNER)
        head = html.split('class="page-head', 1)[1].split("</h1>", 1)[0]
        self.assertNotIn("بازاریابی ·", head)


class TicketSubjectTests(unittest.TestCase):
    def ticket_page(self, subject: str) -> str:
        ticket = SimpleNamespace(id=7, subject=subject, priority="normal", status="open", opener_label="x", messages=[])
        return _page("tickets.html", "/tickets", OWNER, active_ticket=ticket, tickets=[], tg_tickets=[],
                     priority_badge={}, status_badge={}, status_labels={})

    def test_the_subject_opens_the_body_not_the_header(self) -> None:
        html = self.ticket_page("اتصال قطع است")
        modal = html.split('id="modal-ticket-view"', 1)[1]
        head, body = modal.split('<p class="ticket-subject">', 1)
        self.assertNotIn("اتصال قطع است", head)
        self.assertTrue(body.startswith("اتصال قطع است</p>"))

    def test_the_subject_is_escaped(self) -> None:
        html = self.ticket_page("<script>alert(1)</script>")
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)


if __name__ == "__main__":
    unittest.main()
