"""Shared helpers for irreversible delete reason fields."""

from __future__ import annotations

from typing import Any, Iterable, Mapping, MutableMapping
import logging

logger = logging.getLogger(__name__)


def _iter_form_values(form: Mapping[str, Any] | MutableMapping[str, Any], key: str) -> Iterable[Any]:
    """Yield every value for ``key`` — Starlette/FormData ``.get()`` is last-wins.

    Empty placeholders (``<input name=reason value="">``) plus a later filled
    field are fine, but the reverse order (filled then empty) made
    ``form.get("reason")`` return ``""`` and reject a valid delete reason.
    """
    getlist = getattr(form, "getlist", None)
    if callable(getlist):
        try:
            values = getlist(key)
        except Exception:
            values = None
        if values is not None:
            for raw in values:
                yield raw
            return
    multi_items = getattr(form, "multi_items", None)
    if callable(multi_items):
        try:
            for k, raw in multi_items():
                if k == key:
                    yield raw
            return
        except Exception:
            logger.debug("_iter_form_values: ignored failure", exc_info=True)
    raw = form.get(key)
    if raw is not None:
        yield raw


def extract_delete_reason(form: Mapping[str, Any] | MutableMapping[str, Any], *names: str) -> str:
    """Return the first non-empty trimmed reason from preferred form keys.

    Panel JS writes ``reason`` (or a custom ``data-confirm-reason-name``) and
    also mirrors ``confirm_reason``. Scan **all** values per key so a trailing
    empty ``reason=`` cannot shadow a confirmed value.
    """
    keys = names or ("reason", "confirm_reason")
    for key in keys:
        for raw in _iter_form_values(form, key):
            if raw is None:
                continue
            text = str(raw).strip()
            if text:
                return text
    return ""


def delete_reason_too_short(reason: str, *, minimum: int = 3) -> bool:
    return len((reason or "").strip()) < int(minimum)
