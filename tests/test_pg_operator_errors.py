"""Staff-facing PasarGuard errors must show clear Persian causes, not bare 422."""

from __future__ import annotations

import unittest

from app.services.credential_policy import friendly_pg_error
from app.services.pasarguard import PasarGuardError, _pg_error_detail
from app.services.redact import operator_error, user_safe_error


class PgErrorDetailTests(unittest.TestCase):
    def test_pydantic_list_detail(self):
        body = {
            "detail": [
                {
                    "type": "missing",
                    "loc": ["body", "group_ids"],
                    "msg": "Field required",
                }
            ]
        }
        detail = _pg_error_detail(body)
        self.assertIsNotNone(detail)
        self.assertIn("group_ids", detail or "")

    def test_string_detail(self):
        self.assertEqual(
            _pg_error_detail({"detail": "Username already exists"}),
            "Username already exists",
        )


class Friendly422Tests(unittest.TestCase):
    def test_bare_422_is_persian(self):
        msg = friendly_pg_error("POST /api/user failed (422)", status_code=422)
        self.assertRegex(msg, r"[\u0600-\u06FF]")
        self.assertNotIn("POST /api/user", msg)

    def test_group_ids_required(self):
        msg = friendly_pg_error("group_ids: Field required", status_code=422)
        self.assertIn("گروه", msg)

    def test_duplicate_still_clear(self):
        exc = PasarGuardError(
            "POST /api/user failed (422)",
            422,
            {"detail": "Username already exists"},
        )
        self.assertIn("قبلاً ثبت شده", exc.user_message())


class OperatorErrorTests(unittest.TestCase):
    def test_provision_style_message(self):
        exc = PasarGuardError(
            "POST /api/user failed (422)",
            422,
            {
                "detail": [
                    {
                        "loc": ["body", "group_ids"],
                        "msg": "Field required",
                    }
                ]
            },
        )
        out = operator_error(exc, prefix="اختصاص پلن ناموفق")
        self.assertTrue(out.startswith("اختصاص پلن ناموفق"))
        self.assertIn("گروه", out)
        self.assertNotIn("failed (422)", out)

    def test_user_safe_no_longer_hides_mapped_422(self):
        exc = PasarGuardError("POST /api/user failed (422)", 422, "")
        msg = user_safe_error(exc, fallback="fallback")
        self.assertIn("نامعتبر", msg)
        self.assertNotEqual(msg, "fallback")


class UserPagesContractTests(unittest.TestCase):
    def test_uses_operator_error_helper(self):
        from pathlib import Path

        src = Path("app/api/user_pages.py").read_text(encoding="utf-8")
        self.assertIn("def _op_err", src)
        self.assertIn('prefix="اختصاص پلن ناموفق"', src)
        self.assertNotIn('err=f"اختصاص پلن ناموفق: {e}"', src)


if __name__ == "__main__":
    unittest.main()
