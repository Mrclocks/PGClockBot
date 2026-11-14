"""page_header(): one header markup for the desktop title row and the phone capsule, with «⋯» only when needed."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from jinja2 import Environment, FileSystemLoader

ENV = Environment(loader=FileSystemLoader("app/web/templates"), autoescape=True)
ENV.globals["guide_topics"] = {"plans": {"href": "/help/plans/"}}
IMPORT = '{% from "macros.html" import page_header, act_label %}'


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
        self.assertIn('href="/help/plans/"', html.split('class="page-more"', 1)[1])
        # The secondary action sits inside the «⋯» container, the primary one outside it.
        more = html.split('class="page-more"', 1)[1].split("</div>", 1)[0]
        self.assertIn('id="cat"', more)
        self.assertNotIn('id="add"', more)

    def test_a_page_without_actions_or_help_has_no_menu(self) -> None:
        html = render("{{ page_header('users', 'گزارش کارکنان') }}")
        self.assertNotIn("page-more", html)
        self.assertNotIn("page-head-actions", html)

    def test_help_alone_still_gets_the_menu(self) -> None:
        html = render("{{ page_header('plans', 'پلن‌ها', help='plans') }}")
        self.assertIn("data-page-more-toggle", html)
        self.assertIn("راهنمای این صفحه", html)

    def test_a_detail_page_has_a_back_button_and_its_path(self) -> None:
        html = render("{{ page_header('bot', 'هویت ربات', back='/settings', back_label='تنظیمات ربات', subtitle='تنظیمات ربات · ظاهر ربات') }}")
        self.assertIn("page-head--detail", html)
        self.assertIn('href="/settings"', html)
        self.assertIn('aria-label="بازگشت به تنظیمات ربات"', html)
        self.assertIn("تنظیمات ربات · ظاهر ربات", html)

    def test_the_dashboard_keeps_its_title_out_of_the_capsule(self) -> None:
        self.assertIn('data-page-head="flow"', render("{{ page_header('home', 'داشبورد', capsule=false) }}"))

    def test_titles_and_paths_are_escaped(self) -> None:
        html = render("{{ page_header('users', '<b>x</b>', subtitle='<i>y</i>', back='/s', back_label='<u>z</u>') }}")
        self.assertNotIn("<b>x</b>", html)
        self.assertNotIn("<i>y</i>", html)
        self.assertNotIn("<u>z</u>", html)

    def test_the_short_label_is_hidden_from_screen_readers(self) -> None:
        html = render("{{ act_label('کاربر جدید', 'کاربر') }}")
        self.assertIn('<span class="act-long">کاربر جدید</span>', html)
        self.assertIn('<span class="act-short" aria-hidden="true">کاربر</span>', html)


if __name__ == "__main__":
    unittest.main()
