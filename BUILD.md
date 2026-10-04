# Upstream — the build

This is the working hackathon build of the concept in [README.md](README.md): the printable card, the
photo reader, the outbreak simulator, the breach locator, a household photo page, a WhatsApp bot and the
utility dashboard. Everything runs on one laptop, offline, from one Python package.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
upstream serve                      # http://127.0.0.1:8000/dashboard/
```

`upstream serve` takes a few seconds to start (it rebuilds the ward's learned state). Set `UPSTREAM_TZ`
(for example `Asia/Kolkata`) when the server's clock is not in the households' time zone: phones
record photo times without one. Then open:

| Page | What it is |
| --- | --- |
| `/dashboard/` | Utility dashboard: **Outbreak replay**, **Live intake**, **Household card** |
| `/report` | The household page: photograph the card, send, get the reading back (`/report?h=H-0042` pre-fills the home) |
| `/docs` | Interactive API documentation |

## The two demo moments (README §6)

**1. Live glass test.** Print a card (`upstream card --household H-0042`, print the SVG at 100 % on A5
landscape; the bar at the bottom must measure 50 mm). Open the dashboard's **Live intake** tab, press
**Water is on now**, and scan the QR code with a phone to open the household page. A judge stirs mud
into a glass on the card, dips a strip, and sends one photo. The reading (cloudiness, free chlorine,
green/amber/red) comes back on the phone and the pipes upstream of that home light up on the map.
No printer? **Use a muddy demo photo** renders a synthetic photo of a muddy glass and sends it.

**2. The epicentre map.** The **Outbreak replay** tab replays a Bhagirathpura-style outbreak photo by
photo: amber readings make Upstream ask nearby homes to test at the next supply, the first red reading
starts triangulation, and the uncertainty ring tightens onto one stretch of pipe. Switch **Pipe map**
to *Rebuilt from photo timings* to see the same search with no pipe map at all, and look at each
downstream home's safe-after time.

## What each README component became

| README | Code | Notes |
| --- | --- | --- |
| Card (§5.2) | `upstream/card/` | A5 SVG for printing and a raster renderer, both from one spec: ArUco corner markers, checkerboard under the glass circle plus a clear reference band, strip slot with a pad box, chlorine colour scale, colour patches, household QR code |
| Reading engine (§5.4) | `upstream/reader/core.py` | Homography from the markers; affine colour correction from the patches; strip pad placed on the printed scale in CIELAB; turbidity index = 1 − contrast(under glass) ÷ contrast(reference); blur, glare, overexposure and card-not-found checks; EXIF time; SHA-256 and perceptual hash for re-used photos |
| Signal levels (§5.5) | `upstream/locate/levels.py` | Per-tap robust baselines; IS 10500's 0.2 mg/L minimum; the complaint-cluster rule |
| Timing model and map rebuilding (§5.6) | `upstream/locate/traveltime.py`, `rebuild.py` | Per-pipe travel times fitted to normal-day photo times; the README's greedy rebuilding algorithm |
| Triangulation (§5.7) | `upstream/locate/triangulate.py` | Bayesian scoring of every ≤ 20 m pipe piece, and Plan B (the graph rule) |
| Outputs (§5.8) | `advisories.py`, `planner.py` | Safe-after times, boil-water messages, strip requests, sentinel placement, lanes to recruit |
| Intake (§8) | `upstream/intake/` | FastAPI app, SQLite store, household page, WhatsApp Cloud API webhook |
| Dashboard (§8) | `upstream/dashboard/static/` | Leaflet (vendored, works offline) |
| Simulator (§7) | `upstream/sim/`, `upstream/scenario.py` | Street grid, pipe layout with loops, hydraulics, households, outbreak, closed-loop replay |

## How the locator works

**The flow tree.** When supply comes on, the water front reaches each point of the network along its
fastest path. Those paths form a tree even when the pipes form loops; a looped pipe is filled from both
ends and the fronts meet part-way (`upstream/network.py`). Sewage that seeped into an emptied pipe is
carried ahead of the front, so it reaches exactly the homes downstream of the breach in that tree,
diluted with distance and at every junction (`upstream/physics.py`).

**The velocity model.** Each home's normal-day photos give its typical delay after supply starts. A
regularised least-squares fit turns those delays into a travel time for every pipe, and with it the
direction water runs round each loop. When the utility has no pipe map, the README's rebuilding
algorithm grows a pipe tree over the street grid in order of arrival time, and the same fit runs on
that.

**Scoring.** Every pipe piece is a candidate breach. For each one, each report's likelihood combines
turbidity rise, chlorine drop (with strip readings of zero treated as censored), the one-tap answer,
and timing. Each report is a mixture of the physical model and a broad outlier component, so a prank
or a bad photo barely moves the result. The slug's strength in each supply is unknown and marginalised
over a grid that includes zero, because a crack need not leak every time. A model-error term stops the
posterior becoming overconfident about distances.

**Flow-direction uncertainty.** Photo timings cannot always settle which way water runs round a loop,
and a wrong guess sends the search down the wrong street. So the likelihood is averaged over an
ensemble of eight flow trees: the best fit, refits on households resampled with replacement, and draws
from the best fit's posterior (for a rebuilt map the refits also rebuild the layout). The 90 % region
therefore widens where the network is ambiguous instead of confidently pointing the wrong way.

**Outputs.** The dig spot is the most probable piece; the 90 % region is the smallest set of pieces
holding 90 % of the probability; the ring encloses it. Each home's chance of being downstream and its
safe-after time (90th percentile of when the slug has passed, plus five minutes) come from the same
posterior. Strip requests and the sentinel planner rank homes and lanes by the expected information
(bits) one more reading there would give.

## Results in simulation

`upstream evaluate` runs 60 independent simulated outbreaks. Each has its own pipe layout (with
loops), hydraulics and 140 participating homes; twelve normal supplies teach Upstream the baselines and
travel times; then a crack opens next to a sewer and leaks for two supplies, every other day as in
Indore. Cracks are drawn over the whole network, weighted by risk, so some have no participating home
downstream: no method can see those. The simulator's slug physics deliberately differ from the
locator's assumptions.

| Measure (`data/evaluation.json`) | Result |
| --- | --- |
| Outbreaks with a participating home downstream of the crack | 35 of 60 |
| … that raised a red alert within two supplies, with a dirty reading downstream | 28 (80 %); 61 % of those on the first supply |
| False alarms: an alert with no dirty reading downstream of the real crack | 1 of 60 |
| Dig spot to real crack, with the utility's pipe map | median **16 m**, 90th percentile 84 m |
| Real crack inside the 90 % search region | **96 %** of detected outbreaks; median region 72 m of pipe |
| With no pipe map at all (rebuilt from photo timings) | median 21 m; crack inside the region 75 % of the time |
| First complaint ticket to real crack, for comparison | median 157 m (centre of all complaints: 187 m) |

Without a pipe map the region can only contain pipes the rebuild drew, and the rebuild recovers about
70 % of the real pipe length, which is why its coverage is lower.

The flow-tree ensemble is what makes the search region honest. Same 60 outbreaks, utility pipe map:

| Ensemble | Median error | 90th percentile | Crack inside the 90 % region |
| --- | --- | --- | --- |
| None: the best-fit tree only | 17 m | 120 m | 75 % |
| 8 bootstrap refits | 16 m | 117 m | 85 % |
| 8 posterior draws | 18 m | 92 m | 90 % |
| **8 mixed (default)** | **16 m** | **84 m** | **96 %** |
| 16 posterior draws | 16 m | 81 m | 93 % |
| 16 mixed (`--ensemble 16`) | 15 m | 74 m | 97 % |

The README's headline number is how many days earlier Upstream finds the crack than handling
complaints one ticket at a time. That depends on how a utility actually works its ticket queue, which
the simulation cannot model. What it does show: 61 % of detected outbreaks raised the red alert on the
first supply after the crack opened (within two days at alternate-day supply) and the rest on the
second, and the dig spot lands about ten times closer to the crack than the first complaint does.

## Commands

| Command | What it does |
| --- | --- |
| `upstream serve [--port 8000]` | Intake app and dashboard |
| `upstream card --household H-0042 [--out card.png --dpi 300]` | Printable card (SVG by default) |
| `upstream read photo.jpg` | Read one photo, print JSON |
| `upstream demo-photo --turbidity 40 --chlorine 0 --out muddy.jpg` | Synthetic first-glass photo |
| `upstream calibrate samples.csv` | Fit turbidity index → NTU from photos of reference samples (`photo,ntu` columns) |
| `upstream simulate [--seed 7] [--streets FILE]` | Run an outbreak and write the replay (`data/scenarios/demo.json`) |
| `upstream evaluate [--runs 60]` | Headline numbers over many simulated outbreaks (`data/evaluation.json`) |
| `upstream export-site [--target static\|artifact]` | Dashboard as a static site (`site/`) |
| `upstream fetch-streets --place "Bhagirathpura, Indore"` | Download a real street grid from OpenStreetMap |

## Replaying on Indore's real street grid

The bundled replay uses a synthetic ward because the machine this was built on could not reach
OpenStreetMap. It is placed at latitude/longitude (0, 0) so it can never be mistaken for a real place.
With internet access:

```bash
upstream fetch-streets --place "Bhagirathpura, Indore" --radius 700 --out data/streets/bhagirathpura.osm.json
upstream simulate --streets data/streets/bhagirathpura.osm.json --out data/scenarios/bhagirathpura.json
UPSTREAM_SCENARIO=data/scenarios/bhagirathpura.json upstream serve
```

`fetch-streets` geocodes the name with Nominatim and downloads the roads within the radius from the
Overpass API (or pass `--bbox south,west,north,east`). Pipes, households, risk attributes and the
outbreak are still simulated; only the street grid is real. The dashboard then shows OpenStreetMap
tiles underneath. `--inlet` picks the street node where supply enters (default: the west edge).

## Calibrating the card

* **Chlorine.** The scale colours in `upstream/card/spec.py` approximate a DPD strip chart. Before a
  pilot, replace them with the colours of the strip brand being distributed, and check readings against
  strips dipped in known standards.
* **Turbidity.** The reader reports a turbidity *index* (0 = clear). It is used to spot spikes against
  each tap's own baseline, not to certify the 1 NTU standard. To also get an NTU estimate, photograph
  reference samples on the card, list them in a CSV (`photo,ntu`), run `upstream calibrate samples.csv`,
  and set `UPSTREAM_TURBIDITY_CALIBRATION` to the file it writes.
* **Print check.** The 50 mm bar must measure 50 mm; print at 100 %, not "fit to page".

## WhatsApp

Set `WHATSAPP_TOKEN`, `WHATSAPP_VERIFY_TOKEN` and `WHATSAPP_APP_SECRET` (and optionally
`WHATSAPP_API_VERSION`, default `v21.0`), expose the app over HTTPS, and register
`https://<host>/whatsapp/webhook` in the Meta app with the same verify token. Households send the card
photo (a caption like "smells bad" counts as the quick answer) and get the reading back; a one-word
reply (fine, smell, dirty, ill) attaches the answer to their latest photo. Every delivery's
`X-Hub-Signature-256` is checked. Phone numbers are not stored: replies go to the sender of the message
being handled, and the link from a sender to their latest reading is kept under a keyed hash.

## API

| Method and path | Purpose |
| --- | --- |
| `POST /api/photo` (multipart `photo`, optional `household`, `answer`) | Read, store and level a first-glass photo; 422 with retake tips if unusable |
| `POST /api/read` (multipart `photo`) | Read only, store nothing |
| `POST /api/reports` (JSON `household`, `turbidity`, `chlorine`, `answer`, `time`) | Add a reading by hand |
| `POST /api/reports/{id}/answer` | Attach a quick answer |
| `POST /api/live/supply` | Announce that water is on (otherwise inferred from the first photo) |
| `GET /api/live` | Live levels, posterior, dig spot, advisories, strip requests |
| `GET /api/evidence.csv`, `/api/evidence.json` | Evidence export for the utility |
| `GET /card/{household}.svg`, `.png?dpi=300` | Printable card |
| `GET /api/scenario` | The replay data |
| `GET/POST /whatsapp/webhook` | WhatsApp Cloud API |

Re-used photos (identical bytes, the same camera timestamp from another home, or a near-identical
perceptual hash) are flagged and left out of the triangulation. If a home sends more than one photo in
a supply, its most serious reading counts.

## Tests

```bash
python -m pytest -q
```

The suite covers the flow tree (including loops and meeting points), the street loaders, the reader on
synthetic photos under coloured light, blur and glare, the card geometry, travel-time fitting, map
rebuilding, levels and the complaint-cluster rule, the locator end to end, advisories, the planner, the
API (including a mocked WhatsApp Graph API) and both site exports. CI runs it on every push
(`.github/workflows/ci.yml`). `.github/workflows/pages.yml` publishes the dashboard to GitHub Pages
when run by hand, after Pages is set to deploy from GitHub Actions.

## What this build does not do yet

* **No real-world validation.** Accuracy figures come from simulation. The reader has only seen
  synthetic photos; the slug model is simple (no hydraulic solver; WNTR was a stretch goal).
* **Synthetic ward and attributes.** Pipes, households, risk attributes (pipe age, sewer crossings,
  incidents) and outbreaks are simulated, and the default street grid is synthetic too.
* **No CNN turbidity model.** It needs real calibration photos first.
* **Lane-level privacy only.** Homes are stored by household ID and street segment; the live database
  keeps photos for evidence, so a deployment needs a retention policy.
