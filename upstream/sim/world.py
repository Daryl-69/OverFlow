"""A simulated ward: true pipe layout, hydraulics, households and risk.

Everything here is *ground truth* for the simulation. The locator never sees
the true slowness or the hidden household traits; it only sees what a utility
would have (the pipe layout, if it has a GIS) and what households report.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import networkx as nx
import numpy as np

from ..network import FlowTree
from .streets import StreetGraph

ROLES = {
    # role: (strip pack size, chance of reporting in a normal supply cycle)
    "regular": (10, 0.45),
    "volunteer": (14, 0.85),
    "sentinel": (21, 0.95),
}


@dataclass
class Household:
    id: str
    edge: int          # street edge the home's connection is on
    frac: float        # position along that edge
    role: str = "regular"
    # hidden traits (simulation only)
    habit_delay: float = 2.0   # minutes between water arriving and the photo
    habit_sd: float = 0.8
    turb_base: float = 0.08    # turbidity index of this tap's clear water
    turb_sd: float = 0.02
    cl_offset: float = 0.0

    @property
    def pack(self) -> int:
        return ROLES[self.role][0]

    def public(self, streets: StreetGraph) -> dict:
        """What the system stores about a home: an ID, its lane and role."""
        x, y = streets.point_on_edge(self.edge, self.frac)
        lat, lon = streets.to_latlon(x, y)
        return {"id": self.id, "edge": int(self.edge), "frac": round(float(self.frac), 4),
                "lat": round(float(lat), 7), "lon": round(float(lon), 7),
                "role": self.role, "pack": self.pack}


@dataclass
class Supply:
    start_minute: int = 6 * 60   # 06:00
    interval_days: int = 2       # Indore: once every two days
    duration_min: int = 120

    def start(self, cycle: int) -> float:
        """Minutes since the scenario epoch at which ``cycle`` starts."""
        return float(cycle * self.interval_days * 1440 + self.start_minute)


@dataclass
class World:
    streets: StreetGraph
    pipe_edges: np.ndarray         # street edges carrying a pipe (may form loops)
    inlet: int                     # street node where supply enters the ward
    slowness: np.ndarray           # true minutes per metre, per pipe
    households: list[Household]
    risk: np.ndarray               # per pipe, 0..1
    risk_features: dict[str, np.ndarray]
    supply: Supply = field(default_factory=Supply)
    _tree: FlowTree | None = field(default=None, repr=False)

    @property
    def tree(self) -> FlowTree:
        """The true first-arrival flow tree."""
        if self._tree is None:
            self._tree = FlowTree(self.streets, self.pipe_edges, self.slowness, self.inlet)
        return self._tree

    def household(self, hid: str) -> Household:
        for h in self.households:
            if h.id == hid:
                return h
        raise KeyError(hid)

    def risk_by_edge(self) -> dict[int, float]:
        return {int(e): float(r) for e, r in zip(self.pipe_edges, self.risk)}


def choose_inlet(streets: StreetGraph) -> int:
    """Default inlet: the street node nearest the middle of the west edge,
    where the trunk main is assumed to enter."""
    target_x = streets.x.min()
    target_y = np.median(streets.y)
    return int(np.argmin(np.hypot(streets.x - target_x, streets.y - target_y)))


def generate_layout(streets: StreetGraph, inlet: int, rng: np.random.Generator,
                    loop_frac: float = 0.35) -> np.ndarray:
    """A plausible pipe layout: a randomised shortest-path tree from the inlet
    (every lane reachable) plus a share of the remaining lanes, which close
    loops. Returns street-edge indices."""
    g = streets.nx_graph()
    for _, _, d in g.edges(data=True):
        d["w"] = d["length"] * rng.uniform(0.7, 1.6)
    _, paths = nx.single_source_dijkstra(g, inlet, weight="w")
    tree = set()
    for path in paths.values():
        for a, b in zip(path[:-1], path[1:]):
            tree.add(g.edges[a, b]["edge"])
    rest = [e for e in range(streets.n_edges) if e not in tree]
    extra = [e for e in rest if rng.random() < loop_frac]
    return np.asarray(sorted(tree | set(extra)), dtype=np.int64)


def true_slowness(streets: StreetGraph, pipe_edges: np.ndarray, inlet: int,
                  rng: np.random.Generator, v_min: float = 0.12, v_max: float = 0.9,
                  heterogeneity: float = 0.15) -> np.ndarray:
    """Front speed per pipe: fast in mains that feed much of the ward, slow in
    small lanes. Returns minutes per metre."""
    uniform = FlowTree(streets, pipe_edges, np.ones(len(pipe_edges)), inlet)
    total = uniform.total_length
    fed = np.zeros(len(pipe_edges))
    for k, chains in enumerate(uniform.chains):
        length = streets.length[pipe_edges[k]]
        if len(chains) == 1 and np.isnan(uniform.meet_frac[k]):
            last = int(chains[0].nodes[-1])
            fed[k] = uniform.subtree_len[last] + length
        else:
            fed[k] = length / 2
    v = v_min + (v_max - v_min) * np.sqrt(np.clip(fed / total, 0, 1))
    v = v * rng.lognormal(0.0, heterogeneity, size=len(pipe_edges))
    return 1.0 / (np.clip(v, 0.05, 2.0) * 60.0)


def synthetic_risk(streets: StreetGraph, pipe_edges: np.ndarray,
                   rng: np.random.Generator) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Synthetic risk attributes per pipe (replace with utility records):
    pipe age, a sewer in the same lane, sewer crossings at the pipe's ends,
    past incidents and known sewage sources above the line."""
    a, b = streets.edges[pipe_edges, 0], streets.edges[pipe_edges, 1]
    mx = (streets.x[a] + streets.x[b]) / 2
    my = (streets.y[a] + streets.y[b]) / 2
    span = max(np.ptp(streets.x), np.ptp(streets.y), 1.0)
    cx, cy = np.median(streets.x), np.median(streets.y)
    r2 = ((mx - cx) ** 2 + (my - cy) ** 2) / (0.35 * span) ** 2
    age = np.clip(15 + 35 * np.exp(-r2 / 2) + rng.normal(0, 6, len(pipe_edges)), 5, 70)
    sewer_edge = rng.random(streets.n_edges) < 0.7
    sewer_deg = np.zeros(streets.n_nodes)
    np.add.at(sewer_deg, streets.edges[sewer_edge, 0], 1)
    np.add.at(sewer_deg, streets.edges[sewer_edge, 1], 1)
    alongside = sewer_edge[pipe_edges].astype(float)
    crossings = sewer_deg[a] + sewer_deg[b] - 2 * alongside
    incidents = rng.poisson(0.12 * age / 40 + 0.1 * alongside)
    hazard = (rng.random(len(pipe_edges)) < 0.03).astype(float)

    def norm(v):
        v = np.asarray(v, dtype=float)
        return (v - v.min()) / (np.ptp(v) or 1.0)

    risk = norm(0.35 * norm(crossings + 2 * alongside) + 0.3 * norm(age)
                + 0.2 * norm(incidents) + 0.15 * hazard)
    feats = {"age_years": np.round(age, 1), "sewer_alongside": alongside,
             "sewer_crossings": crossings, "past_incidents": incidents.astype(float),
             "sewage_source_above": hazard}
    return risk, feats


def place_households(streets: StreetGraph, pipe_edges: np.ndarray, n: int,
                     rng: np.random.Generator, volunteer_frac: float = 0.2) -> list[Household]:
    """Participating homes spread along the pipes (uniform per metre).
    Sentinels are assigned later by the planner."""
    lengths = streets.length[pipe_edges]
    picks = rng.choice(len(pipe_edges), size=n, p=lengths / lengths.sum())
    homes = []
    for i, k in enumerate(picks):
        role = "volunteer" if rng.random() < volunteer_frac else "regular"
        homes.append(Household(
            id=f"H-{i + 1:04d}",
            edge=int(pipe_edges[k]),
            frac=float(rng.uniform(0.05, 0.95)),
            role=role,
            habit_delay=float(rng.uniform(0.5, 4.0)),
            habit_sd=float(rng.uniform(0.3, 1.5)),
            turb_base=float(rng.uniform(0.04, 0.12)),
            turb_sd=float(rng.uniform(0.012, 0.03)),
            cl_offset=float(rng.normal(0, 0.05)),
        ))
    return homes


def make_world(streets: StreetGraph, seed: int = 7, n_households: int = 140,
               inlet: int | None = None, loop_frac: float = 0.35) -> World:
    rng = np.random.default_rng(seed)
    inlet = choose_inlet(streets) if inlet is None else int(inlet)
    pipes = generate_layout(streets, inlet, rng, loop_frac=loop_frac)
    slow = true_slowness(streets, pipes, inlet, rng)
    risk, feats = synthetic_risk(streets, pipes, rng)
    homes = place_households(streets, pipes, n_households, rng)
    return World(streets=streets, pipe_edges=pipes, inlet=inlet, slowness=slow,
                 households=homes, risk=risk, risk_features=feats)
