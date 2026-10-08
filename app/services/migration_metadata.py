"""Read remote Alembic revision metadata without executing migration code."""

from __future__ import annotations

import ast
import asyncio
import hashlib
from collections import OrderedDict
from urllib.parse import quote

import httpx

MAX_MIGRATION_FILES = 256
MAX_SOURCE_BYTES = 256 * 1024
MAX_PARALLEL_DOWNLOADS = 6
_REVISION_CACHE: OrderedDict[str, str] = OrderedDict()


def clear_revision_metadata_cache() -> None:
    _REVISION_CACHE.clear()


def parse_revision_assignment(source: str) -> str | None:
    """Read a top-level literal revision, including annotated assignments."""
    try:
        module = ast.parse(source)
    except SyntaxError:
        return None
    for node in module.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if any(isinstance(target, ast.Name) and target.id == "revision" for target in targets):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value.strip() or None
            return None
    return None


def _migration_blobs(payload: object) -> list[tuple[str, str]]:
    if not isinstance(payload, dict) or payload.get("truncated"):
        raise ValueError("Incomplete migration tree")
    tree = payload.get("tree")
    if not isinstance(tree, list):
        raise ValueError("Missing migration tree")
    blobs: list[tuple[str, str]] = []
    for item in tree:
        if not isinstance(item, dict) or item.get("type") != "blob":
            continue
        path = str(item.get("path") or "")
        if not path.startswith("alembic/versions/") or not path.endswith(".py"):
            continue
        name = path.removeprefix("alembic/versions/")
        if "/" in name or name.startswith("__"):
            continue
        sha = str(item.get("sha") or "")
        if len(sha) != 40 or any(char not in "0123456789abcdef" for char in sha):
            raise ValueError("Invalid migration blob id")
        blobs.append((path, sha))
    if not blobs or len(blobs) > MAX_MIGRATION_FILES:
        raise ValueError("Unexpected migration tree size")
    return blobs


async def _read_revision(
    client: httpx.AsyncClient, semaphore: asyncio.Semaphore, *, url: str, sha: str,
) -> str:
    async with semaphore:
        cached = _REVISION_CACHE.get(sha)
        if cached is not None:
            _REVISION_CACHE.move_to_end(sha)
            return cached
        response = await client.get(url)
        response.raise_for_status()
        content = response.content
        if len(content) > MAX_SOURCE_BYTES:
            raise ValueError("Migration source exceeds size limit")
        # A branch can move after the tree lookup; only accept the listed blob.
        blob = f"blob {len(content)}\0".encode() + content
        actual_sha = hashlib.sha1(blob, usedforsecurity=False).hexdigest()
        if actual_sha != sha:
            raise ValueError("Migration source differs from channel tree")
        revision = await asyncio.to_thread(parse_revision_assignment, response.text)
        if revision is None:
            raise ValueError("Migration has no literal revision metadata")
        _REVISION_CACHE[sha] = revision
        _REVISION_CACHE.move_to_end(sha)
        while len(_REVISION_CACHE) > MAX_MIGRATION_FILES:
            _REVISION_CACHE.popitem(last=False)
        return revision


async def fetch_revision_ids(
    client: httpx.AsyncClient, *, repository: str, channel: str, headers: dict[str, str],
) -> set[str]:
    url = f"https://api.github.com/repos/{repository}/git/trees/{channel}?recursive=1"
    response = await client.get(url, headers=headers)
    response.raise_for_status()
    blobs = _migration_blobs(response.json())
    semaphore = asyncio.Semaphore(MAX_PARALLEL_DOWNLOADS)
    results = await asyncio.gather(*(
        _read_revision(
            client, semaphore,
            url=f"https://raw.githubusercontent.com/{repository}/{channel}/{quote(path, safe='/')}",
            sha=sha,
        ) for path, sha in blobs
    ), return_exceptions=True)
    revisions: set[str] = set()
    for result in results:
        if isinstance(result, BaseException):
            raise ValueError("Could not read complete migration metadata") from result
        revisions.add(result)
    return revisions
