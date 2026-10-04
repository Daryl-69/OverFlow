"""Triangulation engine (README §5.7).

Every pipe is split into pieces of at most 20 m; each piece is a candidate
breach. For each candidate the model predicts which reporting taps the dirty
front reaches and how diluted it is there, and scores how well that matches
the readings::

    posterior[c] ∝ prior[c] · mean_trees Π_cycles Σ_S p(S) Π_reports p(report | c, S, tree)

* ``S`` is the slug strength in a cycle. It is unknown and marginalised on a
  grid that includes 0, because a crack need not leak every cycle.
* Each report is a mixture of the physical model and a broad outlier
  component, so a few pranks or bad photos barely move the result.
* Timing enters through the model branch: a "first glass" photographed when
  water could not yet have arrived is mostly explained as an outlier.
* Which way water runs round a loop is itself uncertain when it has to be
  learned from photo timings, so the likelihood is averaged over an ensemble
  of plausible flow trees (bootstrap refits of the timing model).

Plan B (the pure graph rule) is :func:`plan_b`.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

from ..network import FlowTree
from ..physics import SlugPhysics
from ..reports import COMPLAINTS, Report
from ..sim.streets import StreetGraph
from .baseline import Baselines
from .traveltime import TravelTimeFit

LOG_2PI = math.log(2 * math.pi)
LEVEL_RANK = {"red": 3, "amber": 2, "green": 1}


@dataclass
class LocatorConfig:
    physics: SlugPhysics = field(default_factory=SlugPhysics)
    outlier_prob: float = 0.03
    strengths: tuple[float, ...] = tuple(float(v) for v in np.geomspace(0.03, 2.0, 40))
    zero_strength_weight: float = 0.1
    model_error: float = 0.35        # relative uncertainty of predicted contamination at a tap
    complaint_floor: float = 0.03
    complaint_scale: float = 0.2
    risk_weight: float = 1.5
    credible: float = 0.9
    timing_sd_model: float = 6.0     # minutes, homes without their own timing history
    timing_span: float = 270.0       # minutes, outlier timing density is uniform over this
    cl_zero: float = 0.02            # strip readings at or below this count as zero
    prior_breach: float = 0.5        # prior probability that an alert is a real breach


def _log_erfc(x: np.ndarray) -> np.ndarray:
    """log(erfc(x)) without underflow (Numerical Recipes erfcc, rel. err < 1.2e-7)."""
    x = np.asarray(x, dtype=float)
    z = np.abs(x)
    t = 1.0 / (1.0 + 0.5 * z)
    poly = (-1.26551223 + t * (1.00002368 + t * (0.37409196 + t * (0.09678418 + t * (
        -0.18628806 + t * (0.27886807 + t * (-1.13520398 + t * (1.48851587 + t * (
            -0.82215223 + t * 0.17087277)))))))))
    log_r = np.log(t) - z * z + poly
    return np.where(x >= 0, log_r, np.log(2.0 - np.exp(log_r)))


def _log_ndtr(z: np.ndarray) -> np.ndarray:
    return math.log(0.5) + _log_erfc(-np.asarray(z, dtype=float) / math.sqrt(2.0))


def _lognorm(x, m, s):
    return -0.5 * ((x - m) / s) ** 2 - np.log(s) - 0.5 * LOG_2PI


@dataclass
class Segments:
    """Fixed candidate pieces along every pipe, independent of flow direction."""

    edge: np.ndarray
    frac: np.ndarray
    length: np.ndarray
    x: np.ndarray
    y: np.ndarray
    lat: np.ndarray
    lon: np.ndarray
    a_frac: np.ndarray   # segment start / end along its street edge
    b_frac: np.ndarray

    @classmethod
    def from_edges(cls, streets: StreetGraph, edges, max_piece: float = 20.0) -> "Segments":
        e_l, f_l, len_l, fa_l, fb_l = [], [], [], [], []
        for e in sorted(int(v) for v in edges):
            L = float(streets.length[e])
            n = max(1, math.ceil(L / max_piece - 1e-9))
            for j in range(n):
                e_l.append(e)
                f_l.append((j + 0.5) / n)
                len_l.append(L / n)
                fa_l.append(j / n)
                fb_l.append((j + 1) / n)
        edge = np.asarray(e_l, dtype=np.int64)
        frac = np.asarray(f_l)
        a = streets.edges[edge, 0]
        b = streets.edges[edge, 1]
        x = streets.x[a] + frac * (streets.x[b] - streets.x[a])
        y = streets.y[a] + frac * (streets.y[b] - streets.y[a])
        lat, lon = streets.to_latlon(x, y)
        return cls(edge, frac, np.asarray(len_l), x, y, lat, lon, np.asarray(fa_l), np.asarray(fb_l))

    def __len__(self) -> int:
        return len(self.edge)

    def endpoints_latlon(self, streets: StreetGraph) -> np.ndarray:
        """(n, 2, 2) [[lat, lon], [lat, lon]] for drawing."""
        a = streets.edges[self.edge, 0]
        b = streets.edges[self.edge, 1]
        out = np.empty((len(self), 2, 2))
        for k, f in enumerate((self.a_frac, self.b_frac)):
            x = streets.x[a] + f * (streets.x[b] - streets.x[a])
            y = streets.y[a] + f * (streets.y[b] - streets.y[a])
            la, lo = streets.to_latlon(x, y)
            out[:, k, 0] = la
            out[:, k, 1] = lo
        return out


@dataclass
class CycleData:
    cycle: int
    reports: list[Report]
    terms: dict
    nodes: np.ndarray            # (K, R) tree node of each report per tree, -1 if absent
    pairs: list[tuple[np.ndarray, np.ndarray, np.ndarray]]   # per tree: (seg idx, report idx, dilution)


@dataclass
class Posterior:
    locator: "Locator"
    joint: np.ndarray            # (K trees, n segments)
    prob: np.ndarray             # marginal over segments
    log_bf: float
    p_breach: float
    map_seg: int
    region: np.ndarray           # segment indices in the credible region
    region_length_m: float
    ring: dict
    n_reports: int
    cycles: list[int]
    evidence: list[dict]
    skipped: list[str]

    @property
    def segments(self) -> Segments:
        return self.locator.seg

    @property
    def map_xy(self) -> tuple[float, float]:
        return float(self.segments.x[self.map_seg]), float(self.segments.y[self.map_seg])

    def error_m(self, x: float, y: float) -> float:
        mx, my = self.map_xy
        return float(math.hypot(mx - x, my - y))

    def segment_at(self, edge: int, frac: float) -> int | None:
        seg = self.segments
        hit = np.flatnonzero((seg.edge == edge) & (seg.a_frac <= frac + 1e-12) & (seg.b_frac >= frac - 1e-12))
        return int(hit[0]) if hit.size else None

    def region_contains(self, edge: int, frac: float) -> bool:
        s = self.segment_at(edge, frac)
        return s is not None and bool(np.isin(s, self.region))

    def summary(self) -> dict:
        seg = self.segments
        i = self.map_seg
        return {
            "dig": {"lat": round(float(seg.lat[i]), 7), "lon": round(float(seg.lon[i]), 7),
                    "segment": int(i), "street_edge": int(seg.edge[i]), "prob": round(float(self.prob[i]), 4)},
            "p_breach": round(self.p_breach, 4),
            "log10_bayes_factor": round(self.log_bf / math.log(10), 2),
            "region": [int(s) for s in self.region],
            "region_length_m": round(self.region_length_m, 1),
            "ring": self.ring,
            "n_reports": self.n_reports,
            "cycles": self.cycles,
        }

    def sparse(self, min_prob: float = 1e-4) -> dict[int, float]:
        keep = np.flatnonzero(self.prob >= min_prob)
        return {int(i): round(float(self.prob[i]), 5) for i in keep}


def risk_prior(seg: Segments, risk_by_edge: dict[int, float] | None, weight: float) -> np.ndarray:
    """Prior over segments ∝ length × (1 + weight · risk of the pipe)."""
    p = seg.length.copy()
    if risk_by_edge:
        r = np.asarray([risk_by_edge.get(int(e), 0.0) for e in seg.edge])
        p *= 1.0 + weight * r
    return p / p.sum()


class Locator:
    def __init__(self, trees: list[FlowTree], homes: dict[str, tuple[int, float]], baselines: Baselines,
                 timing: TravelTimeFit | None = None, config: LocatorConfig | None = None,
                 risk_by_edge: dict[int, float] | None = None) -> None:
        if not trees:
            raise ValueError("need at least one flow tree")
        self.trees = trees
        self.streets = trees[0].streets
        self.homes = homes
        self.baselines = baselines
        self.timing = timing
        self.cfg = config or LocatorConfig()
        edges = sorted(set().union(*(set(int(e) for e in t.pipe_edges) for t in trees)))
        self.seg = Segments.from_edges(self.streets, edges, trees[0].max_piece)
        K, N = len(trees), len(self.seg)
        self.seg_child = np.full((K, N), -1, dtype=np.int64)
        self.seg_off = np.zeros((K, N))
        for k, t in enumerate(trees):
            for i in range(N):
                e, f = int(self.seg.edge[i]), float(self.seg.frac[i])
                if t.has_edge(e):
                    self.seg_child[k, i], self.seg_off[k, i] = t.locate_point(e, f)
        self.valid = self.seg_child >= 0
        self.prior = risk_prior(self.seg, risk_by_edge, self.cfg.risk_weight)
        self.log_prior = np.log(self.prior)
        self._node_cache: dict[tuple[int, str], int] = {}
        self._cycle_cache: dict[tuple, tuple] = {}
        s = np.asarray(self.cfg.strengths, dtype=float)
        w = np.full(len(s), (1.0 - self.cfg.zero_strength_weight) / len(s))
        self.S = np.concatenate([[0.0], s])
        self.log_wS = np.log(np.concatenate([[self.cfg.zero_strength_weight], w]))

    # -------------------------------------------------------------- helpers
    def node_of(self, household: str, k: int = 0) -> int:
        """Tree node of a home in tree ``k`` (-1 if it cannot be placed)."""
        key = (k, household)
        if key not in self._node_cache:
            loc = self.homes.get(household)
            node = -1
            if loc is not None and self.trees[k].has_edge(int(loc[0])):
                node = self.trees[k].attach(int(loc[0]), float(loc[1]))
            self._node_cache[key] = node
        return self._node_cache[key]

    def response(self, k: int, nodes: np.ndarray):
        """For tree ``k``: (downstream bool, pipe distance, junction count),
        each (n_segments, len(nodes)), for the given tree nodes (-1 = absent)."""
        t = self.trees[k]
        c = self.seg_child[k][:, None]
        m = np.asarray(nodes)[None, :]
        ok = self.valid[k][:, None] & (m >= 0)
        cc = np.where(c >= 0, c, 0)
        mm = np.where(m >= 0, m, 0)
        down = ok & (t.tin[mm] >= t.tin[cc]) & (t.tin[mm] <= t.tout[cc])
        dist = t.dist[mm] - t.dist[cc] + self.seg_off[k][:, None]
        junc = t.junctions[mm] - t.junctions[cc]
        return down, dist, junc

    def point_nodes(self, edge: int, frac: float) -> np.ndarray:
        """Tree node of an arbitrary point (e.g. a lane where a home might
        join) in every tree, -1 where that tree has no pipe there."""
        return np.asarray([t.attach(edge, frac) if t.has_edge(edge) else -1 for t in self.trees])

    def _prepare(self, cycle: int, reports: list[Report]) -> tuple[CycleData | None, list[str]]:
        # One glass per home per supply: its most serious reading (a later
        # muddy re-test must not be hidden behind an earlier clean one),
        # the earliest among equally serious ones.
        first: dict[str, Report] = {}
        for r in sorted(reports, key=lambda r: (-LEVEL_RANK.get(r.level, 0), r.t_obs)):
            first.setdefault(r.household, r)
        reps, skipped = [], []
        for r in first.values():
            if self.node_of(r.household, 0) < 0:
                skipped.append(r.id)
            else:
                reps.append(r)
        if not reps:
            return None, skipped
        K = len(self.trees)
        nodes = np.asarray([[self.node_of(r.household, k) for r in reps] for k in range(K)], dtype=np.int64)
        ph = self.cfg.physics
        pairs = []
        for k in range(K):
            down, dist, junc = self.response(k, nodes[k])
            si, ri = np.nonzero(down)
            A = ph.dilution(dist[si, ri], junc[si, ri])
            pairs.append((si, ri, A))

        R = len(reps)
        turb = np.zeros(R); tmed = np.zeros(R); tsd = np.ones(R); has_t = np.zeros(R, bool)
        cl = np.zeros(R); cmed = np.zeros(R); csd = np.ones(R); has_c = np.zeros(R, bool)
        ans = np.zeros(R)
        lt = np.zeros(R)
        ll_out = np.zeros(R)
        log_span = -math.log(self.cfg.timing_span)
        for i, r in enumerate(reps):
            b = self.baselines.get(r.household)
            if r.turbidity is not None and b.turb_med is not None:
                has_t[i] = True
                turb[i], tmed[i], tsd[i] = r.turbidity, b.turb_med, b.turb_sd
                # outlier turbidity: uniform on [0, 1] -> log density 0
            if r.chlorine is not None and b.cl_med is not None:
                has_c[i] = True
                cl[i], cmed[i], csd[i] = r.chlorine, b.cl_med, b.cl_sd
                ll_out[i] += math.log(0.1) if r.chlorine <= self.cfg.cl_zero else -math.log(3.0)
            if r.answer in COMPLAINTS:
                ans[i] = 1
                ll_out[i] += math.log(0.5)
            elif r.answer == "fine":
                ans[i] = -1
                ll_out[i] += math.log(0.5)
            # timing: first water reaches a tap at its usual time whatever the
            # breach, so this term weighs the model against the outlier branch
            if b.delay_med is not None:
                lt[i] = _lognorm(r.delay, b.delay_med, b.delay_sd)
            elif self.timing is not None:
                n0 = self.timing.tree.attach(*self.homes[r.household]) \
                    if self.timing.tree.has_edge(self.homes[r.household][0]) else None
                if n0 is None:
                    lt[i] = log_span
                else:
                    pred = self.timing.delta + float(self.timing.tree.T[n0])
                    lt[i] = _lognorm(r.delay, pred, self.cfg.timing_sd_model)
            else:
                lt[i] = log_span
            ll_out[i] += log_span
        terms = dict(turb=turb, tmed=tmed, tsd=tsd, has_t=has_t, cl=cl, cmed=cmed, csd=csd,
                     has_c=has_c, zero_c=has_c & (cl <= self.cfg.cl_zero), ans=ans, lt=lt,
                     ll_out=ll_out)
        return CycleData(cycle, reps, terms, nodes, pairs), skipped

    def _ll(self, s: np.ndarray, t: dict) -> np.ndarray:
        """Per-report log-likelihood (mixture) for contamination levels ``s``
        (any shape broadcasting against the report axis)."""
        ph, cfg = self.cfg.physics, self.cfg
        s = np.asarray(s, dtype=float)
        ll = np.broadcast_to(t["lt"], s.shape).copy()
        # model discrepancy: the contamination actually reaching a glass
        # varies around the prediction (local mixing, when it was filled)
        k = cfg.model_error
        sd_t = np.sqrt(t["tsd"] ** 2 + (k * ph.turbidity_per_unit * s) ** 2)
        ll += np.where(t["has_t"], _lognorm(t["turb"], t["tmed"] + ph.turbidity_per_unit * s, sd_t), 0.0)
        mean_cl = np.maximum(0.0, t["cmed"] - ph.chlorine_per_unit * s)
        sd_c = np.sqrt(t["csd"] ** 2 + (k * ph.chlorine_per_unit * s) ** 2)
        ll_cl = np.where(t["zero_c"], _log_ndtr((cfg.cl_zero - mean_cl) / sd_c),
                         _lognorm(t["cl"], mean_cl, sd_c))
        ll += np.where(t["has_c"], ll_cl, 0.0)
        cf, cs = cfg.complaint_floor, cfg.complaint_scale
        log_fine = math.log(1 - cf) - s / cs
        log_comp = np.log1p(-np.exp(log_fine))
        ll += np.where(t["ans"] > 0, log_comp, np.where(t["ans"] < 0, log_fine, 0.0))
        eps = cfg.outlier_prob
        return np.logaddexp(math.log(1 - eps) + ll, math.log(eps) + t["ll_out"])

    def _cycle_loglik(self, cd: CycleData):
        """Per tree: (log-likelihood per segment (-inf where the tree has no
        pipe), per-S table). Plus the no-breach log-likelihood.

        A report not downstream of a candidate sees s = 0 under it, exactly
        as under "no breach", so only downstream pairs need evaluating."""
        t = cd.terms
        ll0 = self._ll(np.zeros(len(cd.reports)), t)
        null = float(ll0.sum())
        n = len(self.seg)
        out = []
        for k, (si, ri, A) in enumerate(cd.pairs):
            tz = {key: v[ri] for key, v in t.items()}
            diff = self._ll(self.S[1:, None] * A[None, :], tz) - ll0[ri][None, :]
            LS = np.empty((len(self.S), n))
            LS[0] = null
            for j in range(1, len(self.S)):
                LS[j] = null + np.bincount(si, weights=diff[j - 1], minlength=n)
            tot = LS + self.log_wS[:, None]
            m = tot.max(axis=0)
            L = m + np.log(np.exp(tot - m).sum(axis=0))
            L = np.where(self.valid[k], L, -np.inf)
            out.append((L, LS))
        return out, null

    # ----------------------------------------------------------------- main
    def locate(self, reports: list[Report]) -> Posterior | None:
        by: dict[int, list[Report]] = defaultdict(list)
        for r in reports:
            by[r.cycle].append(r)
        K = len(self.trees)
        total = np.zeros((K, len(self.seg)))
        log_null = 0.0
        skipped: list[str] = []
        prepared = []
        n_used = 0
        for cyc in sorted(by):
            key = (cyc, tuple(sorted((r.id, r.t_obs, r.turbidity, r.chlorine, r.answer, r.household, r.level or "")
                                     for r in by[cyc])))
            if key not in self._cycle_cache:
                cd, sk = self._prepare(cyc, by[cyc])
                res = self._cycle_loglik(cd) if cd is not None else None
                if len(self._cycle_cache) > 64:
                    self._cycle_cache.clear()
                self._cycle_cache[key] = (cd, sk, res)
            cd, sk, res = self._cycle_cache[key]
            skipped += sk
            if cd is None:
                continue
            per_tree, null = res
            for k, (L, _) in enumerate(per_tree):
                total[k] += L
            log_null += null
            prepared.append((cd, per_tree))
            n_used += len(cd.reports)
        if not prepared:
            return None
        log_joint = self.log_prior[None, :] - math.log(K) + total
        m = float(np.max(log_joint))
        log_ev = m + math.log(float(np.exp(log_joint - m).sum()))
        joint = np.exp(log_joint - log_ev)
        prob = joint.sum(axis=0)
        log_bf = float(log_ev - log_null)
        lo = math.log(self.cfg.prior_breach / (1 - self.cfg.prior_breach)) + log_bf
        p_breach = 1.0 / (1.0 + math.exp(-lo)) if lo > -700 else 0.0

        order = np.argsort(-prob, kind="stable")
        csum = np.cumsum(prob[order])
        n_reg = int(np.searchsorted(csum, self.cfg.credible) + 1)
        reg = order[: min(n_reg, len(order))]
        seg = self.seg
        map_i = int(order[0])
        w = prob[reg] / prob[reg].sum()
        cx = float(np.sum(w * seg.x[reg]))
        cy = float(np.sum(w * seg.y[reg]))
        rad = float(np.max(np.hypot(seg.x[reg] - cx, seg.y[reg] - cy)))
        rad = max(rad + float(seg.length[reg].max()) / 2, 15.0)
        clat, clon = self.streets.to_latlon(cx, cy)
        ring = {"lat": round(float(clat), 7), "lon": round(float(clon), 7), "radius_m": round(rad, 1)}
        evidence = self._evidence(prepared, joint, map_i)
        return Posterior(locator=self, joint=joint, prob=prob, log_bf=log_bf, p_breach=p_breach,
                         map_seg=map_i, region=reg, region_length_m=float(seg.length[reg].sum()),
                         ring=ring, n_reports=n_used, cycles=[cd.cycle for cd, _ in prepared],
                         evidence=evidence, skipped=skipped)

    def _evidence(self, prepared, joint: np.ndarray, map_i: int) -> list[dict]:
        """How much each report pushed towards the top candidate versus no
        breach (log-likelihood ratio, best tree, that cycle's best strength)."""
        k = int(np.argmax(joint[:, map_i]))
        out = []
        for cd, per_tree in prepared:
            LS = per_tree[k][1]
            j = int(np.argmax(LS[:, map_i]))
            down, dist, junc = self.response(k, cd.nodes[k])
            A = np.where(down[map_i], self.cfg.physics.dilution(np.where(down[map_i], dist[map_i], 0.0),
                                                               np.where(down[map_i], junc[map_i], 0)), 0.0)
            s = self.S[j] * A
            llr = self._ll(s, cd.terms) - self._ll(np.zeros_like(s), cd.terms)
            for r, v, dn in zip(cd.reports, llr, down[map_i]):
                out.append({"report": r.id, "household": r.household, "cycle": cd.cycle,
                            "downstream_of_top": bool(dn), "log_lr": round(float(v), 3)})
        out.sort(key=lambda e: -abs(e["log_lr"]))
        return out


def plan_b(locator: Locator, reports: list[Report]) -> dict:
    """Pure graph rule on the best-fit flow tree: the breach must sit upstream
    of every red tap and not upstream of any green tap. Real data can
    contradict itself (a diluted green tap far downstream, a prank), so this
    returns the candidates with the fewest violations rather than a possibly
    empty strict intersection."""
    reds, greens = [], []
    for r in reports:
        n = locator.node_of(r.household, 0)
        if n < 0 or r.level is None:
            continue
        if r.level == "red":
            reds.append(n)
        elif r.level == "green":
            greens.append(n)
    if not reds:
        return {"segments": [], "violations": None, "length_m": 0.0, "n_red": 0, "n_green": len(greens)}
    viol = np.zeros(len(locator.seg), dtype=np.int64)
    down_r, _, _ = locator.response(0, np.asarray(reds))
    viol += (~down_r).sum(axis=1)
    if greens:
        down_g, _, _ = locator.response(0, np.asarray(greens))
        viol += down_g.sum(axis=1)
    viol = np.where(locator.valid[0], viol, np.iinfo(np.int64).max)
    best = int(viol.min())
    keep = np.flatnonzero(viol == best)
    return {"segments": [int(s) for s in keep], "violations": best,
            "length_m": round(float(locator.seg.length[keep].sum()), 1),
            "n_red": len(reds), "n_green": len(greens)}
