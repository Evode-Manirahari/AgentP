.PHONY: build up down logs ps test smoke container-test demo

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

container-test:
	./scripts/container_tests.sh

demo:
	./scripts/demo_e2e.sh

