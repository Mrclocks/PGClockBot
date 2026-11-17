"""Offline process, readiness, resource and cancellation boundaries for probes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services.connection_monitor_catalog import plan_monitor_hosts
from app.services.xray_probe import run_xray_batch
from app.services.xray_probe_process import START_MARKER, drain_output, spawn_process, stop_process, wait_ready
from app.services.xray_probe_resources import XrayBinary, XrayProbeError, prepare_resources


class FakeProcess:
    def __init__(self, *, version: bytes | None = None, started: bool = True, ignores_term: bool = False) -> None:
        self.stdout = asyncio.StreamReader()
        self.returncode = 0 if version is not None else None
        self.exited = asyncio.Event()
        self.signals: list[str] = []
        self.waits = 0
        self.ignores_term = ignores_term
        if version is not None:
            self.stdout.feed_data(version)
            self.stdout.feed_eof()
            self.exited.set()
        elif started:
            self.stdout.feed_data(b"[Warning] " + START_MARKER + b"\n")

    def terminate(self) -> None:
        self.signals.append("terminate")
        if not self.ignores_term:
            self.returncode = -15
            self.exited.set()

    def kill(self) -> None:
        self.signals.append("kill")
        self.returncode = -9
        self.exited.set()

    async def wait(self) -> int:
        self.waits += 1
        await self.exited.wait()
        return self.returncode


class FakeSocket:
    port = 19000

    def __init__(self, *args: object) -> None:
        FakeSocket.port += 1
        self.port = FakeSocket.port

    def bind(self, address: tuple[str, int]) -> None:
        assert address == ("127.0.0.1", 0)

    def getsockname(self) -> tuple[str, int]:
        return "127.0.0.1", self.port

    def close(self) -> None:
        return None


class XrayLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "core"
        self.path.write_bytes(b"synthetic executable")
        self.path.chmod(0o700)
        self.binary = XrayBinary(self.path, hashlib.sha256(self.path.read_bytes()).hexdigest())
        catalog = json.loads((Path(__file__).parent / "fixtures/connection_monitor_catalog.json").read_text())
        self.endpoints = plan_monitor_hosts(**catalog, selected_ids=(41,), staff={"pg_is_owner": True}, account_revision="a")[0].endpoints
        self.socket_patch = patch("app.services.xray_probe_resources.socket.socket", FakeSocket)
        self.socket_patch.start()
        self.addCleanup(self.socket_patch.stop)
        self.addCleanup(self.folder.cleanup)

    def test_config_has_private_permissions_from_creation_with_an_unrestricted_umask(self) -> None:
        modes: list[int] = []
        original_open, original_chmod = os.open, os.chmod

        def record_open(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
            descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
            if Path(path).name == "config.json":
                modes.append(stat.S_IMODE(os.fstat(descriptor).st_mode))
            return descriptor

        def record_chmod(path: Path, mode: int) -> None:
            if Path(path).name == "config.json":
                modes.append(stat.S_IMODE(Path(path).stat().st_mode))
            original_chmod(path, mode)

        resources = None
        previous = os.umask(0)
        try:
            with patch("os.open", side_effect=record_open), patch("os.chmod", side_effect=record_chmod):
                resources = prepare_resources(self.endpoints)
        finally:
            os.umask(previous)
            if resources is not None:
                resources.close()
        self.assertEqual(modes, [0o600])

    def test_writable_binary_parent_is_rejected(self) -> None:
        for mode in (0o770, 0o707):
            with self.subTest(mode=oct(mode)):
                self.path.parent.chmod(mode)
                try:
                    with self.assertRaisesRegex(XrayProbeError, "^core_binary$"):
                        self.binary.verify()
                finally:
                    self.path.parent.chmod(0o700)

    async def test_binary_verification_and_hashing_run_outside_the_event_loop_thread(self) -> None:
        threads: list[int] = []
        original_verify = XrayBinary.verify
        event_loop_thread = threading.get_ident()

        def verify(binary: XrayBinary) -> Path:
            threads.append(threading.get_ident())
            return original_verify(binary)

        children = [FakeProcess(version=b"Xray 26.3.27 (Xray)\n"), FakeProcess()]
        with patch.object(XrayBinary, "verify", new=verify), patch("asyncio.create_subprocess_exec", side_effect=children):
            async with run_xray_batch(self.binary, self.endpoints):
                self.assertEqual(len(threads), 1)
                self.assertNotEqual(threads[0], event_loop_thread)

    async def test_one_process_per_batch_has_private_config_and_cleanup(self) -> None:
        child = FakeProcess()
        captured: list[Path] = []

        async def factory(*args: str, **kwargs: object) -> FakeProcess:
            self.assertEqual(kwargs["env"], {"LANG": "C", "TZ": "UTC"})
            self.assertTrue(kwargs["start_new_session"])
            if args[1] == "version":
                return FakeProcess(version=b"Xray 26.3.27 (Xray) synthetic\n")
            config_path = Path(args[-1])
            captured.append(config_path)
            self.assertEqual((await asyncio.to_thread(config_path.stat)).st_mode & 0o777, 0o600)
            self.assertEqual((await asyncio.to_thread(config_path.parent.stat)).st_mode & 0o777, 0o700)
            config = json.loads(await asyncio.to_thread(config_path.read_text))
            self.assertEqual(len(config["inbounds"]), 2)
            self.assertTrue(all(row["listen"] == "127.0.0.1" for row in config["inbounds"]))
            self.assertNotIn(self.endpoints[0].outbound_json, " ".join(args))
            return child

        with patch("asyncio.create_subprocess_exec", side_effect=factory) as spawn:
            async with run_xray_batch(self.binary, self.endpoints) as batch:
                self.assertEqual(len(batch.bindings), 2)
                self.assertTrue(batch.is_alive)
            self.assertEqual(spawn.call_count, 2)
        self.assertEqual(child.signals, ["terminate"])
        self.assertGreaterEqual(child.waits, 1)
        self.assertFalse(captured[0].parent.exists())

    async def test_failed_bind_retries_new_ports_and_reaps_each_child(self) -> None:
        failed = FakeProcess(started=False)
        failed.returncode = 1
        failed.exited.set()
        good = FakeProcess()
        children = [FakeProcess(version=b"Xray 26.3.27 (Xray)\n"), failed, good]
        with patch("asyncio.create_subprocess_exec", side_effect=children) as spawn:
            async with run_xray_batch(self.binary, self.endpoints) as batch:
                self.assertTrue(batch.is_alive)
            paths = [Path(call.args[-1]) for call in spawn.call_args_list[1:]]
        self.assertEqual(spawn.call_count, 3)
        self.assertGreaterEqual(failed.waits, 1)
        self.assertTrue(all(not path.parent.exists() for path in paths))

    async def test_old_open_port_without_child_marker_does_not_become_ready(self) -> None:
        child = FakeProcess(started=False)
        with patch("app.services.xray_probe_process.CORE_START_SECONDS", 0.01):
            with self.assertRaisesRegex(XrayProbeError, "^core_readiness$"):
                await wait_ready(child, asyncio.Event())

    async def test_exhausted_readiness_retries_clean_all_processes_and_files(self) -> None:
        children = [FakeProcess(started=False) for _ in range(3)]
        version = FakeProcess(version=b"Xray 26.3.27 (Xray)\n")
        with patch("asyncio.create_subprocess_exec", side_effect=[version, *children]) as spawn:
            with patch("app.services.xray_probe_process.CORE_START_SECONDS", 0.01):
                with self.assertRaisesRegex(XrayProbeError, "^core_readiness$"):
                    async with run_xray_batch(self.binary, self.endpoints):
                        self.fail("A child without its own startup marker must not be ready")
        self.assertEqual(spawn.call_count, 4)
        self.assertTrue(all(child.signals == ["terminate"] for child in children))
        self.assertTrue(all(not Path(call.args[-1]).parent.exists() for call in spawn.call_args_list[1:]))

    async def test_marker_from_an_exited_child_is_not_ready(self) -> None:
        child = FakeProcess(version=b"ignored")
        ready = asyncio.Event()
        ready.set()
        with self.assertRaisesRegex(XrayProbeError, "^core_start$"):
            await wait_ready(child, ready)

    async def test_output_is_drained_in_chunks_and_split_marker_is_detected(self) -> None:
        child = FakeProcess(started=False)
        ready = asyncio.Event()
        reader = asyncio.create_task(drain_output(child, ready))
        for _ in range(100):
            child.stdout.feed_data(b"sensitive-data" * 500)
            await asyncio.sleep(0)
        child.stdout.feed_data(b"\n[Warning] " + START_MARKER[:12])
        await asyncio.sleep(0)
        child.stdout.feed_data(START_MARKER[12:] + b"\n")
        child.stdout.feed_eof()
        await reader
        self.assertTrue(ready.is_set())

    async def test_cancellation_reaps_child_and_removes_config(self) -> None:
        entered = asyncio.Event()
        child = FakeProcess()
        paths: list[Path] = []

        async def factory(*args: str, **kwargs: object) -> FakeProcess:
            if args[1] == "version":
                return FakeProcess(version=b"Xray 26.3.27 (Xray)\n")
            paths.append(Path(args[-1]))
            return child

        async def run() -> None:
            async with run_xray_batch(self.binary, self.endpoints):
                entered.set()
                await asyncio.Event().wait()

        with patch("asyncio.create_subprocess_exec", side_effect=factory):
            task = asyncio.create_task(run())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(child.signals, ["terminate"])
        self.assertFalse(paths[0].parent.exists())

    async def test_ignored_termination_uses_kill_then_wait(self) -> None:
        child = FakeProcess(ignores_term=True)
        with patch("app.services.xray_probe_process.CORE_STOP_SECONDS", 0.01):
            await stop_process(child)
        self.assertEqual(child.signals, ["terminate", "kill"])
        self.assertGreaterEqual(child.waits, 2)

    async def test_cancel_during_spawn_waits_for_child_then_stops_it(self) -> None:
        requested, released = asyncio.Event(), asyncio.Event()
        child = FakeProcess()

        async def factory(*args: str, **kwargs: object) -> FakeProcess:
            requested.set()
            await released.wait()
            return child

        with patch("asyncio.create_subprocess_exec", side_effect=factory):
            task = asyncio.create_task(spawn_process("synthetic", "run"))
            await requested.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            released.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(child.signals, ["terminate"])

    async def test_start_os_error_is_safe(self) -> None:
        with patch("asyncio.create_subprocess_exec", side_effect=OSError("sensitive-path")):
            with self.assertRaisesRegex(XrayProbeError, "^core_start$"):
                await spawn_process("synthetic")

    async def test_invalid_binary_paths_permissions_and_checksums_stop_before_spawn(self) -> None:
        bad = [XrayBinary(Path("relative"), self.binary.sha256),
               XrayBinary(self.path.with_name("missing"), self.binary.sha256),
               XrayBinary(self.path, "0" * 64), XrayBinary(self.path, "not-a-checksum")]
        link = self.path.with_name("symlink")
        link.symlink_to(self.path)
        bad.append(XrayBinary(link, self.binary.sha256))
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
            for binary in bad:
                with self.subTest(binary=binary), self.assertRaises(XrayProbeError):
                    async with run_xray_batch(binary, self.endpoints):
                        self.fail("Invalid binaries must not execute")
            self.path.chmod(0o777)
            with self.assertRaisesRegex(XrayProbeError, "^core_binary$"):
                async with run_xray_batch(self.binary, self.endpoints):
                    self.fail("Writable binaries must not execute")
            spawn.assert_not_called()
