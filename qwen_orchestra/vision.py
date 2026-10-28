"""Картинки для vision-моделей (gemma3 и др.): проверка, сжатие, выбор модели.

Gemma3 всё равно масштабирует вход до 896×896, поэтому «слишком хорошие» картинки
(большая сторона > `MAX_SIDE` или файл > `RECOMPRESS_BYTES`) уменьшаем и пережимаем в JPEG:
меньше трафика, быстрее prefill, тот же результат для модели.
"""

from __future__ import annotations

import base64
import binascii
import io
from dataclasses import dataclass
from typing import Any

MAX_IMAGES = 4
MAX_INPUT_BYTES = 12 * 1024 * 1024  # исходник после декодирования base64
MAX_SIDE = 1280
RECOMPRESS_BYTES = 1_500_000
JPEG_QUALITY = 85
THUMB_SIDE = 160
ALLOWED_FORMATS = frozenset({"JPEG", "PNG", "WEBP", "GIF", "BMP"})

# Имена vision-моделей Ollama / OpenRouter (подстроки, нижний регистр)
_VISION_MARKERS = (
    "gemma3",
    "gemma-3",
    "llava",
    "bakllava",
    "minicpm-v",
    "llama3.2-vision",
    "llama-3.2-vision",
    "qwen2.5vl",
    "qwen2.5-vl",
    "qwen3-vl",
    "qwen3vl",
    "granite3.2-vision",
    "moondream",
    "pixtral",
)
# gemma3:270m / 1b — только текст
_TEXT_ONLY_GEMMA = ("gemma3:270m", "gemma3:1b", "gemma3:latest-1b")


class ImageError(ValueError):
    """Картинка не принята (формат, размер, битые данные)."""


@dataclass(frozen=True)
class PreparedImage:
    b64: str  # без data:-префикса — формат Ollama `images`
    mime: str
    width: int
    height: int
    original_bytes: int
    bytes: int
    recompressed: bool
    thumb: str  # data:image/jpeg;base64,… — превью для UI

    @property
    def data_url(self) -> str:
        return f"data:{self.mime};base64,{self.b64}"

    def info(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "original_bytes": self.original_bytes,
            "bytes": self.bytes,
            "recompressed": self.recompressed,
            "thumb": self.thumb,
        }


def is_vision_model(name: str) -> bool:
    n = (name or "").strip().lower()
    if not n or any(n.startswith(p) for p in _TEXT_ONLY_GEMMA):
        return False
    return any(m in n for m in _VISION_MARKERS)


def _decode_b64(raw: str) -> bytes:
    data = (raw or "").strip()
    if data.startswith("data:"):
        _, _, data = data.partition(",")
    try:
        return base64.b64decode(data, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ImageError("Некорректные данные картинки (base64)") from exc


def _flatten(img):  # noqa: ANN001, ANN202
    """RGBA/P → RGB на белом фоне (JPEG не хранит альфу)."""
    from PIL import Image

    if img.mode in {"RGBA", "LA"} or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.getchannel("A"))
        return bg
    return img.convert("RGB") if img.mode != "RGB" else img


def _jpeg(img, quality: int) -> bytes:  # noqa: ANN001
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
    return buf.getvalue()


def prepare_image(raw: bytes | str, *, name: str = "") -> PreparedImage:
    """Проверить и (при необходимости) сжать картинку. `raw` — байты или base64/data-URL."""
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError as exc:  # pragma: no cover
        raise ImageError("Для картинок нужен Pillow: pip install Pillow") from exc

    data = _decode_b64(raw) if isinstance(raw, str) else raw
    if not data:
        raise ImageError("Пустой файл")
    if len(data) > MAX_INPUT_BYTES:
        raise ImageError(
            f"Картинка слишком большая ({len(data) // 1024 // 1024} МБ > "
            f"{MAX_INPUT_BYTES // 1024 // 1024} МБ)"
        )
    try:
        img = Image.open(io.BytesIO(data))
        fmt = (img.format or "").upper()
        img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ImageError(f"Не удалось прочитать картинку {name or ''}".strip()) from exc
    if fmt not in ALLOWED_FORMATS:
        raise ImageError(f"Формат {fmt or '?'} не поддерживается (JPEG, PNG, WebP, GIF, BMP)")

    img = ImageOps.exif_transpose(img)  # поворот по EXIF, иначе фото «лежит на боку»
    w0, h0 = img.size
    too_big_side = max(w0, h0) > MAX_SIDE
    too_heavy = len(data) > RECOMPRESS_BYTES
    # JPEG/PNG небольшого размера отдаём как есть; остальное (WebP/GIF/BMP) — в JPEG.
    keep_as_is = fmt in {"JPEG", "PNG"} and not too_big_side and not too_heavy

    if keep_as_is:
        out, mime, recompressed = data, f"image/{fmt.lower()}", False
        w, h = w0, h0
    else:
        if too_big_side:
            img.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
        flat = _flatten(img)
        out = _jpeg(flat, JPEG_QUALITY)
        mime, recompressed = "image/jpeg", True
        w, h = flat.size
        # пережатие не должно раздувать файл (мелкий PNG-скриншот → тяжёлый JPEG)
        if len(out) >= len(data) and fmt in {"JPEG", "PNG"} and not too_big_side:
            out, mime, recompressed, (w, h) = data, f"image/{fmt.lower()}", False, (w0, h0)

    thumb_img = _flatten(img.copy())
    thumb_img.thumbnail((THUMB_SIDE, THUMB_SIDE), Image.Resampling.LANCZOS)
    thumb = "data:image/jpeg;base64," + base64.b64encode(_jpeg(thumb_img, 70)).decode("ascii")

    return PreparedImage(
        b64=base64.b64encode(out).decode("ascii"),
        mime=mime,
        width=w,
        height=h,
        original_bytes=len(data),
        bytes=len(out),
        recompressed=recompressed,
        thumb=thumb,
    )


def prepare_images(items: list[bytes | str], *, names: list[str] | None = None) -> list[PreparedImage]:
    if len(items) > MAX_IMAGES:
        raise ImageError(f"Не больше {MAX_IMAGES} картинок за раз")
    names = names or []
    return [
        prepare_image(it, name=names[i] if i < len(names) else "")
        for i, it in enumerate(items)
    ]
