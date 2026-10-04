"""Render the card as print-ready SVG and as a raster image.

Raster images in this package are RGB uint8 arrays (converted to/from
OpenCV's BGR only at file boundaries).
"""

from __future__ import annotations

from xml.sax.saxutils import escape

import cv2
import numpy as np

from . import spec as S
from .spec import DEFAULT_SPEC, CardSpec, household_payload

INSTRUCTIONS = (
    "1  When water comes on, fill a clear glass with the FIRST water from the tap.",
    "2  Stand the glass on the circle. Dip one chlorine strip, lay it in the slot, pad in the box.",
    "3  Take ONE photo of the whole card (all four corner squares visible) and send it.",
    "4  Tap one answer: smells bad / looks dirty / someone is ill / fine.",
)


def _aruco_dict():
    return cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, S.ARUCO_DICT))


def marker_bits(marker_id: int) -> np.ndarray:
    """6×6 boolean array, True = black cell."""
    img = cv2.aruco.generateImageMarker(_aruco_dict(), marker_id, S.MARKER_CELLS, borderBits=1)
    return img < 128


def qr_bits(text: str) -> np.ndarray:
    """QR modules (True = dark) with a 4-module quiet zone."""
    img = cv2.QRCodeEncoder.create().encode(text)
    dark = img < 128
    rows = np.flatnonzero(dark.any(axis=1))
    cols = np.flatnonzero(dark.any(axis=0))
    core = dark[rows.min(): rows.max() + 1, cols.min(): cols.max() + 1]
    return np.pad(core, 4, constant_values=False)


def _fmt(v: float) -> str:
    return f"{v:.3f}".rstrip("0").rstrip(".")


def card_svg(household_id: str, spec: CardSpec = DEFAULT_SPEC) -> str:
    """Print at 100 % scale on A5 landscape (or A4 with the card centred)."""
    out: list[str] = []
    a = out.append
    a(f'<svg xmlns="http://www.w3.org/2000/svg" width="{_fmt(spec.width)}mm" height="{_fmt(spec.height)}mm" '
      f'viewBox="0 0 {_fmt(spec.width)} {_fmt(spec.height)}" font-family="Helvetica, Arial, sans-serif">')
    a(f"<title>Upstream first-glass card — {escape(household_id)}</title>")
    c = S.CHECKER
    a("<defs>"
      f'<pattern id="chk" patternUnits="userSpaceOnUse" x="0" y="0" width="{_fmt(2 * c)}" height="{_fmt(2 * c)}">'
      f'<rect x="0" y="0" width="{_fmt(c)}" height="{_fmt(c)}" fill="#000"/>'
      f'<rect x="{_fmt(c)}" y="{_fmt(c)}" width="{_fmt(c)}" height="{_fmt(c)}" fill="#000"/></pattern>'
      "</defs>")
    a(f'<rect x="0" y="0" width="{_fmt(spec.width)}" height="{_fmt(spec.height)}" fill="#fff"/>')
    # markers
    cell = S.MARKER_SIZE / S.MARKER_CELLS
    for mid, (mx, my) in spec.markers.items():
        bits = marker_bits(mid)
        a('<g shape-rendering="crispEdges" fill="#000">')
        for r in range(S.MARKER_CELLS):
            for col in range(S.MARKER_CELLS):
                if bits[r, col]:
                    a(f'<rect x="{_fmt(mx + col * cell)}" y="{_fmt(my + r * cell)}" '
                      f'width="{_fmt(cell + 0.01)}" height="{_fmt(cell + 0.01)}"/>')
        a("</g>")
    # title and QR
    a('<text x="34" y="15" font-size="7" font-weight="700">UPSTREAM</text>')
    a('<text x="34" y="21.5" font-size="3.6">first-glass card · photograph the first water of every supply</text>')
    a(f'<text x="34" y="29" font-size="4.2" font-weight="700">Household {escape(household_id)}</text>')
    q = qr_bits(household_payload(household_id))
    qr = spec.qr_rect
    m = qr.w / q.shape[0]
    a('<g shape-rendering="crispEdges" fill="#000">')
    for r in range(q.shape[0]):
        for col in range(q.shape[1]):
            if q[r, col]:
                a(f'<rect x="{_fmt(qr.x + col * m)}" y="{_fmt(qr.y + r * m)}" width="{_fmt(m + 0.01)}" '
                  f'height="{_fmt(m + 0.01)}"/>')
    a("</g>")
    # glass circle with checkerboard
    gx, gy = S.GLASS_CENTER
    a(f'<circle cx="{_fmt(gx)}" cy="{_fmt(gy)}" r="{_fmt(S.GLASS_RADIUS)}" fill="url(#chk)"/>')
    a(f'<circle cx="{_fmt(gx)}" cy="{_fmt(gy)}" r="{_fmt(S.GLASS_RADIUS + 0.6)}" fill="none" '
      f'stroke="#1f5fbf" stroke-width="1"/>')
    a(f'<text x="{_fmt(gx)}" y="{_fmt(gy - S.GLASS_RADIUS - 3)}" font-size="3.6" text-anchor="middle">'
      "Stand the glass here</text>")
    # reference band
    rb = spec.ref_band
    a(f'<rect x="{_fmt(rb.x)}" y="{_fmt(rb.y)}" width="{_fmt(rb.w)}" height="{_fmt(rb.h)}" fill="url(#chk)"/>')
    a(f'<text x="{_fmt(rb.x)}" y="{_fmt(rb.y - 1.5)}" font-size="2.8">clear reference — keep uncovered</text>')
    # strip slot
    sl, pb = spec.slot, spec.pad_box
    a(f'<rect x="{_fmt(sl.x)}" y="{_fmt(sl.y)}" width="{_fmt(sl.w)}" height="{_fmt(sl.h)}" '
      f'fill="rgb{S.SLOT_RGB}"/>')
    a(f'<rect x="{_fmt(pb.x)}" y="{_fmt(pb.y)}" width="{_fmt(pb.w)}" height="{_fmt(pb.h)}" fill="none" '
      f'stroke="#fff" stroke-width="0.4" stroke-dasharray="1 0.8"/>')
    a(f'<text x="{_fmt(pb.x + pb.w + 3)}" y="{_fmt(sl.y + sl.h / 2 + 1.2)}" font-size="3.2" fill="#fff">'
      "◀ strip pad in the box, strip along the slot</text>")
    # chlorine scale
    for rect, lvl, rgb in zip(spec.scale_rects(), spec.chlorine_levels, spec.chlorine_rgb):
        a(f'<rect x="{_fmt(rect.x)}" y="{_fmt(rect.y)}" width="{_fmt(rect.w)}" height="{_fmt(rect.h)}" '
          f'fill="rgb{tuple(rgb)}" stroke="#999" stroke-width="0.2"/>')
        a(f'<text x="{_fmt(rect.x + rect.w / 2)}" y="{_fmt(rect.y + rect.h + 3.6)}" font-size="3" '
          f'text-anchor="middle">{_fmt(lvl)}</text>')
    last = spec.scale_rects()[-1]
    a(f'<text x="{_fmt(last.x + last.w + 1)}" y="{_fmt(last.y + last.h + 3.6)}" font-size="2.6">mg/L</text>')
    # colour patches
    pr = spec.patch_rects()
    a(f'<text x="{_fmt(pr[0].x)}" y="{_fmt(pr[0].y - 1.5)}" font-size="2.8">colour reference</text>')
    for rect, (_, rgb) in zip(pr, spec.patches):
        a(f'<rect x="{_fmt(rect.x)}" y="{_fmt(rect.y)}" width="{_fmt(rect.w)}" height="{_fmt(rect.h)}" '
          f'fill="rgb{tuple(rgb)}" stroke="#999" stroke-width="0.2"/>')
    # privacy note, instructions (kept clear of the corner markers), print check
    a('<text x="104" y="110" font-size="2.6">Only the household ID travels with a reading.</text>')
    a('<text x="104" y="113.6" font-size="2.6">Readings are used for water safety only.</text>')
    for i, line in enumerate(INSTRUCTIONS):
        a(f'<text x="34" y="{_fmt(122.5 + i * 3.5)}" font-size="2.6">{escape(line)}</text>')
    bx, by, bl = S.SCALE_BAR
    a(f'<rect x="{_fmt(bx)}" y="{_fmt(by)}" width="{_fmt(bl)}" height="1" fill="#000"/>')
    a(f'<text x="{_fmt(bx + bl + 2)}" y="{_fmt(by + 1.2)}" font-size="2.6">print check: this bar is 50 mm</text>')
    a("</svg>")
    return "\n".join(out)


def _px(v: float, ppm: float) -> int:
    return int(round(v * ppm))


def _fill_rect(img: np.ndarray, x: float, y: float, w: float, h: float, rgb, ppm: float) -> None:
    img[_px(y, ppm):_px(y + h, ppm), _px(x, ppm):_px(x + w, ppm)] = rgb


def _text(img, txt, x, y, size_mm, ppm, rgb=(0, 0, 0), bold=False):
    scale = size_mm * ppm / 22.0
    cv2.putText(img, txt, (_px(x, ppm), _px(y, ppm)), cv2.FONT_HERSHEY_SIMPLEX, scale, rgb,
                max(1, int(round(scale * (2.2 if bold else 1.4)))), cv2.LINE_AA)


def render_card(household_id: str, ppm: float = 6.0, spec: CardSpec = DEFAULT_SPEC) -> np.ndarray:
    """RGB raster of the card at ``ppm`` pixels per millimetre."""
    W, H = _px(spec.width, ppm), _px(spec.height, ppm)
    img = np.full((H, W, 3), 255, np.uint8)
    yy, xx = np.mgrid[0:H, 0:W]
    xm = (xx + 0.5) / ppm
    ym = (yy + 0.5) / ppm
    checker = ((np.floor(xm / S.CHECKER) + np.floor(ym / S.CHECKER)) % 2) == 0
    gx, gy = S.GLASS_CENTER
    circle = (xm - gx) ** 2 + (ym - gy) ** 2 <= S.GLASS_RADIUS ** 2
    rb = spec.ref_band
    band = (xm >= rb.x) & (xm < rb.x + rb.w) & (ym >= rb.y) & (ym < rb.y + rb.h)
    img[(circle | band) & checker] = 0
    cv2.circle(img, (_px(gx, ppm), _px(gy, ppm)), _px(S.GLASS_RADIUS + 0.6, ppm), (31, 95, 191),
               max(1, _px(1.0, ppm)), cv2.LINE_AA)
    cell = S.MARKER_SIZE / S.MARKER_CELLS
    for mid, (mx, my) in spec.markers.items():
        bits = marker_bits(mid)
        for r in range(S.MARKER_CELLS):
            for c in range(S.MARKER_CELLS):
                if bits[r, c]:
                    _fill_rect(img, mx + c * cell, my + r * cell, cell, cell, (0, 0, 0), ppm)
    q = qr_bits(household_payload(household_id))
    qr = spec.qr_rect
    m = qr.w / q.shape[0]
    for r in range(q.shape[0]):
        for c in range(q.shape[1]):
            if q[r, c]:
                _fill_rect(img, qr.x + c * m, qr.y + r * m, m, m, (0, 0, 0), ppm)
    sl, pb = spec.slot, spec.pad_box
    _fill_rect(img, sl.x, sl.y, sl.w, sl.h, S.SLOT_RGB, ppm)
    cv2.rectangle(img, (_px(pb.x, ppm), _px(pb.y, ppm)), (_px(pb.x + pb.w, ppm), _px(pb.y + pb.h, ppm)),
                  (255, 255, 255), max(1, _px(0.3, ppm)))
    for rect, lvl, rgb in zip(spec.scale_rects(), spec.chlorine_levels, spec.chlorine_rgb):
        _fill_rect(img, rect.x, rect.y, rect.w, rect.h, rgb, ppm)
        _text(img, f"{lvl:g}", rect.x + 2, rect.y + rect.h + 3.6, 3, ppm)
    for rect, (_, rgb) in zip(spec.patch_rects(), spec.patches):
        _fill_rect(img, rect.x, rect.y, rect.w, rect.h, rgb, ppm)
    _text(img, "UPSTREAM", 34, 15, 7, ppm, bold=True)
    _text(img, f"Household {household_id}", 34, 29, 4.2, ppm, bold=True)
    for i, line in enumerate(INSTRUCTIONS):
        _text(img, line, 34, 122.5 + i * 3.5, 2.4, ppm)
    bx, by, bl = S.SCALE_BAR
    _fill_rect(img, bx, by, bl, 1.0, (0, 0, 0), ppm)
    return img
