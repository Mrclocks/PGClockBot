import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

from app.bot.handlers import plan_catalog_manage as pcm
from app.bot.handlers import reseller_plans as rp


def _state(data: dict) -> MagicMock:
    state = MagicMock()
    state.get_data = AsyncMock(return_value=data)
    state.set_state = AsyncMock()
    state.clear = AsyncMock()
    return state


def _message(text: str) -> MagicMock:
    message = MagicMock()
    message.text = text
    message.answer = AsyncMock()
    return message


class CategoryDescriptionBotTests(unittest.IsolatedAsyncioTestCase):
    async def _save(self, text: str) -> AsyncMock:
        update = AsyncMock()
        with patch.object(pcm, "resolve_catalog_staff", AsyncMock(return_value={"role": "admin"})), \
             patch.object(pcm, "_pcm_ui", AsyncMock(return_value={})), \
             patch.object(pcm, "finish_text_step", AsyncMock()), \
             patch("app.services.plan_categories.update_category", update), \
             patch("app.services.plan_categories.list_categories", AsyncMock(return_value=[])):
            await pcm.pcm_cat_desc_save(
                _message(text), _state({"_pcm_edit_cat_id": 7}), MagicMock(), MagicMock()
            )
        return update

    async def test_text_is_saved_as_category_description(self) -> None:
        update = await self._save("پلن‌های ماهانه")
        self.assertEqual(update.await_args.kwargs["description"], "پلن‌های ماهانه")

    async def test_dash_clears_category_description(self) -> None:
        update = await self._save("-")
        self.assertEqual(update.await_args.kwargs["description"], "")


class ResellerPlanDescriptionBotTests(unittest.IsolatedAsyncioTestCase):
    async def _save(self, plan: NS, owner_id: int, text: str) -> MagicMock:
        session = MagicMock()
        session.get = AsyncMock(return_value=plan)
        session.commit = AsyncMock()
        message = _message(text)
        with patch.object(rp, "_actor", AsyncMock(return_value=(owner_id, NS()))), \
             patch.object(rp, "has_bot_perm", return_value=True), \
             patch.object(rp, "_plan_text_for", AsyncMock(return_value="x")), \
             patch.object(rp, "_plan_item_kb", return_value=None):
            await rp.res_plan_description_save(
                message, _state({"description_plan_id": 3}), session, MagicMock()
            )
        return session

    async def test_owner_can_set_and_clear_description(self) -> None:
        plan = NS(id=3, owner_reseller_id=5, description=None)
        await self._save(plan, 5, "یک ماهه")
        self.assertEqual(plan.description, "یک ماهه")
        await self._save(plan, 5, "-")
        self.assertIsNone(plan.description)

    async def test_other_shops_plan_is_not_changed(self) -> None:
        plan = NS(id=3, owner_reseller_id=99, description="keep")
        session = await self._save(plan, 5, "hack")
        self.assertEqual(plan.description, "keep")
        session.commit.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
