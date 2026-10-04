import json
from pathlib import Path

import networkx as nx
import numpy as np

from upstream.sim.streets import (
    StreetGraph,
    bbox_around,
    load_geojson,
    load_overpass_json,
    load_streets,
    overpass_query,
    synthetic_streets,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_synthetic_is_deterministic_connected_and_not_a_real_place():
    a, b = synthetic_streets(), synthetic_streets()
    assert np.array_equal(a.edges, b.edges) and np.allclose(a.lat, b.lat)
    assert nx.is_connected(a.nx_graph())
    assert a.synthetic and "not a real place" in a.attribution
    assert abs(a.lat.mean()) < 0.01 and abs(a.lon.mean()) < 0.01   # Null Island, by design
    assert 10_000 < a.length.sum() < 25_000


def test_overpass_parser_filters_and_cleans():
    g = load_overpass_json(FIXTURES / "overpass_small.json")
    # footway and area=yes ways dropped, self-loop and duplicate removed, detached way 7-8 dropped
    assert g.n_nodes == 5
    assert g.n_edges == 5
    assert not g.synthetic and "OpenStreetMap" in g.attribution
    assert nx.is_connected(g.nx_graph())
    assert np.all(g.length > 50) and np.all(g.length < 200)


def test_round_trip_and_dispatch(tmp_path):
    g = load_overpass_json(FIXTURES / "overpass_small.json")
    p = tmp_path / "s.json"
    g.save(p)
    h = load_streets(p)
    assert isinstance(h, StreetGraph) and np.array_equal(h.edges, g.edges)
    assert load_streets(FIXTURES / "overpass_small.json").n_edges == 5


def test_geojson_loader(tmp_path):
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[75.86, 22.74], [75.861, 22.74], [75.862, 22.74]]}},
        {"type": "Feature", "geometry": {"type": "MultiLineString", "coordinates": [[[75.861, 22.74], [75.861, 22.741]]]}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [75.0, 22.0]}},
    ]}
    p = tmp_path / "x.geojson"
    p.write_text(json.dumps(fc))
    g = load_geojson(p)
    assert g.n_nodes == 4 and g.n_edges == 3


def test_overpass_query_and_bbox():
    s, w, n, e = bbox_around(22.74, 75.86, 700)
    assert s < 22.74 < n and w < 75.86 < e
    assert abs((n - s) * 111_320 - 1400) < 1
    q = overpass_query((s, w, n, e))
    assert q.startswith("[out:json]") and 'way["highway"]' in q and "(._;>;);out body;" in q
