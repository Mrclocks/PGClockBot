"""The terminal is English-only: sizes, restore results and service errors never print Persian."""

from __future__ import annotations

import io
import re
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from app.cli import backup_cmds, shop_review_cmds
from app.cli.context import CliContext
from app.cli.output import CliError, NO_ENGLISH_DETAIL, err, human_size, info, kv, terminal_text
from app.services.service_shop_review import NOT_UNDER_REVIEW, SHOP_NOT_FOUND

PERSIAN = re.compile(r"[؀-ۿ]")
ROOT = Path(__file__).resolve().parents[1]


def _printed(fn, *args, **kwargs) -> str:
    out, error = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(error):
        fn(*args, **kwargs)
    return out.getvalue() + error.getvalue()


class TerminalTextTests(unittest.TestCase):
    def test_english_text_is_unchanged(self) -> None:
        self.assertEqual(terminal_text("certbot: DNS problem"), "certbot: DNS problem")

    def test_persian_runs_are_dropped_and_english_details_kept(self) -> None:
        self.assertEqual(terminal_text("apt پیدا نشد — دستی: sudo apt install certbot"), "apt — : sudo apt install certbot")

    def test_a_persian_only_message_becomes_an_english_note(self) -> None:
        self.assertEqual(terminal_text("حجم بکاپ بیش از حد مجاز است (۵۰۰ مگابایت)"), NO_ENGLISH_DETAIL)

    def test_every_output_helper_keeps_persian_off_the_terminal(self) -> None:
        for helper, args in ((info, ("ریستور انجام شد",)), (err, ("خطا",)), (kv, ("size", "۱۲ مگ"))):
            self.assertFalse(PERSIAN.search(_printed(helper, *args)), helper.__name__)


class SizeTests(unittest.TestCase):
    def test_sizes_are_english_binary_units(self) -> None:
        self.assertEqual(human_size(512), "512 B")
        self.assertEqual(human_size(12.3 * 1024 * 1024), "12.3 MB")
        self.assertEqual(human_size(3 * 1024**3), "3.0 GB")
        self.assertEqual(human_size(None), "0 B")


class CommandTests(unittest.TestCase):
    def _ctx(self) -> CliContext:
        return CliContext(root=ROOT)

    def test_the_pre_update_backup_prints_its_size_in_english(self) -> None:
        result = {"ok": True, "filename": "b.zip", "id": "b1", "path": "/x/b.zip", "db_engine": "sqlite",
                  "size": 5 * 1024 * 1024, "size_human": "۵ مگ", "sha256": "ab" * 32}
        with patch.object(backup_cmds, "_bind_paths"), patch("app.services.backup.create_backup", return_value=result):
            text = _printed(backup_cmds.cmd_backup, self._ctx(), note="pre-update")
        self.assertIn("5.0 MB", text)
        self.assertFalse(PERSIAN.search(text))

    def test_a_restore_result_is_reported_in_english(self) -> None:
        done = {"ok": True, "message": "ریستور انجام شد", "safety_id": "s1", "restart_scheduled": True}
        with patch.object(backup_cmds, "_bind_paths"), patch("app.services.backup.get_backup_path", return_value=Path("b.zip")), \
                patch("app.services.backup.restore_backup", return_value=done):
            text = _printed(backup_cmds.cmd_restore, self._ctx(), backup_id="b1", yes=True)
        self.assertIn("restore complete", text)
        self.assertFalse(PERSIAN.search(text))

    def test_a_shop_review_refusal_is_explained_in_english(self) -> None:
        for service_error, expected in ((SHOP_NOT_FOUND, "shop 9 not found"), (NOT_UNDER_REVIEW, "service 4 is not waiting")):
            async def refuse(*_args, error=service_error):
                raise ValueError(error)

            with patch.object(shop_review_cmds, "_assign", refuse):
                with self.assertRaises(CliError) as caught:
                    shop_review_cmds.cmd_shop_review_assign(self._ctx(), service_id=4, shop="9")
            self.assertIn(expected, str(caught.exception))
            self.assertFalse(PERSIAN.search(str(caught.exception)))


if __name__ == "__main__":
    unittest.main()
