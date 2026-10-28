"""Signed, expiring keyset cursors bound to the complete integration query."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from itsdangerous import BadData, URLSafeTimedSerializer

from app.services.integration_policy import IntegrationError

PAGE_DEFAULT = 25
PAGE_MAX = 100
CURSOR_TTL = 15 * 60


@dataclass(frozen=True, slots=True)
class CursorBinding:
    integration_id: str
    shop_id: int | None
    customer_id: str | None
    kind: str
    filters: str = ""


@dataclass(frozen=True, slots=True)
class Keyset:
    last: int
    high: int


def page_limit(value: str | int = PAGE_DEFAULT) -> int:
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 3:
        value = int(value)
    if type(value) is not int or not 1 <= value <= PAGE_MAX:
        raise IntegrationError("invalid_request")
    return value


class IntegrationCursors:
    def __init__(self, secret: str) -> None:
        if not secret:
            raise IntegrationError("security_unavailable")
        self.signer = URLSafeTimedSerializer(secret, salt="pgclock-integration-cursor-v1")

    def decode(self, cursor: str, binding: CursorBinding) -> Keyset:
        try:
            if not isinstance(cursor, str) or len(cursor) > 1024:
                raise ValueError
            payload = self.signer.loads(cursor, max_age=CURSOR_TTL)
            if not isinstance(payload, dict) or set(payload) != {"binding", "last", "high"}:
                raise ValueError
            if payload["binding"] != asdict(binding):
                raise ValueError
            last, high = payload["last"], payload["high"]
            if type(last) is not int or type(high) is not int or not 0 < last <= high <= 2**31 - 1:
                raise ValueError
            return Keyset(last, high)
        except (BadData, ValueError, TypeError, KeyError):
            raise IntegrationError("invalid_cursor") from None

    def encode(self, binding: CursorBinding, key: Keyset) -> str:
        return self.signer.dumps({"binding": asdict(binding), "last": key.last, "high": key.high})
