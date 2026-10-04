"""Rebuilding a missing pipe map from arrival times (README §5.6).

    network = {inlet}
    for tap in sorted(taps, key=lambda t: t.typical_delay):
        path = cheapest_street_path(tap, network,
                                    cost=distance + LAMBDA * delay_violation)
        network |= path
    travel_time_model = fit_travel_times(network, observed_delays)

Taps are joined in order of arrival, each along the street path that is short
*and* consistent with its delay: joining at a point the water reaches late
(or too early to explain the tap's delay) costs extra.
"""

from __future__ import annotations

import heapq

import networkx as nx
import numpy as np

from ..network import FlowTree
from ..sim.streets import StreetGraph
from .traveltime import Tap, TravelTimeFit, fit_travel_times, sample_fit

LAMBDA = 1.0


def _street_adjacency(streets: StreetGraph) -> list[list[tuple[int, int]]]:
    adj: list[list[tuple[int, int]]] = [[] for _ in range(streets.n_nodes)]
    for e, (a, b) in enumerate(streets.edges):
        adj[int(a)].append((int(b), e))
        adj[int(b)].append((int(a), e))
    return adj


def _speed_estimate(streets: StreetGraph, inlet: int, taps: list[Tap]) -> tuple[float, float]:
    """(delta, slowness) from delay ≈ delta + slowness · street distance."""
    g = streets.nx_graph()
    dist = nx.single_source_dijkstra_path_length(g, inlet, weight="length")
    d = []
    for t in taps:
        a, b = (int(v) for v in streets.edges[t.edge])
        L = float(streets.length[t.edge])
        d.append(min(dist.get(a, np.inf) + t.frac * L, dist.get(b, np.inf) + (1 - t.frac) * L))
    d = np.asarray(d)
    y = np.asarray([t.delay for t in taps])
    A = np.column_stack([np.ones_like(d), d])
    (delta, s), *_ = np.linalg.lstsq(A, y, rcond=None)
    if delta < 0 or s <= 0:
        delta = 0.0
        s = float(np.sum(y * d) / max(np.sum(d * d), 1e-9))
    return float(delta), float(max(s, 1e-4))


def rebuild_layout(streets: StreetGraph, inlet: int, taps: list[Tap],
                   extra: list[tuple[int, float]] = (), lam: float = LAMBDA) -> np.ndarray:
    """Street edges of a likely pipe network. ``extra`` points (homes with no
    timing history) are connected afterwards by plain shortest paths."""
    adj = _street_adjacency(streets)
    delta, s_hat = _speed_estimate(streets, inlet, taps) if taps else (0.0, 0.03)
    in_net = np.zeros(streets.n_nodes, dtype=bool)
    t_net = np.full(streets.n_nodes, np.nan)
    in_net[inlet] = True
    t_net[inlet] = 0.0
    net_edges: set[int] = set()

    def join(edge: int, frac: float, target: float | None) -> None:
        if edge in net_edges:
            return
        a, b = (int(v) for v in streets.edges[edge])
        L = float(streets.length[edge])
        dist = {a: frac * L, b: (1 - frac) * L}
        pred: dict[int, tuple[int, int] | None] = {a: None, b: None}
        heap = [(dist[a], a), (dist[b], b)]
        done: set[int] = set()
        best = None
        while heap:
            d, u = heapq.heappop(heap)
            if u in done:
                continue
            done.add(u)
            if in_net[u]:
                if target is None:
                    cost = d
                else:
                    cost = d + lam * abs(t_net[u] + d * s_hat - target) / s_hat
                if best is None or cost < best[0]:
                    best = (cost, u, d)
                continue  # network nodes are terminals
            for v, e in adj[u]:
                if e == edge:
                    continue
                nd = d + float(streets.length[e])
                if nd < dist.get(v, np.inf) - 1e-9:
                    dist[v] = nd
                    pred[v] = (u, e)
                    heapq.heappush(heap, (nd, v))
        if best is None:
            raise ValueError("street graph is disconnected from the inlet")
        _, m, d_m = best
        # walk back from the network node to the tap, timing the new nodes
        if target is not None and d_m > 0 and target > t_net[m]:
            s_eff = float(np.clip((target - t_net[m]) / d_m, s_hat / 3, s_hat * 3))
        else:
            s_eff = s_hat
        chain = [m]
        while pred[chain[-1]] is not None:
            u, e = pred[chain[-1]]
            net_edges.add(e)
            chain.append(u)
        t = float(t_net[m])
        for prev, node in zip(chain[:-1], chain[1:]):
            step = dist[prev] - dist[node]
            t += step * s_eff
            if not in_net[node]:
                in_net[node] = True
                t_net[node] = t
        net_edges.add(edge)
        end = chain[-1]                      # a or b, wherever the path entered the tap's lane
        other = b if end == a else a
        if not in_net[other]:
            in_net[other] = True
            t_net[other] = t_net[end] + L * s_eff

    for tap in sorted(taps, key=lambda t: t.delay):
        join(tap.edge, tap.frac, max(tap.delay - delta, 0.0))
    for edge, frac in extra:
        join(int(edge), float(frac), None)
    return np.asarray(sorted(net_edges), dtype=np.int64)


def rebuild_fits(streets: StreetGraph, inlet: int, taps: list[Tap], homes: list[tuple[int, float]],
                 n: int = 8, seed: int = 0, mode: str = "mixed") -> list[TravelTimeFit]:
    """Rebuilt network + fitted travel times: first from all taps, then
    alternatives the locator averages over. Bootstrap resamples of the
    households give other plausible *layouts*; draws from the best fit's
    posterior give other plausible *flow directions* on its layout.
    ``mode`` is ``bootstrap`` (layouts only) or ``mixed``/``posterior``
    (half and half)."""
    rng = np.random.default_rng(seed)
    best = fit_travel_times(streets, rebuild_layout(streets, inlet, taps, extra=homes), inlet, taps)
    fits = [best]
    n_boot = n - 1 if mode == "bootstrap" else (n - 1) // 2
    for _ in range(n_boot):
        sample = [taps[j] for j in rng.integers(0, len(taps), len(taps))]
        layout = rebuild_layout(streets, inlet, sample, extra=homes)
        try:
            fits.append(fit_travel_times(streets, layout, inlet, sample))
        except ValueError:
            continue
    while len(fits) < n:
        fits.append(sample_fit(best, rng))
    return fits


def score_rebuild(streets: StreetGraph, true_edges: np.ndarray, rebuilt_edges: np.ndarray,
                  true_tree: FlowTree, rebuilt_tree: FlowTree,
                  homes: list[tuple[int, float]]) -> dict:
    """How close the rebuilt network is to the real one: pipe overlap (by
    length) and agreement on which homes are downstream of which."""
    t = set(int(e) for e in true_edges)
    r = set(int(e) for e in rebuilt_edges)
    L = streets.length
    both = sum(L[e] for e in t & r)
    prec = both / max(sum(L[e] for e in r), 1e-9)
    rec = both / max(sum(L[e] for e in t), 1e-9)
    nt = np.asarray([true_tree.attach(e, f) for e, f in homes])
    nr = np.asarray([rebuilt_tree.attach(e, f) if rebuilt_tree.has_edge(e) else -1 for e, f in homes])
    ok = nr >= 0
    A = true_tree.downstream(nt[ok], nt[ok])
    B = rebuilt_tree.downstream(nr[ok], nr[ok])
    off = ~np.eye(int(ok.sum()), dtype=bool)
    agree = float((A == B)[off].mean()) if ok.sum() > 1 else float("nan")
    # agreement restricted to pairs that truly are upstream/downstream
    pos = A & off
    recall_down = float((B & pos).sum() / max(pos.sum(), 1))
    return {"pipe_precision": round(float(prec), 3), "pipe_recall": round(float(rec), 3),
            "downstream_agreement": round(agree, 4), "downstream_recall": round(recall_down, 3),
            "rebuilt_length_m": round(float(sum(L[e] for e in r)), 1),
            "true_length_m": round(float(sum(L[e] for e in t)), 1)}
