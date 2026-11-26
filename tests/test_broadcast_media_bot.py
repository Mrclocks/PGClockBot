"""Group messages with a photo or video in the bot, through the real dispatcher: owner and shop flows."""

from __future__ import annotations

from datetime import datetime, timezone

from aiogram.types import Animation, Chat, Document, Message, PhotoSize, Update

from app.bot.broadcast_media_input import GIF_NOT_SUPPORTED, TEXT_PROMPT
from app.bot.broadcast_segments import PLATFORM_PICK, SHOP_PICK
from app.bot.message_button_picker import QUESTION
from app.db.models import BotUser
from tests.chat_sim import Shown
# A module import, so pytest does not collect the reused flow classes a second time here.
from tests import test_broadcast_segments_bot as segments


async def send(sim, **content) -> None:
    """The person sends one message with ``content`` (photo, document, animation, caption)."""
    shown = Shown(next(sim.telegram.ids), False, "[media]")
    sim.telegram.messages[shown.message_id] = shown
    message = Message(
        message_id=shown.message_id, date=datetime.now(timezone.utc),
        chat=Chat(id=sim.telegram.chat_id, type="private"), from_user=sim.person, **content,
    ).as_(sim.bot)
    await sim._feed(Update(update_id=next(sim.update_ids), message=message))


def photo(n: int = 1) -> list[PhotoSize]:
    return [PhotoSize(file_id=f"p{n}", file_unique_id=f"u{n}", width=10, height=10)]


def image_file() -> Document:
    return Document(file_id="d1", file_unique_id="du1", mime_type="image/jpeg", file_name="a.jpg")


def gif() -> Animation:
    return Animation(file_id="g1", file_unique_id="gu1", width=10, height=10, duration=1)


class MediaFlowMixin:
    pick: str

    def last_texts(self, count: int = 2) -> str:
        return "\n".join(m.text for m in self.sim.telegram.visible()[-count:])

    async def open_text_step(self) -> None:
        await self.show("x", f"{self.pick}all")
        await self.sim.tap("x")
        self.assertIn(TEXT_PROMPT, self.last_texts())

    async def test_a_photo_with_a_caption_moves_on_to_the_buttons(self) -> None:
        await self.open_text_step()
        await send(self.sim, photo=photo(), caption="تخفیف ویژه")
        self.assertIn(QUESTION, self.last_texts())

    async def test_an_image_sent_as_a_file_is_explained_and_the_step_stays_open(self) -> None:
        await self.open_text_step()
        await send(self.sim, document=image_file())
        self.assertIn("به‌صورت «فایل» رسید", self.last_texts())
        await send(self.sim, photo=photo(2))
        self.assertIn(QUESTION, self.last_texts())

    async def test_a_gif_is_explained(self) -> None:
        await self.open_text_step()
        await send(self.sim, animation=gif(), document=Document(file_id="g1", file_unique_id="gu1", mime_type="video/mp4"))
        self.assertIn(GIF_NOT_SUPPORTED, self.last_texts())


class OwnerMediaTests(MediaFlowMixin, segments.OwnerFlowTests):
    pick = PLATFORM_PICK

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        async with self.sim.Session() as session:
            session.add(BotUser(id=2, telegram_id=6000, referral_code="c2", role="user"))
            await session.commit()


class ShopMediaTests(MediaFlowMixin, segments.ShopFlowTests):
    pick = SHOP_PICK


# Only the fixtures of the segment flow tests are reused; their own tests run in their module.
for _base in (segments.OwnerFlowTests, segments.ShopFlowTests):
    for _name in [n for n in vars(_base) if n.startswith("test_")]:
        for _cls in (OwnerMediaTests, ShopMediaTests):
            setattr(_cls, _name, None)
del _base, _cls, _name
