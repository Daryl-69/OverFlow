"""Reading engine (README §5.4): one photo of the card -> time, turbidity,
free chlorine, plus photo-quality checks.

* **Geometry** — the four ArUco corner markers give a homography that maps
  the photo onto the card's own millimetre grid.
* **Colour** — an affine correction fitted on the printed neutral and
  primary patches removes the light's colour cast.
* **Chlorine** — the strip pad's colour is placed on the printed chlorine
  scale, *as photographed under the same light*, by projecting it onto the
  polyline through the scale colours in CIELAB.
* **Turbidity** — turbid water blurs and veils the checkerboard under the
  glass. The index is ``1 - contrast(under glass) / contrast(clear reference)``:
  0 for perfectly clear water, rising towards 1. It is meant for spotting
  spikes against a tap's own baseline; an NTU estimate is given only when a
  calibration from reference samples is supplied.
"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import cv2
import numpy as np

from ..card import spec as S
from ..card.spec import DEFAULT_SPEC, CardSpec, Rect, parse_payload
from .color import apply_correction, fit_correction, linear_to_lab, linear_to_srgb, luminance, srgb_to_linear

PPM = 6.0                     # canonical pixels per millimetre
MAX_SIDE = 2400               # photos are downscaled to this before reading
MIN_MARKERS = 3
MIN_PX_PER_MM = 2.5           # below this the card is too far away / too small
SHARPNESS_MIN = 0.15          # see _sharpness(); sharp photos score ~0.35+, ~0.45 mm blur ~0.15
GLARE_LEVEL = 253
GLARE_MAX_GLASS = 0.08        # clipped pixels in the glass beyond those in the clear reference
GLARE_MAX_STRIP = 0.05
OVEREXPOSED = 0.35            # share of clipped pixels in the clear reference
COLOUR_FIT_MAX = 25.0         # RMS error (0–255) of the colour correction
PAD_MIN_COVERAGE = 0.2
PAD_SPLIT_DE = 5.0           # a*b* distance that separates a coloured pad from white plastic


@dataclass
class TurbidityCalibration:
    """Monotone map from turbidity index to NTU, from reference samples."""

    index: list[float]
    ntu: list[float]

    @classmethod
    def fit(cls, pairs: list[tuple[float, float]]) -> "TurbidityCalibration":
        if len(pairs) < 2:
            raise ValueError("need at least two calibration samples")
        pairs = sorted(pairs)
        idx = [p[0] for p in pairs]
        ntu = list(np.maximum.accumulate([p[1] for p in pairs]))
        return cls(index=[float(v) for v in idx], ntu=[float(v) for v in ntu])

    def __call__(self, index: float) -> float:
        return float(np.interp(index, self.index, self.ntu))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, path: str | Path) -> "TurbidityCalibration":
        d = json.loads(Path(path).read_text())
        return cls(index=d["index"], ntu=d["ntu"])


@dataclass
class ReadResult:
    ok: bool
    issues: list[str] = field(default_factory=list)      # need a retake
    warnings: list[str] = field(default_factory=list)    # reading usable, with caveats
    household_id: str | None = None
    taken_at: str | None = None
    turbidity: dict | None = None
    chlorine: dict | None = None
    quality: dict = field(default_factory=dict)
    phash: str | None = None
    sha256: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- helpers
def _crop(img: np.ndarray, r: Rect) -> np.ndarray:
    return img[int(round(r.y * PPM)):int(round((r.y + r.h) * PPM)),
               int(round(r.x * PPM)):int(round((r.x + r.w) * PPM))]


def _median_rgb(img: np.ndarray, r: Rect) -> np.ndarray:
    return np.median(_crop(img, r.inner(0.6)).reshape(-1, 3), axis=0)


def exif_time(data: bytes) -> str | None:
    """DateTimeOriginal (or DateTime) as an ISO string, with the UTC offset
    when the camera recorded one."""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            ex = im.getexif()
            sub = ex.get_ifd(0x8769)
            raw = sub.get(0x9003) or ex.get(0x0132)
            off = sub.get(0x9011)
    except Exception:  # noqa: BLE001 — any unreadable metadata just means "no EXIF time"
        return None
    if not raw or not isinstance(raw, str) or len(raw) < 19:
        return None
    iso = raw[:10].replace(":", "-") + "T" + raw[11:19]
    if isinstance(off, str) and len(off) == 6 and off[0] in "+-":
        iso += off
    return iso


def phash(rgb: np.ndarray) -> str:
    """64-bit DCT perceptual hash (hex)."""
    g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    small = cv2.resize(g, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    d = cv2.dct(small)[:8, :8].ravel()
    med = np.median(d[1:])
    bits = d > med
    return f"{int(''.join('1' if b else '0' for b in bits), 2):016x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _sharpness(gray: np.ndarray) -> float:
    """Scale-free sharpness of the reference checkerboard: mean gradient
    magnitude at its edges relative to its contrast. A sharp 2.5 mm board at
    6 px/mm scores ~0.3; a blurred one tends to 0."""
    g = gray.astype(np.float32)
    lo, hi = np.percentile(g, [5, 95])
    if hi - lo < 1e-3:
        return 0.0
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.hypot(gx, gy) / 8.0
    return float(np.percentile(mag, 90) / (hi - lo))


def _michelson(y: np.ndarray) -> float:
    lo, hi = np.percentile(y, [5, 95])
    return float((hi - lo) / (hi + lo)) if hi + lo > 1e-9 else 0.0


# ------------------------------------------------------------------- main
def find_card(rgb: np.ndarray, spec: CardSpec = DEFAULT_SPEC):
    """Detect the corner markers. Returns (homography photo->canonical,
    markers found, px-per-mm in the photo, reprojection error in mm) or None."""
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    det = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, S.ARUCO_DICT)),
                                  cv2.aruco.DetectorParameters())
    corners, ids, _ = det.detectMarkers(gray)
    if ids is None:
        return None
    found: dict[int, np.ndarray] = {}
    for c, i in zip(corners, ids.ravel()):
        i = int(i)
        if i in spec.markers and i not in found:
            found[i] = c.reshape(4, 2)
    if len(found) < MIN_MARKERS:
        return None, sorted(found), 0.0, 0.0
    src, dst, sides = [], [], []
    for i, c in found.items():
        src.extend(c.tolist())
        dst.extend([(x * PPM, y * PPM) for x, y in spec.marker_corners(i)])
        sides.extend(np.linalg.norm(c - np.roll(c, -1, axis=0), axis=1).tolist())
    src_a = np.asarray(src, np.float32)
    dst_a = np.asarray(dst, np.float32)
    H, _ = cv2.findHomography(src_a, dst_a, cv2.RANSAC, 4.0)
    if H is None:
        return None, sorted(found), 0.0, 0.0
    proj = cv2.perspectiveTransform(src_a[None], H)[0]
    err_mm = float(np.mean(np.linalg.norm(proj - dst_a, axis=1)) / PPM)
    px_per_mm = float(np.mean(sides) / S.MARKER_SIZE)
    return H, sorted(found), px_per_mm, err_mm


def read_photo(data: bytes, spec: CardSpec = DEFAULT_SPEC,
               calibration: TurbidityCalibration | None = None) -> ReadResult:
    res = ReadResult(ok=False, sha256=hashlib.sha256(data).hexdigest())
    bgr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if bgr is None:
        res.issues.append("not_an_image: the file could not be opened as a photo")
        return res
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    scale = MAX_SIDE / max(rgb.shape[:2])
    if scale < 1:
        rgb = cv2.resize(rgb, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    res.phash = phash(rgb)
    res.taken_at = exif_time(data)

    found = find_card(rgb, spec)
    if found is None or found[0] is None:
        n = 0 if found is None else len(found[1])
        res.quality["markers"] = n
        res.issues.append("card_not_found: make sure all four black corner squares are in the photo")
        return res
    H, markers, px_per_mm, err_mm = found
    res.quality.update(markers=len(markers), px_per_mm=round(px_per_mm, 2), homography_error_mm=round(err_mm, 3))
    if px_per_mm < MIN_PX_PER_MM:
        res.issues.append("too_far: move closer so the card fills most of the photo")
    if err_mm > 1.5:
        res.issues.append("card_bent: lay the card flat")
    W, Hh = int(round(spec.width * PPM)), int(round(spec.height * PPM))
    canon = cv2.warpPerspective(rgb, H, (W, Hh), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)

    # household ID from the QR code (canonical crop first, then the photo)
    qr = spec.qr_rect
    crop = _crop(canon, Rect(qr.x - 3, qr.y - 3, qr.w + 6, qr.h + 6))
    det = cv2.QRCodeDetector()
    text = ""
    try:
        text = det.detectAndDecode(cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC))[0]
        if not text:
            text = det.detectAndDecode(rgb)[0]
    except cv2.error:
        text = ""
    res.household_id = parse_payload(text)

    # quality: sharpness and glare
    gray = cv2.cvtColor(canon, cv2.COLOR_RGB2GRAY)
    ref_inner = spec.ref_band.inner(0.85)
    sharp = _sharpness(_crop(gray, ref_inner))
    res.quality["sharpness"] = round(sharp, 3)
    if sharp < SHARPNESS_MIN:
        res.issues.append("blurry: hold the phone steady and tap to focus on the card")
    gx, gy = S.GLASS_CENTER
    rr = S.GLASS_RADIUS * S.GLASS_READ_FRAC
    yy, xx = np.mgrid[0:Hh, 0:W]
    disk = ((xx + 0.5) / PPM - gx) ** 2 + ((yy + 0.5) / PPM - gy) ** 2 <= rr ** 2
    clipped = canon.min(axis=2) >= GLARE_LEVEL
    # Overexposure clips white paper everywhere; glare is a local hot spot.
    # So glare on the glass is clipping there in excess of the reference band.
    clip_ref = float(_crop(clipped, ref_inner).mean())
    glare_glass = max(0.0, float(clipped[disk].mean()) - clip_ref)
    res.quality["overexposed"] = round(clip_ref, 4)
    if clip_ref > OVEREXPOSED:
        res.warnings.append("photo is overexposed — readings may be less reliable")
    strip_rects = [spec.pad_box] + [r for r, (name, _) in zip(spec.patch_rects(), spec.patches)
                                    if name not in ("white",)] + spec.scale_rects()[2:]
    strip_px = np.concatenate([_crop(clipped, r).ravel() for r in strip_rects])
    glare_strip = float(strip_px.mean())
    res.quality.update(glare_glass=round(glare_glass, 4), glare_strip=round(glare_strip, 4))
    if glare_glass > GLARE_MAX_GLASS:
        res.issues.append("glare_on_glass: tilt the phone or move away from the light")

    # colour correction from the printed patches
    obs = np.asarray([_median_rgb(canon, r) for r in spec.patch_rects()])
    nom = np.asarray([rgb for _, rgb in spec.patches], dtype=float)
    M = fit_correction(srgb_to_linear(obs), srgb_to_linear(nom))
    fitted = linear_to_srgb(apply_correction(M, srgb_to_linear(obs)))
    rmse = float(np.sqrt(np.mean((fitted - nom) ** 2)))
    res.quality["colour_fit_rmse"] = round(rmse, 2)
    if rmse > COLOUR_FIT_MAX:
        res.warnings.append("colour reference patches look wrong (shadow or glare on them?)")

    def corrected_lin(px: np.ndarray) -> np.ndarray:
        return np.clip(apply_correction(M, srgb_to_linear(px)), 0.0, 1.0)

    # chlorine from the strip pad
    if glare_strip > GLARE_MAX_STRIP:
        res.warnings.append("glare on the strip or colour scale — chlorine not read")
    else:
        res.chlorine = _read_chlorine(canon, spec, corrected_lin, res)

    # turbidity from contrast loss
    lin = corrected_lin(canon.reshape(-1, 3).astype(float)).reshape(canon.shape)
    Y = luminance(lin)
    ref_mask = np.zeros_like(disk)
    ri = spec.ref_band.inner(0.85)
    ref_mask[int(round(ri.y * PPM)):int(round((ri.y + ri.h) * PPM)),
             int(round(ri.x * PPM)):int(round((ri.x + ri.w) * PPM))] = True
    c_in = _michelson(Y[disk])
    c_ref = _michelson(Y[ref_mask])
    ratio = c_in / c_ref if c_ref > 1e-6 else 0.0
    index = float(np.clip(1.0 - ratio, 0.0, 1.0))
    lab = linear_to_lab(lin)
    y_in = Y[disk]
    y_ref = Y[ref_mask]
    white_in = lab[disk][y_in >= np.percentile(y_in, 75)].mean(axis=0)
    white_ref = lab[ref_mask][y_ref >= np.percentile(y_ref, 75)].mean(axis=0)
    turb = {"index": round(index, 4), "contrast_ratio": round(float(ratio), 4),
            "discolouration_dE": round(float(np.linalg.norm(white_in - white_ref)), 2),
            "yellowing_db": round(float(white_in[2] - white_ref[2]), 2),
            "ntu_estimate": None}
    if calibration is not None:
        turb["ntu_estimate"] = round(calibration(index), 1)
    res.turbidity = turb
    res.ok = not res.issues
    return res


def _pad_colour(pix: np.ndarray) -> np.ndarray:
    """Median Lab colour of the reagent pad among the bright pixels in the
    pad box, which may also show some of the strip's white plastic.

    Two-means on the (a*, b*) plane separates a coloured pad from the
    plastic; when the two groups are not clearly distinct (a pad at or near
    0 mg/L is as white as the plastic) every bright pixel is the pad."""
    ab = pix[:, 1:]
    chroma = np.hypot(ab[:, 0], ab[:, 1])
    c = np.stack([ab[np.argmin(chroma)], ab[np.argmax(chroma)]]).astype(float)
    lab = np.zeros(len(ab), dtype=int)
    for _ in range(20):
        d = np.linalg.norm(ab[:, None, :] - c[None, :, :], axis=2)
        new = np.argmin(d, axis=1)
        if np.array_equal(new, lab) and _ > 0:
            break
        lab = new
        for k in (0, 1):
            if np.any(lab == k):
                c[k] = ab[lab == k].mean(axis=0)
    share = np.bincount(lab, minlength=2) / len(lab)
    sep = float(np.linalg.norm(c[0] - c[1]))
    if sep >= PAD_SPLIT_DE and share.min() >= 0.15:
        pad_k = int(np.argmax(np.hypot(c[:, 0], c[:, 1])))
        return np.median(pix[lab == pad_k], axis=0)
    return np.median(pix, axis=0)


def _read_chlorine(canon: np.ndarray, spec: CardSpec, corrected_lin, res: ReadResult) -> dict | None:
    box = _crop(canon, spec.pad_box.inner(0.9)).reshape(-1, 3).astype(float)
    sl = spec.slot
    bg_rect = Rect(sl.x + sl.w * 0.75, sl.y + 2, sl.w * 0.2, sl.h - 4)
    bg = _crop(canon, bg_rect).reshape(-1, 3).astype(float)
    lab_box = linear_to_lab(corrected_lin(box))
    L_bg = float(np.median(linear_to_lab(corrected_lin(bg))[:, 0]))
    bright = lab_box[:, 0] > L_bg + 20
    coverage = float(bright.mean())
    if coverage < PAD_MIN_COVERAGE:
        res.warnings.append("no strip found in the slot — chlorine not read")
        return None
    pad = _pad_colour(lab_box[bright])
    scale_lab = np.asarray([linear_to_lab(corrected_lin(_median_rgb(canon, r)[None]))[0]
                            for r in spec.scale_rects()])
    levels = spec.chlorine_levels
    best = None
    for i in range(len(levels) - 1):
        a, b = scale_lab[i], scale_lab[i + 1]
        d = b - a
        t = float(np.clip(np.dot(pad - a, d) / max(np.dot(d, d), 1e-9), 0.0, 1.0))
        dist = float(np.linalg.norm(pad - (a + t * d)))
        if best is None or dist < best[0]:
            best = (dist, levels[i] + t * (levels[i + 1] - levels[i]))
    dist, mg = best
    conf = "high" if dist < 6 else "medium" if dist < 12 else "low"
    if conf == "low":
        res.warnings.append("strip colour does not match the printed scale — check the strip type")
    return {"mg_per_l": round(float(mg), 3), "residual_dE": round(dist, 2), "confidence": conf,
            "pad_lab": [round(float(v), 2) for v in pad], "pad_coverage": round(coverage, 3),
            "below_minimum": bool(mg < 0.2)}
