"""page_header(): one header markup for the desktop title row and the phone capsule, with «⋯» only when needed."""

from __future__ import annotations

import re
import unittest


from jinja2 import Environment, FileSystemLoader

ENV = Environment(loader=FileSystemLoader("app/web/templates"), autoescape=True)
ENV.globals["guide_topics"] = {"plans": {"href": "/help/plans/"}}
IMPORT = '{% from "macros.html" import page_header, act_label %}'
SPAN = re.compile(r'<span class="(?P<cls>act-long|act-short)"(?P<attrs>[^>]*)>(?P<text>[^<]*)</span>')


def render(body: str) -> str:
    return ENV.from_string(IMPORT + body).render()


class PageHeaderTests(unittest.TestCase):
    def test_primary_more_and_help_render_once_with_the_menu_toggle(self) -> None:
        html = render(
            "{% call(slot) page_header('plans', 'پلن‌ها', help='plans') %}"
            "{% if slot == 'primary' %}<button class=\"btn\" id=\"add\">{{ act_label('افزودن پلن', 'پلن') }}</button>"
            "{% elif slot == 'more' %}<button class=\"btn btn-ghost\" id=\"cat\">برچسب دسته</button>{% endif %}"
            "{% endcall %}"
        )
        self.assertEqual(html.count('id="add"'), 1)
        self.assertEqual(html.count('id="cat"'), 1)
        self.assertIn('data-page-head="capsule"', html)
        self.assertIn('aria-controls="page-more"', html)
        self.assertIn('aria-expanded="false"', html)
        # The secondary action sits inside the «⋯» container, the primary one outside it; help sits by the title.
        more = html.split('class="page-more"', 1)[1].split("</div>", 1)[0]
        self.assertIn('id="cat"', more)
        self.assertNotIn('id="add"', more)
        self.assertNotIn("/help/plans/", more)
        self.assertIn('class="page-help-btn" href="/help/plans/"', html.split("</h1>", 1)[1])

    def test_a_page_without_actions_or_help_has_no_menu(self) -> None:
        html = render("{{ page_header('users', 'گزارش کارکنان') }}")
        self.assertNotIn("page-more", html)
        self.assertNotIn("page-head-actions", html)

    def test_help_alone_is_a_small_question_mark_and_no_menu(self) -> None:
        html = render("{{ page_header('plans', 'پلن‌ها', help='plans') }}")
        self.assertNotIn("data-page-more-toggle", html)
        self.assertNotIn("page-head-actions", html)
        self.assertIn('class="page-help-btn" href="/help/plans/" aria-label="راهنمای پلن‌ها"', html)

    def test_a_primary_action_alone_has_no_menu(self) -> None:
        html = render(
            "{% call(slot) page_header('finance', 'مالی', help='plans') %}"
            "{% if slot == 'primary' %}<button class=\"btn\" id=\"set\">تنظیمات</button>{% endif %}"
            "{% endcall %}"
        )
        self.assertIn('id="set"', html)
        self.assertNotIn("data-page-more-toggle", html)
        self.assertNotIn('id="page-more"', html)

    def test_a_list_search_submits_with_the_round_magnifier(self) -> None:
        html = ENV.from_string('{% from "macros.html" import search_submit %}{{ search_submit() }}').render()
        self.assertIn('class="search-submit" type="submit" aria-label="جستجو"', html)
        self.assertIn("<svg", html)
        self.assertNotIn(">جستجو<", html)

    def test_a_detail_page_has_a_back_button(self) -> None:
        html = render("{{ page_header('bot', 'هویت ربات', back='/settings', back_label='تنظیمات ربات') }}")
        self.assertIn("page-head--detail", html)
        self.assertIn('href="/settings"', html)
        self.assertIn('aria-label="بازگشت به تنظیمات ربات"', html)

    def test_the_dashboard_keeps_its_title_out_of_the_capsule(self) -> None:
        self.assertIn('data-page-head="flow"', render("{{ page_header('home', 'داشبورد', capsule=false) }}"))

    def test_titles_and_back_labels_are_escaped(self) -> None:
        html = render("{{ page_header('users', '<b>x</b>', back='/s', back_label='<u>z</u>') }}")
        self.assertNotIn("<b>x</b>", html)
        self.assertNotIn("<u>z</u>", html)

    def test_the_short_label_is_hidden_from_screen_readers(self) -> None:
        html = render("{{ act_label('کاربر جدید', 'کاربر') }}")
        spans = {match["cls"]: match for match in SPAN.finditer(html)}
        self.assertEqual(spans["act-long"]["text"], "کاربر جدید")
        self.assertNotIn("aria-hidden", spans["act-long"]["attrs"])
        self.assertEqual(spans["act-short"]["text"], "کاربر")
        self.assertIn('aria-hidden="true"', spans["act-short"]["attrs"])

    def test_action_labels_follow_their_own_reading_direction(self) -> None:
        html = render("{{ act_label('New user', 'User') }}")
        for match in SPAN.finditer(html):
            self.assertIn('dir="auto"', match["attrs"])


if __name__ == "__main__":
    unittest.main()


class PageMoreMenuIconTests(unittest.TestCase):
    """Every «⋯» row on a phone starts with its own icon (DESIGN_SYSTEM.md §7, «منوی ⋯»)."""

    def test_the_plans_menu_row_carries_the_tag_icon(self) -> None:
        from tests.test_marketing_nav import OWNER, _page

        html = _page("plans.html", "/plans", OWNER, plans=[], values={})
        menu = html.split('id="page-more"', 1)[1].split("</div>", 1)[0]
        self.assertIn('class="more-ico"', menu)
        self.assertIn("برچسب دسته", menu)

    def test_the_tag_icon_is_drawn(self) -> None:
        tag = ENV.from_string('{% from "_icons.html" import icon %}{{ icon("tag") }}').render()
        clock = ENV.from_string('{% from "_icons.html" import icon %}{{ icon("no-such-icon") }}').render()
        self.assertNotEqual(tag, clock)
