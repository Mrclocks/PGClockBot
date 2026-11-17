"""Run get.sh and pgclock.sh against throwaway git repositories.

These tests execute the real shell code (no network, no systemd): a bare
repository stands in for GitHub, and shell functions or PATH stubs stand in for
systemctl, the database and the service.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PGCLOCK_SH = ROOT / "pgclock.sh"
GET_SH = ROOT / "get.sh"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}

# Stand-in manager: get.sh hands off to it, it reports what it was given.
STUB_MANAGER = """#!/usr/bin/env bash
echo "MANAGER head=$(git rev-parse HEAD) ref=${PGCLOCK_REF:-} args=$*"
"""


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PGCLOCK_")}
    env.update(GIT_ENV)
    env.update(extra)
    return env


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=cwd, env=_env(), check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


class _RepoFixture(unittest.TestCase):
    """A bare 'origin' plus a scratch checkout used to publish commits to it."""

    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git not installed")
        self.tmp = Path(tempfile.mkdtemp(prefix="pgclock-scripts-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.origin = self.tmp / "origin.git"
        _git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        _git(self.origin, "config", "uploadpack.allowReachableSHA1InWant", "true")
        self.author = self.tmp / "author"
        _git(self.tmp, "clone", "-q", str(self.origin), str(self.author))
        _git(self.author, "checkout", "-q", "-b", "main")
        (self.author / "pgclock.sh").write_text(STUB_MANAGER, encoding="utf-8")
        (self.author / "run.py").write_text("", encoding="utf-8")

    def publish(self, label: str) -> str:
        (self.author / "VERSION").write_text(label + "\n", encoding="utf-8")
        _git(self.author, "add", "-A")
        _git(self.author, "commit", "-q", "-m", label)
        _git(self.author, "push", "-q", "origin", "main")
        return _git(self.author, "rev-parse", "HEAD")

    def tag(self, name: str) -> None:
        _git(self.author, "tag", "-a", name, "-m", name)
        _git(self.author, "push", "-q", "origin", name)

    def clone_install(self, name: str = "PGClockBot") -> Path:
        install = self.tmp / name
        _git(self.tmp, "clone", "-q", "-b", "main", "--single-branch", str(self.origin), str(install))
        return install


class PgclockShellHarness(_RepoFixture):
    """Source pgclock.sh, move into the install dir, then run a snippet."""

    def run_pgclock(self, install: Path, snippet: str, **env: str) -> subprocess.CompletedProcess[str]:
        script = (
            f'source "{PGCLOCK_SH}"\n'
            f'cd "{install}"\n'
            f'SCRIPT_DIR="{install}"\n'
            f"{snippet}\n"
        )
        return subprocess.run(
            ["bash", "-c", script],
            env=_env(PGCLOCK_REPO=str(self.origin), **env),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            timeout=120,
        )


class SyncCodeTests(PgclockShellHarness):
    def test_pinned_tag_checks_out_tag_commit_and_keeps_runtime_files(self) -> None:
        tagged = self.publish("one")
        self.tag("v1.0.0")
        install = self.clone_install()
        self.publish("two")
        (install / ".env").write_text('WEB_PORT="9000"\n', encoding="utf-8")
        (install / "data").mkdir()
        (install / "data" / "keep.txt").write_text("x", encoding="utf-8")
        (install / "stray.txt").write_text("x", encoding="utf-8")

        res = self.run_pgclock(install, "sync_code_to_channel main", PGCLOCK_REF="v1.0.0")

        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(_git(install, "rev-parse", "HEAD"), tagged)
        self.assertTrue((install / ".env").exists())
        self.assertTrue((install / "data" / "keep.txt").exists())
        self.assertFalse((install / "stray.txt").exists())

    def test_pinned_full_sha_checks_out_that_commit(self) -> None:
        first = self.publish("one")
        install = self.clone_install()
        self.publish("two")

        res = self.run_pgclock(install, "sync_code_to_channel main", PGCLOCK_REF=first)

        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(_git(install, "rev-parse", "HEAD"), first)

    def test_option_like_ref_is_rejected_without_touching_tree(self) -> None:
        first = self.publish("one")
        install = self.clone_install()
        self.publish("two")

        res = self.run_pgclock(
            install, "sync_code_to_channel main || echo REJECTED", PGCLOCK_REF="--upload-pack=x"
        )

        self.assertIn("REJECTED", res.stdout)
        self.assertEqual(_git(install, "rev-parse", "HEAD"), first)

    def test_channel_sync_follows_tip_of_configured_repo(self) -> None:
        self.publish("one")
        install = self.clone_install()
        _git(install, "remote", "set-url", "origin", str(self.tmp / "missing.git"))
        tip = self.publish("two")

        res = self.run_pgclock(install, "sync_code_to_channel main")

        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(_git(install, "rev-parse", "HEAD"), tip)
        self.assertEqual(_git(install, "remote", "get-url", "origin"), str(self.origin))


# Stubs for everything cmd_update touches outside git. Each records a call so the
# test can see the order; the schema revision is read from a file the stubs move.
UPDATE_STUBS = r"""
calls="$SCRIPT_DIR/../calls.log"
schema_file="$SCRIPT_DIR/../schema.txt"
banner_small() { :; }
print_success() { echo "SUCCESS $1" >> "$calls"; }
ensure_pg_client_tools() { :; }
backup_before_update() { echo backup >> "$calls"; [[ "${FAIL_AT:-}" != backup ]]; }
stop_service_if_any() { echo stop >> "$calls"; }
ensure_python() { :; }
ensure_venv() { echo "venv $(git rev-parse --short HEAD)" >> "$calls"; [[ "${FAIL_AT:-}" != venv ]]; }
ensure_public_web_host() { :; }
db_schema_revision() { cat "$schema_file"; }
ensure_db_schema() {
  [[ -n "${MIGRATE_TO:-}" ]] && echo "$MIGRATE_TO" > "$schema_file"
  [[ "${FAIL_AT:-}" != schema ]]
}
service_installed() { return 1; }
restart_service_if_any() {
  echo "restart $(git rev-parse --short HEAD)" >> "$calls"
  if [[ "${FAIL_AT:-}" == restart && ! -e "$SCRIPT_DIR/../restarted_once" ]]; then
    touch "$SCRIPT_DIR/../restarted_once"
    return 1
  fi
  return 0
}
ensure_monitor_xray() { :; }
PY=/bin/true
"""


class _UpdateFixture(PgclockShellHarness):
    """An installed old version with a newer commit published upstream."""

    def setUp(self) -> None:
        super().setUp()
        self.old = self.publish("old")
        self.install = self.clone_install()
        (self.install / ".env").write_text('DEPLOYED_CHANNEL="main"\n', encoding="utf-8")
        self.new = self.publish("new")
        (self.tmp / "schema.txt").write_text("0100\n", encoding="utf-8")

    def update(self, **env: str) -> tuple[int, list[str]]:
        res = self.run_pgclock(
            self.install,
            UPDATE_STUBS + 'cmd_update main && echo "RC=0" || echo "RC=1"',
            **env,
        )
        calls_file = self.tmp / "calls.log"
        calls = calls_file.read_text(encoding="utf-8").split("\n")[:-1] if calls_file.exists() else []
        rc = 0 if "RC=0" in res.stdout else 1
        self.assertIn("RC=", res.stdout, res.stdout + res.stderr)
        return rc, calls

    def head(self) -> str:
        return _git(self.install, "rev-parse", "HEAD")


class SafeUpdateTests(_UpdateFixture):
    def test_successful_update_backs_up_first_and_starts_new_code(self) -> None:
        rc, calls = self.update(MIGRATE_TO="0101")
        self.assertEqual(rc, 0)
        self.assertEqual(calls[:2], ["backup", "stop"])
        self.assertIn(f"restart {self.new[:7]}", calls)
        self.assertEqual(self.head(), self.new)

    def test_failed_backup_leaves_running_panel_untouched(self) -> None:
        rc, calls = self.update(FAIL_AT="backup")
        self.assertEqual(rc, 1)
        self.assertEqual(calls, ["backup"])
        self.assertEqual(self.head(), self.old)

    def test_failed_migration_with_unchanged_schema_restores_previous_version(self) -> None:
        rc, calls = self.update(FAIL_AT="schema")
        self.assertEqual(rc, 1)
        self.assertEqual(self.head(), self.old)
        self.assertEqual(calls[-1], f"restart {self.old[:7]}")
        env_text = (self.install / ".env").read_text(encoding="utf-8")
        self.assertNotIn("UPDATE_CHANNEL", env_text)

    def test_failed_migration_with_changed_schema_does_not_restore_old_code(self) -> None:
        rc, calls = self.update(FAIL_AT="schema", MIGRATE_TO="0101")
        self.assertEqual(rc, 1)
        self.assertEqual(self.head(), self.new)
        self.assertFalse(any(c.startswith("restart") for c in calls))

    def test_failed_package_install_restores_previous_version(self) -> None:
        rc, calls = self.update(FAIL_AT="venv")
        self.assertEqual(rc, 1)
        self.assertEqual(self.head(), self.old)
        self.assertEqual(calls[-1], f"restart {self.old[:7]}")

    def test_new_version_that_does_not_start_is_replaced_by_previous(self) -> None:
        rc, calls = self.update(FAIL_AT="restart")
        self.assertEqual(rc, 1)
        self.assertEqual(self.head(), self.old)
        self.assertEqual(
            [c for c in calls if c.startswith("restart")],
            [f"restart {self.new[:7]}", f"restart {self.old[:7]}"],
        )

    def test_new_version_that_migrated_and_did_not_start_is_left_in_place(self) -> None:
        rc, calls = self.update(FAIL_AT="restart", MIGRATE_TO="0101")
        self.assertEqual(rc, 1)
        self.assertEqual(self.head(), self.new)
        self.assertEqual([c for c in calls if c.startswith("restart")], [f"restart {self.new[:7]}"])


class UpdateFailureDetectionTests(_UpdateFixture):
    """Failures inside the real helpers must stop the update (set -e is off in `if`)."""

    def _real_venv_update(self, pip_body: str) -> tuple[int, list[str], str]:
        (self.author / "requirements.txt").write_text("demo==1\n", encoding="utf-8")
        self.new = self.publish("new-with-requirements")
        venv_bin = self.install / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "activate").write_text("", encoding="utf-8")
        fake_py = venv_bin / "python"
        fake_py.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_py.chmod(0o755)
        stubs = UPDATE_STUBS.replace(
            'ensure_venv() { echo "venv $(git rev-parse --short HEAD)" >> "$calls"; [[ "${FAIL_AT:-}" != venv ]]; }\n',
            "",
        )
        snippet = (
            stubs
            + "SYSTEM_PY=/bin/true\n_python_is_supported() { return 0; }\n"
            + f"pip() {{ {pip_body}; }}\n"
            + 'cmd_update main && echo "RC=0" || echo "RC=1"'
        )
        res = self.run_pgclock(self.install, snippet)
        calls_file = self.tmp / "calls.log"
        calls = calls_file.read_text(encoding="utf-8").split("\n")[:-1] if calls_file.exists() else []
        return (0 if "RC=0" in res.stdout else 1), calls, res.stdout

    def test_failed_pip_stops_update_and_restores_previous_version(self) -> None:
        rc, calls, out = self._real_venv_update("return 1")
        self.assertEqual(rc, 1, out)
        self.assertNotIn("Python dependencies installed", out)
        self.assertEqual(self.head(), self.old)
        self.assertNotIn("SUCCESS Update complete", calls)
        self.assertFalse((self.install / ".venv" / ".requirements.sha256").exists())

    def test_working_pip_records_requirements_hash(self) -> None:
        rc, calls, out = self._real_venv_update("return 0")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.head(), self.new)
        self.assertTrue((self.install / ".venv" / ".requirements.sha256").exists())

    def test_failed_checkout_stops_update_and_restores_previous_version(self) -> None:
        snippet = (
            UPDATE_STUBS
            + 'git() { if [[ "$1" == checkout && "${3:-}" == -B && "${4:-}" == main ]]; then return 1; fi; command git "$@"; }\n'
            + 'cmd_update main && echo "RC=0" || echo "RC=1"'
        )
        res = self.run_pgclock(self.install, snippet)
        self.assertIn("RC=1", res.stdout, res.stdout + res.stderr)
        self.assertNotIn("Code synced", res.stdout)
        calls = (self.tmp / "calls.log").read_text(encoding="utf-8")
        self.assertNotIn("SUCCESS", calls)

    def test_failed_restart_stops_crashing_new_version_before_restoring(self) -> None:
        rc, calls = self.update(FAIL_AT="restart")
        self.assertEqual(rc, 1)
        first_restart = calls.index(f"restart {self.new[:7]}")
        self.assertIn("stop", calls[first_restart:])


class BackupBeforeUpdateTests(PgclockShellHarness):
    def setUp(self) -> None:
        super().setUp()
        self.publish("one")
        self.install = self.clone_install()
        self.fake_py = self.tmp / "fake_py"

    def _fake_python(self, exit_code: int) -> None:
        self.fake_py.write_text(
            f'#!/usr/bin/env bash\necho "$*" >> "{self.tmp}/py.log"\nexit {exit_code}\n',
            encoding="utf-8",
        )
        self.fake_py.chmod(0o755)

    def test_backup_runs_cli_backup_for_this_install(self) -> None:
        self._fake_python(0)
        res = self.run_pgclock(self.install, f'PY="{self.fake_py}"; backup_before_update && echo OK')
        self.assertIn("OK", res.stdout)
        logged = (self.tmp / "py.log").read_text(encoding="utf-8")
        self.assertIn(f"-m app.cli --root {self.install} backup --note pre-update", logged)

    def test_backup_failure_is_reported(self) -> None:
        self._fake_python(1)
        res = self.run_pgclock(self.install, f'PY="{self.fake_py}"; backup_before_update || echo FAILED')
        self.assertIn("FAILED", res.stdout)

    def test_skip_flag_bypasses_backup(self) -> None:
        self._fake_python(1)
        res = self.run_pgclock(
            self.install,
            f'PY="{self.fake_py}"; backup_before_update && echo OK',
            PGCLOCK_SKIP_BACKUP="1",
        )
        self.assertIn("OK", res.stdout)
        self.assertFalse((self.tmp / "py.log").exists())


class HealthBodyTests(PgclockShellHarness):
    def test_only_ok_true_counts_as_healthy(self) -> None:
        self.publish("one")
        install = self.clone_install()
        cases = {
            '{"ok": true}': "yes",
            '{"ok":true}': "yes",
            '{"ok": false}': "no",
            "<html>ok, true story</html>": "no",
            "": "no",
        }
        for body, expected in cases.items():
            with self.subTest(body=body):
                res = self.run_pgclock(
                    install, "health_body_is_ok \"$BODY\" && echo yes || echo no", BODY=body
                )
                self.assertEqual(res.stdout.strip().splitlines()[-1], expected)


class _GetShFixture(_RepoFixture):
    """A stub systemctl and a private TMPDIR for get.sh runs."""

    def setUp(self) -> None:
        super().setUp()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        systemctl = self.bin / "systemctl"
        # is-active succeeds only when the test says the panel is running.
        systemctl.write_text(
            '#!/usr/bin/env bash\n[[ "$1" == is-active && "${FAKE_ACTIVE:-0}" == 1 ]]\n',
            encoding="utf-8",
        )
        systemctl.chmod(0o755)
        self.tmpdir = self.tmp / "tmp"
        self.tmpdir.mkdir()

    def run_get(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        res = subprocess.run(
            ["bash", str(GET_SH), *args],
            cwd=self.tmp,
            env=_env(
                PGCLOCK_REPO=str(self.origin),
                PATH=f"{self.bin}:{os.environ['PATH']}",
                TMPDIR=str(self.tmpdir),
                PGCLOCK_MARKER=str(self.tmp / "no-marker"),
                **env,
            ),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            timeout=120,
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        return res


class GetShTests(_GetShFixture):
    def test_fresh_server_clones_and_hands_off_to_manager(self) -> None:
        tip = self.publish("one")
        res = self.run_get("status")
        self.assertIn(f"MANAGER head={tip}", res.stdout)
        self.assertIn("args=status", res.stdout)

    def test_running_panel_code_is_not_changed_by_bootstrap(self) -> None:
        old = self.publish("one")
        install = self.clone_install()
        self.publish("two")
        res = self.run_get(FAKE_ACTIVE="1")
        self.assertEqual(_git(install, "rev-parse", "HEAD"), old)
        self.assertIn(f"MANAGER head={old}", res.stdout)

    def test_stopped_panel_is_brought_to_branch_tip(self) -> None:
        self.publish("one")
        install = self.clone_install()
        tip = self.publish("two")
        self.run_get()
        self.assertEqual(_git(install, "rev-parse", "HEAD"), tip)

    def test_forced_channel_sync_keeps_runtime_files_and_leaves_no_temp_copy(self) -> None:
        self.publish("one")
        install = self.clone_install()
        (install / ".env").write_text('BOT_TOKEN="secret"\n', encoding="utf-8")
        (install / "data").mkdir()
        (install / "data" / "db.txt").write_text("rows", encoding="utf-8")
        tip = self.publish("two")
        self.run_get(PGCLOCK_CHANNEL="main")
        self.assertEqual(_git(install, "rev-parse", "HEAD"), tip)
        self.assertEqual((install / ".env").read_text(encoding="utf-8"), 'BOT_TOKEN="secret"\n')
        self.assertEqual((install / "data" / "db.txt").read_text(encoding="utf-8"), "rows")
        self.assertEqual(list(self.tmpdir.glob("pgclock-runtime.*")), [])

    def test_pinned_tag_on_fresh_server_runs_manager_from_tag(self) -> None:
        tagged = self.publish("one")
        self.tag("v1.0.0")
        self.publish("two")
        res = self.run_get(PGCLOCK_REF="v1.0.0")
        self.assertIn(f"MANAGER head={tagged} ref=v1.0.0", res.stdout)



class NoTerminalTests(_RepoFixture):
    """Commands allowed without a TTY must work from cron, ssh or the web panel."""

    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        install = self.tmp / "noterm"
        install.mkdir(exist_ok=True)
        shutil.copy(PGCLOCK_SH, install / "pgclock.sh")
        return subprocess.run(
            ["bash", str(install / "pgclock.sh"), *args],
            cwd=install,
            env=_env(),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            timeout=120,
        )

    def test_help_and_update_print_to_stdout_without_a_terminal(self) -> None:
        res = self.run_script("help")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("/dev/tty", res.stderr)
        res = self.run_script("update", "main")
        self.assertEqual(res.returncode, 1)
        self.assertIn(".env not found", res.stdout)
        self.assertIn("Update (main)", res.stdout)
        self.assertNotIn("/dev/tty", res.stderr)

    def test_interactive_command_explains_missing_terminal(self) -> None:
        res = self.run_script("env")
        self.assertEqual(res.returncode, 1)
        self.assertIn("No interactive terminal", res.stderr)


class MenuActionTests(PgclockShellHarness):
    def test_failed_option_returns_to_menu_and_keeps_set_e_inside(self) -> None:
        self.publish("one")
        install = self.clone_install()
        snippet = (
            "failing() { false; echo AFTER_FALSE; }\n"
            "menu_action failing\n"
            "echo MENU_STILL_RUNNING\n"
        )
        res = self.run_pgclock(install, snippet)
        self.assertIn("MENU_STILL_RUNNING", res.stdout)
        self.assertNotIn("AFTER_FALSE", res.stdout)
        self.assertIn("Stopped with an error", res.stdout)


class GetShFailureTests(_GetShFixture):
    def test_unreachable_remote_leaves_tree_and_no_secret_copy(self) -> None:
        old = self.publish("one")
        install = self.clone_install()
        (install / ".env").write_text('BOT_TOKEN="secret"\n', encoding="utf-8")
        res = subprocess.run(
            ["bash", str(GET_SH)],
            cwd=self.tmp,
            env=_env(
                PGCLOCK_REPO=str(self.tmp / "missing.git"),
                PGCLOCK_CHANNEL="main",
                PATH=f"{self.bin}:{os.environ['PATH']}",
                TMPDIR=str(self.tmpdir),
                PGCLOCK_MARKER=str(self.tmp / "no-marker"),
            ),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            timeout=120,
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(_git(install, "rev-parse", "HEAD"), old)
        self.assertEqual((install / ".env").read_text(encoding="utf-8"), 'BOT_TOKEN="secret"\n')
        self.assertEqual(list(self.tmpdir.glob("pgclock-runtime.*")), [])

    def test_failed_reclone_keeps_folder_without_git(self) -> None:
        self.publish("one")
        folder = self.tmp / "PGClockBot"
        folder.mkdir()
        (folder / "run.py").write_text("", encoding="utf-8")
        (folder / ".env").write_text('BOT_TOKEN="secret"\n', encoding="utf-8")
        res = subprocess.run(
            ["bash", str(GET_SH)],
            cwd=self.tmp,
            env=_env(
                PGCLOCK_REPO=str(self.tmp / "missing.git"),
                PATH=f"{self.bin}:{os.environ['PATH']}",
                TMPDIR=str(self.tmpdir),
                PGCLOCK_MARKER=str(self.tmp / "no-marker"),
            ),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            timeout=120,
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual((folder / ".env").read_text(encoding="utf-8"), 'BOT_TOKEN="secret"\n')
        self.assertEqual(list(self.tmp.glob("PGClockBot.clone.*")), [])

    def test_successful_reclone_keeps_runtime_files(self) -> None:
        tip = self.publish("one")
        folder = self.tmp / "PGClockBot"
        folder.mkdir()
        (folder / "run.py").write_text("", encoding="utf-8")
        (folder / ".env").write_text('BOT_TOKEN="secret"\n', encoding="utf-8")
        self.run_get()
        self.assertEqual(_git(folder, "rev-parse", "HEAD"), tip)
        self.assertEqual((folder / ".env").read_text(encoding="utf-8"), 'BOT_TOKEN="secret"\n')



    def test_one_liner_from_another_directory_reuses_marked_install(self) -> None:
        old = self.publish("one")
        install = self.clone_install("installed-elsewhere")
        marker = self.tmp / "install_root"
        marker.write_text(f"{install}\n", encoding="utf-8")
        elsewhere = self.tmp / "home"
        elsewhere.mkdir()
        res = subprocess.run(
            ["bash", str(GET_SH)],
            cwd=elsewhere,
            env=_env(
                PGCLOCK_REPO=str(self.origin),
                PATH=f"{self.bin}:{os.environ['PATH']}",
                TMPDIR=str(self.tmpdir),
                PGCLOCK_MARKER=str(marker),
                FAKE_ACTIVE="1",
            ),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            timeout=120,
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn(f"MANAGER head={old}", res.stdout)
        self.assertFalse((elsewhere / "PGClockBot").exists())


# Stand-ins that report which side of the wrapper ran and from which directory.
FAKE_MANAGER = '#!/usr/bin/env bash\necho "MANAGER cwd=$(pwd) args=$*"\n'
FAKE_OPS_PY = '#!/usr/bin/env bash\necho "OPS cwd=$(pwd) args=$*"\n'


class GlobalCommandTests(unittest.TestCase):
    """`pgclock` must work from any directory, for menu and operations commands."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pgclock-cli-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.install = self.tmp / "PGClockBot"
        (self.install / "app").mkdir(parents=True)
        (self.install / "scripts").mkdir()
        (self.install / "run.py").write_text("", encoding="utf-8")
        (self.install / "pgclock.sh").write_text(FAKE_MANAGER, encoding="utf-8")
        shutil.copy(ROOT / "scripts" / "pgclock", self.install / "scripts" / "pgclock")
        venv_py = self.install / ".venv" / "bin" / "python"
        venv_py.parent.mkdir(parents=True)
        venv_py.write_text(FAKE_OPS_PY, encoding="utf-8")
        venv_py.chmod(0o755)
        self.lib = self.tmp / "lib"
        self.bin = self.tmp / "bin" / "pgclock"
        res = subprocess.run(
            ["bash", str(ROOT / "scripts" / "install_global_cli.sh"), str(self.install)],
            env=_env(PGCLOCK_CLI_LIB=str(self.lib), PGCLOCK_CLI_BIN=str(self.bin)),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.elsewhere = self.tmp / "root-home"
        self.elsewhere.mkdir()

    def pgclock(self, *args: str, marker: Path | None = None) -> subprocess.CompletedProcess[str]:
        env = _env(PGCLOCK_MARKER=str(marker or self.lib / "install_root"), HOME=str(self.elsewhere))
        return subprocess.run(
            [str(self.bin), *args],
            cwd=self.elsewhere,
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )

    def test_installer_writes_marker_and_the_repo_wrapper(self) -> None:
        self.assertEqual((self.lib / "install_root").read_text(encoding="utf-8").strip(), str(self.install))
        self.assertEqual(self.bin.read_bytes(), (ROOT / "scripts" / "pgclock").read_bytes())

    def test_menu_and_manager_commands_run_pgclock_sh_from_any_directory(self) -> None:
        cases = {
            (): "args=",
            ("update",): "args=update",
            ("update", "dev"): "args=update dev",
            ("install", "main"): "args=install main",
            ("env",): "args=env",
            ("uninstall",): "args=uninstall",
            ("status",): "args=status",
        }
        for args, expected in cases.items():
            with self.subTest(args=args):
                res = self.pgclock(*args)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertIn("MANAGER", res.stdout)
                self.assertTrue(res.stdout.strip().endswith(expected), res.stdout)

    def test_operations_run_in_the_install_folder(self) -> None:
        for args in (("logs", "-n", "5"), ("backup", "--list"), ("status", "--json"), ("doctor",)):
            with self.subTest(args=args):
                res = self.pgclock(*args)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertEqual(res.stdout.strip(), f"OPS cwd={self.install} args=-m app.cli {' '.join(args)}")

    def test_help_lists_manager_commands(self) -> None:
        res = self.pgclock("help")
        self.assertEqual(res.returncode, 0)
        self.assertIn("pgclock update", res.stdout)
        self.assertIn(str(self.install), res.stdout)

    def test_missing_install_explains_how_to_fix(self) -> None:
        res = self.pgclock("status", marker=self.tmp / "missing-marker")
        self.assertEqual(res.returncode, 1)
        self.assertIn("install folder not found", res.stderr)
        self.assertIn("PGCLOCK_HOME", res.stderr)


if __name__ == "__main__":
    unittest.main()
