"""Local text recognition with the OCR engine built into Windows (Windows.Media.Ocr), as PowerToys and Text-Grab do."""

import asyncio
import io

from PIL import Image

SMALL_HEIGHT = 60  # Windows OCR misses small text; upscale crops shorter than this


async def _recognize(image: Image.Image) -> str:
    from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter

    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:  # no OCR language pack installed for the user's languages
        return ""
    rgba = image.convert("RGBA")
    writer = DataWriter()
    writer.write_bytes(rgba.tobytes("raw", "BGRA"))
    bitmap = SoftwareBitmap.create_copy_from_buffer(
        writer.detach_buffer(), BitmapPixelFormat.BGRA8, rgba.width, rgba.height, BitmapAlphaMode.PREMULTIPLIED
    )
    result = await engine.recognize_async(bitmap)
    return "\n".join(line.text for line in result.lines)


def read_text(png: bytes) -> str:
    """Text found in a PNG, or "" (OCR is a best-effort hint; the vision model still sees the image)."""
    image = Image.open(io.BytesIO(png))
    if image.height < SMALL_HEIGHT:
        image = image.resize((image.width * 2, image.height * 2), Image.Resampling.LANCZOS)
    try:
        return asyncio.run(_recognize(image)).strip()
    except Exception:  # any WinRT/import failure: the vision model still gets the image, so degrade quietly
        return ""
