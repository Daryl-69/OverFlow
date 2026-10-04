"""Learning travel times from normal-day photos (README §5.6).

Each household's typical delay between supply start and its first-glass photo
is ``delta + T(tap)``: a habit offset (how long people take to fill and
photograph the glass) plus the front's travel time along the flow tree.
``T`` is linear in the per-pipe slowness for a fixed tree, so we alternate:

1. build the first-arrival tree for the current slowness;
2. solve a regularised least-squares problem for (delta, slowness);

until the tree stops changing. With a looped layout the tree itself is part of
what the timings reveal.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..network import FlowTree
from ..sim.streets import StreetGraph


@dataclass
class Tap:
    household: str
    edge: int
    frac: float
    delay: float      # median minutes from supply start to photo
    sd: float         # its robust spread (minutes)


@dataclass
class TravelTimeFit:
    tree: FlowTree
    slowness: np.ndarray
    delta: float               # habit offset, minutes
    residual_sd: float         # minutes
    iterations: int
    tap_arrival: dict[str, float]   # household -> learned first-water arrival (min)
    mean: np.ndarray | None = None  # [delta, slowness...] of the final linear solve
    cov: np.ndarray | None = None   # its (Laplace) covariance
    s_bar: float = 0.0
    inlet: int = 0

    def arrival(self, household: str, node: int) -> float:
        """First-water arrival at a home: its own learned timing if it has a
        history, else the flow model's prediction for its node."""
        if household in self.tap_arrival:
            return self.tap_arrival[household]
        return float(self.tree.T[node])


def _path_weights(tree: FlowTree, node: int, n_pipes: int) -> np.ndarray:
    w = np.zeros(n_pipes)
    while tree.parent[node] >= 0:
        w[tree.pipe_of[node]] += tree.piece_len[node]
        node = int(tree.parent[node])
    return w


def fit_travel_times(streets: StreetGraph, pipe_edges: np.ndarray, inlet: int, taps: list[Tap],
                     prior_rel_sd: float = 0.5, max_iter: int = 8,
                     max_piece: float = 20.0) -> TravelTimeFit:
    pipe_edges = np.asarray(pipe_edges, dtype=np.int64)
    n_pipes = len(pipe_edges)
    tree = FlowTree(streets, pipe_edges, np.ones(n_pipes), inlet, max_piece=max_piece)
    usable = [t for t in taps if tree.has_edge(t.edge)]
    if len(usable) < 3:
        raise ValueError("need at least 3 households with normal-day timings")
    y = np.asarray([t.delay for t in usable])
    sig = np.asarray([max(t.sd, 0.5) for t in usable])

    # initial guess: delay = delta + s * path distance (uniform slowness)
    d = np.asarray([tree.dist[tree.attach(t.edge, t.frac)] for t in usable])
    A = np.column_stack([np.ones_like(d), d])
    (delta, s_bar), *_ = np.linalg.lstsq(A / sig[:, None], y / sig, rcond=None)
    if delta < 0 or s_bar <= 0:
        delta = 0.0
        s_bar = float(np.sum(y * d / sig**2) / max(np.sum(d * d / sig**2), 1e-9))
    s_bar = float(np.clip(s_bar, 1e-4, 1.0))
    slow = np.full(n_pipes, s_bar)

    lengths = streets.length[pipe_edges]
    prior_w = np.sqrt(lengths / lengths.mean()) / (prior_rel_sd * s_bar)
    it = 0
    prev_parent = None
    for it in range(1, max_iter + 1):
        tree = FlowTree(streets, pipe_edges, slow, inlet, max_piece=max_piece)
        W = np.vstack([_path_weights(tree, tree.attach(t.edge, t.frac), n_pipes) for t in usable])
        A = np.zeros((len(usable) + n_pipes, 1 + n_pipes))
        b = np.zeros(len(usable) + n_pipes)
        A[: len(usable), 0] = 1.0 / sig
        A[: len(usable), 1:] = W / sig[:, None]
        b[: len(usable)] = y / sig
        A[len(usable):, 1:] = np.diag(prior_w)
        b[len(usable):] = prior_w * s_bar
        x, *_ = np.linalg.lstsq(A, b, rcond=None)
        A_last, x_last = A, x
        delta = float(np.clip(x[0], 0.0, 15.0))
        slow = np.clip(x[1:], s_bar / 5, s_bar * 5)
        same = prev_parent is not None and np.array_equal(prev_parent, tree.parent)
        prev_parent = tree.parent.copy()
        if same:
            break
    tree = FlowTree(streets, pipe_edges, slow, inlet, max_piece=max_piece)
    pred = np.asarray([delta + tree.T[tree.attach(t.edge, t.frac)] for t in usable])
    resid = y - pred
    rsd = float(1.4826 * np.median(np.abs(resid - np.median(resid))))
    arrival = {t.household: max(0.0, t.delay - delta) for t in usable}
    # Laplace covariance of the linear solve, inflated by the reduced chi-square
    # when households' habits scatter more than their own day-to-day spread
    chi2 = float(np.sum((resid / sig) ** 2) / max(len(usable) - 1, 1))
    cov = np.linalg.pinv(A_last.T @ A_last) * max(1.0, chi2)
    return TravelTimeFit(tree=tree, slowness=slow, delta=delta, residual_sd=max(rsd, 0.5),
                         iterations=it, tap_arrival=arrival, mean=x_last, cov=cov, s_bar=s_bar,
                         inlet=inlet)


def sample_fit(fit: TravelTimeFit, rng: np.random.Generator) -> TravelTimeFit:
    """One draw of the per-pipe slowness from the fit's posterior, and the
    first-arrival tree it implies. Where the timings cannot tell which way
    water runs round a loop, draws route it differently."""
    if fit.mean is None or fit.cov is None:
        raise ValueError("fit has no posterior to sample from")
    C = (fit.cov + fit.cov.T) / 2
    w, V = np.linalg.eigh(C)
    z = V @ (np.sqrt(np.clip(w, 0.0, None)) * rng.standard_normal(len(w)))
    x = fit.mean + z
    slow = np.clip(x[1:], fit.s_bar / 5, fit.s_bar * 5)
    t = fit.tree
    tree = FlowTree(t.streets, t.pipe_edges, slow, fit.inlet, max_piece=t.max_piece)
    return TravelTimeFit(tree=tree, slowness=slow, delta=fit.delta, residual_sd=fit.residual_sd,
                         iterations=0, tap_arrival=fit.tap_arrival, s_bar=fit.s_bar, inlet=fit.inlet)


def ensemble_fits(streets: StreetGraph, pipe_edges: np.ndarray, inlet: int, taps: list[Tap],
                  n: int = 8, seed: int = 0, mode: str = "mixed") -> list[TravelTimeFit]:
    """The best fit first, then ``n - 1`` more plausible fits. The locator
    averages over their flow trees, so routing that timings cannot pin down
    widens the search instead of misleading it.

    * ``posterior`` — draws from the best fit's (Laplace) posterior;
    * ``bootstrap`` — refits on households resampled with replacement;
    * ``mixed`` — half of each.
    """
    best = fit_travel_times(streets, pipe_edges, inlet, taps)
    rng = np.random.default_rng(seed)
    fits = [best]
    n_boot = {"posterior": 0, "bootstrap": n - 1, "mixed": (n - 1) // 2}[mode]
    for _ in range(n_boot):
        sample = [taps[i] for i in rng.integers(0, len(taps), len(taps))]
        try:
            fits.append(fit_travel_times(streets, pipe_edges, inlet, sample))
        except ValueError:
            continue
    while len(fits) < n:
        fits.append(sample_fit(best, rng))
    return fits


def taps_from_baselines(households: list[dict], baselines) -> list[Tap]:
    """Households with a learned delay become timing observations."""
    out = []
    for h in households:
        b = baselines.taps.get(h["id"])
        if b is not None and b.delay_med is not None:
            out.append(Tap(household=h["id"], edge=int(h["edge"]), frac=float(h["frac"]),
                           delay=float(b.delay_med), sd=float(b.delay_sd)))
    return out
