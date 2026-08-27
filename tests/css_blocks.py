"""Brace-matching helpers for CSS assertions.

The panel stylesheet is one large file, so `css.split("@media (max-width: 900px)")`
returns everything to EOF and `css.split(".side-backdrop {")` can land on a
same-named rule nested in another at-rule or buried in a multi-line selector
list. These helpers return exactly one block, so an assertion cannot silently
read the wrong one.
"""

from __future__ import annotations

import re

_WS = re.compile(r"\s+")
_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _norm(selector: str) -> str:
    return _WS.sub(" ", _COMMENT.sub(" ", selector)).strip()


def _body_from(css: str, open_at: int) -> str:
    scan = _blank_comments(css)
    depth = 0
    for i in range(open_at, len(scan)):
        if scan[i] == "{":
            depth += 1
        elif scan[i] == "}":
            depth -= 1
            if depth == 0:
                return css[open_at + 1 : i]
    raise AssertionError("unbalanced braces at offset %d" % open_at)


def _blank_comments(css: str) -> str:
    """Same-length copy with comment bodies blanked, so `{` inside a comment
    cannot unbalance the brace scan while offsets stay valid."""
    return _COMMENT.sub(lambda m: " " * len(m.group(0)), css)


def _preludes(css: str):
    """Yield (normalized prelude, offset of its `{`) for every block in `css`."""
    scan = _blank_comments(css)
    depth = 0
    start = 0
    i = 0
    while i < len(scan):
        ch = scan[i]
        if ch == "{":
            if depth == 0:
                yield _norm(css[start:i]), i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                start = i + 1
        elif ch == ";" and depth == 0:
            start = i + 1
        i += 1


def at_rule(css: str, prelude: str) -> str:
    """Body of the at-rule whose prelude equals `prelude` (e.g. a media query)."""
    return rule(css, prelude)


def rule(css: str, selector: str, within: str | None = None) -> str:
    """Body of the block whose full selector list equals `selector`.

    The selector list is compared after collapsing whitespace and stripping
    comments, so `.side` never matches `.side-backdrop`, and a selector that
    only appears as one item of a longer list is never mistaken for the rule.
    """
    haystack = css if within is None else within
    want = _norm(selector)
    for prelude, open_at in _preludes(haystack):
        if prelude == want:
            return _body_from(haystack, open_at)
    raise AssertionError("no block with selector %r" % selector)


def declarations(block: str) -> str:
    """`block` with comments removed, so prose cannot satisfy or trip an assert."""
    return _COMMENT.sub("", block)


def has_rule(css: str, selector: str, within: str | None = None) -> bool:
    try:
        rule(css, selector, within)
    except AssertionError:
        return False
    return True
