"""Safe string templating for operator/reseller-controlled message texts.

Python's ``str.format`` allows attribute / index traversal on substituted
values (e.g. ``{order_id.__class__.__mro__}``). Templates stored in settings
must never evaluate those expressions.

Only simple ``{name}`` placeholders are substituted. Unknown placeholders are
left unchanged. Attribute, item, and conversion syntax is never interpreted.
"""

from __future__ import annotations

import html
import re
from typing import Any, Mapping

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def safe_format(
    template: str | None,
    mapping: Mapping[str, Any] | None = None,
    *,
    escape_html: bool = False,
    **kwargs: Any,
) -> str:
    """Replace ``{key}`` placeholders from *mapping* / *kwargs* only.

    When *escape_html* is True, substituted dynamic values are HTML-escaped
    so user-controlled data cannot inject Telegram HTML. Static template markup
    from the operator is left intact.
    """
    text = template or ""
    if not text:
        return ""
    values: dict[str, Any] = {}
    if mapping:
        values.update(mapping)
    values.update(kwargs)

    def _repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            return match.group(0)
        val = values[key]
        if val is None:
            return ""
        out = str(val)
        return html.escape(out, quote=False) if escape_html else out

    return _PLACEHOLDER.sub(_repl, text)


def looks_like_format_injection(template: str | None) -> bool:
    """True when the template contains format attribute/index syntax."""
    if not template:
        return False
    # e.g. {foo.bar}, {foo[0]}, {foo!r}, {foo:spec} beyond simple names
    return bool(re.search(r"\{[^{}]*[.\[][^{}]*\}|\{[^{}]*![^{}]*\}|\{[^{}]*:[^{}]+\}", template))


def looks_like_format_injection(template: str | None) -> bool:
    """True when the template contains format attribute/index syntax."""
    if not template:
        return False
    # e.g. {foo.bar}, {foo[0]}, {foo!r}, {foo:spec} beyond simple names
    return bool(re.search(r"\{[^{}]*[.\[][^{}]*\}|\{[^{}]*![^{}]*\}|\{[^{}]*:[^{}]+\}", template))
