"""Login and setup pages sit in the design scope and use switches, not bare checkboxes."""

from __future__ import annotations

import os
import re
import unittest
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from jinja2 import ChainableUndefined

from app.api.app import templates


def render(name: str, **ctx: object) -> str:
    env = templates.env.overlay(undefined=ChainableUndefined)
    request = SimpleNamespace(query_params={}, url=SimpleNamespace(path=f"/{name}"), cookies={})
    return env.get_template(name).render(request=request, path=f"/{name}", values={}, **ctx)


class AuthPagesDesignTests(unittest.TestCase):
    def test_login_panel_is_in_the_design_scope(self) -> None:
        self.assertIn('class="auth-panel auth-panel--stage ds"', render("login.html"))

    def test_setup_clears_the_api_key_with_a_switch(self) -> None:
        html = render("setup.html", has_pg_api_key=True)
        self.assertIn("setup-panel ds", html)
        field = re.search(r'<label class="ui-switch-row[^"]*">(?:(?!</label>).)*name="clear_pg_api_key"', html, re.S)
        self.assertIsNotNone(field)
        self.assertNotIn("ui-check", html)


if __name__ == "__main__":
    unittest.main()
