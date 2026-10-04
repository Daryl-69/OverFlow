import io
import xml.etree.ElementTree as ET

import cv2
import numpy as np
import pytest
from PIL import Image

from upstream.card import spec as S
from upstream.card.render import card_svg, marker_bits, qr_bits, render_card
from upstream.card.spec import household_payload, parse_payload
from upstream.reader.core import TurbidityCalibration, find_card, hamming, read_photo
from upstream.reader.synth import PhotoParams, render_photo


def test_card_svg_is_valid_and_complete():
    svg = card_svg("H-0042")
    root = ET.fromstring(svg)
    assert root.attrib["width"] == "210mm" and root.attrib["height"] == "148mm"
    assert "Household H-0042" in svg and "50 mm" in svg
    # 4 markers × their black cells, plus QR modules
    n_rect = svg.count("<rect")
    expected = sum(int(marker_bits(i).sum()) for i in range(4)) + int(qr_bits(household_payload("H-0042")).sum())
    assert n_rect > expected


def test_card_raster_markers_and_qr_decode():
    img = render_card("H-0042", ppm=6)
    H, found, ppm, err = find_card(img)
    assert found == [0, 1, 2, 3] and abs(ppm - 6) < 0.2 and err < 0.2
    text = cv2.QRCodeDetector().detectAndDecode(img)[0]
    assert parse_payload(text) == "H-0042"
    assert parse_payload("something else") is None


def test_layout_has_no_overlaps():
    sp = S.DEFAULT_SPEC
    rects = [sp.ref_band, sp.slot, sp.qr_rect] + sp.scale_rects() + sp.patch_rects()
    rects += [S.Rect(x, y, S.MARKER_SIZE, S.MARKER_SIZE) for x, y in sp.markers.values()]
    for r in rects:
        assert 0 <= r.x and r.x + r.w <= sp.width and 0 <= r.y and r.y + r.h <= sp.height
    for i, a in enumerate(rects):
        for b in rects[i + 1:]:
            overlap = a.x < b.x + b.w and b.x < a.x + a.w and a.y < b.y + b.h and b.y < a.y + a.h
            assert not overlap, (a, b)
    gx, gy = S.GLASS_CENTER
    for r in rects:   # nothing printed under the glass circle except its checkerboard
        cx = min(max(gx, r.x), r.x + r.w)
        cy = min(max(gy, r.y), r.y + r.h)
        assert (cx - gx) ** 2 + (cy - gy) ** 2 > S.GLASS_RADIUS ** 2


@pytest.mark.parametrize("cl,cast,seed", [
    (0.0, (1, 1, 1), 1), (0.2, (1.15, 1.0, 0.8), 2), (0.5, (0.85, 1.0, 1.2), 3),
    (1.0, (1, 1, 1), 4), (2.0, (1.1, 0.95, 0.9), 5),
])
def test_chlorine_reading(cl, cast, seed):
    """The reader recovers the strip level under coloured light (strips
    rendered exactly at their chart colour)."""
    r = read_photo(render_photo("H-0007", PhotoParams(turbidity=2, chlorine=cl, cast=cast, strip_noise=0), seed=seed))
    assert r.ok, r.issues
    assert r.household_id == "H-0007"
    assert r.chlorine["mg_per_l"] == pytest.approx(cl, abs=0.03 + 0.06 * cl)
    assert r.chlorine["confidence"] == "high"


def test_chlorine_reading_with_strip_variation():
    """Real strips vary; readings stay unbiased and within about one chart step."""
    for cl in (0.2, 1.0):
        vals = [read_photo(render_photo("H-0007", PhotoParams(chlorine=cl), seed=s)).chlorine["mg_per_l"]
                for s in range(6)]
        assert abs(np.mean(vals) - cl) < 0.04 + 0.05 * cl
        assert max(abs(v - cl) for v in vals) < 0.1 + 0.15 * cl


def test_turbidity_index_rises_with_mud():
    vals = [read_photo(render_photo("H-0001", PhotoParams(turbidity=t, chlorine=0.5), seed=11)).turbidity["index"]
            for t in (0, 5, 20, 60)]
    assert vals[0] < 0.05
    assert all(b > a for a, b in zip(vals, vals[1:]))
    assert vals[-1] > 0.5


def test_quality_checks():
    blurry = read_photo(render_photo("H-0001", PhotoParams(blur=9), seed=2))
    assert not blurry.ok and any(i.startswith("blurry") for i in blurry.issues)
    glare = read_photo(render_photo("H-0001", PhotoParams(glare=True), seed=8))
    assert not glare.ok and any(i.startswith("glare_on_glass") for i in glare.issues)
    noise = (np.random.default_rng(0).random((600, 800, 3)) * 255).astype(np.uint8)
    ok, buf = cv2.imencode(".jpg", noise)
    nc = read_photo(buf.tobytes())
    assert not nc.ok and nc.issues[0].startswith("card_not_found")
    assert read_photo(b"not a photo").issues[0].startswith("not_an_image")


def test_no_strip_means_no_chlorine():
    r = read_photo(render_photo("H-0001", PhotoParams(chlorine=None), seed=4))
    assert r.ok and r.chlorine is None
    assert any("no strip" in w for w in r.warnings)


def test_exif_time_and_reuse_hash():
    data = render_photo("H-0001", PhotoParams(taken_at="2026:01:02 06:41:10"), seed=1)
    r = read_photo(data)
    assert r.taken_at == "2026-01-02T06:41:10"
    im = Image.open(io.BytesIO(data)).resize((1200, 900))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=70)
    copy = read_photo(buf.getvalue())
    other = read_photo(render_photo("H-0001", PhotoParams(), seed=5))
    assert hamming(r.phash, copy.phash) <= 4 < hamming(r.phash, other.phash)
    assert copy.sha256 != r.sha256


def test_turbidity_calibration(tmp_path):
    cal = TurbidityCalibration.fit([(0.02, 0.5), (0.3, 10), (0.2, 12), (0.8, 60)])
    assert cal.ntu == sorted(cal.ntu)              # made monotone
    assert cal(0.02) == pytest.approx(0.5) and cal(0.8) == pytest.approx(60)
    p = tmp_path / "cal.json"
    cal.save(p)
    assert TurbidityCalibration.load(p)(0.5) == cal(0.5)
    r = read_photo(render_photo("H-0001", PhotoParams(turbidity=20), seed=3), calibration=cal)
    assert r.turbidity["ntu_estimate"] is not None
