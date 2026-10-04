"""Headline numbers (README §6): across many simulated outbreaks, how often
does Upstream raise an alert, and how close does its dig spot get to the
real breach — with the utility's pipe map and with none?

Every run draws a fresh ward layout, households, hydraulics and breach.
Breaches are drawn over all pipes (weighted by risk), including ones with no
participating home downstream, which no method could see. Localisation is
scored on *detected* outbreaks: a red alert was raised and at least one home
downstream of the real breach reported an amber or red reading. Alerts
without that are counted as false alarms.
"""

from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace

import numpy as np

from .scenario import ScenarioConfig, run_scenario


def _one(cfg: ScenarioConfig) -> dict:
    sc = run_scenario(cfg)
    f = sc.final
    reports = sc.outbreak_reports()
    red_cycles = sorted({r.cycle for r in reports if r.level == "red"})
    tree = sc.world.tree
    piece = tree.piece_at(sc.breach.edge, sc.breach.frac)
    nodes = np.asarray([tree.attach(h.edge, h.frac) for h in sc.world.households])
    down = {h.id for h, d in zip(sc.world.households, tree.downstream(np.asarray([piece]), nodes)[0]) if d}
    signal = any(r.household in down and r.level in ("amber", "red") for r in reports)
    out = {
        "seed": cfg.seed,
        "alert": bool(f.get("alert")),
        "true_alert": bool(f.get("alert")) and signal,
        "first_red_cycle": (red_cycles[0] - sc.breach.start_cycle + 1) if red_cycles else None,
        "downstream_homes": int(tree.downstream(np.asarray([piece]), nodes).sum()),
        "complaints": f.get("complaints"),
    }
    for net in ("gis", "rebuilt"):
        if net in f:
            g = f[net]
            out[net] = {"error_m": g["error_m"], "truth_in_region": g["truth_in_region"],
                        "region_m": g["region_length_m"], "ring_m": g["ring"]["radius_m"]}
    return out


def _summ(rows: list[dict], net: str) -> dict:
    v = [r[net] for r in rows if net in r]
    if not v:
        return {}
    err = np.asarray([x["error_m"] for x in v])
    return {
        "median_error_m": round(float(np.median(err)), 1),
        "p90_error_m": round(float(np.percentile(err, 90)), 1),
        "within_50m_rate": round(float(np.mean(err <= 50)), 3),
        "truth_in_region_rate": round(float(np.mean([x["truth_in_region"] for x in v])), 3),
        "median_region_m": round(float(np.median([x["region_m"] for x in v])), 1),
        "median_ring_m": round(float(np.median([x["ring_m"] for x in v])), 1),
    }


def evaluate(runs: int = 60, seed0: int = 1000, workers: int | None = None, **cfg_kw) -> dict:
    base = ScenarioConfig(frames=False, **cfg_kw)
    cfgs = [replace(base, seed=seed0 + i) for i in range(runs)]
    workers = workers or max(1, min(os.cpu_count() or 1, 8))
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            rows = list(ex.map(_one, cfgs))
    else:
        rows = [_one(c) for c in cfgs]
    alerted = [r for r in rows if r["alert"]]
    detected = [r for r in rows if r["true_alert"]]
    detectable = [r for r in rows if r["downstream_homes"] > 0]
    first = [r["first_red_cycle"] for r in detected if r["first_red_cycle"] is not None]
    tick = [r["complaints"]["first_ticket_error_m"] for r in detected
            if r["complaints"] and r["complaints"]["first_ticket_error_m"] is not None]
    cent = [r["complaints"]["centroid_error_m"] for r in detected
            if r["complaints"] and r["complaints"]["centroid_error_m"] is not None]
    return {
        "runs": runs,
        "outbreak_cycles": base.outbreak_cycles,
        "min_downstream_homes": base.min_downstream_homes,
        "households": base.n_households,
        "ensemble": base.ensemble,
        "ensemble_mode": base.ensemble_mode,
        "alerts": len(alerted),
        "detectable": len(detectable),
        "detected": len(detected),
        "detection_rate": round(len(detected) / max(len(detectable), 1), 3),
        "false_alarms": len(alerted) - len(detected),
        "false_alarm_rate": round((len(alerted) - len(detected)) / runs, 3),
        "alert_in_first_supply_rate": round(float(np.mean([c == 1 for c in first])) if first else 0.0, 3),
        "gis": _summ(detected, "gis"),
        "rebuilt": _summ(detected, "rebuilt"),
        "complaints": {
            "median_first_ticket_error_m": round(float(np.median(tick)), 1) if tick else None,
            "median_centroid_error_m": round(float(np.median(cent)), 1) if cent else None,
        },
        "rows": rows,
    }
