import math

import numpy as np
import pytest

from upstream.locate.advisories import ampm
from upstream.locate.baseline import learn_baselines
from upstream.locate.levels import AMBER, GREEN, RED, apply_levels, classify, complaint_clusters
from upstream.locate.planner import info_gain, place_sentinels, rank_households
from upstream.locate.rebuild import rebuild_layout, score_rebuild
from upstream.locate.traveltime import fit_travel_times, sample_fit, taps_from_baselines
from upstream.locate.triangulate import _log_ndtr, plan_b
from upstream.reports import Report, clock
from upstream.scenario import ScenarioConfig, run_scenario
from upstream.sim.outbreak import simulate_cycle
from upstream.sim.world import make_world


def _rep(hid, turb=None, cl=None, answer=None, cycle=0, t=30.0, rid="r"):
    return Report(id=rid, household=hid, cycle=cycle, supply_start=0.0, t_obs=t, turbidity=turb,
                  chlorine=cl, answer=answer)


@pytest.fixture(scope="module")
def ward(streets):
    w = make_world(streets, seed=3)
    rng = np.random.default_rng(0)
    base = []
    for c in range(12):
        base += simulate_cycle(w, c, rng)
    return w, base, learn_baselines(base)


def test_baselines_are_robust(ward):
    w, base, b = ward
    h = w.households[0]
    tb = b.taps[h.id]
    assert abs(tb.turb_med - h.turb_base) < 0.03
    # median delay = arrival + habit
    arrival = w.tree.T[w.tree.attach(h.edge, h.frac)]
    assert abs(tb.delay_med - (arrival + h.habit_delay)) < 1.5
    unknown = b.get("H-9999")
    assert unknown.delay_med is None and unknown.turb_med == b.population.turb_med


def test_levels(ward):
    w, base, b = ward
    hid = w.households[0].id
    tb = b.taps[hid]
    assert classify(_rep(hid, tb.turb_med, 0.6), b)[0] == GREEN
    assert classify(_rep(hid, tb.turb_med, 0.15), b)[0] == AMBER             # chlorine below 0.2
    assert classify(_rep(hid, tb.turb_med + 0.3, 0.5), b)[0] == AMBER        # turbidity spike only
    assert classify(_rep(hid, tb.turb_med + 0.3, 0.05), b)[0] == RED         # spike + chlorine near zero
    assert classify(_rep(hid, tb.turb_med + 0.3, None), b)[0] == AMBER       # no strip: cannot be red alone


def test_complaint_cluster_rule(ward):
    w, base, b = ward
    pos = {"A": (0, 0), "B": (60, 0), "C": (0, 80), "D": (900, 900)}
    reps = [_rep("A", answer="smell", rid="1"), _rep("B", answer="dirty", rid="2"),
            _rep("C", answer="ill", rid="3"), _rep("D", answer="smell", rid="4")]
    cl = complaint_clusters(reps, pos)
    assert cl == [{"1", "2", "3"}]
    assert complaint_clusters(reps[:2] + reps[3:], pos) == []
    # the same home complaining twice is one complaint
    assert complaint_clusters([reps[0], _rep("A", answer="dirty", rid="5"), reps[1]], pos) == []


def test_travel_time_fit_on_a_tree_layout(ward, streets):
    w, base, b = ward
    hh = [h.public(streets) for h in w.households]
    taps = taps_from_baselines(hh, b)
    # a pure tree layout (no loops): the fitted flow tree must equal the true one
    tree_edges = np.asarray(sorted(set(int(w.tree.pipe_edges[w.tree.pipe_of[n]]) for n in w.tree.pieces
                                       if np.isnan(w.tree.meet_frac[w.tree.pipe_of[n]]))))
    fit = fit_travel_times(streets, tree_edges, w.inlet, taps)
    nodes = [fit.tree.attach(h.edge, h.frac) for h in w.households if fit.tree.has_edge(h.edge)]
    assert len(nodes) > 50
    assert 0.0 <= fit.delta <= 15.0 and fit.residual_sd < 3.0
    draw = sample_fit(fit, np.random.default_rng(1))
    assert draw.tree.n_nodes == fit.tree.n_nodes        # same pipes; only times change


def test_rebuild_recovers_most_of_the_network(ward, streets):
    w, base, b = ward
    hh = [h.public(streets) for h in w.households]
    taps = taps_from_baselines(hh, b)
    homes = [(h.edge, h.frac) for h in w.households]
    layout = rebuild_layout(streets, w.inlet, taps, extra=homes)
    assert set(h.edge for h in w.households) <= set(int(e) for e in layout)
    fit = fit_travel_times(streets, layout, w.inlet, taps)
    sc = score_rebuild(streets, w.pipe_edges, layout, w.tree, fit.tree, homes)
    assert sc["pipe_precision"] > 0.8
    assert sc["downstream_agreement"] > 0.9


def test_demo_outbreak_is_located(demo):
    f = demo.final
    assert f["alert"]
    g = f["gis"]
    assert g["p_breach"] > 0.99
    assert g["truth_in_region"]
    assert g["error_m"] < 100
    assert g["region_length_m"] < 300
    # better than handling complaints one ticket at a time
    assert g["error_m"] < f["complaints"]["centroid_error_m"]


def test_posterior_is_normalised_and_ring_shrinks(demo):
    frames = [fr for fr in demo.frames if fr.get("gis")]
    assert frames, "alert must start triangulation"
    first, last = frames[0]["gis"], frames[-1]["gis"]
    assert last["ring"]["radius_m"] <= first["ring"]["radius_m"]
    post = demo.gis.locate(demo.outbreak_reports())
    assert post.prob.sum() == pytest.approx(1.0) and post.joint.sum() == pytest.approx(1.0)
    assert np.all(post.prob >= 0)


def test_advisories_go_downstream_only(demo):
    adv = {a["household"]: a for a in demo.final["advisories"]}
    tree = demo.world.tree
    piece = tree.piece_at(demo.breach.edge, demo.breach.frac)
    downstream = set()
    for h in demo.world.households:
        if tree.downstream(np.array([piece]), np.array([tree.attach(h.edge, h.frac)]))[0, 0]:
            downstream.add(h.id)
    alerted = {h for h, a in adv.items() if a["status"] != "clear"}
    assert len(downstream & alerted) >= 0.8 * len(downstream)
    # few homes outside the true downstream set are alarmed
    assert len(alerted - downstream) <= max(3, 0.5 * len(downstream))
    for h in alerted:
        a = adv[h]
        assert a["safe_after"] > a["first_water"]
        assert "first fill for cleaning" in a["message"] or "stored or boiled" in a["message"]


def test_plan_b_contains_truth_on_the_demo(demo):
    pb = plan_b(demo.gis, demo.outbreak_reports())
    seg = demo.gis.seg
    br = demo.breach
    s = int(np.flatnonzero((seg.edge == br.edge) & (seg.a_frac <= br.frac) & (seg.b_frac >= br.frac))[0])
    assert pb["violations"] is not None and pb["length_m"] > 0
    assert s in pb["segments"] or pb["violations"] > 0


def test_pranks_barely_move_the_posterior(demo):
    reps = demo.outbreak_reports()
    post = demo.gis.locate(reps)
    far = max(demo.world.households, key=lambda h: math.hypot(*demo.streets.point_on_edge(h.edge, h.frac)))
    prank = Report(id="prank-1", household=far.id, cycle=reps[-1].cycle, supply_start=reps[-1].supply_start,
                   t_obs=reps[-1].supply_start + 400, turbidity=0.9, chlorine=None, answer="dirty")
    post2 = demo.gis.locate(reps + [prank])
    assert post2.map_seg == post.map_seg


def test_planner(demo):
    loc = demo.gis
    post = loc.locate(demo.outbreak_reports())
    ranked = rank_households(loc, post.joint, [h.id for h in demo.world.households], top=5)
    assert len(ranked) == 5 and ranked[0]["gain_bits"] >= ranked[-1]["gain_bits"] >= 0
    nodes = np.asarray([loc.node_of(demo.world.households[0].id, k) for k in range(len(loc.trees))])
    g, pd = info_gain(loc, post.joint, nodes)
    assert 0 <= g <= 1.0 + 1e-9 and 0 <= pd <= 1.0 + 1e-9
    picks = place_sentinels(loc, [h.id for h in demo.world.households], 6)
    bits = [p["signature_bits"] for p in picks]
    assert len(picks) == 6 and all(b2 > b1 for b1, b2 in zip(bits, bits[1:]))


def test_no_breach_means_low_evidence():
    sc = run_scenario(ScenarioConfig(seed=5, outbreak_cycles=0, frames=False))
    rng = np.random.default_rng(9)
    clean = simulate_cycle(sc.world, 20, rng, prank_rate=0.0)
    apply_levels(clean, sc.baselines, sc.positions())
    post = sc.gis.locate(clean)
    assert post.p_breach < 0.5


def test_helpers():
    assert clock(0) == "Day 1 00:00" and clock(1440 + 6 * 60 + 5) == "Day 2 06:05"
    assert ampm(7 * 60 + 14) == "7:14 am" and ampm(13 * 60) == "1:00 pm" and ampm(0) == "12:00 am"
    assert ampm(7 * 60 + 13.2) == "7:14 am"      # rounds up: never earlier than safe
    z = np.array([-40.0, -5.0, 0.0, 3.0])
    ref = np.log(0.5 * np.array([math.erfc(-v / math.sqrt(2)) for v in z[1:]]))
    assert np.allclose(_log_ndtr(z)[1:], ref, rtol=1e-6)
    assert np.isfinite(_log_ndtr(z)[0]) and _log_ndtr(z)[0] < -790
