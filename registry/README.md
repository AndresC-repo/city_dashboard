# Dataset registry (the dashboard's "Datasets" tab)

What each dataset is, what we use it for, where it comes from, which release we used, where it is,
and how far we are with getting it, with a checklist, next steps and comments per dataset.

- Runs as the `registry` service in `../docker-compose.yml`; the dashboard passes `/registry/...` through to it.
  Open http://localhost:8010 and click **Datasets** (or go straight to http://localhost:8010/registry/).
- State: `registry-data/registry.json`, plus the last 50 saves in `registry-data/history/`. Commit this folder.
- `seed/registry.json` is copied only on the very first start (when `registry-data/registry.json` does not exist).
- The "on disk" check reads `../../urban-tree-detection/data` (read-only; change with `PROJECT_DATA=` in `../.env`).
  Folders written in parentheses, such as `(HPC: …)`, are on the cluster and are not checked.
- Start / stop with the dashboard: `make dev` / `make down`. Restart only the registry: `docker compose restart registry`.
- Export: "Export Markdown" at the bottom of the page.
- No login: localhost only.
