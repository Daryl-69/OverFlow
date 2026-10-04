"""Per-tap baselines learned from normal days (README §5.5: every tap gets its
own baseline, because water behaves differently across and within networks)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass

import numpy as np

from ..reports import Report

MIN_READINGS = 3
DELAY_SD_FLOOR = 1.0     # minutes
TURB_SD_FLOOR = 0.015    # turbidity index
CL_SD_FLOOR = 0.08       # mg/L


@dataclass
class TapBaseline:
    n: int = 0
    delay_med: float | None = None    # minutes from supply start to photo
    delay_sd: float | None = None
    turb_med: float | None = None
    turb_sd: float | None = None
    n_cl: int = 0
    cl_med: float | None = None
    cl_sd: float | None = None

    def to_dict(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def _robust(values) -> tuple[float, float]:
    v = np.asarray(values, dtype=float)
    med = float(np.median(v))
    mad = float(np.median(np.abs(v - med))) * 1.4826
    return med, mad


@dataclass
class Baselines:
    taps: dict[str, TapBaseline]
    population: TapBaseline

    def get(self, household: str) -> TapBaseline:
        """The tap's own baseline where it has enough history, filled in from
        the population baseline where it does not. Timing has no population
        fallback (``delay_med`` stays None): arrival time depends on where the
        tap sits, so the locator predicts it from the flow model instead."""
        own = self.taps.get(household, TapBaseline())
        pop = self.population
        out = TapBaseline(n=own.n, n_cl=own.n_cl,
                          delay_med=own.delay_med, delay_sd=own.delay_sd)
        if own.turb_med is not None:
            out.turb_med, out.turb_sd = own.turb_med, own.turb_sd
        else:
            out.turb_med, out.turb_sd = pop.turb_med, pop.turb_sd
        if own.cl_med is not None:
            out.cl_med, out.cl_sd = own.cl_med, own.cl_sd
        else:
            out.cl_med, out.cl_sd = pop.cl_med, pop.cl_sd
        return out

    def to_dict(self) -> dict:
        return {"population": self.population.to_dict(),
                "taps": {k: v.to_dict() for k, v in self.taps.items()}}


def learn_baselines(reports: list[Report]) -> Baselines:
    """Median / MAD statistics per household from normal-day reports.
    Robust statistics keep the odd bad glass from shifting the baseline."""
    by: dict[str, list[Report]] = defaultdict(list)
    for r in reports:
        by[r.household].append(r)
    taps: dict[str, TapBaseline] = {}
    all_turb: list[float] = []
    all_cl: list[float] = []
    for hid, rs in by.items():
        b = TapBaseline(n=len(rs))
        delays = [r.delay for r in rs]
        turbs = [r.turbidity for r in rs if r.turbidity is not None]
        cls = [r.chlorine for r in rs if r.chlorine is not None]
        if len(delays) >= MIN_READINGS:
            m, s = _robust(delays)
            b.delay_med, b.delay_sd = m, max(s, DELAY_SD_FLOOR)
        if len(turbs) >= MIN_READINGS:
            m, s = _robust(turbs)
            b.turb_med, b.turb_sd = m, max(s, TURB_SD_FLOOR)
        b.n_cl = len(cls)
        if len(cls) >= MIN_READINGS:
            m, s = _robust(cls)
            b.cl_med, b.cl_sd = m, max(s, CL_SD_FLOOR)
        taps[hid] = b
        all_turb.extend(turbs)
        all_cl.extend(cls)
    pop = TapBaseline(n=len(reports))
    if all_turb:
        m, s = _robust(all_turb)
        pop.turb_med, pop.turb_sd = m, max(s, TURB_SD_FLOOR)
    else:
        pop.turb_med, pop.turb_sd = 0.08, 0.03
    if all_cl:
        m, s = _robust(all_cl)
        pop.cl_med, pop.cl_sd = m, max(s, CL_SD_FLOOR)
        pop.n_cl = len(all_cl)
    else:
        pop.cl_med, pop.cl_sd = 0.6, 0.25
    return Baselines(taps=taps, population=pop)
