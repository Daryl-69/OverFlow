"""The unit of evidence: one household's first-glass reading in one supply cycle."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

ANSWERS = ("fine", "smell", "dirty", "ill")
COMPLAINTS = frozenset({"smell", "dirty", "ill"})


@dataclass
class Report:
    id: str
    household: str
    cycle: int
    supply_start: float          # minutes since the scenario epoch
    t_obs: float                 # photo time, minutes since the scenario epoch
    turbidity: float | None      # turbidity index 0..1 from the reader
    chlorine: float | None       # free chlorine, mg/L (None = no strip used)
    answer: str | None = None    # one of ANSWERS, or None
    requested: bool = False      # the app asked this home to test this cycle
    source: str = "sim"          # sim | photo | whatsapp | manual
    level: str | None = None     # green | amber | red (filled by the levels step)
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    reading: dict | None = None  # raw reader output for photo reports
    truth: dict | None = None    # simulation ground truth (never used by the locator)

    @property
    def delay(self) -> float:
        """Minutes from supply start to the photo."""
        return self.t_obs - self.supply_start

    @property
    def complaint(self) -> bool:
        return self.answer in COMPLAINTS

    def to_dict(self, include_truth: bool = True) -> dict:
        d = asdict(self)
        if not include_truth:
            d.pop("truth", None)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Report":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


def clock(t_min: float) -> str:
    """'Day N HH:MM' for minutes since the scenario epoch (day 1 = first day)."""
    day = int(t_min // 1440)
    m = int(round(t_min - day * 1440))
    if m >= 1440:
        day, m = day + 1, m - 1440
    return f"Day {day + 1} {m // 60:02d}:{m % 60:02d}"


def hhmm(t_min: float) -> str:
    m = int(round(t_min)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"
