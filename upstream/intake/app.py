"""FastAPI app: the household report page, the utility dashboard and the API.

Run with ``upstream serve`` (or ``uvicorn upstream.intake.app:app``).

Environment:

* ``UPSTREAM_VAR`` — where live data (SQLite, photos, cached scenario) is kept (default ``./var``)
* ``UPSTREAM_SCENARIO`` — scenario JSON to replay (default ``./data/scenarios/demo.json`` if present)
* ``UPSTREAM_TURBIDITY_CALIBRATION`` — optional turbidity calibration JSON (``upstream calibrate``)
* ``UPSTREAM_TZ`` — the ward's time zone for camera times, e.g. ``Asia/Kolkata`` (default: the server's)
* WhatsApp variables, see :mod:`upstream.intake.whatsapp`
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..card.render import card_svg, qr_bits, render_card
from ..reader.core import TurbidityCalibration
from ..reader.synth import PhotoParams, render_photo
from ..scenario import ScenarioConfig, run_scenario, scenario_json
from .live import IntakeError, LiveService, parse_time
from .store import Store
from .whatsapp import WhatsAppBot, WhatsAppConfig, make_router

log = logging.getLogger("upstream")

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard" / "static"
INTAKE_DIR = Path(__file__).resolve().parent / "static"
HOUSEHOLD_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
PHOTO_RE = re.compile(r"^[0-9a-f]{64}\.jpg$")
MAX_UPLOAD = 20 * 1024 * 1024


class ManualReport(BaseModel):
    household: str
    turbidity: float | None = Field(default=None, description="turbidity index 0..1")
    chlorine: float | None = Field(default=None, description="free chlorine, mg/L")
    answer: str | None = None
    time: str | None = Field(default=None, description="ISO time; default now")


class Answer(BaseModel):
    answer: str


class SupplyStart(BaseModel):
    time: str | None = None


def load_or_build_scenario(var: Path, path: Path | None) -> tuple[dict, Path]:
    """The replay JSON: an existing file, or built once and cached in ``var``."""
    if path and path.exists():
        return json.loads(path.read_text()), path
    cached = var / "scenario.json"
    if cached.exists():
        return json.loads(cached.read_text()), cached
    log.info("building the demo scenario (first run only)…")
    cfg = ScenarioConfig(seed=7, min_downstream_homes=6)
    data = scenario_json(run_scenario(cfg))
    var.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(data, separators=(",", ":")))
    return data, cached


def scenario_config(data: dict) -> ScenarioConfig:
    cfg = dict(data["meta"]["config"])
    cfg["strengths"] = tuple(cfg["strengths"])
    return ScenarioConfig(**cfg)


def create_app(var_dir: str | Path | None = None, scenario_path: str | Path | None = None,
               whatsapp: WhatsAppConfig | None = None, wa_client=None) -> FastAPI:
    var = Path(var_dir or os.environ.get("UPSTREAM_VAR", "var"))
    sp = scenario_path or os.environ.get("UPSTREAM_SCENARIO")
    if sp is None and Path("data/scenarios/demo.json").exists():
        sp = "data/scenarios/demo.json"
    data, data_path = load_or_build_scenario(var, Path(sp) if sp else None)
    # Live intake runs on the same ward: rebuild its learned state (no outbreak).
    cfg = scenario_config(data)
    cfg.outbreak_cycles = 0
    cfg.frames = False
    ward = run_scenario(cfg)
    cal_path = os.environ.get("UPSTREAM_TURBIDITY_CALIBRATION")
    calibration = TurbidityCalibration.load(cal_path) if cal_path else None
    live = LiveService(ward, Store(var / "upstream.sqlite3"), var / "photos", calibration=calibration)
    bot = WhatsAppBot(live, whatsapp or WhatsAppConfig.from_env(), client=wa_client)

    app = FastAPI(title="Upstream", description="Seismology for sewage — first-glass intake and breach locator.",
                  version="0.1.0")
    app.add_middleware(GZipMiddleware, minimum_size=2048)
    app.state.live = live
    app.state.scenario_path = data_path
    app.state.bot = bot
    app.include_router(make_router(bot))

    def fail(e: IntakeError):
        return JSONResponse({"error": str(e), **e.detail}, status_code=e.status)

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/dashboard/")

    @app.get("/report", include_in_schema=False)
    def report_page():
        return HTMLResponse((INTAKE_DIR / "report.html").read_text())

    @app.get("/api/health")
    def health():
        return {"service": "upstream", "ok": True, "households": len(live.households),
                "whatsapp": bot.cfg.configured, "turbidity_calibrated": calibration is not None}

    @app.get("/api/scenario")
    def scenario():
        return FileResponse(data_path, media_type="application/json")

    @app.get("/api/households")
    def households():
        st = ward.streets
        return [h.public(st) | {"arrival_min": live.arrival[h.id]} for h in ward.world.households]

    async def _read_upload(photo: UploadFile) -> bytes:
        data = await photo.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "photo is larger than 20 MB")
        if not data:
            raise HTTPException(400, "empty upload")
        return data

    @app.post("/api/read")
    async def read_only(photo: UploadFile = File(...)):
        """Read a photo without storing it (the live glass test)."""
        return live.read(await _read_upload(photo)).to_dict()

    @app.post("/api/photo")
    async def submit_photo(photo: UploadFile = File(...), household: str | None = Form(None),
                           answer: str | None = Form(None)):
        data = await _read_upload(photo)
        if household is not None:
            household = household.strip() or None
        if household is not None and not HOUSEHOLD_RE.match(household):
            raise HTTPException(400, "invalid household id")
        try:
            return live.submit_photo(data, household=household, answer=answer or None)
        except IntakeError as e:
            return fail(e)

    @app.post("/api/reports")
    def submit_manual(body: ManualReport):
        at = parse_time(body.time) if body.time else None
        if body.time and at is None:
            raise HTTPException(400, "time must be ISO 8601")
        try:
            return live.submit_manual(body.household, body.turbidity, body.chlorine, body.answer, at=at)
        except IntakeError as e:
            return fail(e)

    @app.post("/api/reports/{report_id}/answer")
    def set_answer(report_id: str, body: Answer):
        try:
            return live.set_answer(report_id, body.answer)
        except IntakeError as e:
            return fail(e)

    @app.get("/api/live")
    def live_state():
        return live.state()

    @app.post("/api/live/supply")
    def start_supply(body: SupplyStart | None = None):
        at = parse_time(body.time) if body and body.time else None
        if body and body.time and at is None:
            raise HTTPException(400, "time must be ISO 8601")
        return live.start_supply(at)

    @app.post("/api/live/reset")
    def reset():
        live.reset()
        return {"ok": True}

    @app.get("/api/evidence.json")
    def evidence_json():
        return {"state": live.state(), "rows": live.evidence_rows()}

    @app.get("/api/evidence.csv")
    def evidence_csv():
        rows = live.evidence_rows()
        buf = io.StringIO()
        cols = ["report_id", "household", "cycle", "photo_time", "delay_min", "turbidity_index", "chlorine_mg_l",
                "answer", "level", "source", "flags", "photo", "downstream_of_dig_spot", "log_likelihood_ratio"]
        w = csv.DictWriter(buf, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="upstream-evidence.csv"'})

    @app.get("/api/photos/{name}")
    def photo(name: str):
        if not PHOTO_RE.match(name):
            raise HTTPException(404)
        p = live.photo_dir / name
        if not p.exists():
            raise HTTPException(404)
        return FileResponse(p, media_type="image/jpeg")

    @app.get("/card/{household}.svg")
    def card(household: str):
        if not HOUSEHOLD_RE.match(household):
            raise HTTPException(400, "invalid household id")
        return Response(card_svg(household), media_type="image/svg+xml")

    @app.get("/card/{household}.png")
    def card_png(household: str, dpi: int = Query(300, ge=72, le=600)):
        if not HOUSEHOLD_RE.match(household):
            raise HTTPException(400, "invalid household id")
        img = render_card(household, ppm=dpi / 25.4)
        ok, buf = cv2.imencode(".png", cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
        return Response(buf.tobytes(), media_type="image/png")

    @app.get("/api/qr.png")
    def qr_png(data: str = Query(..., min_length=1, max_length=300)):
        """A QR code (e.g. for judges' phones to open the household page)."""
        bits = qr_bits(data)
        img = np.where(bits, 0, 255).astype(np.uint8)
        img = cv2.resize(img, None, fx=8, fy=8, interpolation=cv2.INTER_NEAREST)
        ok, buf = cv2.imencode(".png", img)
        return Response(buf.tobytes(), media_type="image/png")

    @app.get("/api/demo-photo")
    def demo_photo(household: str = "H-0001", turbidity: float = Query(0.0, ge=0, le=200),
                   chlorine: float | None = Query(0.5, ge=0, le=4), seed: int = 0):
        """A synthetic first-glass photo, for trying the pipeline without a printer."""
        if not HOUSEHOLD_RE.match(household):
            raise HTTPException(400, "invalid household id")
        data = render_photo(household, PhotoParams(turbidity=turbidity, chlorine=chlorine), seed=seed)
        return Response(data, media_type="image/jpeg")

    @app.get("/dashboard/card.svg", include_in_schema=False)
    def dashboard_card():
        return Response(card_svg("H-0001"), media_type="image/svg+xml")

    app.mount("/dashboard", StaticFiles(directory=DASHBOARD_DIR, html=True), name="dashboard")
    return app


def __getattr__(name: str):
    # ``uvicorn upstream.intake.app:app`` builds the app lazily on first access.
    if name == "app":
        global app
        app = create_app()
        return app
    raise AttributeError(name)
