"""Sentinel planner: spend strips and recruit homes where they add the most
information (README §5.5 amber step, §5.8 sentinel planner)."""

from __future__ import annotations

import math

import numpy as np

from .triangulate import Locator

DETECT_STRENGTH = 0.6
DETECT_SCALE = 0.1
P_FALSE_POS = 0.03
P_FALSE_NEG = 0.05


def _hb(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-12, 1 - 1e-12)
    return -(p * np.log2(p) + (1 - p) * np.log2(1 - p))


def detection_prob(A: np.ndarray) -> np.ndarray:
    """Chance that a tap with dilution ``A`` shows a clear dirty reading."""
    return P_FALSE_POS + (1 - P_FALSE_POS - P_FALSE_NEG) * (1 - np.exp(-DETECT_STRENGTH * A / DETECT_SCALE))


def info_gain(locator: Locator, joint: np.ndarray, nodes: np.ndarray) -> tuple[float, float]:
    """Expected information (bits) about the breach location from one more
    reading at a point given by its node in each tree, and the probability
    that point is downstream of the breach."""
    p1 = 0.0
    cond = 0.0
    p_down = 0.0
    for k, node in enumerate(nodes):
        if node < 0:
            q = np.full(joint.shape[1], P_FALSE_POS)
        else:
            down, dist, junc = locator.response(k, np.asarray([node]))
            d = down[:, 0]
            A = np.where(d, locator.cfg.physics.dilution(np.where(d, dist[:, 0], 0.0), np.where(d, junc[:, 0], 0)), 0.0)
            q = detection_prob(A)
            p_down += float(joint[k] @ d)
        p1 += float(joint[k] @ q)
        cond += float(joint[k] @ _hb(q))
    return float(_hb(np.asarray(p1)) - cond), p_down


def rank_households(locator: Locator, joint: np.ndarray, households: list[str],
                    exclude: set[str] | frozenset = frozenset(), top: int | None = None) -> list[dict]:
    out = []
    K = len(locator.trees)
    for h in households:
        if h in exclude:
            continue
        nodes = np.asarray([locator.node_of(h, k) for k in range(K)])
        if nodes[0] < 0:
            continue
        g, pd = info_gain(locator, joint, nodes)
        out.append({"household": h, "gain_bits": round(g, 4), "p_downstream": round(pd, 4)})
    out.sort(key=lambda e: -e["gain_bits"])
    return out[:top] if top else out


def nearby_requests(locator: Locator, joint: np.ndarray, center_xy: tuple[float, float],
                    positions: dict[str, tuple[float, float]], exclude=frozenset(),
                    radius_m: float = 400.0, n: int = 5, min_n: int = 3) -> list[dict]:
    """Amber step: the 3–5 nearby homes whose strip test at the next supply
    would tell us the most."""
    cx, cy = center_xy
    near = [h for h, (x, y) in positions.items() if math.hypot(x - cx, y - cy) <= radius_m]
    ranked = rank_households(locator, joint, near, exclude=exclude)
    picked = [r for r in ranked if r["gain_bits"] > 1e-3][:n]
    if len(picked) < min_n:
        picked = ranked[:min_n]
    return picked


def best_lanes(locator: Locator, joint: np.ndarray, top: int = 5) -> list[dict]:
    """Which lane would shrink the uncertainty most if one more home there
    joined (evaluated at the middle of each piped street)."""
    st = locator.streets
    edges = sorted(set(int(e) for e in locator.seg.edge))
    out = []
    for e in edges:
        nodes = locator.point_nodes(e, 0.5)
        if nodes[0] < 0:
            continue
        g, pd = info_gain(locator, joint, nodes)
        x, y = st.point_on_edge(e, 0.5)
        lat, lon = st.to_latlon(x, y)
        out.append({"street_edge": e, "lat": round(float(lat), 7), "lon": round(float(lon), 7),
                    "gain_bits": round(g, 4), "p_downstream": round(pd, 4)})
    out.sort(key=lambda r: -r["gain_bits"])
    return out[:top]


def place_sentinels(locator: Locator, candidates: list[str], k: int,
                    weights: np.ndarray | None = None) -> list[dict]:
    """Greedy sentinel placement before any alert: choose homes so that,
    between them, their clean/dirty pattern splits the (risk-weighted) prior
    over breach locations into as many equally likely groups as possible —
    i.e. maximise the entropy of the signature they would produce."""
    w = locator.prior if weights is None else weights
    labels = np.zeros(len(locator.seg), dtype=np.int64)
    downs = {}
    for h in candidates:
        n = locator.node_of(h, 0)
        if n >= 0:
            downs[h] = locator.response(0, np.asarray([n]))[0][:, 0]
    chosen: list[dict] = []
    current = 0.0
    for _ in range(min(k, len(downs))):
        best = None
        for h, d in downs.items():
            if any(c["household"] == h for c in chosen):
                continue
            _, inv = np.unique(labels * 2 + d, return_inverse=True)
            p = np.bincount(inv.ravel(), weights=w)
            p = p[p > 0] / p.sum()
            H = float(-(p * np.log2(p)).sum())
            if best is None or H > best[0]:
                best = (H, h, inv.ravel())
        if best is None or best[0] <= current + 1e-9:
            break
        current = best[0]
        labels = best[2]
        chosen.append({"household": best[1], "signature_bits": round(current, 4)})
    return chosen
