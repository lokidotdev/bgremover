"""Foreground colour estimation (removes background colour bleeding at soft edges).

Blur-fusion estimator from Forte & Pitié, "Approximate Fast Foreground Colour
Estimation" (ICIP 2021). Box filters make it O(pixels) regardless of radius, so
it stays fast on large images.
"""

from __future__ import annotations

import cv2
import numpy as np


def estimate_foreground(image: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """image: HxWx3 float32 in [0,1]; alpha: HxW float32 in [0,1]. Returns HxWx3 float32 foreground."""
    a = alpha[:, :, None]
    fg, blurred_bg = _blur_fusion(image, image, image, a, 90)
    fg, _ = _blur_fusion(image, fg, blurred_bg, a, 6)
    return fg


def _blur_fusion(image, fg, bg, a, r):
    k = (r, r)
    blurred_a = cv2.blur(a, k)[:, :, None]
    blurred_fg = cv2.blur(fg * a, k) / (blurred_a + 1e-5)
    blurred_bg = cv2.blur(bg * (1 - a), k) / ((1 - blurred_a) + 1e-5)
    fg = blurred_fg + a * (image - a * blurred_fg - (1 - a) * blurred_bg)
    return np.clip(fg, 0.0, 1.0), blurred_bg
