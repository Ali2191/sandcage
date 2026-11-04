.PHONY: check lint types test env-check contract-check contract fmt sandbox-image test-docker api

check: lint types test env-check contract-check

lint:
	uv run --locked ruff check .
	uv run --locked ruff format --check .

types:
	uv run --locked mypy

test:
	uv run --locked pytest

env-check:
	./scripts/check-tracked-secrets.sh

# The typed event contract (contract/) is generated from src/sandcage/contract.
contract:
	uv run --locked python -m sandcage.contract

contract-check:
	uv run --locked python -m sandcage.contract --check

fmt:
	uv run --locked ruff check --fix .
	uv run --locked ruff format .

# The sandbox image is built on the host: the Docker socket proxy refuses builds.
sandbox-image:
	docker build -t sandcage-sandbox:latest src/sandcage/sandbox/image

# Tests that need a real Docker daemon (and the sandbox image).
test-docker:
	uv run --locked pytest -m docker

# The API on 127.0.0.1:8000 (scripted models unless LLM_PROVIDER says otherwise).
api:
	uv run --locked python -m sandcage.api
