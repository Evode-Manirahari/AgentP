.PHONY: build up down logs ps test smoke db-upgrade container-test demo demo-packet \
	packet-eval packet-eval-fast retention

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
	python3 -m compileall agentp_client app evals worker tests
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

packet-eval:
	python3 -m evals.packet_reliability \
		--json-out reports/packet-reliability.json \
		--markdown-out reports/packet-reliability.md

packet-eval-fast:
	python3 -m evals.packet_reliability --skip-ocr

retention:
	docker compose run --rm api python -m worker.retention
