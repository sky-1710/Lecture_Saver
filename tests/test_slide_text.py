"""Reading slide text with the OCR built into Windows (§1, §32)."""

import sys
from pathlib import Path

import pytest

from lecture_saver.slides import slide_text


@pytest.fixture(autouse=True)
def _fresh_probe():
    slide_text.unavailable_reason.cache_clear()
    yield
    slide_text.unavailable_reason.cache_clear()


def test_off_windows_it_says_it_is_unavailable(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert "Windows" in slide_text.unavailable_reason()


def test_a_missing_binding_is_unavailable_not_a_crash(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winrt.windows.media.ocr", None)
    assert "not installed" in slide_text.unavailable_reason()


@pytest.mark.parametrize(
    ("installed", "chosen"),
    [
        (["ko"], ["ko"]),
        (["en-US", "ko"], ["en-US", "ko"]),
        (["ja", "en-GB"], ["en-GB"]),
        (["ja", "zh-Hans"], []),
    ],
)
def test_the_english_and_korean_recognisers_are_used(installed, chosen):
    assert slide_text._pick(installed) == chosen


def _render(path: Path, text: str) -> bool:
    from PIL import Image, ImageDraw, ImageFont

    font_file = Path(r"C:\Windows\Fonts\arial.ttf")
    if not font_file.is_file():
        return False
    image = Image.new("RGB", (1280, 720), "white")
    draw = ImageDraw.Draw(image)
    draw.text((60, 40), text, fill="black", font=ImageFont.truetype(str(font_file), 64))
    draw.text((60, 200), "a line of body text", fill="black",
              font=ImageFont.truetype(str(font_file), 30))
    image.save(path)
    return True


def test_a_real_slide_image_is_read(tmp_path):
    if slide_text.unavailable_reason():
        pytest.skip(slide_text.unavailable_reason())
    image = tmp_path / "001.jpg"
    if not _render(image, "Gradient Descent"):
        pytest.skip("no font to draw a test slide with")

    lines = slide_text.SlideTextReader().read(image)

    text = " ".join(line.text for line in lines)
    assert "Gradient" in text
    title = next(line for line in lines if "Gradient" in line.text)
    body = next(line for line in lines if "body" in line.text)
    # Heights are what tell a heading from body text.
    assert title.height > body.height > 0
