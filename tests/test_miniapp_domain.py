"""The Mini App can have its own domain, on the same certificate as the panel's."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services import service_control, ssl_certs

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "pgclockbot-ctl"


@unittest.skipUnless(shutil.which("bash"), "bash runs the privileged helper")
class HelperScriptTests(unittest.TestCase):
    """The root helper, with certbot replaced by a stub that records its arguments."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.args_file = root / "certbot-args"
        fake_certbot = root / "certbot"
        fake_certbot.write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" > {self.args_file}\n', encoding="utf-8")
        fake_certbot.chmod(0o755)
        self.helper = root / "ctl"
        self.helper.write_text(
            HELPER.read_text(encoding="utf-8").replace("/usr/bin/certbot", str(fake_certbot)), encoding="utf-8",
        )
        self.helper.chmod(0o755)
        self.webroot = root / "data" / "acme-www"
        self.webroot.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_helper(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(self.helper), *args], capture_output=True, text=True, timeout=20)

    def certbot_args(self) -> list[str]:
        return self.args_file.read_text(encoding="utf-8").split()

    def test_capabilities_announce_a_second_name(self) -> None:
        self.assertEqual(self.run_helper("capabilities").stdout.split(), ["san"])

    def test_also_adds_the_mini_app_name_to_the_same_certificate(self) -> None:
        result = self.run_helper("certbot-certonly", "panel.example.com", "a@example.com", str(self.webroot),
                                 "--force", "--also", "app.example.com")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.certbot_args()
        self.assertEqual(args[args.index("--cert-name") + 1], "pgclock-panel.example.com")
        self.assertEqual([args[i + 1] for i, a in enumerate(args) if a == "-d"], ["panel.example.com", "app.example.com"])
        self.assertIn("--force-renewal", args)

    def test_the_old_call_without_options_still_issues_one_name(self) -> None:
        self.assertEqual(self.run_helper("certbot-certonly", "panel.example.com", "a@example.com", str(self.webroot)).returncode, 0)
        args = self.certbot_args()
        self.assertEqual(args.count("-d"), 1)
        self.assertNotIn("--force-renewal", args)

    def test_bad_extra_names_and_unknown_options_are_refused(self) -> None:
        for extra in (["--also", "bad domain"], ["--also", "panel.example.com"], ["--also"], ["--also", "-d"],
                      ["--also", "a.example.com;rm"], ["--pre-hook", "x"]):
            with self.subTest(extra=extra):
                result = self.run_helper("certbot-certonly", "panel.example.com", "a@example.com", str(self.webroot), *extra)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(self.args_file.exists(), "certbot never runs")


class IssueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        cert_dir = root / "certs"
        cert_dir.mkdir()
        self.meta_path = cert_dir / "meta.json"
        self.stack = ExitStack()
        for name, value in (("CERT_DIR", cert_dir), ("META_PATH", self.meta_path),
                            ("PROGRESS_PATH", cert_dir / "progress.json"), ("LIVE_CERT", cert_dir / "fullchain.pem"),
                            ("LIVE_KEY", cert_dir / "privkey.pem"), ("WEBROOT_DIR", root / "acme-www")):
            self.stack.enter_context(patch.object(ssl_certs, name, value))
        settings = SimpleNamespace(web_port=443, public_base_url="")
        self.stack.enter_context(patch("app.services.ssl_certs.get_settings", return_value=settings))
        (cert_dir / "fullchain.pem").write_text("CERT", encoding="utf-8")
        (cert_dir / "privkey.pem").write_text("KEY", encoding="utf-8")

    def tearDown(self) -> None:
        self.stack.close()
        self.tmp.cleanup()

    def issue(self, miniapp_domain: str, certbot=(True, "ok")) -> tuple[dict, list]:
        calls: list = []

        def fake_certbot(domain, email, *, force=False, also=""):
            calls.append((domain, also))
            return certbot

        with patch.object(ssl_certs, "certbot_available", return_value=True), \
                patch.object(ssl_certs, "_certbot_issue", side_effect=fake_certbot), \
                patch.object(ssl_certs, "_find_and_copy_cert", return_value="/etc/letsencrypt/live/x"), \
                patch.object(ssl_certs, "_parse_expiry", return_value=("2027-01-01", False)):
            result = ssl_certs.issue_or_renew(domain="panel.example.com", email="a@example.com",
                                              enable=False, restart=False, miniapp_domain=miniapp_domain)
        return result, calls

    def meta(self) -> dict:
        return json.loads(self.meta_path.read_text(encoding="utf-8"))

    def test_a_separate_mini_app_domain_goes_on_the_certificate_and_into_its_address(self) -> None:
        result, calls = self.issue("App.Example.com")
        self.assertTrue(result["ok"])
        self.assertEqual(calls, [("panel.example.com", "app.example.com")])
        self.assertEqual(self.meta()["miniapp_domain"], "app.example.com")
        meta = {**self.meta(), "ssl_enabled": True}
        self.meta_path.write_text(json.dumps(meta), encoding="utf-8")
        self.assertEqual(ssl_certs.public_miniapp_base_url(), "https://app.example.com")
        self.assertEqual(ssl_certs.public_panel_base_url(), "https://panel.example.com")
        self.assertEqual(ssl_certs.cert_status()["public_miniapp"], "https://app.example.com/miniapp/")

    def test_no_separate_domain_keeps_the_mini_app_on_the_panel(self) -> None:
        for same in ("", "panel.example.com"):
            with self.subTest(miniapp=same or "blank"):
                _result, calls = self.issue(same)
                self.assertEqual(calls[-1], ("panel.example.com", ""))
                meta = {**self.meta(), "ssl_enabled": True}
                self.meta_path.write_text(json.dumps(meta), encoding="utf-8")
                self.assertEqual(ssl_certs.public_miniapp_base_url(), "https://panel.example.com")
                self.assertEqual(ssl_certs.cert_status()["miniapp_domain"], "")

    def test_a_failed_issue_keeps_the_old_mini_app_address(self) -> None:
        self.meta_path.write_text(json.dumps({"domain": "panel.example.com", "miniapp_domain": "old.example.com",
                                              "ssl_enabled": True, "pending_miniapp_domain": "new.example.com"}),
                                  encoding="utf-8")
        result, _calls = self.issue("new.example.com", certbot=(False, "challenge failed"))
        self.assertFalse(result["ok"])
        meta = self.meta()
        self.assertEqual(meta["miniapp_domain"], "old.example.com")
        self.assertNotIn("pending_miniapp_domain", meta)

    def test_an_invalid_mini_app_domain_is_refused_before_certbot(self) -> None:
        result, calls = self.issue("not a domain")
        self.assertFalse(result["ok"])
        self.assertEqual(calls, [])

    def test_https_off_never_points_the_mini_app_at_its_own_domain(self) -> None:
        self.meta_path.write_text(json.dumps({"domain": "panel.example.com", "miniapp_domain": "app.example.com",
                                              "ssl_enabled": False}), encoding="utf-8")
        with patch("app.services.setup_wizard.default_http_panel_url", return_value="http://1.2.3.4:9000"):
            self.assertEqual(ssl_certs.public_miniapp_base_url(), "http://1.2.3.4:9000")


class OldHelperTests(unittest.TestCase):
    def certbot_issue(self, capabilities: frozenset[str]) -> tuple[tuple[bool, str], list]:
        commands: list = []
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(ssl_certs, "WEBROOT_DIR", Path(tmp)), \
                patch.object(ssl_certs, "certbot_bin", return_value="/usr/bin/certbot"), \
                patch.object(ssl_certs, "_port_in_use", return_value=False), \
                patch.object(ssl_certs, "_start_acme_http", return_value=object()), \
                patch.object(ssl_certs, "_stop_acme_http"), \
                patch.object(ssl_certs, "_set_progress"), \
                patch.object(ssl_certs, "_install_progress"), \
                patch.object(ssl_certs, "_run", side_effect=lambda cmd, timeout=0: commands.append(cmd) or (0, "ok")), \
                patch.object(service_control, "ensure_restart_helper", return_value=(True, "")), \
                patch.object(service_control, "helper_capabilities", return_value=capabilities), \
                patch.object(service_control, "HELPER_INSTALL_PATH", Path(tmp) / "ctl"):
            (Path(tmp) / "ctl").write_text("#!/bin/bash\n", encoding="utf-8")
            return ssl_certs._certbot_issue("panel.example.com", "a@example.com", also="app.example.com"), commands

    def test_an_old_helper_is_never_asked_for_a_second_name(self) -> None:
        (ok, message), commands = self.certbot_issue(frozenset())
        self.assertFalse(ok)
        self.assertEqual(message, ssl_certs.HELPER_TOO_OLD)
        self.assertEqual(commands, [], "no certificate is requested with the Mini App name missing")

    def test_a_current_helper_gets_the_mini_app_name(self) -> None:
        (ok, _message), commands = self.certbot_issue(frozenset({"san"}))
        self.assertTrue(ok)
        self.assertEqual(commands[0][-2:], ["--also", "app.example.com"])


class HelperRefreshTests(unittest.TestCase):
    def test_an_outdated_helper_that_cannot_be_replaced_still_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            installed = Path(tmp) / "ctl"
            installed.write_text("old helper", encoding="utf-8")
            sudoers = Path(tmp) / "sudoers"
            sudoers.write_text("x", encoding="utf-8")
            with patch.object(service_control, "HELPER_INSTALL_PATH", installed), \
                    patch.object(service_control, "SUDOERS_PATH", sudoers), \
                    patch.object(service_control, "can_passwordless_sudo", return_value=False), \
                    patch.object(service_control, "_sudo_restart_works", return_value=True):
                self.assertTrue(service_control.ensure_restart_helper()[0])

    def test_an_outdated_helper_is_reinstalled_when_sudo_allows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            installed = Path(tmp) / "ctl"
            installed.write_text("old helper", encoding="utf-8")
            sudoers = Path(tmp) / "sudoers"
            sudoers.write_text("x", encoding="utf-8")
            ran: list = []
            with patch.object(service_control, "HELPER_INSTALL_PATH", installed), \
                    patch.object(service_control, "SUDOERS_PATH", sudoers), \
                    patch.object(service_control, "can_passwordless_sudo", return_value=True), \
                    patch.object(service_control, "_sudo_restart_works", return_value=True), \
                    patch.object(service_control, "_run", side_effect=lambda cmd, timeout=0: ran.append(cmd)
                                 or subprocess.CompletedProcess(cmd, 0, "", "")):
                self.assertTrue(service_control.ensure_restart_helper()[0])
            self.assertTrue(any("bash" in cmd for cmd in ran), "the install script ran")

    def test_capabilities_are_empty_for_a_helper_without_the_subcommand(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            installed = Path(tmp) / "ctl"
            installed.write_text("x", encoding="utf-8")
            with patch.object(service_control, "HELPER_INSTALL_PATH", installed), \
                    patch.object(service_control, "_run", return_value=subprocess.CompletedProcess([], 2, "", "usage")):
                self.assertEqual(service_control.helper_capabilities(), frozenset())
            with patch.object(service_control, "HELPER_INSTALL_PATH", installed), \
                    patch.object(service_control, "_run", return_value=subprocess.CompletedProcess([], 0, "san\n", "")):
                self.assertEqual(service_control.helper_capabilities(), frozenset({"san"}))


class DomainsPageTests(unittest.TestCase):
    def render(self, st: dict) -> str:
        from app.api.app import templates

        request = SimpleNamespace(query_params={}, url=SimpleNamespace(path="/settings"), state=SimpleNamespace())
        base = {"progress": None, "running": False, "certbot": True, "pending_domain": "", "pending_email": "",
                "pending_miniapp_domain": "", "miniapp_domain": "", "public_miniapp": "", "has_cert": False}
        return templates.env.get_template("_settings_ssl.html").render(request=request, ssl={**base, **st})

    def test_mini_app_card_shows_its_domain_and_address(self) -> None:
        html = self.render({"domain": "panel.example.com", "miniapp_domain": "app.example.com",
                            "public_miniapp": "https://app.example.com/miniapp/"})
        self.assertIn("<h2>مینی‌اپ</h2>", html)
        self.assertIn('name="miniapp_domain" value="app.example.com"', html)
        self.assertNotRegex(html, r'name="miniapp_same_domain" value="1"\s+checked')
        self.assertIn("https://app.example.com/miniapp/", html)

    def test_without_its_own_domain_the_same_domain_switch_is_on(self) -> None:
        self.assertRegex(self.render({"domain": "panel.example.com"}), r'name="miniapp_same_domain" value="1"\s+checked')


if __name__ == "__main__":
    unittest.main()
