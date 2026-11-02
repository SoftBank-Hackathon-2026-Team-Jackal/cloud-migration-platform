.PHONY: setup infra-up infra-down api workers worker lint test check openapi smoke-temporal

setup:
	uv sync --extra dev --locked

infra-up:
	docker compose --env-file .env -f infra/local/compose.yaml up -d

infra-down:
	docker compose --env-file .env -f infra/local/compose.yaml down

api:
	uv run uvicorn cmp_api.main:app --reload --host 127.0.0.1 --port 8000

workers:
	uv run python -m cmp_worker.workers.run_all

worker:
	uv run python -m cmp_worker.workers.run --queue $(QUEUE)

lint:
	uv run ruff check .
	uv run ruff format --check .

test:
	uv run pytest

check: lint test

openapi:
	uv run python scripts/export_openapi.py

smoke-temporal:
	uv run python scripts/smoke_temporal.py
