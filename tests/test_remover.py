import asyncio
import io

import numpy as np
import pytest
from PIL import Image

from bgremover.matting import InvalidImage, Remover, RemoveOptions


class CircleModel:
    """Fake MaskModel: a soft-edged disc in the middle of the frame."""

    def predict(self, rgb):
        h, w = rgb.shape[:2]
        yy, xx = np.mgrid[0:h, 0:w]
        d = np.hypot(yy - h / 2, xx - w / 2)
        return np.clip((min(h, w) / 4 - d) / 3 + 0.5, 0, 1).astype(np.float32)


def jpeg(w=200, h=100, color=(200, 30, 30)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "JPEG")
    return buf.getvalue()


def run(data, **opts):
    return asyncio.run(Remover(CircleModel()).remove(data, RemoveOptions(**opts)))


def test_png_has_alpha_from_model():
    out = run(jpeg())
    img = Image.open(io.BytesIO(out.content))
    assert out.media_type == "image/png" and img.mode == "RGBA" and img.size == (200, 100)
    assert img.getpixel((100, 50))[3] == 255
    assert img.getpixel((0, 0))[3] == 0


def test_mask_output_is_grayscale():
    img = Image.open(io.BytesIO(run(jpeg(), format="mask").content))
    assert img.mode == "L" and img.getpixel((100, 50)) == 255


def test_background_colour_is_composited():
    img = Image.open(io.BytesIO(run(jpeg(), background="#00ff00").content))
    assert img.mode == "RGB" and img.getpixel((0, 0)) == (0, 255, 0)


def test_jpg_defaults_to_white_background():
    out = run(jpeg(), format="jpg")
    img = Image.open(io.BytesIO(out.content))
    assert out.media_type == "image/jpeg" and all(c > 245 for c in img.getpixel((0, 0)))


def test_crop_trims_to_subject():
    out = run(jpeg(), crop=True)
    assert out.width < 200 and out.height < 100


def test_exif_rotation_is_applied():
    buf = io.BytesIO()
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° CW on display
    Image.new("RGB", (200, 100)).save(buf, "JPEG", exif=exif)
    assert (run(buf.getvalue()).width, run(buf.getvalue()).height) == (100, 200)


def test_rejects_garbage_and_oversized():
    with pytest.raises(InvalidImage):
        run(b"not an image")
    with pytest.raises(InvalidImage, match="megapixels"):
        asyncio.run(Remover(CircleModel(), max_pixels=10_000).remove(jpeg()))


def test_rejects_bad_options():
    with pytest.raises(InvalidImage):
        RemoveOptions(format="gif")
    with pytest.raises(InvalidImage):
        RemoveOptions(background="red")
