"""Cursor signatures bind the tenant, customer, filters and initial upper bound."""

from dataclasses import replace
from unittest.mock import patch

import pytest

from app.services.integration_pagination import CursorBinding, IntegrationCursors, Keyset, page_limit
from app.services.integration_policy import IntegrationError


@pytest.mark.parametrize("field,value", [("integration_id", "other"), ("shop_id", None), ("customer_id", "cus_other"),
                                         ("kind", "operations"), ("filters", "different")])
def test_cursor_cannot_move_between_boundaries(field, value):
    codec = IntegrationCursors("synthetic-secret")
    binding = CursorBinding("intg_synthetic", 11, "cus_first", "services")
    cursor = codec.encode(binding, Keyset(10, 15))
    assert codec.decode(cursor, binding) == Keyset(10, 15)
    with pytest.raises(IntegrationError):
        codec.decode(cursor, replace(binding, **{field: value}))


def test_cursor_expiry_tampering_and_missing_secret_fail_closed():
    codec = IntegrationCursors("synthetic-secret")
    binding = CursorBinding("intg_synthetic", 11, None, "plans")
    with patch("itsdangerous.timed.time.time", return_value=1000):
        cursor = codec.encode(binding, Keyset(10, 15))
    with patch("itsdangerous.timed.time.time", return_value=2000), pytest.raises(IntegrationError):
        codec.decode(cursor, binding)
    with pytest.raises(IntegrationError):
        codec.decode(cursor+"tampered", binding)
    with pytest.raises(IntegrationError):
        IntegrationCursors("")


@pytest.mark.parametrize("value", [True, False, 0, -1, 101, 1.0, "1.0", " 25", "۱", "1000", "1e2"])
def test_limits_reject_coercion_and_out_of_range(value):
    with pytest.raises(IntegrationError):
        page_limit(value)
