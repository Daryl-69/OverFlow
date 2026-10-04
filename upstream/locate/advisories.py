"""Household safe-after times and boil-water advisories (README §5.8).

The model knows, for every candidate breach, whether the dirty front reaches a
home and how long the slug takes to pass it. Averaging over the posterior
gives each home the probability it is affected and a conservative (90th
percentile) time after which its tap runs clean. Homes upstream of the
suspected breach are not alarmed.
"""

from __future__ import annotations

import math

import numpy as np

from ..reports import clock
from .triangulate import Posterior

AFFECTED_THRESHOLD = 0.10
MARGIN_MIN = 5.0
QUANTILE = 0.9


def ampm(t_min: float) -> str:
    m = int(math.ceil(t_min - 1e-9)) % 1440
    h, mm = divmod(m, 60)
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}:{mm:02d} {suffix}"


def _weighted_quantile(values: np.ndarray, weights: np.ndarray, q: float) -> float:
    order = np.argsort(values)
    v, w = values[order], weights[order]
    c = np.cumsum(w)
    return float(v[min(int(np.searchsorted(c, q * c[-1])), len(v) - 1)])


def household_advisory(post: Posterior, household: str, arrival_min: float, next_start: float,
                       supply_duration: float, threshold: float = AFFECTED_THRESHOLD,
                       margin: float = MARGIN_MIN) -> dict:
    loc = post.locator
    K = len(loc.trees)
    p_aff = 0.0
    vals, wts = [], []
    for k in range(K):
        node = loc.node_of(household, k)
        if node < 0:
            continue
        down, dist, _ = loc.response(k, np.asarray([node]))
        d = down[:, 0]
        w = post.joint[k] * d
        p_aff += float(w.sum())
        if d.any():
            vals.append(loc.cfg.physics.clear_after(dist[d, 0]))
            wts.append(w[d])
    out = {"household": household, "p_affected": round(p_aff, 4)}
    if p_aff < threshold or not vals:
        out.update(status="clear", message=None)
        return out
    clear = _weighted_quantile(np.concatenate(vals), np.concatenate(wts), QUANTILE)
    safe = next_start + arrival_min + clear + margin
    safe = math.ceil(safe - 1e-9)
    out["first_water"] = round(next_start + arrival_min, 1)
    out["safe_after"] = float(safe)
    out["safe_after_clock"] = clock(safe)
    if safe - next_start > supply_duration:
        out["status"] = "no_safe_window"
        out["message"] = ("Upstream water alert: today's supply may stay contaminated. Use stored or boiled "
                          "water for drinking and cooking, and boil all drinking water until the all-clear.")
    else:
        out["status"] = "advisory"
        out["message"] = (f"Upstream water alert: today, fill drinking water after {ampm(safe)}; use the first "
                          f"fill for cleaning. Boil drinking water until the all-clear.")
    return out


def advisories(post: Posterior, households: list[str], arrival: dict[str, float], next_start: float,
               supply_duration: float, **kw) -> list[dict]:
    out = []
    for h in households:
        if h not in arrival:
            continue
        out.append(household_advisory(post, h, arrival[h], next_start, supply_duration, **kw))
    return out
