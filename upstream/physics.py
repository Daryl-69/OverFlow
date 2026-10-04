"""How a contaminated slug behaves once the supply front picks it up.

Between supply cycles sewage seeps into the emptied pipe at the breach. When
supply resumes, the advancing front entrains that slug and carries it
downstream, so homes downstream of the breach get dirty *first* water while
homes upstream never see it (README §4).

On the way the slug

* spreads out by longitudinal dispersion — its peak concentration falls as
  ``1/sqrt(1 + d/L)`` with pipe distance ``d`` (mass is conserved while the
  slug stretches), and it takes longer to pass, ``W0 * sqrt(1 + d/L)``;
* is diluted further at every junction it passes, where the front mixes with
  residual clean water held in the branch pipes (factor ``mix`` per junction).

The same functions are used by the simulator (with "true" parameters) and by
the locator (with its own, deliberately different, estimates).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class SlugPhysics:
    mix_per_junction: float = 0.8
    dispersion_m: float = 100.0
    window0_min: float = 6.0
    tail_frac: float = 0.25
    # measurement response to a contamination level s (s = 1 ~ raw slug)
    turbidity_per_unit: float = 0.5   # rise in turbidity index
    chlorine_per_unit: float = 1.2    # mg/L of free chlorine consumed

    def dilution(self, dist_m, junctions):
        d = np.maximum(np.asarray(dist_m, dtype=float), 0.0)
        return np.power(self.mix_per_junction, junctions) / np.sqrt(1.0 + d / self.dispersion_m)

    def window(self, dist_m):
        """Minutes for which the slug keeps a tap at its peak concentration."""
        d = np.maximum(np.asarray(dist_m, dtype=float), 0.0)
        return self.window0_min * np.sqrt(1.0 + d / self.dispersion_m)

    def profile(self, elapsed_min, dist_m):
        """Fraction of peak concentration ``elapsed_min`` after first water."""
        w = self.window(dist_m)
        e = np.asarray(elapsed_min, dtype=float)
        over = np.maximum(e - w, 0.0)
        return np.where(e < 0, 0.0, np.exp(-over / (self.tail_frac * w)))

    def clear_after(self, dist_m, residual: float = 0.05):
        """Minutes after first water until concentration drops below
        ``residual`` of its peak."""
        w = self.window(dist_m)
        return w * (1.0 + self.tail_frac * math.log(1.0 / residual))
