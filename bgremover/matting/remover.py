"""The Remover: image bytes in, cutout bytes out.

Everything between the HTTP layer and the neural net lives here: decoding,
EXIF orientation, size limits, alpha prediction, edge colour decontamination,
compositing and encoding. Callers only see `Remover.remove()`.
"""

from __future__ import annotations

import asyncio
import io
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Literal, Protocol

import cv2
import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from .foreground import estimate_foreground

Image.MAX_IMAGE_PIXELS = None  # we enforce our own limit below, with a clearer error

OutputFormat = Literal["png", "webp", "jpg", "mask"]
_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")


class MaskModel(Protocol):
    """Internal seam: predicts a soft alpha matte for an RGB image.

    `predict` takes an HxWx3 uint8 RGB array and returns an HxW float32 array in
    [0, 1] of the same size. It must be safe to call from several threads.
    """

    def predict(self, rgb: np.ndarray) -> np.ndarray: ...


class InvalidImage(ValueError):
    """The input could not be decoded, or breaks a size limit. The message is safe to show users."""


@dataclass(frozen=True)
class RemoveOptions:
    format: OutputFormat = "png"
    background: str | None = None  # "#rrggbb" to composite onto a solid colour
    crop: bool = False  # trim to the subject's bounding box
    refine_edges: bool = True  # decontaminate edge colours (removes background halos)

    def __post_init__(self) -> None:
        if self.format not in ("png", "webp", "jpg", "mask"):
            raise InvalidImage(f"unsupported format '{self.format}'")
        if self.background is not None and not _HEX.match(self.background):
            raise InvalidImage("background must be a hex colour like #ffffff")


@dataclass(frozen=True)
class Cutout:
    content: bytes
    media_type: str
    width: int
    height: int


_MEDIA = {"png": "image/png", "webp": "image/webp", "jpg": "image/jpeg", "mask": "image/png"}


class Remover:
    """Removes image backgrounds.

    `remove()` is async and safe to call concurrently; at most `workers` images are
    processed at once (CPU stages overlap, the model adapter serialises GPU work).
    Raises `InvalidImage` for undecodable or oversized input.
    """

    def __init__(self, model: MaskModel, *, max_pixels: int = 40_000_000, workers: int = 4):
        self._model = model
        self._max_pixels = max_pixels
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="remover")

    async def remove(self, data: bytes, options: RemoveOptions = RemoveOptions()) -> Cutout:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, self._remove, data, options)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _remove(self, data: bytes, options: RemoveOptions) -> Cutout:
        rgb = self._decode(data)
        alpha = np.clip(self._model.predict(rgb), 0.0, 1.0).astype(np.float32)

        if options.crop:
            rgb, alpha = _crop_to_subject(rgb, alpha)

        h, w = alpha.shape
        if options.format == "mask":
            content = _encode_png((alpha * 255 + 0.5).astype(np.uint8))
            return Cutout(content, _MEDIA["mask"], w, h)

        if options.refine_edges:
            fg = estimate_foreground(rgb.astype(np.float32) / 255.0, alpha)
        else:
            fg = rgb.astype(np.float32) / 255.0

        a = alpha[:, :, None]
        if options.background or options.format == "jpg":
            bg = np.array(_parse_hex(options.background or "#ffffff"), np.float32) / 255.0
            out = ((fg * a + bg * (1.0 - a)) * 255 + 0.5).astype(np.uint8)
        else:
            out = np.dstack([(fg * 255 + 0.5).astype(np.uint8), (alpha * 255 + 0.5).astype(np.uint8)])

        return Cutout(_encode(out, options.format), _MEDIA[options.format], w, h)

    def _decode(self, data: bytes) -> np.ndarray:
        if not data:
            raise InvalidImage("empty image")
        try:
            img = Image.open(io.BytesIO(data))
            if img.width * img.height > self._max_pixels:
                raise InvalidImage(
                    f"image is {img.width}x{img.height}; the limit is {self._max_pixels // 1_000_000} megapixels"
                )
            img = ImageOps.exif_transpose(img)
            if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
                # Flatten existing transparency onto white so the model sees a plausible photo.
                img = img.convert("RGBA")
                flat = Image.new("RGB", img.size, (255, 255, 255))
                flat.paste(img, mask=img.getchannel("A"))
                img = flat
            return np.asarray(img.convert("RGB"))
        except InvalidImage:
            raise
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise InvalidImage("could not decode image; send a JPEG, PNG, WebP, BMP, TIFF or GIF") from exc


def _parse_hex(color: str) -> tuple[int, int, int]:
    h = _HEX.match(color).group(1)  # type: ignore[union-attr]
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _crop_to_subject(rgb: np.ndarray, alpha: np.ndarray, threshold: float = 0.05):
    ys, xs = np.nonzero(alpha > threshold)
    if len(xs) == 0:
        return rgb, alpha
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    return rgb[y0:y1, x0:x1], alpha[y0:y1, x0:x1]


def _encode_png(arr: np.ndarray) -> bytes:
    if arr.ndim == 3:
        arr = cv2.cvtColor(arr, cv2.COLOR_RGBA2BGRA if arr.shape[2] == 4 else cv2.COLOR_RGB2BGR)
    # Level 3 is ~4x faster than PIL's default with only slightly larger files.
    ok, buf = cv2.imencode(".png", arr, [cv2.IMWRITE_PNG_COMPRESSION, 3])
    if not ok:
        raise RuntimeError("PNG encoding failed")
    return buf.tobytes()


def _encode(arr: np.ndarray, fmt: str) -> bytes:
    if fmt == "png":
        return _encode_png(arr)
    out = io.BytesIO()
    if fmt == "webp":
        Image.fromarray(arr).save(out, "WEBP", quality=92, method=4)
    else:
        Image.fromarray(arr[:, :, :3]).save(out, "JPEG", quality=92, optimize=True)
    return out.getvalue()
