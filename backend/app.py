"""
City dashboard backend: which cities exist, what is done for each, what to run next, and the results.

It never talks to the cluster. It reads data/_dashboard/, a copy of the folder that
`python src/city/status.py` writes on the cluster (see README.md), and it builds the commands
you paste into your cluster terminal.
"""
from __future__ import annotations

import csv
import json
import math
import os
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("DASHBOARD_DATA", ROOT / "data")) / "_dashboard"
CITIES_CSV = Path(os.environ.get("DASHBOARD_CITIES", ROOT / "cities.csv"))
CONFIG = json.loads((ROOT / "config.json").read_text())
CITY_FIELDS = ["name", "label", "state", "place", "lon", "lat", "density_est", "survey"]

MAIN_DENSITY = 4          # every city: the comparable sample
SECOND_DENSITY = 8        # cities whose lidar reaches it: robustness run
CHUNK = 50                # tiles per cluster job

app = FastAPI(title="City dashboard")


# ----------------------------------------------------------------------------- reading
def read_cities() -> list[dict]:
    with open(CITIES_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["lon"], r["lat"] = float(r["lon"]), float(r["lat"])
        r["density_est"] = float(r["density_est"]) if r.get("density_est") else None
    return rows


def synced_density() -> dict[str, dict]:
    """Densest survey per city from the cluster's survey_density.csv, if it was copied over."""
    p = DATA / "survey_density.csv"
    best: dict[str, dict] = {}
    if p.exists():
        with open(p, newline="") as f:
            for r in csv.DictReader(f):
                if not r.get("density_est_pts_per_m2"):
                    continue
                d = float(r["density_est_pts_per_m2"])
                if r["city"] not in best or d > best[r["city"]]["density_est"]:
                    best[r["city"]] = dict(density_est=d, survey=r["survey"])
    return best


def read_status(name: str) -> dict | None:
    p = DATA / name / "status.json"
    return json.loads(p.read_text()) if p.exists() else None


def num(x: str):
    if x == "":
        return None
    try:
        v = float(x)
        return None if math.isnan(v) else v
    except ValueError:
        return x


# ----------------------------------------------------------------------------- steps
def wanted_runs(density: float | None) -> list[tuple[str, int]]:
    runs = [(f"d{MAIN_DENSITY}", MAIN_DENSITY)]
    if density is not None and density >= SECOND_DENSITY:
        runs.append((f"d{SECOND_DENSITY}", SECOND_DENSITY))
    return runs


def build_steps(city: dict, st: dict | None) -> list[dict]:
    c = CONFIG["cluster"]
    name = city["name"]
    cd = f"cd {c['repo']}"
    city_dir = f"data/cities/{name}"
    plan = st["plan"] if st else None
    tiles = st["tiles"] if st else dict(planned=0, downloaded=0, empty=0)
    runs = st["runs"] if st else {}
    measured = next((r["density_measured"] for r in runs.values() if r.get("density_measured")), None)
    density = measured or (plan or {}).get("survey_density_est") or city.get("density_est")
    steps = []

    def add(key, title, state, detail, where, command, why=""):
        steps.append(dict(key=key, title=title, state=state, detail=detail, where=where, command=command, why=why))

    # 1 lidar available?
    if plan:
        add("lidar", "Check the lidar", "done",
            f"Survey {plan['survey']}" + (f", about {density:g} points per m²" if density else ""), "cluster", "")
    elif city.get("density_est"):
        add("lidar", "Check the lidar", "done",
            f"Survey {city.get('survey') or '?'}, about {city['density_est']:g} points per m² (estimate)", "cluster", "")
    else:
        add("lidar", "Check the lidar", "todo", "Is there public lidar for this city, and how dense?", "cluster",
            f"{cd}\npython src/city/survey_density.py --point {name} {city['lon']} {city['lat']}",
            "Lists the public surveys covering the city centre. No survey means the city cannot be processed.")

    # 2 outline
    outline_cmd = (f"{cd}\nconda activate pdal\n"
                   f"python src/city/city_limits.py --name {name} --place \"{city.get('place') or city['label']}\" --state {city['state']}")
    if st and st.get("outline"):
        add("outline", "City outline", "done", "Official city limits", "cluster", "")
    elif plan:
        add("outline", "City outline", "warn", "Planned from a box, not the city limits: neighbourhoods at the edge are cut",
            "cluster", outline_cmd + f"\npython src/city/plan.py --name {name} --polygon data/cities/outlines/{name}.geojson",
            "Optional for a pilot. Re-planning with the outline adds tiles; tiles already downloaded and processed are kept only if the tile grid is unchanged.")
    else:
        add("outline", "City outline", "todo", "Download the official city limits", "cluster", outline_cmd,
            "The outline decides which tiles are downloaded.")

    # 3 plan
    if plan:
        add("plan", "Plan the tiles", "done", f"{plan['n_tiles']} tiles of {plan['tile_m']:g} m = {plan['area_km2']:g} km²", "cluster", "")
    else:
        add("plan", "Plan the tiles", "todo", "Split the city into 500 m tiles and pick the survey", "cluster",
            f"{cd}\nconda activate urban-trees\npython src/city/plan.py --name {name} --polygon data/cities/outlines/{name}.geojson")

    # 4 download
    got, planned = tiles["downloaded"] + tiles["empty"], tiles["planned"]
    fetch_cmd = (f"{cd}\nconda activate urban-trees\n"
                 f"nohup python src/city/fetch.py --city-dir {city_dir} --tiles all \\\n"
                 f"  --pdal {c['pdal']} > fetch_{name}.log 2>&1 &\n"
                 f"# progress:\nls {city_dir}/tiles/*.laz | wc -l")
    if plan and got >= planned:
        add("download", "Download the lidar", "done",
            f"{tiles['downloaded']} tiles" + (f" ({tiles['empty']} empty)" if tiles["empty"] else ""), "cluster", "")
    else:
        add("download", "Download the lidar", "partial" if got else "todo",
            f"{got} of {planned} tiles" if plan else "After the plan", "cluster", fetch_cmd if plan else "",
            "Runs on the login node in the background. Tiles already there are skipped."
            + (f" This is a large city ({planned:,} tiles): check the free space of the workspace first, "
               f"and download in parts with --tiles 0-999, 1000-1999, ..." if planned > 1000 else ""))

    # 5 census
    census = st.get("census") if st else None
    census_cmd = f"{cd}\nconda activate pdal\npython src/city/census.py --city-dir {city_dir} --key YOUR_CENSUS_KEY"
    if census and census.get("has_survey"):
        add("census", "Census data", "done", f"{census['block_groups']} block groups with population and income", "cluster", "")
    else:
        add("census", "Census data", "partial" if census else "todo",
            "Population only; income and housing are missing" if census else "Block groups, population, income, renters, building age",
            "cluster", census_cmd if plan else "", "Needs your free Census key. Independent of the lidar steps.")

    # 6.. per density: process, then tables
    n_jobs = max(math.ceil(planned / CHUNK), 1)
    for tag, dens in wanted_runs(density):
        r = runs.get(tag)
        label = f"{dens} points per m²" + (" (main run)" if dens == MAIN_DENSITY else " (second run)")
        done_tiles = (r["tiles_processed"] + r["tiles_skipped"]) if r else 0
        process_cmd = (f"{cd}\nsbatch --time=01:30:00 --array=0-{n_jobs - 1} "
                       f"--export=ALL,CITY={name},TARGET_DENSITY={dens},TAG={tag} hpc/city_process.slurm\n"
                       f"# watch the queue:\nsqueue --me")
        ready = bool(plan) and tiles["downloaded"] > 0
        if r and tiles["downloaded"] and done_tiles >= tiles["downloaded"] and got >= planned:
            add(f"process_{tag}", f"Process at {label}", "done", f"{r['tiles_processed']} tiles processed", "cluster", "")
        else:
            add(f"process_{tag}", f"Process at {label}", "partial" if done_tiles else "todo",
                f"{done_tiles} of {tiles['downloaded']} downloaded tiles" if ready else "After the download",
                "cluster", process_cmd if ready else "",
                f"{n_jobs} GPU job{'s' if n_jobs > 1 else ''} of up to {CHUNK} tiles. Finished tiles are skipped, so it is safe to submit again.")

        tables_ok = bool(r and r.get("combined") and r.get("neighbourhoods") is not None and not r.get("neighbourhoods_stale"))
        tables_cmd = (f"{cd}\nconda activate pdal\n"
                      f"python src/city/combine.py --city-dir {city_dir} --tag {tag} \\\n"
                      f"  --polygons {city_dir}/block_groups.geojson | tail -4")
        if tables_ok:
            add(f"tables_{tag}", f"Result tables at {dens} points per m²", "done",
                f"{r['summary'].get('trees', 0):,} trees, {r['neighbourhoods']} block groups", "cluster", "")
        else:
            some = bool(r and (r.get("summary") or r.get("neighbourhoods") is not None))
            if r and r.get("combined") and r.get("neighbourhoods") is None:
                detail = "Tree table and summary exist; the neighbourhood table is missing"
            elif some:
                detail = "Out of date: tiles were processed after the last tables"
            else:
                detail = "Tree table, summary and neighbourhood table"
            add(f"tables_{tag}", f"Result tables at {dens} points per m²", "partial" if some else "todo", detail,
                "cluster", tables_cmd if (r and census) else "", "Needs the processing and the census data.")

    # which step is next: the first unfinished one that has a command
    nxt = next((s for s in steps if s["state"] in ("todo", "partial") and s["command"]), None)
    for s in steps:
        s["next"] = s is nxt
    return steps


def overall(city: dict, st: dict | None, steps: list[dict]) -> str:
    if st is None:
        return "no_lidar" if (city.get("density_est") is None and city.get("survey") == "none") else "not_started"
    return "done" if all(s["state"] in ("done", "warn") for s in steps) else "in_progress"


def city_payload(city: dict, with_steps: bool = True) -> dict:
    st = read_status(city["name"])
    steps = build_steps(city, st)
    measured = next((r["density_measured"] for r in (st or {}).get("runs", {}).values() if r.get("density_measured")), None)
    main = (st or {}).get("runs", {}).get(f"d{MAIN_DENSITY}") or {}
    out = dict(city, status=overall(city, st, steps), density_measured=measured,
               steps_done=sum(s["state"] in ("done", "warn") for s in steps), steps_total=len(steps),
               next_step=next((s["title"] for s in steps if s["next"]), None),
               trees=(main.get("summary") or {}).get("trees"),
               updated=(st or {}).get("generated"),
               runs=sorted((st or {}).get("runs", {})))
    if with_steps:
        out["steps"] = steps
        out["plan"] = (st or {}).get("plan")
    return out


# ----------------------------------------------------------------------------- API
@app.get("/api/cities")
def api_cities():
    dens = synced_density()
    cities = read_cities()
    for c in cities:
        if c["name"] in dens:
            c.update(dens[c["name"]])
    return dict(cities=[city_payload(c, with_steps=False) for c in cities], refresh=refresh_commands(),
                data_found=DATA.exists())


def find_city(name: str) -> dict:
    dens = synced_density()
    for c in read_cities():
        if c["name"] == name:
            if name in dens:
                c.update(dens[name])
            return c
    raise HTTPException(404, f"No city called {name}")


@app.get("/api/city/{name}")
def api_city(name: str):
    return city_payload(find_city(name))


@app.get("/api/city/{name}/results")
def api_results(name: str, run: str):
    find_city(name)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run):
        raise HTTPException(400, "bad run name")
    d = DATA / name / run
    summary = json.loads((d / "summary.json").read_text()) if (d / "summary.json").exists() else None
    rows = []
    if (d / "neighbourhoods.csv").exists():
        with open(d / "neighbourhoods.csv", newline="") as f:
            for r in csv.DictReader(f):
                rows.append({k: (v if k == "GEOID" else num(v)) for k, v in r.items()})
    bg = DATA / name / "block_groups.geojson"
    shapes = json.loads(bg.read_text()) if bg.exists() else None
    return dict(summary=summary, rows=rows, shapes=shapes)


class NewCity(BaseModel):
    label: str
    state: str
    place: str = ""
    lon: float
    lat: float


@app.post("/api/cities")
def api_add_city(c: NewCity):
    name = re.sub(r"[^a-z0-9]+", "_", c.label.lower()).strip("_")
    if not name:
        raise HTTPException(400, "The city needs a name")
    if not re.fullmatch(r"[A-Za-z]{2}", c.state):
        raise HTTPException(400, "State must be a two-letter code, e.g. TX")
    rows = read_cities()
    if any(r["name"] == name for r in rows):
        raise HTTPException(400, f"{name} is already in the list")
    with open(CITIES_CSV, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=CITY_FIELDS).writerow(
            dict(name=name, label=c.label.strip(), state=c.state.upper(), place=(c.place or c.label).strip(),
                 lon=round(c.lon, 4), lat=round(c.lat, 4), density_est="", survey=""))
    return dict(name=name)


def refresh_commands() -> dict:
    c = CONFIG["cluster"]
    # Where data/_dashboard is on this PC, as WSL sees it. Inside Docker the app only knows its
    # own /app/data, so the folder comes from DASHBOARD_HOST_DIR (set by docker compose) or config.json.
    host_dir = os.environ.get("DASHBOARD_HOST_DIR") or CONFIG.get("pc_folder_wsl")
    local = f"{host_dir.rstrip('/')}/data/_dashboard" if host_dir else str(DATA)
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", local)           # Windows path -> the same folder seen from WSL
    if m:
        local = f"/mnt/{m.group(1).lower()}/" + m.group(2).replace("\\", "/")
    return dict(
        cluster=f"cd {c['repo']}\npython src/city/status.py",
        pc=f"mkdir -p {local}\nrsync -av --delete \\\n  {c['user']}@{c['host']}:{c['data']}/cities/_dashboard/ \\\n  {local}/")


app.mount("/vendor", StaticFiles(directory=ROOT / "frontend" / "vendor"), name="vendor")


@app.get("/")
def index():
    return FileResponse(ROOT / "frontend" / "index.html")


@app.get("/{path}")
def static(path: str):
    p = ROOT / "frontend" / path
    if path in ("app.js", "style.css") and p.exists():
        return FileResponse(p)
    raise HTTPException(404)
