"""Canonical integration request digests with strict transport-independent keys."""

from __future__ import annotations

import hashlib
import json
import re

from app.services.integration_policy import IntegrationError

KEY_PATTERN = re.compile(r"[A-Za-z0-9._:-]{16,128}", re.ASCII)


def request_key_digest(value: str) -> str:
    if not isinstance(value, str) or KEY_PATTERN.fullmatch(value) is None:
        raise IntegrationError("idempotency_key_required")
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def canonical_digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode("ascii")).hexdigest()
