from __future__ import annotations

import json
from pathlib import Path

import pytest

from upstream.scenario import ScenarioConfig, run_scenario, scenario_json
from upstream.sim.streets import synthetic_streets

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def streets():
    return synthetic_streets()


@pytest.fixture(scope="session")
def demo():
    """One full outbreak scenario (with replay frames), shared by the tests."""
    return run_scenario(ScenarioConfig(seed=7, min_downstream_homes=6))


@pytest.fixture(scope="session")
def demo_json(demo):
    return scenario_json(demo)


@pytest.fixture(scope="session")
def scenario_file(tmp_path_factory, demo_json):
    p = tmp_path_factory.mktemp("scenario") / "demo.json"
    p.write_text(json.dumps(demo_json))
    return p
