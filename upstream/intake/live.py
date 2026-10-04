"""Live intake: turns photos (or manual readings) into reports, assigns signal
levels, and keeps the utility's live picture — triangulation, safe-after
advisories and strip requests — up to date.

The live ward is the scenario's ward: its households, pipe map, learned
baselines and travel times. Live supply cycles are numbered from 0 and timed
in minutes from a live epoch (local midnight of the first live supply day).
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..locate.advisories import advisories, ampm
from ..locate.levels import AMBER, GREEN, RED, apply_levels
from ..locate.planner import best_lanes, nearby_requests
from ..reader.core import ReadResult, TurbidityCalibration, hamming, read_photo
from ..reports import ANSWERS, Report
from ..scenario import Scenario
from .store import Store

REUSE_PHASH_BITS = 4
EXIF_MAX_SKEW_H = 6.0
SUPPLY_STALE_H = 12.0
WINDOW_CYCLES = 3
EXCLUDE_FLAGS = frozenset({"duplicate_photo", "possible_reused_photo"})


def local_tz():
    """The ward's time zone: ``UPSTREAM_TZ`` (e.g. ``Asia/Kolkata``) if set,
    else the server's. Phones write EXIF times without a zone, so this must
    match the households' clocks."""
    name = os.environ.get("UPSTREAM_TZ")
    if name:
        return ZoneInfo(name)
    return datetime.now().astimezone().tzinfo


def now() -> datetime:
    return datetime.now(local_tz())


def parse_time(s: str | None) -> datetime | None:
    """ISO time; values without a zone are taken in the ward's time zone."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz())
    return dt


class IntakeError(ValueError):
    def __init__(self, message: str, status: int = 400, detail: dict | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail or {}


def household_message(level: str | None, reasons: list[str], advisory: dict | None) -> str:
    if level == RED:
        msg = ("Your water shows signs of sewage contamination. Do not drink it without boiling. "
               "The water department has been alerted.")
    elif level == AMBER:
        msg = ("Your reading is unusual. Please test again at the next supply, and boil drinking "
               "water until then.")
    else:
        msg = "Thank you — your reading looks normal for your tap. It helps keep the network map up to date."
    if advisory and advisory.get("message"):
        msg += " " + advisory["message"]
    return msg


class LiveService:
    def __init__(self, scenario: Scenario, store: Store, photo_dir: str | Path,
                 calibration: TurbidityCalibration | None = None) -> None:
        self.sc = scenario
        self.store = store
        self.photo_dir = Path(photo_dir)
        self.photo_dir.mkdir(parents=True, exist_ok=True)
        self.calibration = calibration
        self.households = {h.id for h in scenario.world.households}
        self.positions = scenario.positions()
        self.arrival = scenario.arrival()
        self.supply = scenario.world.supply
        self._lock = threading.RLock()
        self._state: dict | None = None

    # ------------------------------------------------------------ clock
    def epoch(self, at: datetime | None = None) -> datetime:
        e = self.store.get_meta("epoch")
        if e:
            return datetime.fromisoformat(e)
        at = at or now()
        ep = at.replace(hour=0, minute=0, second=0, microsecond=0)
        self.store.set_meta("epoch", ep.isoformat())
        return ep

    def to_min(self, dt: datetime) -> float:
        return (dt - self.epoch(dt)).total_seconds() / 60.0

    def from_min(self, m: float) -> datetime:
        return self.epoch() + timedelta(minutes=m)

    def start_supply(self, at: datetime | None = None) -> dict:
        with self._lock:
            at = at or now()
            cycle = self.store.add_supply(self.to_min(at), at.isoformat())
            self._state = None
            return self.store.current_supply() | {"cycle": cycle}

    def _ensure_supply(self, t: datetime, household: str) -> dict:
        cur = self.store.current_supply()
        t_min = self.to_min(t)
        if cur is not None and -30 <= t_min - cur["start_min"] <= SUPPLY_STALE_H * 60:
            return cur
        # No supply announced: assume this home's first glass came at its usual delay.
        b = self.sc.baselines.get(household)
        delay = b.delay_med if b.delay_med is not None else 15.0
        start = t - timedelta(minutes=delay)
        self.store.add_supply(self.to_min(start), start.isoformat())
        return self.store.current_supply()

    # ------------------------------------------------------------ intake
    def read(self, data: bytes) -> ReadResult:
        return read_photo(data, calibration=self.calibration)

    def submit_photo(self, data: bytes, household: str | None = None, answer: str | None = None,
                     received_at: datetime | None = None, source: str = "photo",
                     message_time: datetime | None = None) -> dict:
        received_at = received_at or now()
        res = self.read(data)
        if not res.ok:
            raise IntakeError("retake", status=422, detail={"issues": res.issues, "warnings": res.warnings,
                                                            "reading": res.to_dict()})
        flags: list[str] = []
        hid = res.household_id or household
        if res.household_id and household and household != res.household_id:
            flags.append("household_mismatch")
        if not hid:
            raise IntakeError("household unknown: the card's QR code could not be read and no household was given",
                              status=422, detail={"reading": res.to_dict()})
        if hid not in self.households:
            raise IntakeError(f"unknown household {hid}", status=404)
        taken = None
        if res.taken_at:
            exif_t = parse_time(res.taken_at)
            ref_t = message_time or received_at
            if exif_t is not None and abs((exif_t - ref_t).total_seconds()) <= EXIF_MAX_SKEW_H * 3600:
                taken = exif_t
            else:
                flags.append("photo_time_ignored")
        t = taken or message_time or received_at
        with self._lock:
            for prev in self.store.find_photo(sha256=res.sha256, taken_at=res.taken_at):
                if prev["sha256"] == res.sha256:
                    flags.append("duplicate_photo")
                    break
                if res.taken_at and prev["taken_at"] == res.taken_at and prev["household"] != hid:
                    flags.append("possible_reused_photo")
                    break
            if res.phash and "duplicate_photo" not in flags and "possible_reused_photo" not in flags:
                for prev in self.store.phashes():
                    if hamming(prev["phash"], res.phash) <= REUSE_PHASH_BITS:
                        flags.append("possible_reused_photo")
                        break
            photo_path = self.photo_dir / f"{res.sha256}.jpg"
            if not photo_path.exists():
                photo_path.write_bytes(data)
            chlorine = res.chlorine["mg_per_l"] if res.chlorine else None
            return self._ingest(hid, res.turbidity["index"], chlorine, answer, t, received_at, source,
                                reading=res.to_dict(), photo=photo_path.name, sha=res.sha256, ph=res.phash,
                                taken_at=res.taken_at, flags=flags)

    def submit_manual(self, household: str, turbidity: float | None, chlorine: float | None,
                      answer: str | None = None, at: datetime | None = None, source: str = "manual") -> dict:
        if household not in self.households:
            raise IntakeError(f"unknown household {household}", status=404)
        if turbidity is not None and not 0.0 <= turbidity <= 1.0:
            raise IntakeError("turbidity index must be between 0 and 1")
        if chlorine is not None and not 0.0 <= chlorine <= 10.0:
            raise IntakeError("chlorine must be between 0 and 10 mg/L")
        with self._lock:
            t = at or now()
            return self._ingest(household, turbidity, chlorine, answer, t, now(), source)

    def set_answer(self, report_id: str, answer: str) -> dict:
        if answer not in ANSWERS:
            raise IntakeError(f"answer must be one of {', '.join(ANSWERS)}")
        with self._lock:
            if not self.store.set_answer(report_id, answer):
                raise IntakeError(f"unknown report {report_id}", status=404)
            self._state = None
            return self.state()

    def _ingest(self, household: str, turbidity, chlorine, answer, t: datetime, received_at: datetime,
                source: str, reading=None, photo=None, sha=None, ph=None, taken_at=None, flags=()) -> dict:
        if answer is not None and answer not in ANSWERS:
            raise IntakeError(f"answer must be one of {', '.join(ANSWERS)}")
        sup = self._ensure_supply(t, household)
        r = Report(id=self.store.next_report_id(sup["cycle"]), household=household, cycle=sup["cycle"],
                   supply_start=sup["start_min"], t_obs=self.to_min(t),
                   turbidity=None if turbidity is None else float(turbidity),
                   chlorine=None if chlorine is None else float(chlorine),
                   answer=answer, source=source, flags=list(flags), reading=reading)
        self.store.add_report(r, received_at.isoformat(), photo=photo, sha256=sha, phash=ph, taken_at=taken_at)
        self._state = None
        state = self.state()
        mine = next(x for x in state["reports"] if x["id"] == r.id)
        adv = next((a for a in state.get("advisories", []) if a["household"] == household), None)
        return {"report": mine, "level": mine["level"], "reasons": mine["reasons"],
                "message": household_message(mine["level"], mine["reasons"], adv),
                "advisory": adv, "alert": state["alert"]}

    # ------------------------------------------------------------- state
    def reset(self) -> None:
        with self._lock:
            self.store.reset()
            self._state = None

    def state(self) -> dict:
        with self._lock:
            if self._state is None:
                self._state = self._compute()
            return self._state

    def _compute(self) -> dict:
        cur = self.store.current_supply()
        if cur is None:
            return {"supply": None, "reports": [], "alert": False, "posterior": None, "advisories": [],
                    "requests": [], "lanes": []}
        reps = self.store.reports(min_cycle=cur["cycle"] - (WINDOW_CYCLES - 1))
        by: dict[int, list[Report]] = {}
        for r in reps:
            by.setdefault(r.cycle, []).append(r)
        for rs in by.values():
            apply_levels(rs, self.sc.baselines, self.positions)
        self.store.update_levels(reps)
        usable = [r for r in reps if not (set(r.flags) & EXCLUDE_FLAGS)]
        alert = any(r.level == RED for r in usable)
        gis = self.sc.gis
        post = gis.locate(usable) if usable else None
        out: dict = {"supply": {**cur, "clock": self.from_min(cur["start_min"]).isoformat()},
                     "alert": alert, "posterior": None, "advisories": [], "requests": [], "lanes": []}
        if post is not None and alert:
            s = post.summary()
            out["posterior"] = {"post": post.sparse(2e-4), "ring": s["ring"], "dig": s["dig"],
                                "region": s["region"], "region_length_m": s["region_length_m"],
                                "p_breach": s["p_breach"], "log10_bf": s["log10_bayes_factor"],
                                "n_reports": s["n_reports"], "evidence": post.evidence[:12]}
            next_start = cur["start_min"] + self.supply.interval_days * 1440
            adv = advisories(post, sorted(self.households), self.arrival, next_start, self.supply.duration_min)
            for a in adv:
                if "safe_after" in a:
                    a["safe_after_iso"] = self.from_min(a["safe_after"]).isoformat()
                    a["safe_after_ampm"] = ampm(a["safe_after"])
            out["advisories"] = [a for a in adv if a["status"] != "clear"]
            out["lanes"] = best_lanes(gis, post.joint, top=5)
        if post is not None:
            seen: set[str] = set()
            for r in reps:
                if r.cycle == cur["cycle"] and r.level == AMBER:
                    for p in nearby_requests(gis, post.joint, self.positions[r.household], self.positions,
                                             exclude={r.household}):
                        if p["household"] not in seen:
                            seen.add(p["household"])
                            out["requests"].append({**p, "reason": f"amber reading at {r.household}"})
        out["reports"] = []
        for r in reps:
            d = r.to_dict(include_truth=False)
            d["time"] = self.from_min(r.t_obs).isoformat()
            d["delay_min"] = round(r.delay, 1)
            d["excluded"] = bool(set(r.flags) & EXCLUDE_FLAGS)
            out["reports"].append(d)
        out["counts"] = {lv: sum(1 for r in reps if r.level == lv and r.cycle == cur["cycle"])
                         for lv in (GREEN, AMBER, RED)}
        return out

    def evidence_rows(self) -> list[dict]:
        state = self.state()
        post = state.get("posterior") or {}
        llr = {e["report"]: e for e in post.get("evidence", [])}
        rows = []
        for d in self.store.rows():
            rows.append({
                "report_id": d["id"], "household": d["household"], "cycle": d["cycle"],
                "photo_time": self.from_min(d["t_obs"]).isoformat(),
                "delay_min": round(d["t_obs"] - d["supply_start"], 1),
                "turbidity_index": d["turbidity"], "chlorine_mg_l": d["chlorine"], "answer": d["answer"],
                "level": d["level"], "source": d["source"], "flags": d["flags"], "photo": d["photo"],
                "downstream_of_dig_spot": llr.get(d["id"], {}).get("downstream_of_top"),
                "log_likelihood_ratio": llr.get(d["id"], {}).get("log_lr"),
            })
        return rows
