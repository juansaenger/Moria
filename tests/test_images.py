from __future__ import annotations

import base64

import pytest

from homebot.agent import user_content
from homebot.discord_bot import MAX_IMAGE_BYTES, MAX_IMAGES, read_images


class FakeAttachment:
    """Stands in for discord.Attachment."""

    def __init__(self, filename: str, content_type: str | None, size: int, data: bytes = b"x", fail: bool = False):
        self.filename = filename
        self.content_type = content_type
        self.size = size
        self._data = data
        self._fail = fail

    async def read(self) -> bytes:
        if self._fail:
            raise RuntimeError("download exploded")
        return self._data


def png(name: str = "shot.png", size: int = 1000, data: bytes = b"PNGDATA") -> FakeAttachment:
    return FakeAttachment(name, "image/png", size, data)


# ---- what gets turned into image blocks


@pytest.mark.asyncio
async def test_supported_images_are_read():
    images, skipped = await read_images([png(data=b"abc")])
    assert images == [("image/png", b"abc")]
    assert skipped == []


@pytest.mark.asyncio
async def test_every_type_claude_accepts_is_allowed():
    atts = [
        FakeAttachment("a.jpg", "image/jpeg", 10),
        FakeAttachment("b.png", "image/png", 10),
        FakeAttachment("c.gif", "image/gif", 10),
        FakeAttachment("d.webp", "image/webp", 10),
    ]
    images, skipped = await read_images(atts)
    assert [m for m, _ in images] == ["image/jpeg", "image/png", "image/gif", "image/webp"]
    assert skipped == []


@pytest.mark.asyncio
async def test_a_charset_suffix_does_not_defeat_the_type_check():
    images, _ = await read_images([FakeAttachment("a.png", "image/png; charset=binary", 10)])
    assert [m for m, _ in images] == ["image/png"]


@pytest.mark.asyncio
async def test_heic_is_skipped_by_name_so_the_user_learns_why():
    """iPhone photos arrive as HEIC, which Claude cannot read."""
    images, skipped = await read_images([FakeAttachment("IMG_0001.HEIC", "image/heic", 10)])
    assert images == []
    assert skipped == ["IMG_0001.HEIC (not a supported image)"]


@pytest.mark.asyncio
async def test_an_attachment_with_no_content_type_is_skipped_not_crashed():
    images, skipped = await read_images([FakeAttachment("mystery", None, 10)])
    assert images == [] and "mystery" in skipped[0]


@pytest.mark.asyncio
async def test_oversized_images_are_skipped():
    images, skipped = await read_images([png(size=MAX_IMAGE_BYTES + 1)])
    assert images == []
    assert skipped == ["shot.png (over 5 MB)"]


@pytest.mark.asyncio
async def test_only_the_first_few_images_are_taken():
    atts = [png(f"{i}.png") for i in range(MAX_IMAGES + 2)]
    images, skipped = await read_images(atts)
    assert len(images) == MAX_IMAGES
    assert len(skipped) == 2 and "more than" in skipped[0]


@pytest.mark.asyncio
async def test_a_failed_download_is_reported_and_the_rest_still_arrive():
    atts = [FakeAttachment("broken.png", "image/png", 10, fail=True), png("good.png", data=b"ok")]
    images, skipped = await read_images(atts)
    assert images == [("image/png", b"ok")]
    assert skipped == ["broken.png (download failed)"]


@pytest.mark.asyncio
async def test_no_attachments_is_not_an_error():
    assert await read_images([]) == ([], [])


# ---- how the message is shaped for the model


def test_text_only_stays_a_plain_string():
    assert user_content("[Juan, now] hello", None) == "[Juan, now] hello"
    assert user_content("[Juan, now] hello", []) == "[Juan, now] hello"


def test_images_come_before_the_text():
    blocks = user_content("[Juan, now] what is this", [("image/png", b"abc")])
    assert isinstance(blocks, list)
    assert [b["type"] for b in blocks] == ["image", "text"]
    assert blocks[-1]["text"] == "[Juan, now] what is this"


def test_image_blocks_carry_base64_of_the_real_bytes():
    raw = b"\x89PNG\r\n\x1a\nnot really a png"
    blocks = user_content("hi", [("image/png", raw)])
    source = blocks[0]["source"]
    assert source["type"] == "base64"
    assert source["media_type"] == "image/png"
    assert base64.standard_b64decode(source["data"]) == raw


def test_several_images_keep_their_order_and_types():
    blocks = user_content("hi", [("image/png", b"one"), ("image/jpeg", b"two")])
    assert [b["source"]["media_type"] for b in blocks[:2]] == ["image/png", "image/jpeg"]
    assert base64.standard_b64decode(blocks[0]["source"]["data"]) == b"one"
