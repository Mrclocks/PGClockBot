"""Fix 4/5: callback↔handler contract and NAV_RULES allow-list checks."""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "app" / "bot"

# callback_data prefixes / exact values that are intentionally not Router handlers
# (URL buttons, web_app, switch_inline, noop placeholders, dynamic pay ids, etc.).
CALLBACK_ALLOWLIST_PREFIXES = (
    "http://",
    "https://",
    "tg://",
    "noop",
    "shop:wholesale:noop",
)

# Explicit full callback_data strings that may appear without a handler match.
CALLBACK_ALLOWLIST_EXACT = frozenset(
    {
        "noop",
    }
)


def _collect_handler_prefixes() -> set[str]:
    """Rough set of registered callback filters from handler modules."""
    prefixes: set[str] = set()
    exact: set[str] = set()
    pat_eq = re.compile(
        r'F\.data\s*==\s*["\']([^"\']+)["\']'
    )
    pat_start = re.compile(
        r'F\.data\.startswith\(\s*["\']([^"\']+)["\']\s*\)'
    )
    pat_in = re.compile(
        r'F\.data\.in_\(\s*\{([^}]+)\}'
    )
    for path in (BOT / "handlers").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for m in pat_eq.finditer(text):
            exact.add(m.group(1))
        for m in pat_start.finditer(text):
            prefixes.add(m.group(1))
        for m in pat_in.finditer(text):
            for part in re.findall(r'["\']([^"\']+)["\']', m.group(1)):
                exact.add(part)
    # Also nav_input cancel router
    prefixes.add("nv:cancel:")
    # menu:home lives in start.py
    exact.add("menu:home")
    return prefixes | exact


def _literal_callback_data_from_keyboards() -> set[str]:
    """Collect string-literal callback_data values from keyboard builders."""
    found: set[str] = set()
    pat = re.compile(r'callback_data\s*=\s*["\']([^"\']+)["\']')
    for path in [
        BOT / "keyboards.py",
        BOT / "nav_inline.py",
        BOT / "reply_keyboards.py",
        * (BOT / "handlers").rglob("*.py"),
    ]:
        if not path.exists():
            continue
        for m in pat.finditer(path.read_text(encoding="utf-8")):
            found.add(m.group(1))
    return found


def _matches_handler(cb: str, handlers: set[str]) -> bool:
    if cb in handlers:
        return True
    for h in handlers:
        if h.endswith(":") and cb.startswith(h):
            return True
        if not h.endswith(":") and cb.startswith(h + ":"):
            return True
    return False


class CallbackHandlerContractTests(unittest.TestCase):
    def test_guide_home_uses_menu_home(self):
        from app.bot.handlers.guides import guides_list_keyboard

        kb = guides_list_keyboard([], {}, back_callback=None)
        flat = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("menu:home", flat)
        self.assertNotIn("nav:home", flat)

    def test_nav_home_dead_button_gone(self):
        text = (BOT / "handlers" / "guides.py").read_text(encoding="utf-8")
        self.assertNotIn('callback_data="nav:home"', text)
        self.assertNotIn("callback_data='nav:home'", text)

    def test_keyboard_callbacks_have_handlers(self):
        handlers = _collect_handler_prefixes()
        literals = _literal_callback_data_from_keyboards()
        # Only check stable literals without format placeholders.
        static = {
            c
            for c in literals
            if "{" not in c and "}" not in c and not c.startswith(("http", "tg:"))
        }
        missing: list[str] = []
        for cb in sorted(static):
            if cb in CALLBACK_ALLOWLIST_EXACT:
                continue
            if any(cb.startswith(p) for p in CALLBACK_ALLOWLIST_PREFIXES):
                continue
            if not _matches_handler(cb, handlers):
                missing.append(cb)
        # Keep the failure actionable — show a short sample.
        self.assertEqual(
            missing,
            [],
            msg=f"unhandled callback_data literals (sample): {missing[:20]}",
        )


class NavRulesDocTests(unittest.TestCase):
    def test_nav_rules_doc_exists(self):
        path = ROOT / "docs" / "NAV_RULES.md"
        self.assertTrue(path.is_file())
        body = path.read_text(encoding="utf-8")
        self.assertIn("ask_text", body)
        self.assertIn("ReplyKeyboard", body)


class CallbackNoNewInlinePanelTests(unittest.TestCase):
    """Customer payment/receipt paths must not answer a second inline panel."""

    def test_await_order_receipt_single_edit(self):
        text = (BOT / "handlers" / "shop.py").read_text(encoding="utf-8")
        # The helper must not answer a follow-up cancel_reply / second panel.
        start = text.index("async def _await_order_receipt")
        end = text.index("\n@", start)
        body = text[start:end]
        self.assertNotIn("callback.message.answer", body)
        self.assertIn("with_cancel_row", body)

    def test_stars_and_psp_no_cancel_reply_answer(self):
        text = (BOT / "handlers" / "shop.py").read_text(encoding="utf-8")
        for name in ("async def pay_stars_cb", "async def pay_psp_cb"):
            start = text.index(name)
            nxt = text.find("\nasync def ", start + 10)
            window = text[start:nxt] if nxt != -1 else text[start : start + 4000]
            self.assertNotIn("cancel_reply", window)
            self.assertIn("cancel_keyboard", window)


class CancelReplyCustomerContractTests(unittest.TestCase):
    """No handler module may call cancel_reply() after the 0.2.51 migration."""

    def test_no_cancel_reply_calls_in_handlers(self):
        for path in sorted((BOT / "handlers").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "cancel_reply":
                    self.fail(f"{path.name}:{node.lineno} still calls cancel_reply()")
                if isinstance(func, ast.Name) and func.id == "cancel_reply":
                    self.fail(f"{path.name}:{node.lineno} still calls cancel_reply()")


if __name__ == "__main__":
    unittest.main()
