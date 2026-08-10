.PHONY: build up down logs ps test smoke db-upgrade container-test demo demo-packet retention

build:
	docker compose build

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f api worker

ps:
	docker compose ps

test:
	python3 -m compileall app worker tests
	python3 -m pytest

smoke:
	curl -sS --max-time 10 http://localhost:8000/health

db-upgrade:
	alembic upgrade head

container-test:
	./scripts/container_tests.sh

demo:
	./scripts/demo_e2e.sh

demo-packet:
	./scripts/demo_packet.sh

retention:
	docker compose run --rm api python -m worker.retention
