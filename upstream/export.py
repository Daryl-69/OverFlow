"""Export the dashboard as a static site.

* ``static`` — a folder (index.html, app.js, style.css, vendored Leaflet,
  data/scenario.json, card.svg) for GitHub Pages or any static host.
* ``artifact`` — one self-contained HTML page body: Leaflet from jsDelivr with
  Subresource Integrity, everything else inline.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
from pathlib import Path

from .card.render import card_svg

STATIC = Path(__file__).resolve().parent / "dashboard" / "static"
LEAFLET_CDN = ('<script src="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js" '
               'integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin="anonymous"></script>')


def _scenario(scenario_path: Path, evaluation_path: Path | None) -> dict:
    data = json.loads(Path(scenario_path).read_text())
    if evaluation_path and Path(evaluation_path).exists():
        ev = json.loads(Path(evaluation_path).read_text())
        ev.pop("rows", None)
        data["evaluation"] = ev
    return data


def _set_mode(html: str, mode: str) -> str:
    out, n = re.subn(r'<div id="upstream" data-mode="[a-z]+">', f'<div id="upstream" data-mode="{mode}">', html)
    if n != 1:
        raise RuntimeError("dashboard template is missing its mode marker")
    return out


def export_static(out: Path, scenario_path: Path, evaluation_path: Path | None = None,
                  household: str = "H-0001") -> Path:
    out = Path(out)
    if out.exists():
        shutil.rmtree(out)
    (out / "data").mkdir(parents=True)
    shutil.copytree(STATIC / "vendor", out / "vendor")
    shutil.copy2(STATIC / "app.js", out / "app.js")
    shutil.copy2(STATIC / "style.css", out / "style.css")
    (out / "index.html").write_text(_set_mode((STATIC / "index.html").read_text(), "static"))
    data = _scenario(scenario_path, evaluation_path)
    (out / "data" / "scenario.json").write_text(json.dumps(data, separators=(",", ":")))
    (out / "card.svg").write_text(card_svg(household))
    (out / ".nojekyll").write_text("")
    return out


def export_artifact(out_file: Path, scenario_path: Path, evaluation_path: Path | None = None,
                    household: str = "H-0001") -> Path:
    html = (STATIC / "index.html").read_text()
    title = re.search(r"<title>.*?</title>", html, re.S).group(0)
    fonts = re.search(r'<link rel="stylesheet" href="https://fonts\.googleapis\.com[^"]*">', html).group(0)
    body = re.search(r"<body>(.*)</body>", html, re.S).group(1)
    body = _set_mode(body, "artifact")
    # The live tab needs the server, and the artifact viewer cannot download files: drop both.
    body, n_live = re.subn(r'\s*<!-- LIVE \(app only\) -->\s*<section id="tab-live".*?</section>', "", body, flags=re.S)
    body, n_links = re.subn(r'\s*<p class="links" id="card-links">.*?</p>', "", body, flags=re.S)
    if n_live != 1 or n_links != 1:
        raise RuntimeError("dashboard template is missing the live tab or the card links")
    svg = card_svg(household).encode()
    body = body.replace('src="card.svg"', f'src="data:image/svg+xml;base64,{base64.b64encode(svg).decode()}"', 1)
    data = json.dumps(_scenario(scenario_path, evaluation_path), separators=(",", ":")).replace("</", "<\\/")
    app_js = (STATIC / "app.js").read_text()
    if "</script" in app_js:
        raise RuntimeError("app.js cannot be inlined: it contains a closing script tag")
    scripts = (f'{LEAFLET_CDN}\n<script type="application/json" id="scenario-data">{data}</script>\n'
               f"<script>\n{app_js}\n</script>")
    body, n = re.subn(r'<script src="vendor/leaflet/leaflet\.js"></script>\s*<script src="app\.js"></script>',
                      lambda _: scripts, body)
    if n != 1:
        raise RuntimeError("dashboard template is missing its script tags")
    css = (STATIC / "vendor" / "leaflet" / "leaflet.css").read_text() + "\n" + (STATIC / "style.css").read_text()
    page = f"{title}\n{fonts}\n<style>\n{css}\n</style>\n{body.strip()}\n"
    out_file = Path(out_file)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(page)
    return out_file
