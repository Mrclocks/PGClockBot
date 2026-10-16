"""The platform Owner's bot lists and opens platform panel users only; a shop's customers stay in the web panel."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from app.services.bot_pg_user_authz import BotPgUserGate, list_scoped_pg_users, lookup_scoped_pg_user_by_username

OWNER_USER = {"id": 1, "username": "mine", "admin": {"username": "owner"}}
SHOP_USER = {"id": 2, "username": "theirs", "admin": {"username": "Shop_Admin"}}


def _gate() -> BotPgUserGate:
    client = AsyncMock()
    client.get_users.return_value = {"users": [OWNER_USER, SHOP_USER], "total": 2}
    client.get_user_by_username.side_effect = lambda name: {"mine": OWNER_USER, "theirs": SHOP_USER}.get(name)
    return BotPgUserGate(
        allowed=True, reason="ok", user_message="", staff={"role": "admin"}, pg_client=client,
        shop_pg_admins=frozenset({"shop_admin"}),
    )


class OwnerBotShopUsersTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_excludes_users_owned_by_a_shop_panel_admin(self) -> None:
        users, _ = await list_scoped_pg_users(_gate())
        self.assertEqual([u["id"] for u in users], [1])

    async def test_search_by_exact_username_does_not_reveal_a_shop_user(self) -> None:
        gate = _gate()
        self.assertIsNone(await lookup_scoped_pg_user_by_username(gate, "theirs"))
        self.assertEqual((await lookup_scoped_pg_user_by_username(gate, "mine"))["id"], 1)
