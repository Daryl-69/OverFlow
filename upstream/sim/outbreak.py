"""Simulated supply cycles: who reports, when, and what their glass shows."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..physics import SlugPhysics
from ..reports import Report
from .world import ROLES, World

# The simulator's "true" physics differs from the locator's defaults on
# purpose, so the locator is never fitted to the exact data-generating model.
TRUE_PHYSICS = SlugPhysics(mix_per_junction=0.85, dispersion_m=120.0, window0_min=7.0,
                           tail_frac=0.25, turbidity_per_unit=0.55, chlorine_per_unit=1.4)

CL_AT_INLET = 1.0          # mg/L free chlorine leaving the service reservoir
CL_DECAY_MIN = 100.0       # e-folding time of chlorine decay in the network


@dataclass
class Breach:
    edge: int                       # street edge of the cracked pipe
    frac: float                     # position along it
    start_cycle: int                # first supply cycle that carries sewage
    strengths: list[float] = field(default_factory=lambda: [0.45, 0.9])

    def strength(self, cycle: int) -> float:
        i = cycle - self.start_cycle
        if i < 0:
            return 0.0
        return float(self.strengths[min(i, len(self.strengths) - 1)])


def chlorine_base(arrival_min: float, offset: float) -> float:
    return max(0.0, CL_AT_INLET * float(np.exp(-arrival_min / CL_DECAY_MIN)) + offset)


def sample_breach(world: World, rng: np.random.Generator, start_cycle: int,
                  strengths=(0.45, 0.9), min_downstream_homes: int = 0) -> Breach:
    """Draw a breach location with probability ∝ pipe length × (0.25 + risk).
    Optionally require that some participating homes sit downstream (for
    demos; evaluation keeps the default 0 so undetectable breaches count)."""
    lengths = world.streets.length[world.pipe_edges]
    w = lengths * (0.25 + world.risk)
    tree = world.tree
    nodes = np.asarray([tree.attach(h.edge, h.frac) for h in world.households])
    for _ in range(500):
        k = int(rng.choice(len(world.pipe_edges), p=w / w.sum()))
        b = Breach(edge=int(world.pipe_edges[k]), frac=float(rng.uniform(0.1, 0.9)),
                   start_cycle=start_cycle, strengths=list(strengths))
        if min_downstream_homes <= 0:
            return b
        piece = tree.piece_at(b.edge, b.frac)
        if int(tree.downstream(np.array([piece]), nodes).sum()) >= min_downstream_homes:
            return b
    raise RuntimeError("could not place a breach with enough downstream homes")


def simulate_cycle(world: World, cycle: int, rng: np.random.Generator,
                   breach: Breach | None = None, requested: set[str] | frozenset = frozenset(),
                   physics: SlugPhysics = TRUE_PHYSICS, prank_rate: float = 0.008,
                   id_prefix: str = "R") -> list[Report]:
    tree = world.tree
    t0 = world.supply.start(cycle)
    strength = breach.strength(cycle) if breach is not None else 0.0
    piece = tree.piece_at(breach.edge, breach.frac) if strength > 0 else None
    since = cycle - breach.start_cycle if breach is not None else -1
    out: list[Report] = []
    for h in world.households:
        asked = h.id in requested
        p = 0.9 if asked else ROLES[h.role][1]
        if rng.random() > p:
            continue
        node = tree.attach(h.edge, h.frac)
        arrival = float(tree.T[node])
        delay = max(0.2, float(rng.normal(h.habit_delay, h.habit_sd)))
        s = 0.0
        dist = None
        if piece is not None and tree.downstream(np.array([piece]), np.array([node]))[0, 0]:
            dist = float(tree.distance_down(np.array([piece]), np.array([node]))[0, 0])
            junc = int(tree.junctions_between(np.array([piece]), np.array([node]))[0, 0])
            s = strength * float(physics.dilution(dist, junc))
        prank = rng.random() < prank_rate
        turb = h.turb_base + physics.turbidity_per_unit * s + rng.normal(0, h.turb_sd)
        cl_true = max(0.0, chlorine_base(arrival, h.cl_offset) - physics.chlorine_per_unit * s)
        uses_strip = (h.role in ("volunteer", "sentinel") or asked or rng.random() < 0.2
                      or (s > 0.15 and rng.random() < 0.8))
        chlorine = None
        if uses_strip:
            chlorine = max(0.0, cl_true + rng.normal(0, 0.04 + 0.08 * cl_true))
        p_complain = 0.02 + 0.98 * (1 - np.exp(-s / 0.2))
        if rng.random() < p_complain:
            weights = np.array([0.5, 0.4, 0.1 if since >= 1 else 0.0])
            answer = str(rng.choice(["smell", "dirty", "ill"], p=weights / weights.sum()))
        else:
            answer = "fine" if rng.random() < 0.5 else None
        if prank:
            answer = "dirty"
            turb += rng.uniform(0.2, 0.5)
            chlorine = None
        out.append(Report(
            id="", household=h.id, cycle=cycle, supply_start=t0,
            t_obs=round(t0 + arrival + delay, 2),
            turbidity=round(float(np.clip(turb, 0.0, 1.0)), 3),
            chlorine=None if chlorine is None else round(float(chlorine), 2),
            answer=answer, requested=asked, source="sim",
            truth={"s": round(s, 4), "downstream": dist is not None,
                   "dist_m": None if dist is None else round(dist, 1), "prank": prank,
                   "arrival_min": round(arrival, 2)},
        ))
    out.sort(key=lambda r: r.t_obs)
    for i, r in enumerate(out):
        r.id = f"{id_prefix}{cycle:03d}-{i + 1:03d}"
    return out
