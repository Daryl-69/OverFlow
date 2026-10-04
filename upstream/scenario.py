"""End-to-end scenario: a Bhagirathpura-style outbreak replayed through the
whole Upstream pipeline.

1. Normal days: households send first-glass photos; Upstream learns each
   tap's baseline and the network's travel times (with the utility's pipe
   map, and separately rebuilding the map from timings alone).
2. A crack opens next to a sewer. Each supply cycle the dirty slug rides the
   front to downstream homes.
3. Signal levels are assigned as photos arrive. Amber readings make the app
   ask nearby homes to test at the next supply; a red reading starts
   triangulation, alerts the utility and sends downstream homes a safe-after
   time.

:func:`run_scenario` returns everything the dashboard replays, as plain
JSON-serialisable data.
"""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, field

import numpy as np

from .locate.advisories import advisories
from .locate.baseline import Baselines, TapBaseline, learn_baselines
from .locate.levels import AMBER, RED, apply_levels
from .locate.planner import best_lanes, nearby_requests, place_sentinels, rank_households
from .locate.rebuild import rebuild_fits, score_rebuild
from .locate.traveltime import TravelTimeFit, ensemble_fits, taps_from_baselines
from .locate.triangulate import Locator, Posterior, plan_b
from .network import FlowTree
from .reports import Report, clock
from .sim.outbreak import TRUE_PHYSICS, Breach, sample_breach, simulate_cycle
from .sim.streets import StreetGraph, load_streets, synthetic_streets
from .sim.world import World, make_world

SCHEMA_VERSION = 1


@dataclass
class ScenarioConfig:
    seed: int = 7
    n_households: int = 140
    baseline_cycles: int = 12
    outbreak_cycles: int = 2
    strengths: tuple[float, ...] = (0.45, 0.9)
    ensemble: int = 8
    ensemble_mode: str = "mixed"
    n_sentinels: int = 8
    min_downstream_homes: int = 0
    loop_frac: float = 0.35
    streets_path: str | None = None
    inlet: int | None = None
    frames: bool = True


@dataclass
class Scenario:
    cfg: ScenarioConfig
    world: World
    breach: Breach
    baselines: Baselines
    baseline_reports: list[Report]
    gis_fits: list[TravelTimeFit]
    reb_fits: list[TravelTimeFit]
    gis: Locator
    reb: Locator
    cycles: list[dict] = field(default_factory=list)
    frames: list[dict] = field(default_factory=list)
    final: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)

    @property
    def streets(self) -> StreetGraph:
        return self.world.streets

    def positions(self) -> dict[str, tuple[float, float]]:
        st = self.streets
        return {h.id: st.point_on_edge(h.edge, h.frac) for h in self.world.households}

    def arrival(self) -> dict[str, float]:
        """Learned first-water arrival per home (minutes after supply start)."""
        fit = self.gis_fits[0]
        out = {}
        for h in self.world.households:
            node = self.gis.node_of(h.id, 0)
            out[h.id] = round(fit.arrival(h.id, node), 2)
        return out

    def outbreak_reports(self) -> list[Report]:
        return [r for c in self.cycles for r in c["reports"]]


def _round_ll(lat, lon) -> list[float]:
    return [round(float(lat), 7), round(float(lon), 7)]


def _front_arrival(tree: FlowTree, child: int, offset_m: float) -> float | None:
    """Minutes after supply start at which the front passes a point that
    lies ``offset_m`` upstream of tree node ``child``."""
    if child < 0:
        return None
    k = int(tree.pipe_of[child])
    return round(float(tree.T[child] - offset_m * tree.slowness[k]), 2)


def _post_frame(post: Posterior | None, bx: float, by: float, reports: list[Report]) -> dict | None:
    if post is None:
        return None
    s = post.summary()
    pb = plan_b(post.locator, reports)
    loc = post.locator
    dig_arrival = _front_arrival(loc.trees[0], int(loc.seg_child[0, post.map_seg]),
                                 float(loc.seg_off[0, post.map_seg]))
    return {
        "post": post.sparse(2e-4),
        "ring": s["ring"],
        "dig": s["dig"],
        "region": s["region"],
        "region_length_m": s["region_length_m"],
        "p_breach": s["p_breach"],
        "log10_bf": s["log10_bayes_factor"],
        "error_m": round(post.error_m(bx, by), 1),
        "truth_in_region": bool(min(np.hypot(post.segments.x[post.region] - bx,
                                             post.segments.y[post.region] - by)) <= 12.0),
        "plan_b": {"segments": pb["segments"], "length_m": pb["length_m"], "violations": pb["violations"]},
        "n_reports": s["n_reports"],
        "dig_arrival_min": dig_arrival,
    }


def _complaint_baselines(reports: list[Report], positions, bx: float, by: float) -> dict:
    """What handling complaints one ticket at a time would point at."""
    comp = sorted((r for r in reports if r.complaint), key=lambda r: r.t_obs)
    if not comp:
        return {"first_ticket_error_m": None, "centroid_error_m": None, "n_tickets": 0}
    fx, fy = positions[comp[0].household]
    xy = np.asarray([positions[r.household] for r in comp])
    cx, cy = xy.mean(axis=0)
    return {"first_ticket_error_m": round(math.hypot(fx - bx, fy - by), 1),
            "centroid_error_m": round(math.hypot(cx - bx, cy - by), 1),
            "n_tickets": len(comp)}


def build_world(cfg: ScenarioConfig) -> World:
    streets = load_streets(cfg.streets_path) if cfg.streets_path else synthetic_streets()
    return make_world(streets, seed=cfg.seed, n_households=cfg.n_households, inlet=cfg.inlet,
                      loop_frac=cfg.loop_frac)


def run_scenario(cfg: ScenarioConfig | None = None, world: World | None = None) -> Scenario:
    cfg = cfg or ScenarioConfig()
    t_start = time.time()
    world = world or build_world(cfg)
    st = world.streets
    rng = np.random.default_rng(cfg.seed + 1000)
    homes = {h.id: (h.edge, h.frac) for h in world.households}
    positions = {h.id: st.point_on_edge(h.edge, h.frac) for h in world.households}

    # Sentinel homes are chosen by the planner before any alert, on the
    # utility's pipe map with uniform (not yet learned) travel times.
    if cfg.n_sentinels > 0:
        prelim = FlowTree(st, world.pipe_edges, np.ones(len(world.pipe_edges)), world.inlet)
        empty = Baselines(taps={}, population=TapBaseline())
        pre_loc = Locator([prelim], homes, empty, risk_by_edge=world.risk_by_edge())
        cands = [h.id for h in world.households if h.role == "regular"]
        for pick in place_sentinels(pre_loc, cands, cfg.n_sentinels):
            world.household(pick["household"]).role = "sentinel"

    # 1. normal days
    base_reports: list[Report] = []
    for c in range(cfg.baseline_cycles):
        base_reports += simulate_cycle(world, c, rng)
    baselines = learn_baselines(base_reports)
    public = [h.public(st) for h in world.households]
    taps = taps_from_baselines(public, baselines)
    t0 = time.time()
    gis_fits = ensemble_fits(st, world.pipe_edges, world.inlet, taps, n=cfg.ensemble, seed=cfg.seed,
                             mode=cfg.ensemble_mode)
    reb_fits = rebuild_fits(st, world.inlet, taps, [homes[h] for h in homes], n=cfg.ensemble, seed=cfg.seed,
                            mode=cfg.ensemble_mode)
    t_fit = time.time() - t0
    gis = Locator([f.tree for f in gis_fits], homes, baselines, timing=gis_fits[0],
                  risk_by_edge=world.risk_by_edge())
    reb = Locator([f.tree for f in reb_fits], homes, baselines, timing=reb_fits[0])

    # 2. the breach
    B = cfg.baseline_cycles
    breach = sample_breach(world, rng, start_cycle=B, strengths=cfg.strengths,
                           min_downstream_homes=cfg.min_downstream_homes)
    bx, by = st.point_on_edge(breach.edge, breach.frac)
    sc = Scenario(cfg=cfg, world=world, breach=breach, baselines=baselines, baseline_reports=base_reports,
                  gis_fits=gis_fits, reb_fits=reb_fits, gis=gis, reb=reb)
    arrival = sc.arrival()

    # 3. outbreak cycles, replayed photo by photo
    requested: set[str] = set()
    request_reasons: dict[str, str] = {}
    seen: list[Report] = []
    alert = False
    t_loc = 0.0
    for i in range(cfg.outbreak_cycles):
        c = B + i
        reps = simulate_cycle(world, c, rng, breach=breach, requested=requested)
        cycle_info = {"cycle": c, "supply_start": world.supply.start(c), "reports": reps,
                      "requested": sorted(requested), "request_reasons": dict(request_reasons)}
        partial: list[Report] = []
        for r in reps:
            partial.append(r)
            before = {x.id: x.level for x in partial}
            apply_levels(partial, baselines, positions)
            changed = {x.id: x.level for x in partial if before.get(x.id) != x.level}
            seen.append(r)
            new_alert = not alert and any(x.level == RED for x in seen)
            alert = alert or new_alert
            frame = {"t": r.t_obs, "clock": clock(r.t_obs), "cycle": c, "report": r.id,
                     "levels": changed, "alert": alert, "alert_started": new_alert}
            if alert and cfg.frames:
                tl = time.time()
                frame["gis"] = _post_frame(gis.locate(seen), bx, by, seen)
                frame["rebuilt"] = _post_frame(reb.locate(seen), bx, by, seen)
                t_loc += time.time() - tl
            sc.frames.append(frame)

        # end of the supply cycle: plan the next one
        post = gis.locate(seen)
        next_req: dict[str, str] = {}
        if post is not None:
            for r in reps:
                if r.level == AMBER:
                    for p in nearby_requests(gis, post.joint, positions[r.household], positions,
                                             exclude={r.household}):
                        next_req.setdefault(p["household"], f"amber reading at {r.household}")
            if alert:
                adv = advisories(post, [h.id for h in world.households], arrival,
                                 world.supply.start(c + 1), world.supply.duration_min)
                cycle_info["advisories"] = [a for a in adv if a["status"] != "clear"]
                for a in cycle_info["advisories"]:
                    if a["p_affected"] >= 0.05:
                        next_req.setdefault(a["household"], "likely downstream of the suspected breach")
                for p in rank_households(gis, post.joint, [h.id for h in world.households], top=5):
                    next_req.setdefault(p["household"], "most informative test for the next supply")
        requested = set(next_req)
        request_reasons = next_req
        cycle_info["next_requests"] = [{"household": h, "reason": why} for h, why in sorted(next_req.items())]
        sc.cycles.append(cycle_info)

    # 4. final outputs
    final: dict = {"alert": alert}
    last = B + cfg.outbreak_cycles - 1
    for name, loc in (("gis", gis), ("rebuilt", reb)):
        post = loc.locate(seen)
        if post is None:
            continue
        f = _post_frame(post, bx, by, seen)
        f["evidence"] = post.evidence[:12]
        f["lanes"] = best_lanes(loc, post.joint, top=5)
        final[name] = f
        if name == "gis":
            adv = advisories(post, [h.id for h in world.households], arrival,
                             world.supply.start(last + 1), world.supply.duration_min)
            final["advisories"] = adv
            final["next_supply_start"] = world.supply.start(last + 1)
    final["complaints"] = _complaint_baselines(seen, positions, bx, by)
    sc.final = final
    sc.timings = {"fit_s": round(t_fit, 2), "locate_frames_s": round(t_loc, 2),
                  "total_s": round(time.time() - t_start, 2)}
    return sc


# ------------------------------------------------------------------ export
def _segments_json(loc: Locator) -> dict:
    ends = loc.seg.endpoints_latlon(loc.streets)
    return {"edge": loc.seg.edge.tolist(),
            "a": [_round_ll(*p) for p in ends[:, 0]],
            "b": [_round_ll(*p) for p in ends[:, 1]],
            "length_m": [round(float(v), 1) for v in loc.seg.length]}


def _flow_json(fit: TravelTimeFit) -> dict:
    """Per pipe of the best-fit tree: direction water runs (from, to street
    node; null for a loop pipe filled from both ends) and arrival time at its
    middle."""
    tree = fit.tree
    st = tree.streets
    out_dir, out_t, out_meet = [], [], []
    for k, e in enumerate(tree.pipe_edges):
        a, b = (int(v) for v in st.edges[e])
        chains = tree.chains[k]
        if len(chains) == 1 and np.isnan(tree.meet_frac[k]):
            start = int(tree.street_node[chains[0].nodes[0]])
            out_dir.append([start, b if start == a else a])
            out_meet.append(None)
        else:
            out_dir.append(None)
            out_meet.append(None if np.isnan(tree.meet_frac[k]) else round(float(tree.meet_frac[k]), 3))
        node = tree.attach(int(e), 0.5)
        out_t.append(round(float(tree.T[node]), 2))
    return {"edges": tree.pipe_edges.tolist(), "direction": out_dir, "meet_frac": out_meet,
            "arrival_min": out_t}


def _report_json(r: Report) -> dict:
    d = r.to_dict()
    d["clock"] = clock(r.t_obs)
    d["delay_min"] = round(r.delay, 2)
    return d


def scenario_json(sc: Scenario) -> dict:
    w, st = sc.world, sc.streets
    bx, by = st.point_on_edge(sc.breach.edge, sc.breach.frac)
    blat, blon = st.to_latlon(bx, by)
    arrival = sc.arrival()
    tree = w.tree
    tnodes = np.asarray([tree.attach(h.edge, h.frac) for h in w.households])
    tpiece = tree.piece_at(sc.breach.edge, sc.breach.frac)
    down = tree.downstream(np.asarray([tpiece]), tnodes)[0]
    homes_json = []
    for h, dn in zip(w.households, down):
        d = h.public(st)
        d["arrival_min"] = arrival[h.id]
        d["baseline"] = sc.baselines.taps.get(h.id, TapBaseline()).to_dict()
        d["truth_downstream"] = bool(dn)
        homes_json.append(d)
    per_cycle = []
    pos = sc.positions()
    for c in range(sc.cfg.baseline_cycles):
        rs = [r for r in sc.baseline_reports if r.cycle == c]
        apply_levels(rs, sc.baselines, pos)
        per_cycle.append({"cycle": c, "supply_start": w.supply.start(c), "n": len(rs),
                          **{lv: sum(1 for r in rs if r.level == lv) for lv in ("green", "amber", "red")}})
    reb_score = score_rebuild(st, w.pipe_edges, sc.reb_fits[0].tree.pipe_edges, tree, sc.reb_fits[0].tree,
                              [(h.edge, h.frac) for h in w.households])
    gis_seg = sc.gis.seg
    reb_seg = sc.reb.seg

    def seg_index(seg, edge, frac):
        hit = np.flatnonzero((seg.edge == edge) & (seg.a_frac <= frac) & (seg.b_frac >= frac))
        return int(hit[0]) if hit.size else None

    cycles = []
    for c in sc.cycles:
        cc = {k: v for k, v in c.items() if k != "reports"}
        cc["reports"] = [_report_json(r) for r in c["reports"]]
        cycles.append(cc)
    ins = w.streets.to_latlon(st.x[w.inlet], st.y[w.inlet])
    return {
        "version": SCHEMA_VERSION,
        "meta": {
            "name": st.name, "synthetic": st.synthetic, "attribution": st.attribution,
            "seed": sc.cfg.seed, "config": asdict(sc.cfg),
            "supply": asdict(w.supply), "timings": sc.timings,
            "physics_true": asdict(TRUE_PHYSICS), "physics_model": asdict(sc.gis.cfg.physics),
        },
        "streets": {"nodes": [_round_ll(a, b) for a, b in zip(st.lat, st.lon)], "edges": st.edges.tolist()},
        "inlet": {"node": int(w.inlet), "lat": round(float(ins[0]), 7), "lon": round(float(ins[1]), 7)},
        "pipes": {
            "edges": w.pipe_edges.tolist(),
            "risk": [round(float(v), 3) for v in w.risk],
            **{k: [round(float(x), 1) for x in v] for k, v in w.risk_features.items()},
            "flow": _flow_json(sc.gis_fits[0]),
        },
        "rebuilt": {"edges": sc.reb_fits[0].tree.pipe_edges.tolist(), "score": reb_score,
                    "flow": _flow_json(sc.reb_fits[0])},
        "segments": {"gis": _segments_json(sc.gis), "rebuilt": _segments_json(sc.reb)},
        "households": homes_json,
        "baseline": {
            "cycles": sc.cfg.baseline_cycles, "reports": len(sc.baseline_reports), "per_cycle": per_cycle,
            "fit": {"delta_min": round(sc.gis_fits[0].delta, 2),
                    "residual_sd_min": round(sc.gis_fits[0].residual_sd, 2),
                    "ensemble": len(sc.gis_fits)},
            "population": sc.baselines.population.to_dict(),
        },
        "outbreak": {"cycles": cycles},
        "frames": sc.frames,
        "final": sc.final,
        "truth": {
            "edge": int(sc.breach.edge), "frac": round(sc.breach.frac, 4),
            "lat": round(float(blat), 7), "lon": round(float(blon), 7),
            "start_cycle": sc.breach.start_cycle, "strengths": list(sc.breach.strengths),
            "segment_gis": seg_index(gis_seg, sc.breach.edge, sc.breach.frac),
            "segment_rebuilt": seg_index(reb_seg, sc.breach.edge, sc.breach.frac),
            "arrival_min": _front_arrival(tree, *tree.locate_point(sc.breach.edge, sc.breach.frac)),
            "downstream_households": [h.id for h, dn in zip(w.households, down) if dn],
        },
    }
