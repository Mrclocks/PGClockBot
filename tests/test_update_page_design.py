"""The panel update page: one version card whose colour and buttons follow the update state, and a changelog card.

The card is brand-coloured old → new while an update waits or runs, green with only the installed version when the
panel is up to date, and red after a failed run. The changelog shows each note's topic as a tag and its PR numbers
as a quiet trailer; past the first five notes the rest wait behind «نمایش همه».
"""

from __future__ import annotations

import unittest
from html.parser import HTMLParser

from app.services.release_notes import split_release_note
from tests.test_message_variables_entry import OWNER, _page

CHANNELS = [{"id": "main", "label": "پایدار"}, {"id": "dev", "label": "توسعه"}]


class _Elements(HTMLParser):
    """Attributes and text of every element that has an id, and the rows of the changelog."""

    def __init__(self) -> None:
        super().__init__()
        self.by_id: dict[str, dict[str, str | None]] = {}
        self.text: dict[str, str] = {}
        self.rows: list[dict[str, str | None]] = []
        self.html = ""
        self._open: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = dict(attrs)
        if found.get("id"):
            self.by_id[found["id"]] = found
            self.text[found["id"]] = ""
            self._open.append(found["id"])
        if "upd-note-row" in (found.get("class") or ""):
            self.rows.append(found)

    def handle_data(self, data: str) -> None:
        for element_id in self._open:
            self.text[element_id] += data

    def handle_endtag(self, tag: str) -> None:
        if self._open and tag in {"button", "span", "strong", "small", "b", "h2"}:
            self._open.pop()


def _notes(count: int, topic: str = "تمدید") -> list[str]:
    return [f"{topic}: تغییر شمارهٔ {n} (#{100 + n})" for n in range(count)]


def render_update(*, available: bool, state: str = "idle", notes: list[str] | None = None) -> _Elements:
    from app.services.release_notes import changelog_for_update_page

    remote = "0.9.1" if available else "0.9.0"
    changelog = changelog_for_update_page(local="0.9.0", remote=remote, remote_notes={"0.9.1": notes or _notes(2)})
    html = _page(
        "settings.html", "/settings", OWNER, query=b"tab=update", tabs=[], tab="update", is_panel_settings=True,
        update_info={"update_available": available, "remote_version": remote, "checked": True, "label": ""},
        status={"state": state, "percent": 40, "step": "نصب وابستگی‌ها", "message": "نصب وابستگی‌ها", "to_version": remote},
        local_version="0.9.0", can_start_update=available, changelog=changelog, update_channels=CHANNELS,
        update_channel="main", update_channel_label="پایدار", migration_preflight={},
    )
    parsed = _Elements()
    parsed.html = html
    parsed.feed(html)
    return parsed


class VersionCardTests(unittest.TestCase):
    def test_an_available_update_shows_old_to_new_in_the_brand_colour_with_one_install_button(self) -> None:
        page = render_update(available=True)
        self.assertEqual(page.by_id["upd-hero"]["data-tone"], "brand")
        self.assertNotIn("hidden", page.by_id["upd-local-box"])
        self.assertIn("v0.9.1", page.text["upd-remote-ver"])
        self.assertNotIn("hidden", page.by_id["upd-start"])
        self.assertIn("نصب", page.text["upd-start"])
        self.assertNotIn("hidden", page.by_id["upd-idle-note"])

    def test_an_up_to_date_panel_is_green_with_only_the_installed_version_and_no_install_button(self) -> None:
        page = render_update(available=False)
        self.assertEqual(page.by_id["upd-hero"]["data-tone"], "ok")
        self.assertIn("hidden", page.by_id["upd-local-box"])
        self.assertIn("hidden", page.by_id["upd-arrow"])
        self.assertIn("v0.9.0", page.text["upd-remote-ver"])
        self.assertIn("hidden", page.by_id["upd-start"])
        self.assertIn("روی آخرین نسخه", page.text["upd-state-label"])

    def test_a_running_update_shows_its_step_and_progress(self) -> None:
        page = render_update(available=True, state="running")
        self.assertEqual(page.by_id["upd-hero"]["data-tone"], "brand")
        self.assertNotIn("hidden", page.by_id["upd-ops"])
        self.assertIn("نصب وابستگی‌ها", page.text["upd-step"])
        self.assertIn("۴۰", page.text["upd-percent"])
        self.assertIn("disabled", page.by_id["upd-start"])

    def test_a_failed_update_turns_the_card_red_and_offers_a_retry(self) -> None:
        page = render_update(available=True, state="error")
        self.assertEqual(page.by_id["upd-hero"]["data-tone"], "err")
        self.assertIn("تلاش دوباره", page.text["upd-start"])
        self.assertNotIn("disabled", page.by_id["upd-start"])

    def test_the_channel_is_a_two_option_capsule_over_the_saved_choice(self) -> None:
        page = render_update(available=False)
        self.assertIn("hidden", page.by_id["upd-channel"])
        self.assertEqual(page.by_id["upd-channel-seg"]["role"], "radiogroup")


class ChangelogCardTests(unittest.TestCase):
    def test_notes_past_the_first_five_wait_behind_show_all(self) -> None:
        page = render_update(available=True, notes=_notes(7))
        self.assertEqual(len(page.rows), 7)
        self.assertEqual([("hidden" in row) for row in page.rows], [False] * 5 + [True] * 2)
        self.assertNotIn("hidden", page.by_id["upd-changelog-more"])
        self.assertIn("۷", page.text["upd-changelog-more"])

    def test_a_short_changelog_has_no_show_all_button(self) -> None:
        page = render_update(available=True, notes=_notes(3))
        self.assertIn("hidden", page.by_id["upd-changelog-more"])

    def test_fixes_and_security_notes_get_their_own_tag_colour(self) -> None:
        page = render_update(available=True, notes=["رفع اشکال: الف", "امنیت: ب", "تمدید: ج"])
        self.assertEqual([row["data-tone"] for row in page.rows], ["fix", "security", "topic"])

    def test_note_text_is_escaped(self) -> None:
        page = render_update(available=True, notes=["<b>تگ</b>: <img src=x onerror=alert(1)>"])
        self.assertNotIn("<img src=x", page.html)
        self.assertNotIn("<b>تگ</b>", page.html)
        self.assertIn("&lt;img src=x", page.html)


class SplitReleaseNoteTests(unittest.TestCase):
    def test_topic_text_and_pr_numbers_are_split(self) -> None:
        item = split_release_note("ربات: ویرایش متن پیام‌ها (#1465، #1471)")
        self.assertEqual(item, {"topic": "ربات", "text": "ویرایش متن پیام‌ها", "refs": ["#1465", "#1471"], "tone": "topic"})

    def test_a_note_without_topic_or_numbers_stays_whole(self) -> None:
        item = split_release_note("ریستور commit 0e76d630")
        self.assertEqual(item, {"topic": "", "text": "ریستور commit 0e76d630", "refs": [], "tone": "topic"})

    def test_brackets_in_the_middle_are_kept_in_the_text(self) -> None:
        item = split_release_note("ریستور: commit 0659eee (dev پیش از #1492 تا #1503)")
        self.assertEqual(item["refs"], [])
        self.assertIn("(dev پیش از #1492 تا #1503)", item["text"])

    def test_fix_and_security_topics_have_their_own_tone(self) -> None:
        self.assertEqual(split_release_note("رفع اشکال: x")["tone"], "fix")
        self.assertEqual(split_release_note("امنیت: x")["tone"], "security")


if __name__ == "__main__":
    unittest.main()
