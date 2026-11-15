# City dashboard

One place to see, for every city: what is done, what to run next, and the results.
It belongs to the city pipeline in `urban-tree-detection` (`src/city/`).

- **Map:** every candidate city, coloured by status (done, in progress, not started).
- **To do (per city):** the pipeline steps with their state and the exact command for the next one.
- **Results (per city):** trees, canopy cover, a neighbourhood map you can colour by trees or by
  income, renters, density, and a scatter plot of one against the other.

The dashboard never connects to the cluster. You paste its commands into your cluster terminal,
and it reads a small copy of the results that you fetch with one `rsync`.

## Start

With Docker, like the labelling tool (run in WSL, in this folder):

```bash
make dev-build      # first time, or after requirements.txt changed
make dev            # afterwards
```

Open http://localhost:8010. Stop with Ctrl+C or `make down`. The map background needs internet.
Port 8010 does not clash with the labelling tool (8000, 5173, 80), so both can run together.

Without Docker: `pip install -r requirements.txt`, then `python run.py`.

## Get the data from the cluster

Press **Refresh from cluster** in the app. It shows two commands:

1. on the cluster: `python src/city/status.py` writes `data/cities/_dashboard/` (status of every
   city plus the small result tables; no lidar and no per-tree tables);
2. in WSL on your PC: an `rsync` that copies that folder into `data/_dashboard/` here.

Then press "I ran both: reload".

Step 2 is also available as `make refresh` (in WSL, in this folder).

## The steps it tracks

| Step | Script on the cluster | Done when |
|---|---|---|
| Check the lidar | `survey_density.py` | a survey covers the city |
| City outline | `city_limits.py` | the official outline was downloaded |
| Plan the tiles | `plan.py --polygon` | `plan.json` exists |
| Download the lidar | `fetch.py` | every planned tile is there |
| Census data | `census.py --key` | block groups with population and income exist |
| Process at 4 points per m² | `hpc/city_process.slurm`, `TAG=d4` | every downloaded tile is processed |
| Result tables at 4 | `combine.py --tag d4 --polygons` | summary and neighbourhood table are current |
| Process / tables at 8 | same with `d8` | only for cities whose lidar reaches 8 points per m² |

Decisions built in (2026-11-15): a tree is at least 2.5 m high with a crown of at least 3 m²;
every city runs at 4 points per m², and at 8 as a second run where the lidar allows; cities are
outlined by their official limits.

## Files

- `cities.csv`: the city list (name, state, Census place name, position, estimated lidar density).
  "Add city" in the app appends to it; you can also edit it by hand.
- `config.json`: cluster user, host and paths used to build the commands; this folder's WSL path
  (`pc_folder_wsl`, used for the copy command); the port for `python run.py`.
- `Dockerfile`, `docker-compose.yml`, `Makefile`: the container setup. One service; the backend
  serves the page, so there is no separate frontend container.
- `backend/app.py`: reads `data/_dashboard/`, decides the state of each step, builds the commands.
- `frontend/`: the page (Vue and Leaflet are included in `frontend/vendor/`, no build step).
- `data/_dashboard/`: the copy from the cluster (not in git).

## Limits

- It shows what the last refresh copied, not the live state of the cluster. Running jobs are not
  shown: a step stays "partly done" until its tiles are finished and you refresh.
- The neighbourhood map shows block groups; individual trees are not copied to the PC.
- Tested with Gainesville's numbers and stand-in block group shapes; the first refresh from the
  real cluster is the real test.
