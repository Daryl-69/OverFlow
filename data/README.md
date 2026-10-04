# data/

Everything here is simulated or generated; there is no personal data.

| Path | What it is | Regenerate with |
| --- | --- | --- |
| `scenarios/demo.json` | The outbreak replay the dashboard shows: a synthetic ward, its pipes and households, normal-day baselines, two outbreak supplies photo by photo, and the locator's output after every photo | `upstream simulate` |
| `evaluation.json` | Headline numbers over many simulated outbreaks, plus one row per run | `upstream evaluate` |
| `streets/` | Real street grids downloaded with `upstream fetch-streets` (none bundled: OpenStreetMap was not reachable when this was built) | `upstream fetch-streets --place "Bhagirathpura, Indore"` |

Street data fetched from OpenStreetMap is © OpenStreetMap contributors, available under the
Open Database License (ODbL).
