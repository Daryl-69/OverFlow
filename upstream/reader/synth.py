"""Synthetic first-glass photos, for tests and offline demos.

This is a rendering model, not physics: turbid water is imitated by blurring
the checkerboard under the glass and veiling it with a light brown haze, both
growing with ``turbidity`` (an NTU-like knob). Real calibration must come from
photographs of real samples (README §7, hours 0–3).
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image

from ..card import spec as S
from ..card.render import render_card
from ..card.spec import DEFAULT_SPEC, CardSpec
from .color import linear_to_srgb, srgb_to_linear

VEIL_RGB = (196, 182, 156)


@dataclass
class PhotoParams:
    turbidity: float = 0.0          # NTU-like: 0 clear, 5 slightly hazy, 50 muddy
    chlorine: float | None = 0.5    # mg/L on the strip, None = no strip
    strip_noise: float = 2.0        # strip-to-strip colour variation (sRGB units, per channel)
    tilt: float = 0.18              # perspective strength
    cast: tuple[float, float, float] = (1.0, 1.0, 1.0)   # white-balance gains (R, G, B)
    exposure: float = 1.0
    noise: float = 2.5
    blur: float = 0.0               # Gaussian sigma in photo pixels (camera shake / focus)
    glare: bool = False
    width: int = 2400
    height: int = 1800
    taken_at: str | None = None     # "YYYY:MM:DD HH:MM:SS" written to EXIF


def strip_colour(mg: float, spec: CardSpec = DEFAULT_SPEC) -> np.ndarray:
    """Pad colour for a chlorine level: interpolate the chart in linear RGB."""
    lv = np.asarray(spec.chlorine_levels)
    lin = srgb_to_linear(np.asarray(spec.chlorine_rgb, dtype=float))
    out = [np.interp(mg, lv, lin[:, c]) for c in range(3)]
    return linear_to_srgb(np.asarray(out))


def _glass(card: np.ndarray, ppm: float, turbidity: float, rng: np.random.Generator) -> np.ndarray:
    img = card.astype(np.float32)
    gx, gy = S.GLASS_CENTER
    H, W = img.shape[:2]
    yy, xx = np.mgrid[0:H, 0:W]
    r_mm = np.hypot((xx + 0.5) / ppm - gx, (yy + 0.5) / ppm - gy)
    inside = r_mm <= S.GLASS_RADIUS * 0.97
    sigma = (0.12 + 0.012 * turbidity) * ppm          # in pixels at this ppm
    blurred = cv2.GaussianBlur(img, (0, 0), sigmaX=sigma)
    f = 0.04 + 0.9 * (1.0 - np.exp(-turbidity / 60.0))
    veil = np.asarray(VEIL_RGB, np.float32)
    water = (1 - f) * blurred + f * veil
    out = img.copy()
    out[inside] = water[inside]
    # the glass wall: a darker rim and a soft highlight
    rim = np.abs(r_mm - S.GLASS_RADIUS * 0.97) < 0.8
    out[rim] *= 0.75
    hl = np.exp(-(((xx / ppm - (gx - 14)) ** 2) / 18 + ((yy / ppm - (gy - 18)) ** 2) / 4))
    out += (hl * 40.0 * inside)[..., None]
    return np.clip(out, 0, 255)


def render_photo(household_id: str, p: PhotoParams, seed: int = 0, spec: CardSpec = DEFAULT_SPEC,
                 ppm: float = 8.0) -> bytes:
    """A JPEG of the card with a glass and a strip on it, photographed at an angle."""
    rng = np.random.default_rng(seed)
    card = render_card(household_id, ppm=ppm, spec=spec)
    img = _glass(card, ppm, p.turbidity, rng)
    if p.chlorine is not None:
        sl, pb = spec.slot, spec.pad_box
        y0 = sl.y + (sl.h - 5.0) / 2 + rng.uniform(-0.6, 0.6)
        x0 = pb.x + 1.5 + rng.uniform(-0.8, 0.8)
        img[int(y0 * ppm):int((y0 + 5) * ppm), int(x0 * ppm):int((x0 + 80) * ppm)] = (244, 244, 240)
        col = strip_colour(p.chlorine, spec) + rng.normal(0, p.strip_noise, 3)
        img[int(y0 * ppm):int((y0 + 5) * ppm), int(x0 * ppm):int((x0 + 6) * ppm)] = np.clip(col, 0, 255)
    Hc, Wc = img.shape[:2]
    # place the card in the frame with a random perspective
    W, H = p.width, p.height
    fill = rng.uniform(0.62, 0.8)
    cw = W * fill
    ch = cw * Hc / Wc
    cx, cy = W / 2 + rng.uniform(-0.05, 0.05) * W, H / 2 + rng.uniform(-0.05, 0.05) * H
    base = np.array([[cx - cw / 2, cy - ch / 2], [cx + cw / 2, cy - ch / 2],
                     [cx + cw / 2, cy + ch / 2], [cx - cw / 2, cy + ch / 2]])
    jitter = rng.uniform(-p.tilt, p.tilt, size=(4, 2)) * np.array([cw, ch]) * 0.35
    ang = rng.uniform(-0.25, 0.25)
    R = np.array([[np.cos(ang), -np.sin(ang)], [np.sin(ang), np.cos(ang)]])
    dst = ((base + jitter - [cx, cy]) @ R.T + [cx, cy]).astype(np.float32)
    src = np.array([[0, 0], [Wc, 0], [Wc, Hc], [0, Hc]], np.float32)
    Hm = cv2.getPerspectiveTransform(src, dst)
    table = np.empty((H, W, 3), np.float32)
    table[:] = rng.uniform(90, 150, 3)
    table += rng.normal(0, 6, (H, W, 1))
    photo = cv2.warpPerspective(img, Hm, (W, H), dst=table, borderMode=cv2.BORDER_TRANSPARENT,
                                flags=cv2.INTER_LINEAR)
    # light: gradient, colour cast, exposure
    gy_, gx_ = np.mgrid[0:H, 0:W]
    grad = 1.0 + 0.12 * ((gx_ / W - 0.5) * rng.uniform(-1, 1) + (gy_ / H - 0.5) * rng.uniform(-1, 1))
    lin = srgb_to_linear(photo) * grad[..., None] * np.asarray(p.cast) * p.exposure
    photo = linear_to_srgb(lin).astype(np.float32)
    if p.glare:
        gxp, gyp = dst[:, 0].mean() - 0.18 * cw, dst[:, 1].mean() + 0.02 * ch
        spot = np.exp(-(((gx_ - gxp) / (0.09 * cw)) ** 2 + ((gy_ - gyp) / (0.09 * cw)) ** 2))
        photo = photo + spot[..., None] * 400.0
    if p.blur > 0:
        photo = cv2.GaussianBlur(photo, (0, 0), sigmaX=p.blur)
    photo += rng.normal(0, p.noise, photo.shape)
    photo = np.clip(photo, 0, 255).astype(np.uint8)
    im = Image.fromarray(photo)
    buf = io.BytesIO()
    exif = Image.Exif()
    if p.taken_at:
        exif[0x0132] = p.taken_at
        exif.get_ifd(0x8769)[0x9003] = p.taken_at
    im.save(buf, format="JPEG", quality=88, exif=exif.tobytes())
    return buf.getvalue()
