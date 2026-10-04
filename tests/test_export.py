import json
import re

from upstream.export import export_artifact, export_static


def test_static_export(tmp_path, scenario_file):
    out = export_static(tmp_path / "site", scenario_file)
    html = (out / "index.html").read_text()
    assert 'data-mode="static"' in html
    for f in ("app.js", "style.css", "card.svg", "vendor/leaflet/leaflet.js", "vendor/leaflet/leaflet.css",
              "data/scenario.json", ".nojekyll"):
        assert (out / f).exists(), f
    data = json.loads((out / "data" / "scenario.json").read_text())
    assert data["frames"] and data["truth"]


def test_artifact_export_is_self_contained(tmp_path, scenario_file):
    ev = tmp_path / "ev.json"
    ev.write_text(json.dumps({"runs": 3, "rows": [1, 2, 3], "gis": {}}))
    page = export_artifact(tmp_path / "a.html", scenario_file, ev).read_text()
    assert not page.lstrip().lower().startswith("<!doctype")
    assert "<html" not in page and "<body>" not in page and "<head>" not in page
    assert page.startswith("<title>")
    assert 'data-mode="artifact"' in page
    srcs = re.findall(r'<script[^>]*\ssrc="([^"]+)"', page)
    assert srcs == ["https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js"]
    assert 'integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="' in page
    hrefs = re.findall(r'<link[^>]*href="([^"]+)"', page)
    assert all(h.startswith("https://fonts.googleapis.com") for h in hrefs)
    m = re.search(r'<script type="application/json" id="scenario-data">(.*?)</script>', page, re.S)
    data = json.loads(m.group(1))
    assert data["evaluation"]["runs"] == 3 and "rows" not in data["evaluation"]
    assert 'src="data:image/svg+xml;base64,' in page
    # no file downloads and no server-only features in the artifact
    assert "download" not in page.lower().replace("downloads", "")
    assert "/api/" not in page.split('id="scenario-data"')[0]
    assert 'id="tab-live"' not in page and 'id="tab-replay"' in page and 'id="tab-card"' in page
