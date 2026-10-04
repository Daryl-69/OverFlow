"""The first-arrival flow tree.

When supply switches on, the water front spreads out from the inlet and
reaches every point of the network along its fastest path. Those fastest
paths form a tree — the *flow tree* — even when the pipes themselves form
loops. Contamination picked up by the front at a breach therefore reaches
exactly the taps in the breach's subtree, and nothing upstream of it.

:class:`FlowTree` computes that tree from a set of pipes laid along street
edges plus a slowness (minutes per metre) for each pipe, then splits every
pipe into pieces of at most ``max_piece`` metres. Every piece is a candidate
breach and is identified by its downstream (child) node.

A pipe that closes a loop is filled from both ends; the two fronts meet
part-way along it. Such a pipe becomes two dead-end chains that meet at the
meeting point, which is what the front actually does.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass

import numpy as np

from .sim.streets import StreetGraph


@dataclass
class Chain:
    """Tree nodes laid along one pipe, starting at a street node.

    ``fracs`` are positions measured from the street edge's first endpoint
    (``streets.edges[e][0]``), so they decrease along a chain that starts at
    the second endpoint.
    """

    nodes: np.ndarray
    fracs: np.ndarray


class FlowTree:
    def __init__(
        self,
        streets: StreetGraph,
        pipe_edges: np.ndarray,
        slowness: np.ndarray,
        inlet: int,
        max_piece: float = 20.0,
    ) -> None:
        self.streets = streets
        self.pipe_edges = np.asarray(pipe_edges, dtype=np.int64)
        self.slowness = np.asarray(slowness, dtype=float)
        if self.slowness.shape != self.pipe_edges.shape:
            raise ValueError("slowness must align with pipe_edges")
        if np.any(self.slowness <= 0):
            raise ValueError("slowness must be positive")
        self.inlet = int(inlet)
        self.max_piece = float(max_piece)
        self.k_of_edge = {int(e): k for k, e in enumerate(self.pipe_edges)}
        if len(self.k_of_edge) != len(self.pipe_edges):
            raise ValueError("duplicate pipe edges")
        self._build()

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        st = self.streets
        n_street = st.n_nodes
        adj: list[list[tuple[int, int]]] = [[] for _ in range(n_street)]
        for k, e in enumerate(self.pipe_edges):
            a, b = (int(v) for v in st.edges[e])
            adj[a].append((b, k))
            adj[b].append((a, k))
        if not adj[self.inlet]:
            raise ValueError("inlet is not on any pipe")

        t_street = np.full(n_street, np.inf)
        pred = np.full(n_street, -1, dtype=np.int64)
        t_street[self.inlet] = 0.0
        heap = [(0.0, self.inlet)]
        done = np.zeros(n_street, dtype=bool)
        while heap:
            t, u = heapq.heappop(heap)
            if done[u]:
                continue
            done[u] = True
            for v, k in adj[u]:
                nt = t + st.length[self.pipe_edges[k]] * self.slowness[k]
                if nt < t_street[v] - 1e-12:
                    t_street[v] = nt
                    pred[v] = k
                    heapq.heappush(heap, (nt, v))
        self.street_time = t_street

        px: list[float] = []
        py: list[float] = []
        parent: list[int] = []
        plen: list[float] = []
        tt: list[float] = []
        pedge: list[int] = []
        snode: list[int] = []

        def add(x: float, y: float, par: int, length: float, t: float, k: int, s: int) -> int:
            px.append(x)
            py.append(y)
            parent.append(par)
            plen.append(length)
            tt.append(t)
            pedge.append(k)
            snode.append(s)
            return len(px) - 1

        sid = np.full(n_street, -1, dtype=np.int64)
        for n in np.flatnonzero(done):
            sid[n] = add(float(st.x[n]), float(st.y[n]), -1, 0.0, float(t_street[n]), -1, int(n))
        self.root = int(sid[self.inlet])
        self.tree_node_of_street = sid

        chains: list[list[Chain]] = [[] for _ in range(len(self.pipe_edges))]
        self.meet_frac = np.full(len(self.pipe_edges), np.nan)

        def lay_chain(k: int, start: int, f0: float, f1: float, end_node: int | None) -> Chain:
            """Lay pieces along pipe k from street node ``start`` (at fraction
            f0) to fraction f1. If ``end_node`` is given it becomes the last
            node of the chain (a street node already in the tree)."""
            e = self.pipe_edges[k]
            span = abs(f1 - f0) * st.length[e]
            n_pieces = max(1, math.ceil(span / self.max_piece - 1e-9))
            piece = span / n_pieces
            s = self.slowness[k]
            nodes = [int(sid[start])]
            fracs = [f0]
            prev = int(sid[start])
            for i in range(1, n_pieces + 1):
                f = f0 + (f1 - f0) * i / n_pieces
                t = tt[prev] + piece * s
                if i == n_pieces and end_node is not None:
                    node = int(sid[end_node])
                    parent[node] = prev
                    plen[node] = piece
                    pedge[node] = k
                else:
                    x, y = st.point_on_edge(int(e), f)
                    node = add(x, y, prev, piece, t, k, -1)
                nodes.append(node)
                fracs.append(f)
                prev = node
            return Chain(np.asarray(nodes, dtype=np.int64), np.asarray(fracs, dtype=float))

        # Tree pipes in order of arrival so chain times build on final parents.
        order = np.argsort(t_street, kind="stable")
        for v in order:
            if not done[v] or v == self.inlet:
                continue
            k = int(pred[v])
            a, b = (int(x) for x in st.edges[self.pipe_edges[k]])
            if v == b:
                chains[k].append(lay_chain(k, a, 0.0, 1.0, b))
            else:
                chains[k].append(lay_chain(k, b, 1.0, 0.0, a))

        tree_pipe = set(int(k) for k in pred[pred >= 0])
        for k, e in enumerate(self.pipe_edges):
            if k in tree_pipe:
                continue
            a, b = (int(x) for x in st.edges[e])
            if not (done[a] and done[b]):
                continue  # pipe not connected to the inlet
            w = st.length[e] * self.slowness[k]
            fm = float(np.clip((t_street[b] - t_street[a] + w) / (2 * w), 0.0, 1.0))
            self.meet_frac[k] = fm
            if fm * st.length[e] > 1e-6:
                chains[k].append(lay_chain(k, a, 0.0, fm, None))
            if (1 - fm) * st.length[e] > 1e-6:
                chains[k].append(lay_chain(k, b, 1.0, fm, None))

        self.chains = chains
        self.x = np.asarray(px)
        self.y = np.asarray(py)
        self.parent = np.asarray(parent, dtype=np.int64)
        self.piece_len = np.asarray(plen)
        self.T = np.asarray(tt)
        self.pipe_of = np.asarray(pedge, dtype=np.int64)
        self.street_node = np.asarray(snode, dtype=np.int64)
        self.lat, self.lon = st.to_latlon(self.x, self.y)
        self._index()

    def _index(self) -> None:
        n = len(self.parent)
        children: list[list[int]] = [[] for _ in range(n)]
        for c in range(n):
            p = self.parent[c]
            if p >= 0:
                children[p].append(c)
        self.children = children
        self.n_children = np.asarray([len(c) for c in children], dtype=np.int64)
        tin = np.full(n, -1, dtype=np.int64)
        tout = np.full(n, -1, dtype=np.int64)
        dist = np.zeros(n)
        junc = np.zeros(n, dtype=np.int64)
        order: list[int] = []
        stack = [(self.root, False)]
        while stack:
            u, leaving = stack.pop()
            if leaving:
                tout[u] = len(order) - 1
                continue
            tin[u] = len(order)
            order.append(u)
            stack.append((u, True))
            for c in reversed(children[u]):
                dist[c] = dist[u] + self.piece_len[c]
                junc[c] = junc[u] + (1 if len(children[u]) >= 2 else 0)
                stack.append((c, False))
        if len(order) != n:
            raise RuntimeError("flow tree is not connected")
        self.order = np.asarray(order, dtype=np.int64)
        self.tin = tin
        self.tout = tout
        self.dist = dist
        self.junctions = junc
        self.subtree_len = np.zeros(n)
        for u in reversed(order):
            p = self.parent[u]
            if p >= 0:
                self.subtree_len[p] += self.subtree_len[u] + self.piece_len[u]
        self.pieces = np.flatnonzero(self.parent >= 0)
        pp = self.parent[self.pieces]
        self.mid_x = (self.x[pp] + self.x[self.pieces]) / 2
        self.mid_y = (self.y[pp] + self.y[self.pieces]) / 2
        self.mid_lat, self.mid_lon = self.streets.to_latlon(self.mid_x, self.mid_y)
        self.piece_index = np.full(n, -1, dtype=np.int64)
        self.piece_index[self.pieces] = np.arange(len(self.pieces))

    # ---------------------------------------------------------------- queries
    @property
    def n_nodes(self) -> int:
        return len(self.parent)

    @property
    def total_length(self) -> float:
        return float(self.piece_len.sum())

    def has_edge(self, edge: int) -> bool:
        k = self.k_of_edge.get(int(edge))
        return k is not None and bool(self.chains[k])

    def _chain_for(self, edge: int, frac: float) -> Chain:
        k = self.k_of_edge.get(int(edge))
        if k is None:
            raise KeyError(f"street edge {edge} carries no pipe in this network")
        chains = self.chains[k]
        if not chains:
            raise ValueError(f"pipe on street edge {edge} is not connected to the inlet")
        if len(chains) == 1:
            return chains[0]
        fm = self.meet_frac[k]
        return chains[0] if frac <= fm else chains[1]

    def attach(self, edge: int, frac: float) -> int:
        """Tree node nearest to the point ``frac`` along street ``edge`` (on
        the side of the meeting point the point lies on)."""
        ch = self._chain_for(edge, frac)
        return int(ch.nodes[int(np.argmin(np.abs(ch.fracs - frac)))])

    def piece_at(self, edge: int, frac: float) -> int:
        """The piece (its child node) containing the point ``frac`` along
        street ``edge``."""
        ch = self._chain_for(edge, frac)
        lo = np.minimum(ch.fracs[:-1], ch.fracs[1:])
        hi = np.maximum(ch.fracs[:-1], ch.fracs[1:])
        hit = np.flatnonzero((frac >= lo - 1e-12) & (frac <= hi + 1e-12))
        if hit.size == 0:
            # numerical edge: nearest piece
            i = int(np.argmin(np.minimum(np.abs(lo - frac), np.abs(hi - frac))))
        else:
            i = int(hit[0])
        return int(ch.nodes[i + 1])

    def locate_point(self, edge: int, frac: float) -> tuple[int, float]:
        """(piece containing the point, metres from the point down to that
        piece's child node)."""
        ch = self._chain_for(edge, frac)
        child = self.piece_at(edge, frac)
        i = int(np.flatnonzero(ch.nodes == child)[0])
        off = abs(float(ch.fracs[i]) - frac) * float(self.streets.length[edge])
        return child, off

    def downstream(self, cands: np.ndarray, nodes: np.ndarray) -> np.ndarray:
        """Boolean (len(cands), len(nodes)): node lies in the subtree of the
        candidate piece, i.e. the front reaches it after passing the piece."""
        c = np.asarray(cands)[:, None]
        m = np.asarray(nodes)[None, :]
        return (self.tin[m] >= self.tin[c]) & (self.tin[m] <= self.tout[c])

    def distance_down(self, cands: np.ndarray, nodes: np.ndarray) -> np.ndarray:
        """Pipe distance (m) from each candidate's midpoint down to each node.
        Only meaningful where :meth:`downstream` is true."""
        c = np.asarray(cands)[:, None]
        m = np.asarray(nodes)[None, :]
        return self.dist[m] - self.dist[c] + self.piece_len[c] / 2.0

    def junctions_between(self, cands: np.ndarray, nodes: np.ndarray) -> np.ndarray:
        c = np.asarray(cands)[:, None]
        m = np.asarray(nodes)[None, :]
        return self.junctions[m] - self.junctions[c]

    def arrival_mid(self, cands: np.ndarray) -> np.ndarray:
        """Front arrival time (min after supply start) at piece midpoints."""
        c = np.asarray(cands)
        return (self.T[self.parent[c]] + self.T[c]) / 2.0

    def path_to_root(self, node: int) -> list[int]:
        out = []
        while node >= 0:
            out.append(int(node))
            node = int(self.parent[node])
        return out

    def piece_segments(self) -> np.ndarray:
        """(n_pieces, 2, 2) array of [[lat, lon], [lat, lon]] from parent to child."""
        p = self.parent[self.pieces]
        c = self.pieces
        return np.stack(
            [np.column_stack([self.lat[p], self.lon[p]]), np.column_stack([self.lat[c], self.lon[c]])],
            axis=1,
        )
