"""Terms/rules gates — settings keys, packing, acceptance hash."""

from __future__ import annotations

import unittest
from pathlib import Path

from app.services.rich_text import (
    TERMS_RICH_KEYS,
    content_fingerprint,
    merge_rich_settings_on_save,
    pack_rich_text,
    prepare_settings_values_for_web,
    rich_plain_text,
    unpack_rich_text,
)
from app.services.terms import GATE_META, GATES, build_prompt, gate_enabled, shop_scope_id
from app.services.users import DEFAULT_SETTINGS, SETTINGS_TABS, TAB_SETTING_GROUPS
from app.services.resellers import RESELLER_SETTINGS_TABS
from app.services.button_styles import BUTTON_STYLE_CATALOG


class TermsSettingsTests(unittest.TestCase):
    def test_gates_and_tabs(self):
        self.assertEqual(set(GATES), {"entry", "buy_user", "buy_reseller"})
        tab_ids = {t[0] for t in SETTINGS_TABS}
        self.assertIn("terms", tab_ids)
        self.assertIn("terms", {t[0] for t in RESELLER_SETTINGS_TABS})
        self.assertEqual(TAB_SETTING_GROUPS["terms"], ["قوانین ورود به ربات", "قوانین خرید پلن کاربر", "قوانین خرید پلن نماینده"])

    def test_default_keys(self):
        for gate, meta in GATE_META.items():
            for k in ("enabled", "text", "btn", "reaccept"):
                self.assertIn(meta[k], DEFAULT_SETTINGS, msg=f"{gate}.{k}")

    def test_button_styles(self):
        ids = {i["id"] for i in BUTTON_STYLE_CATALOG}
        self.assertTrue({"terms_entry", "terms_buy_user", "terms_buy_reseller"} <= ids)
        by_id = {i["id"]: i for i in BUTTON_STYLE_CATALOG}
        for sid in ("terms_entry", "terms_buy_user", "terms_buy_reseller"):
            self.assertEqual(by_id[sid]["group"], "قوانین")
        from app.services.users import keys_for_tab

        style_keys = {
            "btn_style_terms_entry",
            "btn_style_terms_buy_user",
            "btn_style_terms_buy_reseller",
        }
        self.assertTrue(style_keys <= keys_for_tab("terms"))
        self.assertTrue(style_keys <= keys_for_tab("colors"))
        from app.services.button_styles import colors_page_grouped_sections

        user_sec = next(s for s in colors_page_grouped_sections() if s[0] == "user")
        group_names = [g[0] for g in user_sec[3]]
        self.assertIn("قوانین", group_names)

    def test_shop_scope(self):
        self.assertEqual(shop_scope_id(), 0)
        self.assertEqual(shop_scope_id(reseller_owner_id=42), 42)

    def test_gate_requires_text(self):
        ui = {**DEFAULT_SETTINGS, "terms_entry_enabled": "1", "terms_entry_text": ""}
        self.assertTrue(gate_enabled(ui, "entry"))
        self.assertIsNone(build_prompt(ui, "entry"))
        ui["terms_entry_text"] = "قوانین تست"
        prompt = build_prompt(ui, "entry")
        self.assertIsNotNone(prompt)
        self.assertEqual(prompt.btn_label, DEFAULT_SETTINGS["terms_entry_btn"])


class RichTextTermsTests(unittest.TestCase):
    def test_pack_roundtrip_and_fingerprint(self):
        from aiogram.types import MessageEntity

        text = "قوانین 😀"
        # custom emoji entity (premium) — type custom_emoji
        ent = [MessageEntity(type="custom_emoji", offset=7, length=2, custom_emoji_id="123")]
        packed = pack_rich_text(text, ent)
        self.assertTrue(packed.startswith("\x1e") or packed.startswith(""))
        plain, ents = unpack_rich_text(packed)
        self.assertEqual(plain, text)
        self.assertTrue(ents)
        self.assertEqual(content_fingerprint(packed), content_fingerprint(text))
        self.assertEqual(rich_plain_text(packed), text)

    def test_web_prepare_and_merge_keeps_entities(self):
        packed = pack_rich_text("hello", None)  # plain
        # with entities
        from aiogram.types import MessageEntity

        packed = pack_rich_text(
            "Hi!",
            [MessageEntity(type="custom_emoji", offset=0, length=2, custom_emoji_id="9")],
        )
        values = {"terms_entry_text": packed, "terms_entry_btn": "OK"}
        web = prepare_settings_values_for_web(values)
        self.assertEqual(web["terms_entry_text"], "Hi!")
        # unchanged plain on save keeps packed
        payload = {"terms_entry_text": "Hi!"}
        merged = merge_rich_settings_on_save(values, payload)
        self.assertEqual(merged["terms_entry_text"], packed)
        # changed text drops packing
        payload2 = {"terms_entry_text": "Hi! changed"}
        merged2 = merge_rich_settings_on_save(values, payload2)
        self.assertEqual(merged2["terms_entry_text"], "Hi! changed")

    def test_rich_keys_cover_gate_texts(self):
        for meta in GATE_META.values():
            self.assertIn(meta["text"], TERMS_RICH_KEYS)
            self.assertIn(meta["btn"], TERMS_RICH_KEYS)


class TermsWiringTests(unittest.TestCase):
    def test_middleware_and_handlers_registered(self):
        init = Path("app/bot/__init__.py").read_text()
        self.assertIn("TermsEntryMiddleware", init)
        self.assertIn("terms.router", init)
        mw = Path("app/bot/middlewares.py").read_text()
        self.assertIn("class TermsEntryMiddleware", mw)
        self.assertIn('startswith("terms:")', mw)
        start = Path("app/bot/handlers/start.py").read_text()
        self.assertIn("needs_entry_gate", start)
        shop = Path("app/bot/handlers/shop.py").read_text()
        self.assertIn("prompt_terms_if_needed", shop)
        self.assertEqual(shop.count("prompt_terms_if_needed"), 6)  # import+call x3
        res = Path("app/bot/handlers/reseller.py").read_text()
        self.assertIn("buy_reseller", res)
        self.assertIn("prompt_terms_if_needed", res)

    def test_model_and_migration(self):
        models = Path("app/db/models.py").read_text()
        self.assertIn("class TermsAcceptance", models)
        mig = Path("alembic/versions/0025_terms_acceptances.py").read_text()
        self.assertIn("terms_acceptances", mig)
        self.assertIn("0024_user_service_quota_cache", mig)


if __name__ == "__main__":
    unittest.main()
