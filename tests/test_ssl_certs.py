"""SSL helpers regression tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class SslDomainTests(unittest.TestCase):
    def test_normalize_and_validate(self):
        from app.services.ssl_certs import is_valid_domain, is_valid_ipv4, is_valid_tls_host, normalize_domain

        self.assertEqual(normalize_domain("https://Bot.Example.com/x"), "bot.example.com")
        self.assertTrue(is_valid_domain("bot.example.com"))
        self.assertFalse(is_valid_domain("localhost"))
        self.assertFalse(is_valid_domain(""))
        self.assertFalse(is_valid_domain("1.2.3.4"))
        self.assertTrue(is_valid_ipv4("1.2.3.4"))
        self.assertFalse(is_valid_ipv4("999.1.1.1"))
        self.assertTrue(is_valid_tls_host("bot.example.com"))
        self.assertTrue(is_valid_tls_host("203.0.113.10"))


class SslSelfSignedIpTests(unittest.TestCase):
    def test_issue_self_signed_ip_writes_cert_and_meta(self):
        from app.services import ssl_certs

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cert_dir = root / "certs"
            with patch.object(ssl_certs, "CERT_DIR", cert_dir), patch.object(
                ssl_certs, "META_PATH", cert_dir / "meta.json"
            ), patch.object(ssl_certs, "PROGRESS_PATH", cert_dir / "progress.json"), patch.object(
                ssl_certs, "LIVE_CERT", cert_dir / "fullchain.pem"
            ), patch.object(ssl_certs, "LIVE_KEY", cert_dir / "privkey.pem"), patch.object(
                ssl_certs, "WEBROOT_DIR", root / "acme-www"
            ), patch(
                "app.services.setup_wizard.update_env_keys", return_value=Path(tmp) / ".env"
            ), patch(
                "app.services.ssl_certs.get_settings"
            ) as gs:
                class _S:
                    web_port = 9443

                gs.return_value = _S()
                gs.cache_clear = lambda: None

                result = ssl_certs.issue_self_signed_ip("203.0.113.50", enable=True, restart=False)
                self.assertTrue(result.get("ok"), result)
                self.assertTrue(ssl_certs.cert_files_exist())
                meta = json.loads((cert_dir / "meta.json").read_text(encoding="utf-8"))
                self.assertTrue(meta.get("ssl_enabled"))
                self.assertTrue(meta.get("self_signed"))
                self.assertEqual(meta.get("mode"), "self_signed_ip")
                self.assertEqual(meta.get("domain"), "203.0.113.50")
                self.assertIn("https://203.0.113.50:9443", result.get("public_https") or "")


class SslConfigureInstallTests(unittest.TestCase):
    def test_configure_none(self):
        from app.services import ssl_certs

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cert_dir = root / "certs"
            with patch.object(ssl_certs, "CERT_DIR", cert_dir), patch.object(
                ssl_certs, "META_PATH", cert_dir / "meta.json"
            ), patch.object(ssl_certs, "PROGRESS_PATH", cert_dir / "progress.json"), patch.object(
                ssl_certs, "LIVE_CERT", cert_dir / "fullchain.pem"
            ), patch.object(ssl_certs, "LIVE_KEY", cert_dir / "privkey.pem"), patch.object(
                ssl_certs, "WEBROOT_DIR", root / "acme-www"
            ):
                result = ssl_certs.configure_for_install(mode="none")
                self.assertTrue(result.get("ok"))
                self.assertEqual(result.get("mode"), "none")
                meta = json.loads((cert_dir / "meta.json").read_text(encoding="utf-8"))
                self.assertFalse(meta.get("ssl_enabled"))


class SslUiWiredTests(unittest.TestCase):
    def test_settings_tab_and_template(self):
        from app.services.users import PANEL_SETTINGS_TABS, SETTINGS_TABS, TAB_SETTING_GROUPS

        self.assertNotIn(("ssl", "دامنه‌ها"), SETTINGS_TABS)
        self.assertIn(("ssl", "دامنه‌ها"), PANEL_SETTINGS_TABS)
        self.assertEqual(TAB_SETTING_GROUPS.get("ssl"), [])
        self.assertTrue(Path("app/web/templates/_settings_ssl.html").is_file())
        self.assertTrue(Path("app/services/ssl_certs.py").is_file())
        src = Path("app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("/settings/ssl/progress", src)
        self.assertIn("PANEL_SETTINGS", src)
        self.assertIn("start_issue_job", src)
        self.assertIn("enable_https", src)
        main = Path("app/main.py").read_text(encoding="utf-8")
        self.assertIn("uvicorn_ssl_kwargs", main)
        ssl_src = Path("app/services/ssl_certs.py").read_text(encoding="utf-8")
        self.assertIn("enable_https", ssl_src)
        self.assertIn("auto_enabled", ssl_src)
        self.assertIn("_start_acme_http", ssl_src)
        self.assertIn("issue_self_signed_ip", ssl_src)
        self.assertIn("configure_for_install", ssl_src)
        self.assertIn("secp256r1", ssl_src)

    def test_install_script_prompts_port_and_ssl(self):
        src = Path("pgclock.sh").read_text(encoding="utf-8")
        self.assertIn("prompt_install_port_and_ssl", src)
        self.assertIn("apply_install_ssl", src)
        self.assertIn("PGCLOCK_SSL_MODE", src)
        self.assertIn("PGCLOCK_WEB_PORT", src)
        self.assertIn("Temporary self-signed", src)
        self.assertIn("configure_for_install", src)
        self.assertIn("probe_panel_health", src)
        self.assertIn("verify_tls_material", src)
        self.assertIn("ensure_db_schema", src)
        self.assertIn("PGCLOCK_SSL_INSTALL", src)
        self.assertIn("SSL certificate READY", src)
        self.assertIn("SSL certificate FAILED", src)
        # Setup URL must still print when /health fails (v11.0.8 regression).
        self.assertIn("Setup URL (one-time, 15 min) — open after", src)
        self.assertIn("ALWAYS print Setup URL", src)
        ctl = Path("scripts/pgclockbot-ctl").read_text(encoding="utf-8")
        self.assertIn("--key-type ecdsa", ctl)
        main = Path("app/main.py").read_text(encoding="utf-8")
        self.assertIn("TLS cert not loadable", main)
        self.assertIn("verify_tls_material", main)


class UpdateCopyTests(unittest.TestCase):
    def test_update_template_has_progress_ui(self):
        src = Path("app/web/templates/_settings_update.html").read_text(encoding="utf-8")
        self.assertIn("upd-start", src)
        self.assertNotIn("rollback-version", src)
        self.assertIn("progress-wrap", src)
        self.assertNotIn("update-details", src)
        self.assertNotIn("get.sh", src)


class SslDomainSwitchSafetyTests(unittest.TestCase):
    """Domain switch must not blame/activate the wrong hostname."""

    def test_settings_ssl_does_not_commit_domain_before_issue(self):
        src = Path("app/api/app.py").read_text(encoding="utf-8")
        ssl_start = src.find('if tab == "ssl":')
        self.assertGreater(ssl_start, 0)
        ssl_end = src.find('if tab == "bot":', ssl_start)
        block = src[ssl_start:ssl_end]
        self.assertIn('meta["pending_domain"] = domain', block)
        self.assertIn('meta["pending_email"] = email', block)
        self.assertIn('meta["last_error"] = None', block)
        # Premature commit of active identity before Let's Encrypt succeeds.
        self.assertNotIn(
            'meta.update({"domain": domain, "panel_domain": domain, "miniapp_domain": domain, "email": email})',
            block,
        )
        self.assertIn("برای تعویض دامنه ابتدا", block)

    def test_ssl_template_prefers_pending_domain_in_form(self):
        html = Path("app/web/templates/_settings_ssl.html").read_text(encoding="utf-8")
        self.assertIn("st.pending_domain or st.domain", html)
        self.assertIn("st.pending_email or st.email", html)
        self.assertIn("دامنهٔ فعال همان", html)

    def test_dns_failure_hint_ignores_certbot_boilerplate(self):
        from app.services.ssl_certs import _dns_failure_hint

        boilerplate = (
            "Some challenges have failed.\n"
            "Make sure your domain's DNS A/AAAA record(s) point at this machine.\n"
            "Connection refused on port 80"
        )
        self.assertEqual(_dns_failure_hint(boilerplate), "")
        real = "DNS problem: NXDOMAIN looking up A for new.example.com"
        self.assertIn("DNS دامنه باید به IP همین سرور اشاره کند", _dns_failure_hint(real))
        self.assertIn(
            "DNS دامنه باید به IP همین سرور اشاره کند",
            _dns_failure_hint("Detail: no valid IP addresses found for new.example.com"),
        )

    def test_lineage_names_are_exact_for_domain(self):
        from app.services.ssl_certs import _lineage_names_for_domain

        self.assertEqual(
            _lineage_names_for_domain("New.Example.com"),
            ("pgclock-new.example.com", "new.example.com"),
        )

    def test_find_and_copy_cert_never_falls_back_to_other_lineage(self):
        from app.services import ssl_certs

        calls: list[str] = []

        def fake_copy(name: str):
            calls.append(name)
            return None

        real_path = Path

        class _Child:
            def __init__(self, name: str):
                self.name = name

            def is_dir(self) -> bool:
                return True

        class _LiveRoot:
            def is_dir(self) -> bool:
                return True

            def iterdir(self):
                return [
                    _Child("pgclock-old.example.com"),
                    _Child("unrelated"),
                    _Child("pgclock-new.example.com"),
                ]

        def path_factory(arg="", *args, **kwargs):
            if str(arg) == "/etc/letsencrypt/live":
                return _LiveRoot()
            return real_path(arg, *args, **kwargs)

        with patch.object(ssl_certs, "_copy_live_from_letsencrypt", side_effect=fake_copy), patch(
            "app.services.ssl_certs.Path", side_effect=path_factory
        ):
            found = ssl_certs._find_and_copy_cert("new.example.com")
        self.assertIsNone(found)
        self.assertEqual(
            calls,
            [
                "pgclock-new.example.com",
                "new.example.com",
                "pgclock-new.example.com",
            ],
        )
        self.assertNotIn("pgclock-old.example.com", calls)
        self.assertNotIn("unrelated", calls)

    def test_issue_or_renew_failure_keeps_previous_domain(self):
        from app.services import ssl_certs

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cert_dir = root / "certs"
            cert_dir.mkdir(parents=True)
            (cert_dir / "fullchain.pem").write_text("CERT", encoding="utf-8")
            (cert_dir / "privkey.pem").write_text("KEY", encoding="utf-8")
            meta_path = cert_dir / "meta.json"
            meta_path.write_text(
                json.dumps(
                    {
                        "domain": "old.example.com",
                        "host": "old.example.com",
                        "panel_domain": "old.example.com",
                        "email": "ops@old.example.com",
                        "ssl_enabled": True,
                        "public_https": "https://old.example.com:9443",
                        "mode": "letsencrypt",
                        "pending_domain": "new.example.com",
                        "pending_email": "ops@new.example.com",
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(ssl_certs, "CERT_DIR", cert_dir), patch.object(
                ssl_certs, "META_PATH", meta_path
            ), patch.object(ssl_certs, "PROGRESS_PATH", cert_dir / "progress.json"), patch.object(
                ssl_certs, "LIVE_CERT", cert_dir / "fullchain.pem"
            ), patch.object(ssl_certs, "LIVE_KEY", cert_dir / "privkey.pem"), patch.object(
                ssl_certs, "WEBROOT_DIR", root / "acme-www"
            ), patch.object(ssl_certs, "certbot_available", return_value=True), patch.object(
                ssl_certs,
                "_certbot_issue",
                return_value=(False, "Challenge failed: Connection refused on port 80"),
            ):
                result = ssl_certs.issue_or_renew(
                    domain="new.example.com",
                    email="ops@new.example.com",
                    enable=False,
                    restart=False,
                )
            self.assertFalse(result.get("ok"))
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(meta.get("domain"), "old.example.com")
            self.assertEqual(meta.get("panel_domain"), "old.example.com")
            self.assertEqual(meta.get("public_https"), "https://old.example.com:9443")
            self.assertTrue(meta.get("ssl_enabled"))
            self.assertNotIn("pending_domain", meta)
            self.assertNotIn("pending_email", meta)
            self.assertIn("Connection refused", meta.get("last_error") or "")
            self.assertNotIn("DNS دامنه باید به IP همین سرور اشاره کند", meta.get("last_error") or "")

    def test_cert_status_exposes_pending_fields(self):
        from app.services import ssl_certs

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cert_dir = root / "certs"
            cert_dir.mkdir(parents=True)
            meta_path = cert_dir / "meta.json"
            meta_path.write_text(
                json.dumps(
                    {
                        "domain": "old.example.com",
                        "email": "a@old.example.com",
                        "ssl_enabled": False,
                        "pending_domain": "New.Example.com",
                        "pending_email": "b@new.example.com",
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(ssl_certs, "CERT_DIR", cert_dir), patch.object(
                ssl_certs, "META_PATH", meta_path
            ), patch.object(ssl_certs, "PROGRESS_PATH", cert_dir / "progress.json"), patch.object(
                ssl_certs, "LIVE_CERT", cert_dir / "fullchain.pem"
            ), patch.object(ssl_certs, "LIVE_KEY", cert_dir / "privkey.pem"), patch.object(
                ssl_certs, "WEBROOT_DIR", root / "acme-www"
            ), patch("app.services.ssl_certs.get_settings") as gs:
                class _S:
                    web_port = 9000

                gs.return_value = _S()
                st = ssl_certs.cert_status()
            self.assertEqual(st["domain"], "old.example.com")
            self.assertEqual(st["pending_domain"], "new.example.com")
            self.assertEqual(st["pending_email"], "b@new.example.com")


if __name__ == "__main__":
    unittest.main()
