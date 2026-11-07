.PHONY: setup infra-up infra-down api workers worker lint test check openapi smoke-temporal \
	data-tunnel-nlb-up data-tunnel-nlb-status data-tunnel-nlb-down

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

# Provisioning 82: 데이터 터널 고정 원본 NLB(standing_ingress). CONTEXT=원본 EKS kubectl context, CIDR=맥 공인 IP/32
data-tunnel-nlb-up:
	uv run python scripts/data_tunnel_nlb.py up --context $(CONTEXT) --cidr $(CIDR)

data-tunnel-nlb-status:
	uv run python scripts/data_tunnel_nlb.py status --context $(CONTEXT)

data-tunnel-nlb-down:
	uv run python scripts/data_tunnel_nlb.py down --context $(CONTEXT)
