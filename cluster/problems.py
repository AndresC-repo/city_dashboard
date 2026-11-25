"""Problem trees for the city dashboard: objects taller than 70 m and trees standing on a structure.

The pipeline already leaves both out of the totals (checks.json counts them); this writes WHERE they
are, so the dashboard can put them on the map and point to the tiles worth a look in the labelling tool.

For every city in data/cities/ and every run (the city folder itself = "native", runs/<tag>/ = <tag>):
reads trees.csv and writes data/cities/_dashboard/<city>/<run>/problems.csv with
    reason (tall | structure), lat, lon, height_m, crown_area_m2, tile_id
Run it after status.py (status.py may rebuild the _dashboard folders):

    cd <repo>
    python src/city/status.py
    python src/city/problems.py              # all cities;  --city tempe  for one

Only the standard library: works in any environment.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

MAX_HEIGHT_M = 70.0          # same as the pipeline's tree definition (combine.py)
TRUE = {"1", "true", "True", "yes", "y", "t"}


def find_cols(header: list[str]) -> dict:
    h = {c.lower(): c for c in header}
    pick = lambda *names: next((h[n] for n in names if n in h), None)
    cols = {
        "lat": pick("lat", "latitude"), "lon": pick("lon", "longitude"),
        "height": pick("height_m", "height"), "crown": pick("crown_area_m2", "crown_area"),
        "tile": pick("tile_id", "tile"), "quality": pick("quality", "flags"),
        # the rooftop test's flag: a column of its own, else a word in the quality notes
        "structure": next((h[c] for c in h if "structure" in c or "rooftop" in c), None),
    }
    return cols


def is_structure(row: dict, cols: dict) -> bool:
    if cols["structure"]:
        return str(row.get(cols["structure"], "")).strip() in TRUE
    q = str(row.get(cols["quality"], "") or "") if cols["quality"] else ""
    return "structure" in q or "rooftop" in q


def run_dirs(city_dir: Path):
    if (city_dir / "trees.csv").exists():
        yield "native", city_dir / "trees.csv"
    runs = city_dir / "runs"
    if runs.is_dir():
        for r in sorted(runs.iterdir()):
            if (r / "trees.csv").exists():
                yield r.name, r / "trees.csv"


def one(city: str, run: str, trees: Path, out_root: Path) -> None:
    out_dir = out_root / city / run
    if not out_dir.is_dir():          # status.py did not export this run: nothing to show it with
        return
    n_tall = n_struct = 0
    with open(trees, newline="") as f, open(out_dir / "problems.csv.tmp", "w", newline="") as g:
        r = csv.DictReader(f)
        cols = find_cols(r.fieldnames or [])
        if not (cols["lat"] and cols["lon"] and cols["height"]):
            print(f"  {city}/{run}: trees.csv lacks lat/lon/height columns ({r.fieldnames[:12]}...), skipped")
            return
        w = csv.writer(g)
        w.writerow(["reason", "lat", "lon", "height_m", "crown_area_m2", "tile_id"])
        for row in r:
            try:
                h = float(row[cols["height"]])
            except (TypeError, ValueError):
                continue
            reason = "tall" if h > MAX_HEIGHT_M else ("structure" if is_structure(row, cols) else None)
            if not reason:
                continue
            if reason == "tall":
                n_tall += 1
            else:
                n_struct += 1
            w.writerow([reason, row[cols["lat"]], row[cols["lon"]], round(h, 1),
                        row.get(cols["crown"], "") if cols["crown"] else "",
                        row.get(cols["tile"], "") if cols["tile"] else ""])
    os.replace(out_dir / "problems.csv.tmp", out_dir / "problems.csv")
    note = "" if (cols["structure"] or cols["quality"]) else "  (no on-structure flag found in trees.csv: only tall objects)"
    print(f"  {city}/{run}: {n_tall} taller than {MAX_HEIGHT_M:g} m, {n_struct} on a structure{note}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cities", default="data/cities")
    ap.add_argument("--city", default=None, help="only this city (folder name)")
    args = ap.parse_args()
    root = Path(args.cities)
    out_root = root / "_dashboard"
    if not out_root.is_dir():
        sys.exit(f"{out_root} not found: run status.py first")
    for city_dir in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))):
        if args.city and city_dir.name != args.city:
            continue
        for run, trees in run_dirs(city_dir):
            one(city_dir.name, run, trees, out_root)


if __name__ == "__main__":
    main()
