.PHONY: help dev dev-build down logs refresh

# Default target
.DEFAULT_GOAL := help

# Cluster settings for `make refresh` (the same values as in config.json)
CLUSTER      := fr_ac1079@uc3.scc.kit.edu
CLUSTER_DATA := /pfs/work9/workspace/scratch/fr_ac1079-urban_trees/data

# ─── Help ────────────────────────────────────────────────────────────────────

help:
	@echo ""
	@echo "City dashboard"
	@echo ""
	@echo "Usage: make <command>"
	@echo ""
	@echo "  dev        Start the dashboard        -> http://localhost:8010"
	@echo "  dev-build  Rebuild the image, then start"
	@echo "  refresh    Copy the latest results from the cluster (run in WSL;"
	@echo "             first run 'python src/city/status.py' on the cluster)"
	@echo "  logs       Tail the container log"
	@echo "  down       Stop and remove the container (data/ stays)"
	@echo ""

# ─── Run ─────────────────────────────────────────────────────────────────────

# The source folder is mounted and uvicorn runs with --reload, so backend changes
# are picked up on their own; frontend changes need a browser reload.
dev:
	docker compose up

dev-build:
	docker compose up --build

# ─── Data ────────────────────────────────────────────────────────────────────

# Copies data/cities/_dashboard/ from the cluster into ./data/_dashboard/.
# Asks for the one-time code, then the password. No lidar is copied, only small tables.
refresh:
	mkdir -p data/_dashboard
	rsync -av --delete $(CLUSTER):$(CLUSTER_DATA)/cities/_dashboard/ data/_dashboard/

# ─── Logs / teardown ─────────────────────────────────────────────────────────

logs:
	docker compose logs -f

down:
	docker compose down
