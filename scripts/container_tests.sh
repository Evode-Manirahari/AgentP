#!/usr/bin/env bash
set -euo pipefail

docker compose build api
docker compose run --rm -v "${PWD}:/app" api sh -lc 'pip install --no-cache-dir -r requirements-dev.txt && python -m pytest'
