"""Template source with its includes inlined, for older tests that check template text."""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "web" / "templates"
_INCLUDE = re.compile(r"""\{%-?\s*include\s+["']([^"']+)["'][^%]*-?%\}""")


def template_with_includes(name: str) -> str:
    """``name`` with the source of every ``{% include "x" %}`` added right after the tag (recursively)."""
    seen: set[str] = set()

    def expand(template: str) -> str:
        if template in seen:
            return ""
        seen.add(template)
        text = (TEMPLATES / template).read_text(encoding="utf-8")
        return _INCLUDE.sub(lambda m: m.group(0) + expand(m.group(1)), text)

    return expand(name)
