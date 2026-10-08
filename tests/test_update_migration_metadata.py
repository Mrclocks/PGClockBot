"""Migration preflight reads Alembic metadata from the selected channel."""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from sqlalchemy import create_engine, text

from app.services.update_channel import (
    clear_alembic_tree_cache,
    evaluate_migration_preflight,
    fetch_remote_alembic_revision_ids,
    migration_preflight,
    parse_revision_assignment,
)


def git_blob_sha(source: str) -> str:
    content = source.encode()
    return hashlib.sha1(
        f"blob {len(content)}\0".encode() + content, usedforsecurity=False,
    ).hexdigest()


class RevisionAssignmentTests(unittest.TestCase):
    def test_annotated_and_multiline_revision_is_read(self) -> None:
        self.assertEqual(
            parse_revision_assignment('revision: str = (\n    "0041_demo_users"\n)\n'),
            "0041_demo_users",
        )

    def test_metadata_is_read_without_running_migration_code(self) -> None:
        source = 'raise RuntimeError("must not run")\nrevision = "0040_contacts_cancel_notices"\n'
        self.assertEqual(parse_revision_assignment(source), "0040_contacts_cancel_notices")

    def test_comment_and_function_assignment_are_not_metadata(self) -> None:
        source = '# revision = "fake"\ndef upgrade():\n    revision = "fake"\n'
        self.assertIsNone(parse_revision_assignment(source))

    def test_invalid_or_computed_metadata_is_unknown(self) -> None:
        for source in ('revision = prefix + "0040"', 'revision = 40', 'revision = ""', 'revision = ('):
            with self.subTest(source=source):
                self.assertIsNone(parse_revision_assignment(source))


class RemoteMigrationMetadataTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        clear_alembic_tree_cache()
        self.calls: list[str] = []
        self.sources = {
            "alembic/versions/0040_purchase_contacts_cancellation_notices.py":
                'revision = "0040_contacts_cancel_notices"\n',
            "alembic/versions/0041_demo_users.py": 'revision: str = "0041_demo_users"\n',
        }
        self.unavailable = False
        self.truncated = False
        self.source_status = 200
        self.changed_source = False
        self.real_client = httpx.AsyncClient

    def tearDown(self) -> None:
        clear_alembic_tree_cache()

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        if self.unavailable:
            raise httpx.ConnectError("GitHub unavailable", request=request)
        if "api.github.com" in request.url.host:
            return httpx.Response(200, json={
                "truncated": self.truncated,
                "tree": [
                    {"path": path, "type": "blob", "sha": git_blob_sha(source)}
                    for path, source in self.sources.items()
                ] + [{"path": "alembic/versions/__init__.py", "type": "blob", "sha": "0" * 40}],
            })
        source = next((value for path, value in self.sources.items() if request.url.path.endswith(path)), None)
        if source is None:
            return httpx.Response(404)
        if self.changed_source:
            source += "# branch changed while reading\n"
        return httpx.Response(self.source_status, text=source)

    async def fetch(self, channel: str = "dev", *, force: bool = False) -> set[str] | None:
        transport = httpx.MockTransport(self.respond)
        with patch(
            "app.services.update_channel.httpx.AsyncClient",
            side_effect=lambda **kwargs: self.real_client(transport=transport, **kwargs),
        ):
            return await fetch_remote_alembic_revision_ids(channel, force=force)

    async def test_previous_database_revision_is_allowed_despite_different_filename(self) -> None:
        revisions = await self.fetch()
        self.assertEqual(revisions, {"0040_contacts_cancel_notices", "0041_demo_users"})
        result = evaluate_migration_preflight(
            db_revision="0040_contacts_cancel_notices", remote_revision_ids=revisions,
        )
        self.assertTrue(result["checked"])
        self.assertFalse(result["blocked"])

    async def test_channel_without_database_revision_still_blocks_update(self) -> None:
        self.sources.pop("alembic/versions/0040_purchase_contacts_cancellation_notices.py")
        result = evaluate_migration_preflight(
            db_revision="0040_contacts_cancel_notices", remote_revision_ids=await self.fetch("main"),
        )
        self.assertTrue(result["blocked"])

    async def test_success_cache_avoids_repeated_downloads_and_returns_copy(self) -> None:
        revisions = await self.fetch()
        count = len(self.calls)
        revisions.clear()
        self.assertEqual(await self.fetch(), {"0040_contacts_cancel_notices", "0041_demo_users"})
        self.assertEqual(len(self.calls), count)

    async def test_force_refresh_reuses_only_unchanged_blob_metadata(self) -> None:
        await self.fetch()
        count = len(self.calls)
        await self.fetch(force=True)
        self.assertEqual(len(self.calls) - count, 1)
        self.sources["alembic/versions/0041_demo_users.py"] = 'revision = "0042_next"\n'
        count = len(self.calls)
        self.assertEqual(await self.fetch(force=True), {"0040_contacts_cancel_notices", "0042_next"})
        self.assertEqual(len(self.calls) - count, 2)

    async def test_download_concurrency_is_bounded(self) -> None:
        self.sources.update({
            f"alembic/versions/{number}_migration.py": f'revision = "{number}_migration"\n'
            for number in range(20)
        })
        active = 0
        peak = 0

        async def respond(request: httpx.Request) -> httpx.Response:
            nonlocal active, peak
            if request.url.host == "raw.githubusercontent.com":
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0)
                active -= 1
            return self.respond(request)

        transport = httpx.MockTransport(respond)
        with patch(
            "app.services.update_channel.httpx.AsyncClient",
            side_effect=lambda **kwargs: self.real_client(transport=transport, **kwargs),
        ):
            revisions = await fetch_remote_alembic_revision_ids("dev")
        self.assertEqual(len(revisions), len(self.sources))
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, 6)

    async def test_installed_database_revision_is_compared_with_remote_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_url = f"sqlite:///{Path(directory) / 'installed.db'}"
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
                connection.execute(text("INSERT INTO alembic_version VALUES (:revision)"), {
                    "revision": "0040_contacts_cancel_notices",
                })
            engine.dispose()
            transport = httpx.MockTransport(self.respond)
            with patch(
                "app.services.update_channel.httpx.AsyncClient",
                side_effect=lambda **kwargs: self.real_client(transport=transport, **kwargs),
            ), patch("app.config.get_settings", return_value=SimpleNamespace(database_url=database_url)):
                result = await migration_preflight("dev")
        self.assertTrue(result["checked"])
        self.assertFalse(result["blocked"])
        self.assertEqual(result["db_revision"], "0040_contacts_cancel_notices")
        self.assertEqual(result["channel"], "dev")

    async def test_failed_other_channel_does_not_reuse_dev_revisions(self) -> None:
        await self.fetch("dev")
        self.unavailable = True
        self.assertIsNone(await self.fetch("main"))
        count = len(self.calls)
        self.assertIsNone(await self.fetch("main"))
        self.assertEqual(len(self.calls), count)

    async def test_failed_refresh_can_reuse_same_channel_revisions(self) -> None:
        expected = await self.fetch()
        self.unavailable = True
        self.assertEqual(await self.fetch(force=True), expected)

    async def test_incomplete_tree_is_unknown_instead_of_reporting_missing_revision(self) -> None:
        self.truncated = True
        self.assertIsNone(await self.fetch())

    async def test_failed_migration_download_is_unknown(self) -> None:
        self.source_status = 404
        self.assertIsNone(await self.fetch())

    async def test_entire_check_has_a_timeout_even_with_many_migrations(self) -> None:
        async def respond(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(1)
            return self.respond(request)

        transport = httpx.MockTransport(respond)
        with patch(
            "app.services.update_channel.httpx.AsyncClient",
            side_effect=lambda **kwargs: self.real_client(transport=transport, **kwargs),
        ):
            self.assertIsNone(await fetch_remote_alembic_revision_ids("dev", timeout=0.01))
        self.assertFalse(self.calls)

    async def test_nonliteral_migration_metadata_is_unknown(self) -> None:
        self.sources["alembic/versions/0041_demo_users.py"] = 'revision = get_revision()\n'
        self.assertIsNone(await self.fetch())

    async def test_source_changed_since_tree_lookup_is_unknown(self) -> None:
        self.changed_source = True
        self.assertIsNone(await self.fetch())


if __name__ == "__main__":
    unittest.main()
