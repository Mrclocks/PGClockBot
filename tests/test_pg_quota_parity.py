"""User limits and representative creation follow PasarGuard's own rules.

``pg_create_allowed`` transcribes PasarGuard's ``_enforce_user_limits`` for a manual
user create by a non-owner admin. The panel's pre-check must give the same answer for
every role limit and every input this panel can send; ``0`` is a set limit in
PasarGuard, only ``None`` is unset.
"""

from __future__ import annotations

import asyncio
import itertools
import time
import unittest
from unittest.mock import AsyncMock, patch

from app.services.pg_access import acl_from_admin_payload, enrich_staff_pg_from_role
from app.services.pg_quota import PgQuotaError, assert_can_create_user, assert_can_modify_user
from app.services.representative_unification import staff_can_manage_representatives

GB = 1024**3
DAY = 86400
STAFF = {"role": "reseller", "pg_admin_username": "shop"}


def pg_create_allowed(limits: dict, *, users: int, data_limit: int | None, expire_s: int | None,
                      hwid: int | None) -> bool:
    """PasarGuard ``_enforce_user_limits`` (create): ``hwid`` is the value PasarGuard receives."""
    def lim(key: str) -> int | None:
        return limits.get(key)

    if lim("max_users") is not None and users >= lim("max_users"):
        return False
    if lim("data_limit_max") is not None and (data_limit is None or data_limit <= 0):
        return False
    if data_limit is not None and data_limit > 0:
        if lim("data_limit_min") is not None and data_limit < lim("data_limit_min"):
            return False
        if lim("data_limit_max") is not None and data_limit > lim("data_limit_max"):
            return False
    if lim("expire_max") is not None and expire_s is None:
        return False
    if expire_s is not None:
        if lim("expire_min") is not None and expire_s < lim("expire_min"):
            return False
        if lim("expire_max") is not None and expire_s > lim("expire_max"):
            return False
    if lim("on_hold_timeout_max") is not None:
        return False  # this panel never sends on_hold_timeout
    if lim("max_hwid_per_user") is not None and (hwid is None or hwid <= 0):
        return False
    if hwid is not None:
        if lim("min_hwid_per_user") is not None and hwid < lim("min_hwid_per_user"):
            return False
        if lim("max_hwid_per_user") is not None and hwid > lim("max_hwid_per_user"):
            return False
    return True


def _role(limits: dict, **extra: object) -> dict:
    # HWID mode "disabled": PasarGuard applies no fallback, so the received value is final.
    return {"id": 5, "is_owner": False, "limits": limits, "hwid": {"mode": "disabled"}, **extra}


async def panel_create_allowed(limits: dict, *, users: int, data_limit: int | None, expire_s: int | None,
                               hwid: int | None, role_extra: dict | None = None) -> bool:
    admin = {"username": "shop", "status": "active", "total_users": users}
    role = _role(limits, **(role_extra or {}))
    expire_ts = int(time.time()) + expire_s if expire_s is not None else None
    with patch("app.services.pg_quota._load_admin_and_role", new=AsyncMock(return_value=(admin, role))):
        try:
            await assert_can_create_user(STAFF, data_limit=data_limit, expire_ts=expire_ts, hwid_limit=hwid)
        except PgQuotaError:
            return False
    return True


LIMIT_VALUES = {
    "max_users": (None, 0, 3, 10),
    "data_limit_min": (None, 0, 10 * GB),
    "data_limit_max": (None, 0, 20 * GB),
    "expire_min": (None, 0, 10 * DAY),
    "expire_max": (None, 0, 40 * DAY),
    "min_hwid_per_user": (None, 0, 2),
    "max_hwid_per_user": (None, 0, 3),
    "on_hold_timeout_max": (None, 7 * DAY),
}
INPUTS = list(itertools.product(
    (0, 5),                              # users already owned
    (None, 0, 5 * GB, 15 * GB, 50 * GB),  # data_limit
    (None, 5 * DAY, 30 * DAY, 90 * DAY),  # seconds until expiry
    (None, 1, 2, 5),                      # HWID value sent (the builder never sends 0)
))


class CreateLimitParityTests(unittest.TestCase):
    def test_each_limit_matches_pasarguard_for_every_input(self):
        for key, values in LIMIT_VALUES.items():
            for value, (users, data_limit, expire_s, hwid) in itertools.product(values, INPUTS):
                limits = {key: value}
                expected = pg_create_allowed(limits, users=users, data_limit=data_limit, expire_s=expire_s, hwid=hwid)
                got = asyncio.run(panel_create_allowed(
                    limits, users=users, data_limit=data_limit, expire_s=expire_s, hwid=hwid))
                with self.subTest(limit=key, value=value, users=users, data=data_limit, expire=expire_s, hwid=hwid):
                    self.assertEqual(got, expected)

    def test_combined_role_limits_match_pasarguard(self):
        limits = {"max_users": 10, "data_limit_min": 10 * GB, "data_limit_max": 20 * GB,
                  "expire_min": 10 * DAY, "expire_max": 40 * DAY, "min_hwid_per_user": 2, "max_hwid_per_user": 3}
        for users, data_limit, expire_s, hwid in INPUTS:
            expected = pg_create_allowed(limits, users=users, data_limit=data_limit, expire_s=expire_s, hwid=hwid)
            got = asyncio.run(panel_create_allowed(
                limits, users=users, data_limit=data_limit, expire_s=expire_s, hwid=hwid))
            with self.subTest(users=users, data=data_limit, expire=expire_s, hwid=hwid):
                self.assertEqual(got, expected)

    def test_owner_role_skips_every_limit(self):
        admin = {"username": "shop", "status": "active", "total_users": 99}
        role = {"is_owner": True, "limits": {"max_users": 1, "data_limit_max": GB}}
        with patch("app.services.pg_quota._load_admin_and_role", new=AsyncMock(return_value=(admin, role))):
            asyncio.run(assert_can_create_user(STAFF, data_limit=None, expire_ts=None))

    def test_template_only_role_refuses_manual_create_and_edit(self):
        admin = {"username": "shop", "status": "active", "total_users": 0}
        role = _role({}, access={"require_template": True})
        with patch("app.services.pg_quota._load_admin_and_role", new=AsyncMock(return_value=(admin, role))):
            with self.assertRaises(PgQuotaError):
                asyncio.run(assert_can_create_user(STAFF, data_limit=GB, expire_ts=None))
            with self.assertRaises(PgQuotaError):
                asyncio.run(assert_can_modify_user(STAFF, data_limit=GB, expire_ts=None))
            asyncio.run(assert_can_create_user(STAFF, from_template=True))

    def test_move_to_on_hold_needs_no_timeout_limit(self):
        admin = {"username": "shop", "status": "active", "total_users": 0}
        with patch("app.services.pg_quota._load_admin_and_role",
                   new=AsyncMock(return_value=(admin, _role({"on_hold_timeout_max": DAY})))):
            with self.assertRaises(PgQuotaError):
                asyncio.run(assert_can_modify_user(STAFF, data_limit=GB, expire_ts=None,
                                                   expire_changed=False, to_on_hold=True))
            asyncio.run(assert_can_modify_user(STAFF, data_limit=GB, expire_ts=None, expire_changed=False))

    def test_hwid_fallback_from_role_override_is_checked(self):
        admin = {"username": "shop", "status": "active", "total_users": 0}
        role = {"is_owner": False, "limits": {"max_hwid_per_user": 3},
                "hwid": {"mode": "override", "fallback_limit": 5}}
        with patch("app.services.pg_quota._load_admin_and_role", new=AsyncMock(return_value=(admin, role))):
            with self.assertRaises(PgQuotaError):
                asyncio.run(assert_can_create_user(STAFF, data_limit=None, expire_ts=None, hwid_limit=None))


class RepresentativeCreationTests(unittest.TestCase):
    def _level1(self, admins: dict, *, status: str = "active") -> dict:
        admin = {"username": "l1", "status": status,
                 "role": {"id": 7, "is_owner": False, "permissions": {"admins": admins, "users": {"read": True}}}}
        features, role, _owner = acl_from_admin_payload(admin)
        staff = {"role": "reseller", "org_principal_id": 11, "org_depth": 1, "org_parent_id": 1,
                 "org_status": "active"}
        return enrich_staff_pg_from_role(staff, features, role)

    def test_level1_with_admins_create_may_add_representatives(self):
        self.assertTrue(staff_can_manage_representatives(self._level1({"create": True})))

    def test_level1_without_admins_create_may_not(self):
        self.assertFalse(staff_can_manage_representatives(self._level1({"read": True})))

    def test_limited_level1_may_not_add_representatives(self):
        self.assertFalse(staff_can_manage_representatives(self._level1({"create": True}, status="limited")))


if __name__ == "__main__":
    unittest.main()
