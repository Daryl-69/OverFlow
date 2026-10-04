"""Green / amber / red signal levels (README §5.5)."""

from __future__ import annotations

import numpy as np

from ..reports import Report
from .baseline import Baselines

CHLORINE_MIN = 0.2         # mg/L, IS 10500 minimum free residual at the consumer end
CHLORINE_NEAR_ZERO = 0.1   # mg/L, the lowest step on common strip charts above zero
TURB_SPIKE_Z = 3.0         # robust z-score against the tap's own baseline
TURB_SPIKE_MIN = 0.05      # and at least this much absolute rise in turbidity index
CLUSTER_RADIUS_M = 150.0
CLUSTER_MIN = 3

GREEN, AMBER, RED = "green", "amber", "red"


def classify(report: Report, baselines: Baselines) -> tuple[str, list[str]]:
    """Level of a single reading against its tap's baseline."""
    b = baselines.get(report.household)
    reasons: list[str] = []
    spike = False
    if report.turbidity is not None and b.turb_med is not None:
        rise = report.turbidity - b.turb_med
        z = rise / b.turb_sd
        if z >= TURB_SPIKE_Z and rise >= TURB_SPIKE_MIN:
            spike = True
            reasons.append(f"turbidity spike (+{rise:.2f}, {z:.0f}σ above this tap's normal)")
    low_cl = near_zero = False
    if report.chlorine is not None:
        if report.chlorine < CHLORINE_MIN:
            low_cl = True
            reasons.append(f"free chlorine {report.chlorine:.2f} mg/L is below 0.2 mg/L")
        if report.chlorine <= CHLORINE_NEAR_ZERO:
            near_zero = True
    if near_zero and spike:
        return RED, reasons
    if low_cl or spike:
        return AMBER, reasons
    return GREEN, reasons


def complaint_clusters(reports: list[Report], positions: dict[str, tuple[float, float]],
                       radius_m: float = CLUSTER_RADIUS_M, min_count: int = CLUSTER_MIN) -> list[set[str]]:
    """Groups of at least ``min_count`` complaint reports (smell, colour,
    illness) from different homes lying within ``radius_m`` of one another
    (single-linkage). Returns sets of report ids."""
    comp = [r for r in reports if r.complaint and r.household in positions]
    # one complaint per home counts towards a cluster
    seen: dict[str, Report] = {}
    for r in comp:
        seen.setdefault(r.household, r)
    comp = list(seen.values())
    if len(comp) < min_count:
        return []
    xy = np.asarray([positions[r.household] for r in comp], dtype=float)
    d = np.hypot(xy[:, None, 0] - xy[None, :, 0], xy[:, None, 1] - xy[None, :, 1])
    near = d <= radius_m
    label = -np.ones(len(comp), dtype=int)
    clusters = []
    for i in range(len(comp)):
        if label[i] >= 0:
            continue
        stack = [i]
        label[i] = len(clusters)
        members = []
        while stack:
            u = stack.pop()
            members.append(u)
            for v in np.flatnonzero(near[u] & (label < 0)):
                label[v] = label[i]
                stack.append(int(v))
        clusters.append(members)
    return [{comp[i].id for i in m} for m in clusters if len(m) >= min_count]


def apply_levels(reports: list[Report], baselines: Baselines,
                 positions: dict[str, tuple[float, float]]) -> list[set[str]]:
    """Set ``level`` and ``reasons`` on every report in place, including the
    complaint-cluster rule, and return the clusters found."""
    for r in reports:
        r.level, r.reasons = classify(r, baselines)
    clusters = complaint_clusters(reports, positions)
    in_cluster = set().union(*clusters) if clusters else set()
    for r in reports:
        if r.id in in_cluster and r.level != RED:
            r.level = RED
            r.reasons = r.reasons + ["part of a cluster of smell / colour / illness reports"]
    return clusters
