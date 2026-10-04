import math

import numpy as np
import pytest

from upstream.geo import haversine_m, project, unproject
from upstream.network import FlowTree
from upstream.physics import SlugPhysics
from upstream.sim.streets import StreetGraph


def _square(slow_right=1.0):
    """Inlet 0 at the left of a 100 m square loop 0-1-2-3 with a tail 2-4.

        3 --- 2 --- 4
        |     |
        0 --- 1
    """
    x = np.array([0.0, 100.0, 100.0, 0.0, 200.0])
    y = np.array([0.0, 0.0, 100.0, 100.0, 100.0])
    lat, lon = unproject(x, y, (0.0, 0.0))
    edges = np.array([[0, 1], [1, 2], [2, 3], [3, 0], [2, 4]])
    st = StreetGraph("square", lat, lon, edges, synthetic=True, attribution="test", origin=(0.0, 0.0))
    slow = np.array([1.0, slow_right, 1.0, 1.0, 1.0]) * 0.02
    return st, FlowTree(st, np.arange(5), slow, inlet=0)


def test_projection_round_trip():
    lat, lon = 22.74, 75.86
    x, y = project(lat + 0.001, lon + 0.002, (lat, lon))
    la, lo = unproject(x, y, (lat, lon))
    assert abs(la - (lat + 0.001)) < 1e-9 and abs(lo - (lon + 0.002)) < 1e-9
    assert abs(math.hypot(x, y) - haversine_m(lat, lon, lat + 0.001, lon + 0.002)) < 0.5


def test_flow_tree_times_and_pieces():
    st, t = _square()
    assert t.piece_len.max() <= 20.0 + 1e-9
    assert math.isclose(t.total_length, st.length.sum(), rel_tol=1e-9)
    # arrival time = parent + length * slowness everywhere
    k = t.pipe_of[t.pieces]
    assert np.allclose(t.T[t.pieces], t.T[t.parent[t.pieces]] + t.piece_len[t.pieces] * t.slowness[k])
    # node 2 is equidistant both ways round the loop; the far corner is reached in 4 min
    n2 = t.tree_node_of_street[2]
    assert math.isclose(t.T[n2], 200 * 0.02)


def test_loop_pipe_is_split_at_the_meeting_point():
    st, t = _square(slow_right=2.0)   # pipe 0-1 side is fast, 1-2 slow
    # 2 is reached via 3 (0->3->2 = 4 min) not via 1 (2 + 4 = 6 min); pipe 1-2 is filled from both ends
    k12 = t.k_of_edge[1]
    assert not np.isnan(t.meet_frac[k12])
    # fronts meet where T1 + f*L*s12 == T2 + (1-f)*L*s12
    T1, T2, w = 2.0, 4.0, 100 * 0.04
    assert math.isclose(t.meet_frac[k12], (T2 - T1 + w) / (2 * w))
    a = t.attach(1, 0.1)    # near node 1 -> on node 1's chain
    b = t.attach(1, 0.9)    # near node 2 -> on node 2's chain
    assert t.tree_node_of_street[1] in t.path_to_root(a)
    assert t.tree_node_of_street[2] in t.path_to_root(b)
    assert t.tree_node_of_street[1] not in t.path_to_root(b)


def test_downstream_relations():
    st, t = _square(slow_right=2.0)
    tail = t.attach(4, 1.0)
    piece_on_tail = t.piece_at(4, 0.5)
    piece_upstream = t.piece_at(3, 0.5)    # pipe 3-0, carries water 0 -> 3 -> 2 -> 4
    near1 = t.attach(1, 0.05)
    d = t.downstream(np.array([piece_on_tail, piece_upstream]), np.array([tail, near1]))
    assert d[0, 0] and not d[0, 1]
    assert d[1, 0] and not d[1, 1]
    dist = t.distance_down(np.array([piece_upstream]), np.array([tail]))[0, 0]
    # from the middle of pipe 3-0 (50 m from 3) via 3 -> 2 -> 4: 50 + 100 + 100
    assert abs(dist - 250.0) < 10.0 + 1e-6


def test_locate_point_offsets():
    st, t = _square()
    child, off = t.locate_point(4, 0.55)
    assert 0 <= off <= 20.0
    assert child == t.piece_at(4, 0.55)


def test_unknown_edge_raises():
    st, t = _square()
    with pytest.raises(KeyError):
        FlowTree(st, np.array([0, 1]), np.array([0.02, 0.02]), inlet=0).attach(4, 0.5)


def test_slug_physics_monotone():
    ph = SlugPhysics()
    d = np.array([0, 50, 100, 400])
    a = ph.dilution(d, 0)
    assert a[0] == 1.0 and np.all(np.diff(a) < 0)
    assert ph.dilution(0, 2) == pytest.approx(ph.mix_per_junction ** 2)
    w = ph.window(d)
    assert np.all(np.diff(w) > 0)
    assert np.all(ph.clear_after(d) > w)
    assert ph.profile(0, 0) == 1.0 and ph.profile(-1, 0) == 0.0
    assert ph.profile(ph.clear_after(0, 0.05), 0) == pytest.approx(0.05, rel=1e-6)
