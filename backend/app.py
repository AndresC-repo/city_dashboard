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

import urllib.error
import urllib.request

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("DASHBOARD_DATA", ROOT / "data")) / "_dashboard"
CITIES_CSV = Path(os.environ.get("DASHBOARD_CITIES", ROOT / "cities.csv"))
CONFIG = json.loads((ROOT / "config.json").read_text())
CITY_FIELDS = ["name", "label", "state", "place", "lon", "lat", "density_est", "survey"]

MAIN_DENSITY = 4          # every city: the comparable sample
SECOND_DENSITY = 8        # optional robustness run, offered for every city
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


def synced_outlines() -> set[str]:
    """Cities whose official outline was downloaded on the cluster (also before they have a plan)."""
    p = DATA / "outlines.json"
    return set(json.loads(p.read_text())) if p.exists() else set()


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
FULL_TAG = "full"          # all points, no thinning: the best detection, for comparisons within a city


def wanted_runs(density: float | None) -> list[tuple[str, int]]:
    """Runs offered for every city: 4 points per m² (main run, comparable across cities), all points
    (full density, best detection within a city) and 8 points per m². One complete run (processing +
    result tables) is enough for a city to count as done; the other runs are optional (see build_steps).
    Density 0 = no thinning."""
    return [(f"d{MAIN_DENSITY}", MAIN_DENSITY), (FULL_TAG, 0), (f"d{SECOND_DENSITY}", SECOND_DENSITY)]


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

    def add(key, title, state, detail, where, command, why="", run=None):
        steps.append(dict(key=key, title=title, state=state, detail=detail, where=where, command=command, why=why,
                          run=run, optional=False))

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
    if (st and st.get("outline")) or (not plan and name in synced_outlines()):
        add("outline", "City outline", "done", "Official city limits", "cluster", "")
    elif plan:
        # No command here on purpose: re-planning renumbers the tiles and would mismatch
        # everything already downloaded and processed in this city's folder.
        add("outline", "City outline", "warn",
            "Planned from a box, not the city limits, so neighbourhoods at the edge are cut. "
            "To process the official city limits, add it as a new city; do not re-plan this one.",
            "cluster", "")
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
                 f"nohup python src/city/fetch.py --city-dir {city_dir} --tiles all --workers 4 \\\n"
                 f"  --pdal {c['pdal']} > fetch_{name}.log 2>&1 &\n"
                 f"# progress:\ntail -2 fetch_{name}.log")
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
    census_cmd = f"{cd}\nconda activate pdal\npython src/city/census.py --city-dir {city_dir}"
    if census and census.get("has_survey"):
        add("census", "Census data", "done", f"{census['block_groups']} block groups with population and income", "cluster", "")
    else:
        add("census", "Census data", "partial" if census else "todo",
            "Population only; income and housing are missing" if census else "Block groups, population, income, renters, building age",
            "cluster", census_cmd if plan else "", "Reads your Census key from the .env file in the repo folder on the cluster (a line KEY=...). Independent of the lidar steps.")

    # greenness from NAIP aerial images (independent of the density runs)
    naip = st.get("naip") if st else None
    naip_cmd = (f"{cd}\nconda activate pdal\n"
                f"python src/city/naip.py --city-dir {city_dir} --workers 4")
    expected = tiles["downloaded"]
    if naip and expected and naip["tiles"] >= expected:
        add("naip", "Greenness (NAIP)", "done", f"NDVI for {naip['tiles']} tiles, NAIP {naip.get('year') or ''}".strip(), "cluster", "")
    else:
        add("naip", "Greenness (NAIP)", "partial" if naip and naip["tiles"] else "todo",
            f"NDVI for {naip['tiles']} of {expected} tiles" if naip and naip["tiles"] else
            "Green cover from aerial images, and a check for missed trees",
            "cluster", naip_cmd if plan and tiles["downloaded"] else "",
            "Login node, needs internet. Picks the NAIP year closest to the lidar. Run the result tables again afterwards.")

    # 6.. per density: process, then tables
    n_jobs = max(math.ceil(planned / CHUNK), 1)
    for tag, dens in wanted_runs(density):
        r = runs.get(tag)
        if dens == 0:
            label = "all points" + (f" (about {density:g} per m²)" if density else "") + " (full density, optional)"
        else:
            label = f"{dens} points per m²" + (" (main run)" if dens == MAIN_DENSITY else " (optional)")
        sparse = (dens > MAIN_DENSITY and density is not None and density < dens)
        sparse_note = (f" This survey has only about {density:g} points per m²: at {dens} nothing is thinned, so this run "
                       f"uses all points." if sparse else "")
        done_tiles = (r["tiles_processed"] + r["tiles_skipped"]) if r else 0
        # job name shown by squeue: <city>-<run>-<step>, e.g. san_diego-d4-process
        process_cmd = (f"{cd}\nsbatch --job-name={name}-{tag}-process --time=01:30:00 --array=0-{n_jobs - 1} "
                       f"--export=ALL,CITY={name},TARGET_DENSITY={dens},TAG={tag} hpc/city_process.slurm\n"
                       f"# watch the queue (full job names):\nsqueue --me -o \"%.18i %.32j %.2t %.10M %R\"")
        ready = bool(plan) and tiles["downloaded"] > 0
        # runs processed before 2026-11-16 have no canopy maps (needed for canopy cover per neighbourhood)
        no_canopy = bool(r and r.get("canopy_maps") is not None and r["canopy_maps"] < r["tiles_processed"])
        if no_canopy and ready:
            add(f"process_{tag}", f"Process at {label}", "partial",
                f"{r['tiles_processed'] - r['canopy_maps']} of {r['tiles_processed']} tiles were processed before the canopy map existed: process again",
                "cluster", process_cmd.replace(f"TAG={tag} ", f"TAG={tag},REDO=1 "),
                "REDO=1 processes the tiles again and replaces their results. Needed once, for canopy cover per neighbourhood.", run=tag)
        elif r and tiles["downloaded"] and done_tiles >= tiles["downloaded"] and got >= planned:
            add(f"process_{tag}", f"Process at {label}", "done", f"{r['tiles_processed']} tiles processed", "cluster", "", run=tag)
        else:
            add(f"process_{tag}", f"Process at {label}", "partial" if done_tiles else "todo",
                f"{done_tiles} of {tiles['downloaded']} downloaded tiles" if ready else "After the download",
                "cluster", process_cmd if ready else "",
                f"{n_jobs} GPU job{'s' if n_jobs > 1 else ''} of up to {CHUNK} tiles. Finished tiles are skipped, so it is safe to submit again."
                + sparse_note
                + (" All points, no thinning: the best detection, for comparisons within the city. Cities have "
                   "different densities, so compare cities with the 4 points per m² run." if dens == 0 else ""), run=tag)

        has_def = bool(r and (r.get("summary") or {}).get("tree_definition"))
        has_canopy = bool(r and r.get("neighbourhoods_has_canopy", True))
        needs_green = bool(naip and expected and naip["tiles"] >= expected and r and r.get("neighbourhoods") is not None
                           and not r.get("neighbourhoods_has_green", False))
        tables_ok = bool(r and r.get("combined") and r.get("neighbourhoods") is not None
                         and not r.get("neighbourhoods_stale") and has_def and has_canopy and not no_canopy
                         and not needs_green)
        tables_cmd = (f"{cd}\nconda activate pdal\n"
                      f"python src/city/combine.py --city-dir {city_dir} --tag {tag} \\\n"
                      f"  --polygons {city_dir}/block_groups.geojson | tail -4")
        if tables_ok:
            add(f"tables_{tag}", f"Result tables, {label.split(' (')[0]}", "done",
                f"{r['summary'].get('trees', 0):,} trees, {r['neighbourhoods']} block groups", "cluster", "", run=tag)
        else:
            some = bool(r and (r.get("summary") or r.get("neighbourhoods") is not None))
            if r and r.get("combined") and r.get("neighbourhoods") is not None and not has_def:
                detail = "Made before the tree definition was set: every detected object is counted. Run again."
            elif r and r.get("neighbourhoods") is not None and (not has_canopy or no_canopy):
                detail = "No canopy cover per neighbourhood yet: " + ("process the tiles again first" if no_canopy else "run again")
            elif needs_green:
                detail = "Greenness is ready but not in the tables yet: run again"
            elif r and r.get("combined") and r.get("neighbourhoods") is None:
                detail = "Tree table and summary exist; the neighbourhood table is missing"
            elif some:
                detail = "Out of date: tiles were processed after the last tables"
            else:
                detail = "Tree table, summary and neighbourhood table"
            add(f"tables_{tag}", f"Result tables, {label.split(' (')[0]}", "partial" if some else "todo", detail,
                "cluster", tables_cmd if (r and census) else "", "Needs the processing and the census data.", run=tag)

    # One complete run is enough: the run that counts is the first finished one (main run first),
    # else the main run. The steps of every other run are optional: shown with their state and
    # command, but not needed for "done", not in the step count and never "Next".
    def finished(tag):
        rs = [s for s in steps if s["run"] == tag]
        return bool(rs) and all(s["state"] in ("done", "warn") for s in rs)
    tags = [t for t, _ in wanted_runs(density)]
    counting = next((t for t in tags if finished(t)), tags[0])
    for s in steps:
        s["optional"] = s["run"] is not None and s["run"] != counting

    # which step is next: the first unfinished required one that has a command
    nxt = next((s for s in steps if s["state"] in ("todo", "partial") and s["command"] and not s["optional"]), None)
    for s in steps:
        s["next"] = s is nxt
    return steps


def overall(city: dict, st: dict | None, steps: list[dict]) -> str:
    if st is None:
        return "no_lidar" if (city.get("density_est") is None and city.get("survey") == "none") else "not_started"
    return "done" if all(s["state"] in ("done", "warn") for s in steps if not s.get("optional")) else "in_progress"


def city_payload(city: dict, with_steps: bool = True) -> dict:
    st = read_status(city["name"])
    steps = build_steps(city, st)
    measured = next((r["density_measured"] for r in (st or {}).get("runs", {}).values() if r.get("density_measured")), None)
    main = (st or {}).get("runs", {}).get(f"d{MAIN_DENSITY}") or {}
    out = dict(city, status=overall(city, st, steps), density_measured=measured,
               steps_done=sum(s["state"] in ("done", "warn") for s in steps if not s["optional"]),
               steps_total=sum(not s["optional"] for s in steps),
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
    checks = json.loads((d / "checks.json").read_text()) if (d / "checks.json").exists() else None
    return dict(summary=summary, rows=rows, shapes=shapes, checks=checks, layers=run_layers(name, run))


@app.get("/api/city/{name}/problems")
def api_problems(name: str, run: str, top: int = 15):
    """Trees the pipeline leaves out of the totals, with their position (cluster script
    src/city/problems.py -> problems.csv): objects taller than 70 m and trees on a structure.
    Also the tiles with the most of them and the commands to get such a tile labelled by the
    model (SAVE_LAS=1) for the labelling tool."""
    find_city(name)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", run):
        raise HTTPException(400, "bad run")
    d = DATA / name / run
    f = d / "problems.csv"
    if not f.exists():
        return {"available": False}
    trees = []
    with open(f, newline="") as fh:
        for r in csv.DictReader(fh):
            lat, lon = num(r.get("lat", "")), num(r.get("lon", ""))
            if lat is None or lon is None:
                continue
            trees.append({"reason": r["reason"], "lat": lat, "lon": lon, "height_m": num(r.get("height_m", "")),
                          "crown_area_m2": num(r.get("crown_area_m2", "")), "tile": r.get("tile_id", "")})
    tiles = {}
    for t in trees:
        if not t["tile"]:
            continue
        e = tiles.setdefault(t["tile"], {"tile": t["tile"], "tall": 0, "structure": 0, "lat": 0.0, "lon": 0.0, "n": 0})
        e[t["reason"]] += 1
        e["lat"] += t["lat"]; e["lon"] += t["lon"]; e["n"] += 1
    for e in tiles.values():
        e["lat"] /= e["n"]; e["lon"] /= e["n"]
    # tall objects first (rare and always worth a look), then most trees on structures
    ranked = sorted(tiles.values(), key=lambda e: (-e["tall"], -e["structure"]))[:top]

    summary = json.loads((d / "summary.json").read_text()) if (d / "summary.json").exists() else {}
    dens = summary.get("target_density")
    dens = int(dens) if isinstance(dens, (int, float)) and float(dens).is_integer() else (dens or 0)
    c = CONFIG["cluster"]
    return {"available": True, "trees": trees,
            "counts": {"tall": sum(t["reason"] == "tall" for t in trees),
                       "structure": sum(t["reason"] == "structure" for t in trees)},
            "tiles": ranked, "density": dens,
            "repo": c["repo"], "data": c["data"], "pdal": c.get("pdal", "pdal"), "login": f"{c['user']}@{c['host']}"}


def run_layers(name: str, run: str) -> dict | None:
    """Map pictures from layers.py for this city and run: NDVI and our canopy against NDVI."""
    f = DATA / name / "layers" / "layers.json"
    if not f.exists():
        return None
    info = json.loads(f.read_text())
    out = {}
    for key, l in info.get("layers", {}).items():
        if key == "ndvi" or l.get("run") == run:
            kind = "ndvi" if key == "ndvi" else "compare" if key.endswith("_compare") else "canopy"
            out[kind] = dict(label=l["label"], url=f"/api/city/{name}/layer/{l['file']}?v={int((f.parent / l['file']).stat().st_mtime)}")
    return dict(bounds=info["bounds"], cell_m=info.get("cell_m"), green_ndvi=info.get("green_ndvi"), items=out) if out else None


@app.get("/api/city/{name}/layer/{file}")
def api_layer(name: str, file: str):
    find_city(name)
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.png", file):
        raise HTTPException(400, "bad file name")
    p = DATA / name / "layers" / file
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="image/png")


@app.get("/api/city/{name}/outline")
def api_outline(name: str):
    """The city's official limits (Census place outline), or null when not downloaded yet."""
    find_city(name)
    f = DATA / "_outlines" / f"{name}.geojson"
    return json.loads(f.read_text()) if f.exists() else None


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
        cluster=f"cd {c['repo']}\npython src/city/status.py\npython src/city/problems.py",
        pc=f"mkdir -p {local}\nrsync -av --delete \\\n  {c['user']}@{c['host']}:{c['data']}/cities/_dashboard/ \\\n  {local}/")


# ─── Dataset registry (the "Datasets" tab) ────────────────────────────────────
# The registry is its own small service (folder registry/, compose service "registry"). These routes pass
# /registry/... through to it, so its page, API and saving all work from this app's address.
REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://registry:8080")


def _registry_call(url: str, method: str, body: bytes | None, headers: dict):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:                      # 400 / 404 / 409 come back as they are
        return e.code, e.read(), dict(e.headers)


@app.get("/registry")
def registry_root():
    return RedirectResponse("/registry/")


@app.api_route("/registry/{path:path}", methods=["GET", "PUT"])
async def registry_proxy(request: Request, path: str):
    url = f"{REGISTRY_URL}/registry/{path}" + (f"?{request.url.query}" if request.url.query else "")
    fwd = {k: v for k, v in request.headers.items() if k.lower() in ("content-type", "if-match")}
    body = await request.body() if request.method == "PUT" else None
    try:
        code, data, h = await run_in_threadpool(_registry_call, url, request.method, body, fwd)
    except OSError:
        return Response("The dataset registry is not running. Start it with: docker compose up -d registry",
                        status_code=503, media_type="text/plain")
    keep = {k: v for k, v in h.items() if k.lower() in ("etag", "content-disposition", "cache-control")}
    return Response(data, status_code=code, media_type=h.get("Content-Type"), headers=keep)


app.mount("/vendor", StaticFiles(directory=ROOT / "frontend" / "vendor"), name="vendor")


NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}


@app.get("/")
def index():
    # The page asks for app.js and style.css with their modification time in the address,
    # so the browser can never show an old copy after the files change.
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    for f in ("app.js", "style.css"):
        html = html.replace(f'"/{f}"', f'"/{f}?v={int((ROOT / "frontend" / f).stat().st_mtime)}"')
    return HTMLResponse(html, headers=NO_CACHE)


@app.get("/{path}")
def static(path: str):
    p = ROOT / "frontend" / path
    if path in ("app.js", "style.css") and p.exists():
        return FileResponse(p, headers=NO_CACHE)
    raise HTTPException(404)
