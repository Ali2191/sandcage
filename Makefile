.PHONY: check lint types test env-check contract-check contract fmt sandbox-image test-docker panel-check

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

# The web panel (frontend/): lint, typecheck, unit tests and the production build.
# Needs `npm ci` in frontend/ once. The backend's `make check` does not run it.
panel-check:
	cd frontend && npm run check
