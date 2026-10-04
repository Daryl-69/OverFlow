"""Command line: ``upstream <command>`` (or ``python -m upstream``)."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path


def _cmd_card(a) -> int:
    from .card.render import card_svg, render_card
    out = Path(a.out or f"card-{a.household}.svg")
    if out.suffix.lower() == ".png":
        import cv2
        img = render_card(a.household, ppm=a.dpi / 25.4)
        cv2.imwrite(str(out), cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
    else:
        out.write_text(card_svg(a.household))
    print(f"wrote {out} — print at 100% on A5 landscape; the bar at the bottom must measure 50 mm")
    return 0


def _cmd_read(a) -> int:
    from .reader.core import TurbidityCalibration, read_photo
    cal = TurbidityCalibration.load(a.calibration) if a.calibration else None
    res = read_photo(Path(a.photo).read_bytes(), calibration=cal)
    print(json.dumps(res.to_dict(), indent=2))
    return 0 if res.ok else 2


def _cmd_demo_photo(a) -> int:
    from .reader.synth import PhotoParams, render_photo
    data = render_photo(a.household, PhotoParams(turbidity=a.turbidity, chlorine=a.chlorine, blur=a.blur,
                                                 glare=a.glare, taken_at=a.taken_at), seed=a.seed)
    Path(a.out).write_bytes(data)
    print(f"wrote {a.out}")
    return 0


def _cmd_calibrate(a) -> int:
    """CSV with columns photo,ntu (one row per reference sample photo)."""
    from .reader.core import TurbidityCalibration, read_photo
    pairs = []
    base = Path(a.csv).parent
    with open(a.csv, newline="") as fh:
        for row in csv.DictReader(fh):
            p = Path(row["photo"])
            p = p if p.is_absolute() else base / p
            res = read_photo(p.read_bytes())
            if not res.ok:
                print(f"skip {p}: {'; '.join(res.issues)}", file=sys.stderr)
                continue
            pairs.append((res.turbidity["index"], float(row["ntu"])))
            print(f"{p.name}: index {res.turbidity['index']:.3f} -> {float(row['ntu'])} NTU")
    cal = TurbidityCalibration.fit(pairs)
    cal.save(a.out)
    print(f"wrote {a.out} from {len(pairs)} samples; set UPSTREAM_TURBIDITY_CALIBRATION={a.out} to use it")
    return 0


def _cmd_simulate(a) -> int:
    from .scenario import ScenarioConfig, run_scenario, scenario_json
    cfg = ScenarioConfig(seed=a.seed, n_households=a.households, baseline_cycles=a.baseline_cycles,
                         outbreak_cycles=a.outbreak_cycles, ensemble=a.ensemble, ensemble_mode=a.ensemble_mode,
                         min_downstream_homes=a.min_downstream, streets_path=a.streets, inlet=a.inlet)
    sc = run_scenario(cfg)
    data = scenario_json(sc)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, separators=(",", ":")))
    f = data["final"]
    print(f"wrote {out} ({out.stat().st_size // 1024} KB, {len(data['frames'])} frames, {sc.timings['total_s']} s)")
    if "gis" in f:
        print(f"  dig spot {f['gis']['error_m']} m from the true breach (utility map), "
              f"{f['rebuilt']['error_m']} m (rebuilt map); 90% region {f['gis']['region_length_m']} m; "
              f"first complaint ticket {f['complaints']['first_ticket_error_m']} m away")
    else:
        print("  no alert was raised in this outbreak")
    return 0


def _cmd_evaluate(a) -> int:
    # one BLAS thread per worker process: the workers already use every core
    for var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ.setdefault(var, "1")
    from .evaluate import evaluate
    res = evaluate(runs=a.runs, seed0=a.seed0, workers=a.workers, outbreak_cycles=a.outbreak_cycles,
                   min_downstream_homes=a.min_downstream, streets_path=a.streets, ensemble=a.ensemble,
                   ensemble_mode=a.ensemble_mode)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=1))
    summary = {k: v for k, v in res.items() if k != "rows"}
    print(json.dumps(summary, indent=2))
    return 0


def _cmd_export(a) -> int:
    from .export import export_artifact, export_static
    ev = Path(a.evaluation) if a.evaluation else None
    if a.target == "artifact":
        p = export_artifact(Path(a.out), Path(a.scenario), ev)
    else:
        p = export_static(Path(a.out), Path(a.scenario), ev)
    print(f"wrote {p}")
    return 0


def _cmd_serve(a) -> int:
    import uvicorn

    from .intake.app import create_app
    app = create_app(var_dir=a.var, scenario_path=a.scenario)
    print(f"Dashboard: http://{a.host}:{a.port}/dashboard/   Household page: http://{a.host}:{a.port}/report")
    uvicorn.run(app, host=a.host, port=a.port, log_level="info")
    return 0


def _cmd_fetch_streets(a) -> int:
    from .sim.streets import bbox_around, fetch_osm, geocode, load_overpass_json
    if a.bbox:
        s, w, n, e = (float(v) for v in a.bbox.split(","))
        bbox = (s, w, n, e)
    else:
        lat, lon, name = geocode(a.place)
        print(f"{a.place!r} -> {name} ({lat:.5f}, {lon:.5f})")
        bbox = bbox_around(lat, lon, a.radius)
    data = fetch_osm(bbox)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data))
    g = load_overpass_json(data, name=out.stem)
    print(f"wrote {out}: {g.n_nodes} junctions, {g.n_edges} street segments, {g.length.sum() / 1000:.1f} km")
    print(f"next: upstream simulate --streets {out} --out data/scenarios/{out.stem}.json")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="upstream", description="Upstream — seismology for sewage.")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("card", help="printable household card (SVG, or PNG by extension)")
    c.add_argument("--household", default="H-0001")
    c.add_argument("--out")
    c.add_argument("--dpi", type=int, default=300)
    c.set_defaults(fn=_cmd_card)

    c = sub.add_parser("read", help="read a first-glass photo")
    c.add_argument("photo")
    c.add_argument("--calibration", help="turbidity calibration JSON from `upstream calibrate`")
    c.set_defaults(fn=_cmd_read)

    c = sub.add_parser("demo-photo", help="render a synthetic first-glass photo")
    c.add_argument("--household", default="H-0001")
    c.add_argument("--turbidity", type=float, default=0.0, help="NTU-like knob: 0 clear, 50 muddy")
    c.add_argument("--chlorine", type=float, default=0.5)
    c.add_argument("--blur", type=float, default=0.0)
    c.add_argument("--glare", action="store_true")
    c.add_argument("--taken-at", help='EXIF time, "YYYY:MM:DD HH:MM:SS"')
    c.add_argument("--seed", type=int, default=0)
    c.add_argument("--out", default="demo-photo.jpg")
    c.set_defaults(fn=_cmd_demo_photo)

    c = sub.add_parser("calibrate", help="fit turbidity index -> NTU from reference photos")
    c.add_argument("csv", help="CSV with columns photo,ntu")
    c.add_argument("--out", default="turbidity-calibration.json")
    c.set_defaults(fn=_cmd_calibrate)

    c = sub.add_parser("simulate", help="run an outbreak scenario and write the replay JSON")
    c.add_argument("--seed", type=int, default=7)
    c.add_argument("--households", type=int, default=140)
    c.add_argument("--baseline-cycles", type=int, default=12)
    c.add_argument("--outbreak-cycles", type=int, default=2)
    c.add_argument("--ensemble", type=int, default=8, help="flow trees averaged over")
    c.add_argument("--ensemble-mode", choices=["mixed", "posterior", "bootstrap"], default="mixed")
    c.add_argument("--min-downstream", type=int, default=6, help="require this many reporting homes downstream")
    c.add_argument("--streets", help="street file (Overpass JSON, GeoJSON or Upstream JSON); default synthetic")
    c.add_argument("--inlet", type=int, help="street node index where supply enters")
    c.add_argument("--out", default="data/scenarios/demo.json")
    c.set_defaults(fn=_cmd_simulate)

    c = sub.add_parser("evaluate", help="headline numbers over many simulated outbreaks")
    c.add_argument("--runs", type=int, default=60)
    c.add_argument("--seed0", type=int, default=1000)
    c.add_argument("--workers", type=int)
    c.add_argument("--outbreak-cycles", type=int, default=2)
    c.add_argument("--min-downstream", type=int, default=0)
    c.add_argument("--ensemble", type=int, default=8, help="flow trees averaged over")
    c.add_argument("--ensemble-mode", choices=["mixed", "posterior", "bootstrap"], default="mixed")
    c.add_argument("--streets")
    c.add_argument("--out", default="data/evaluation.json")
    c.set_defaults(fn=_cmd_evaluate)

    c = sub.add_parser("export-site", help="export the dashboard as a static site")
    c.add_argument("--scenario", default="data/scenarios/demo.json")
    c.add_argument("--evaluation", default="data/evaluation.json")
    c.add_argument("--target", choices=["static", "artifact"], default="static")
    c.add_argument("--out", default="site")
    c.set_defaults(fn=_cmd_export)

    c = sub.add_parser("serve", help="run the intake app and dashboard")
    c.add_argument("--host", default="127.0.0.1")
    c.add_argument("--port", type=int, default=8000)
    c.add_argument("--var", default="var")
    c.add_argument("--scenario")
    c.set_defaults(fn=_cmd_serve)

    c = sub.add_parser("fetch-streets", help="download a real street grid from OpenStreetMap")
    g = c.add_mutually_exclusive_group(required=True)
    g.add_argument("--place", help='place name, e.g. "Bhagirathpura, Indore"')
    g.add_argument("--bbox", help="south,west,north,east in degrees")
    c.add_argument("--radius", type=float, default=700.0, help="half-width (m) of the box around --place")
    c.add_argument("--out", default="data/streets/streets.osm.json")
    c.set_defaults(fn=_cmd_fetch_streets)

    a = p.parse_args(argv)
    return int(a.fn(a) or 0)


if __name__ == "__main__":
    sys.exit(main())
