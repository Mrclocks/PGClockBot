"""CLI menu redesign + main/dev install channel (pgclock.sh / get.sh)."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SH = ROOT / "pgclock.sh"
GET = ROOT / "get.sh"


class InstallMenuChannelTests(unittest.TestCase):
    def test_brand_orange_mrclockbot_menu(self):
        src = SH.read_text(encoding="utf-8")
        self.assertIn("MrClockBot", src)
        self.assertIn("38;5;208m", src)  # orange accent
        self.assertIn("print_status_box", src)
        self.assertIn("Not installed", src)
        self.assertIn("Low resources", src)
        self.assertIn("hw_is_minimal", src)
        self.assertNotIn("C=$'\\033[0;36m'", src)
        self.assertIn("Install main", src)
        self.assertIn("Install dev", src)
        self.assertIn("Update main", src)
        self.assertIn("Update dev", src)
        self.assertNotIn("نصب نیست", src)

    def test_status_box_has_no_vertical_borders(self):
        src = SH.read_text(encoding="utf-8")
        box = src.split("print_status_box() {", 1)[1].split("show_menu() {", 1)[0]
        self.assertIn("Install :", box)
        self.assertIn("Version :", box)
        self.assertIn("Channel :", box)
        self.assertIn("Not installed", box)
        # No box-drawing verticals (break on many terminal fonts).
        self.assertNotIn("│", box)
        self.assertNotIn("┌", box)
        self.assertNotIn("└", box)
        self.assertNotIn("┐", box)
        self.assertNotIn("┘", box)
        self.assertIn("── Status", box)

    def test_install_update_channel_wiring(self):
        src = SH.read_text(encoding="utf-8")
        self.assertIn("set_install_channel", src)
        self.assertNotIn("prompt_install_channel", src)
        self.assertIn("sync_code_to_channel", src)
        self.assertIn("persist_channel_env", src)
        self.assertIn("UPDATE_CHANNEL", src)
        self.assertIn("DEPLOYED_CHANNEL", src)
        self.assertIn("deadsnakes", src)
        menu = src.split("run_menu() {", 1)[1].split("dispatch() {", 1)[0]
        self.assertIn("cmd_install main", menu)
        self.assertIn("cmd_install dev", menu)
        self.assertIn("cmd_update main", menu)
        self.assertIn("cmd_update dev", menu)
        install = src.split("cmd_install() {", 1)[1].split("cmd_update() {", 1)[0]
        self.assertIn("set_install_channel", install)
        self.assertIn("sync_code_to_channel", install)
        update = src.split("cmd_update() {", 1)[1].split("cmd_edit_env() {", 1)[0]
        self.assertIn("set_install_channel", update)
        self.assertIn("sync_code_to_channel", update)
        self.assertIn("stop_service_if_any", update)
        self.assertIn('install_systemd "$svc_user" 0', update)
        self.assertLess(
            update.find("stop_service_if_any"),
            update.find("sync_code_to_channel"),
        )
        self.assertLess(
            update.find("restart_service_if_any"),
            update.find("ensure_monitor_xray"),
        )
        self.assertNotIn("syncing hard to origin/main", update)
        unit = src.split("install_systemd() {", 1)[1].split("stop_service_if_any() {", 1)[0]
        self.assertIn("TimeoutStopSec=45", unit)

    def test_sync_uses_explicit_channel_refspec(self):
        src = SH.read_text(encoding="utf-8")
        sync = src.split("sync_code_to_channel() {", 1)[1].split("panel_is_installed() {", 1)[0]
        self.assertIn("+refs/heads/${ch}:refs/remotes/origin/${ch}", sync)
        self.assertIn("+refs/heads/*:refs/remotes/origin/*", sync)
        self.assertIn("git remote set-url origin", sync)
        self.assertIn('dispatch "$@"', src)
        channel = src.split("set_install_channel() {", 1)[1].split("persist_channel_env() {", 1)[0]
        self.assertIn("env_get DEPLOYED_CHANNEL", channel)

    def test_get_sh_clones_selected_branch(self):
        src = GET.read_text(encoding="utf-8")
        self.assertIn('git clone -b "$REMOTE_BRANCH" --single-branch', src)
        self.assertIn("PGCLOCK_CHANNEL", src)
        self.assertIn('REMOTE_BRANCH="dev"', src)
        self.assertIn("CHANNEL_FROM_ENV", src)
        self.assertIn("+refs/heads/${REMOTE_BRANCH}:refs/remotes/origin/${REMOTE_BRANCH}", src)
        self.assertIn('update-${REMOTE_BRANCH}', src)


class WriteEnvChannelTests(unittest.TestCase):
    def test_write_env_includes_channel_keys(self):
        src = (ROOT / "scripts" / "write_env.py").read_text(encoding="utf-8")
        self.assertIn('"UPDATE_CHANNEL"', src)
        self.assertIn('"DEPLOYED_CHANNEL"', src)


if __name__ == "__main__":
    unittest.main()
