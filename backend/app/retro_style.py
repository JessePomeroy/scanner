"""Shared, dependency-light contract for a derived low-poly asset."""

from __future__ import annotations

from dataclasses import dataclass


TRIANGLE_BUDGETS = (500, 1000, 2000)
TEXTURE_SIZES = (128, 256)


@dataclass(frozen=True)
class RetroStyle:
    triangle_budget: int = 500
    texture_size: int = 256
    dither: bool = False

    def __post_init__(self) -> None:
        if type(self.triangle_budget) is not int or self.triangle_budget not in TRIANGLE_BUDGETS:
            raise ValueError("Retro triangle budget must be 500, 1000 or 2000")
        if type(self.texture_size) is not int or self.texture_size not in TEXTURE_SIZES:
            raise ValueError("Retro texture size must be 128 or 256")
        if type(self.dither) is not bool:
            raise ValueError("Retro dithering must be a boolean")


def quantize_linear_pixels(pixels, *, dither: bool = False):
    """Quantize a linear RGBA float buffer to 5-bit sRGB channels, preserving alpha.

    The caller supplies a float image, avoiding ambiguous byte-image color spaces.
    Ordered dithering is deterministic and affects only the new baked texture.
    """
    import numpy as np

    values = np.asarray(pixels, dtype=np.float32)
    if values.ndim != 3 or values.shape[2] != 4 or not np.isfinite(values).all():
        raise ValueError("Retro texture must be a finite height × width × RGBA buffer")
    result = np.clip(values, 0, 1).copy()
    linear = result[:, :, :3]
    srgb = np.where(linear <= .0031308, linear * 12.92, 1.055 * linear ** (1 / 2.4) - .055)
    if dither:
        bayer = np.array([[0, 8, 2, 10], [12, 4, 14, 6],
                          [3, 11, 1, 9], [15, 7, 13, 5]], dtype=np.float32)
        height, width = values.shape[:2]
        noise = ((bayer[np.arange(height)[:, None] % 4, np.arange(width) % 4] + .5) / 16 - .5) / 31
        srgb = srgb + noise[:, :, None]
    srgb = np.round(np.clip(srgb, 0, 1) * 31) / 31
    result[:, :, :3] = np.where(srgb <= .04045, srgb / 12.92, ((srgb + .055) / 1.055) ** 2.4)
    return result
