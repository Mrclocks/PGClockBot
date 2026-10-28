"""Critical: resellers/pg_staff must never keep users under the panel owner.

PasarGuard enforces role quotas only for the authenticated admin. This panel
always calls the API as sudo/owner then reassigns ownership — so any path that
skips set_owner (or skips quota when PG link is missing) is a bypass.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.db.models import OrderStatus
from app.services.pg_quota import PgQuotaError, assert_can_create_user, assert_reseller_can_deliver


class FailClosedWithoutPgLinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_user_fails_without_pg_username(self):
        with self.assertRaises(PgQuotaError) as ctx:
            await assert_can_create_user(
                {"role": "reseller"},
                data_limit=1024**3,
                expire_ts=None,
            )
        self.assertIn("تنظیم نشده", ctx.exception.message)

    async def test_pg_staff_create_fails_without_pg_username(self):
        with self.assertRaises(PgQuotaError):
            await assert_can_create_user(
                {"role": "pg_staff", "pg_admin_username": "  "},
                from_template=True,
            )

    async def test_reseller_deliver_fails_without_pg_link(self):
        with self.assertRaises(PgQuotaError) as ctx:
            await assert_reseller_can_deliver(pg_admin_username=None)
        self.assertIn("تحویل", ctx.exception.message)


class DeliverOrderOwnerAssignTests(unittest.IsolatedAsyncioTestCase):
    @patch("app.services.orders.optional_legacy_customer", new_callable=AsyncMock, return_value=17)
    async def test_set_owner_failure_deletes_pg_user(self, adapter: AsyncMock) -> None:
        """Legacy owner-token path: set_owner failure must roll back the PG user."""
        from app.services.orders import deliver_order

        plan = SimpleNamespace(
            id=1,
            data_limit_gb=10,
            duration_days=30,
            pg_template_id=5,
            pg_group_ids=None,
        )
        order = SimpleNamespace(
            id=99,
            status=OrderStatus.PAID.value,
            service_id=None,
            user_id=7,
            reseller_id=42,
            amount=1000,
            discount_code=None,
            note=None,
            plan=plan,
            user=None,
        )

        pg = AsyncMock()
        # Legacy owner client has no _login_username → set_owner path is used
        pg._login_username = None
        pg.create_user_from_template = AsyncMock(
            return_value={"id": 555, "username": "clk_x", "subscription_url": "https://x/sub"}
        )
        pg.set_owner_by_id = AsyncMock(side_effect=RuntimeError("owner denied"))
        pg.delete_user_by_id = AsyncMock()

        session = AsyncMock()
        session.commit = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),  # prior-delivery dedup check
                MagicMock(rowcount=1),  # atomic DELIVERING claim
                MagicMock(scalar_one=MagicMock(return_value=order)),  # reload
                MagicMock(scalar_one_or_none=MagicMock(return_value=SimpleNamespace(
                    pg_admin_username="res_admin",
                    pg_admin_password_enc=None,
                    user_id=42,
                ))),
                MagicMock(rowcount=1),  # release claim on failure
            ]
        )

        with (
            patch("app.services.orders.get_pg", return_value=pg),
            patch(
                "app.services.pasarguard.get_pg_for_reseller",
                new=AsyncMock(return_value=pg),
            ),
            patch("app.services.orders.generate_pg_username", new=AsyncMock(return_value="clk_x")),
            patch(
                "app.services.orders._reseller_pg_link",
                new=AsyncMock(return_value=("res_admin", 3)),
            ),
            patch(
                "app.services.orders.assert_provision_create",
                new=AsyncMock(),
            ),
        ):
            with self.assertRaises((ValueError, RuntimeError)):
                await deliver_order(session, order)

        adapter.assert_awaited_once_with(session, order.user_id)
        pg.set_owner_by_id.assert_awaited_once_with(555, "res_admin")
        pg.delete_user_by_id.assert_awaited_once_with(555)
        self.assertGreaterEqual(session.commit.await_count, 1)

    @patch("app.services.orders.optional_legacy_customer", new_callable=AsyncMock, return_value=17)
    async def test_success_assigns_owner_before_commit(self, adapter: AsyncMock) -> None:
        """Shop path authenticates as reseller — ownership is implicit (no set_owner)."""
        from app.services.orders import deliver_order

        plan = SimpleNamespace(
            id=1,
            data_limit_gb=10,
            duration_days=30,
            pg_template_id=5,
            pg_group_ids=None,
        )
        order = SimpleNamespace(
            id=99,
            status=OrderStatus.PAID.value,
            service_id=None,
            user_id=7,
            reseller_id=42,
            amount=1000,
            discount_code=None,
            note=None,
            plan=plan,
            user=None,
        )
        profile = SimpleNamespace(
            pg_admin_username="res_admin",
            pg_admin_password_enc="enc",
            user_id=42,
        )

        pg = AsyncMock()
        # Reseller-authenticated client — skip set_owner transfer
        pg._login_username = "res_admin"
        pg.create_user_from_template = AsyncMock(
            return_value={"id": 555, "username": "clk_x", "subscription_url": "https://x/sub"}
        )
        pg.set_owner_by_id = AsyncMock(return_value={})
        pg.delete_user_by_id = AsyncMock()

        session = AsyncMock()
        session.add = MagicMock()
        session.flush = AsyncMock()
        session.commit = AsyncMock()
        session.refresh = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),  # prior-delivery dedup check
                MagicMock(rowcount=1),  # atomic DELIVERING claim
                MagicMock(scalar_one=MagicMock(return_value=order)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=profile)),
            ]
        )

        with (
            patch("app.services.orders.get_pg", return_value=pg),
            patch(
                "app.services.pasarguard.get_pg_for_reseller",
                new=AsyncMock(return_value=pg),
            ),
            patch("app.services.orders.generate_pg_username", new=AsyncMock(return_value="clk_x")),
            patch(
                "app.services.orders._reseller_pg_link",
                new=AsyncMock(return_value=("res_admin", 3)),
            ),
            patch(
                "app.services.orders.assert_provision_create",
                new=AsyncMock(),
            ),
            # NOTE: UserService itself must stay the real ORM class here — the
            # delivery path now runs a `select(UserService)` idempotency check
            # before creating a new service record, and SQLAlchemy's select()
            # rejects a Mock/lambda in place of a real mapped class.
            patch("app.services.orders.extract_sub_token", return_value="tok"),
        ):
            result = await deliver_order(session, order)

        adapter.assert_awaited_once_with(session, order.user_id)
        pg.set_owner_by_id.assert_not_awaited()
        pg.delete_user_by_id.assert_not_awaited()
        self.assertEqual(result.status, OrderStatus.DELIVERED.value)
        # No commission credit — delivery only touches order/claim executes
        self.assertTrue(session.execute.await_count >= 3)


class SourceWiringGuards(unittest.TestCase):
    def test_orders_no_longer_swallows_set_owner(self):
        src = Path("app/services/orders.py").read_text(encoding="utf-8")
        # Old bypass: set_owner failure was ignored with bare pass
        self.assertNotIn(
            "await pg.set_owner_by_id(service.pg_user_id, profile.pg_admin_username)\n"
            "            except Exception:\n                pass",
            src,
        )
        self.assertIn("مالکیت قابل تنظیم نیست", src)
        self.assertIn("delete_user_by_id", src)
        # Reseller path must always call provision gate (quota + billing)
        self.assertIn("if order.reseller_id:", src)
        self.assertIn("assert_provision_create", src)
        self.assertIn("get_pg_for_reseller", src)

    def test_web_create_requires_pg_owner(self):
        src = Path("app/api/pg_pages.py").read_text(encoding="utf-8")
        self.assertIn("ادمین پاسارگارد برای این حساب تنظیم نشده است", src)
        # hosts mutations gated for limited admins; nodes gated by role action
        hosts_idx = src.find("async def pg_hosts_create")
        nodes_idx = src.find("async def pg_node_reconnect")
        self.assertGreater(hosts_idx, 0)
        self.assertGreater(nodes_idx, 0)
        self.assertIn("assert_can_mutate_owned_users", src[hosts_idx : hosts_idx + 800])
        self.assertIn('staff_pg_action(staff, "nodes", "reconnect")', src[nodes_idx : nodes_idx + 700])
        # Node reconnect uses role ACL + staff PG client (not owned-users mutate gate)
        self.assertIn("_staff_pg", src[nodes_idx : nodes_idx + 700])

    def test_bot_pg_users_still_platform_admin_only(self):
        src = Path("app/bot/handlers/admin_pg_users.py").read_text(encoding="utf-8")
        self.assertIn("is_platform_admin as _is_admin", src)
        self.assertIn("if not _is_admin(db_user):", src)


if __name__ == "__main__":
    unittest.main()
