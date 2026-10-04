"""Geometry and colours of the Upstream first-glass card.

All positions are in millimetres from the card's top-left corner. The SVG
(for printing), the raster renderer (for tests and demo photos) and the
reader all use this one spec, so they cannot drift apart.

The card is A5 landscape (210 × 148 mm):

* four ArUco markers (DICT_4X4_50, ids 0–3 clockwise from top-left) let the
  reader undo angle and distance;
* a checkerboard under the glass circle blurs and fades through turbid water;
  the same checkerboard printed outside the glass is the clear reference;
* a dark strip slot with a pad box sits beside the printed chlorine colour
  scale, so the strip is compared with the scale under the same light;
* neutral and primary colour patches allow colour correction;
* a QR code carries the household ID — no name or phone number needed.

The chlorine scale colours below approximate a DPD free-chlorine strip chart
(white through pink to magenta). They are a starting point: print the card,
photograph strips dipped in known standards, and replace them with the
colours of the strip brand actually distributed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CARD_W = 210.0
CARD_H = 148.0

ARUCO_DICT = "DICT_4X4_50"
MARKER_SIZE = 22.0       # mm, including the black border (6 × 6 cells)
MARKER_CELLS = 6
MARKER_MARGIN = 6.0      # mm from the card edge

# id -> top-left corner (mm); ids clockwise from top-left
MARKERS = {
    0: (MARKER_MARGIN, MARKER_MARGIN),
    1: (CARD_W - MARKER_MARGIN - MARKER_SIZE, MARKER_MARGIN),
    2: (CARD_W - MARKER_MARGIN - MARKER_SIZE, CARD_H - MARKER_MARGIN - MARKER_SIZE),
    3: (MARKER_MARGIN, CARD_H - MARKER_MARGIN - MARKER_SIZE),
}

GLASS_CENTER = (62.0, 82.0)
GLASS_RADIUS = 32.0
GLASS_READ_FRAC = 0.62   # read only the inner part of the circle, away from the glass wall
CHECKER = 2.5            # mm per checkerboard square

REF_BAND = (104.0, 36.0, 96.0, 12.0)    # x, y, w, h — clear-reference checkerboard

SLOT = (104.0, 55.0, 98.0, 10.0)        # dark strip channel
PAD_BOX = (106.0, 55.0, 10.0, 10.0)     # where the reagent pad goes
SLOT_RGB = (40, 40, 40)

CHLORINE_LEVELS = (0.0, 0.1, 0.2, 0.5, 1.0, 2.0, 4.0)     # mg/L
CHLORINE_RGB = (
    (250, 248, 246),
    (247, 229, 236),
    (243, 212, 227),
    (233, 178, 208),
    (216, 140, 187),
    (190, 96, 160),
    (156, 56, 128),
)
SCALE_ORIGIN = (104.0, 71.0)
SCALE_PATCH = (12.0, 10.0)
SCALE_GAP = 2.0

COLOR_PATCHES = (
    ("white", (255, 255, 255)),
    ("grey75", (191, 191, 191)),
    ("grey50", (128, 128, 128)),
    ("grey25", (64, 64, 64)),
    ("black", (0, 0, 0)),
    ("red", (200, 40, 40)),
    ("green", (40, 160, 70)),
    ("blue", (40, 70, 190)),
    ("yellow", (235, 205, 40)),
    ("cyan", (40, 175, 205)),
)
PATCH_ORIGIN = (104.0, 94.0)
PATCH_SIZE = (8.6, 9.0)
PATCH_GAP = 1.2

QR_ORIGIN = (150.0, 6.0)
QR_SIZE = 26.0           # including a 4-module quiet zone
QR_PREFIX = "UPSTREAM:"

SCALE_BAR = (34.0, 138.5, 50.0)   # x, y, length — "this bar should measure 50 mm"


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    def inner(self, frac: float) -> "Rect":
        """The central ``frac`` of the rectangle (for sampling away from edges)."""
        dw, dh = self.w * (1 - frac) / 2, self.h * (1 - frac) / 2
        return Rect(self.x + dw, self.y + dh, self.w - 2 * dw, self.h - 2 * dh)


@dataclass(frozen=True)
class CardSpec:
    width: float = CARD_W
    height: float = CARD_H
    chlorine_levels: tuple[float, ...] = CHLORINE_LEVELS
    chlorine_rgb: tuple[tuple[int, int, int], ...] = CHLORINE_RGB
    patches: tuple[tuple[str, tuple[int, int, int]], ...] = COLOR_PATCHES
    markers: dict = field(default_factory=lambda: dict(MARKERS))

    def marker_corners(self, marker_id: int) -> list[tuple[float, float]]:
        """TL, TR, BR, BL corners (mm) — the order OpenCV reports them in."""
        x, y = self.markers[marker_id]
        s = MARKER_SIZE
        return [(x, y), (x + s, y), (x + s, y + s), (x, y + s)]

    def scale_rects(self) -> list[Rect]:
        x0, y0 = SCALE_ORIGIN
        w, h = SCALE_PATCH
        return [Rect(x0 + i * (w + SCALE_GAP), y0, w, h) for i in range(len(self.chlorine_levels))]

    def patch_rects(self) -> list[Rect]:
        x0, y0 = PATCH_ORIGIN
        w, h = PATCH_SIZE
        return [Rect(x0 + i * (w + PATCH_GAP), y0, w, h) for i in range(len(self.patches))]

    @property
    def ref_band(self) -> Rect:
        return Rect(*REF_BAND)

    @property
    def slot(self) -> Rect:
        return Rect(*SLOT)

    @property
    def pad_box(self) -> Rect:
        return Rect(*PAD_BOX)

    @property
    def qr_rect(self) -> Rect:
        return Rect(QR_ORIGIN[0], QR_ORIGIN[1], QR_SIZE, QR_SIZE)


DEFAULT_SPEC = CardSpec()


def household_payload(household_id: str) -> str:
    return f"{QR_PREFIX}{household_id}"


def parse_payload(text: str | None) -> str | None:
    if text and text.startswith(QR_PREFIX):
        hid = text[len(QR_PREFIX):].strip()
        return hid or None
    return None
