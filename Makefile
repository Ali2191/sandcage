.PHONY: check lint types test env-check fmt sandbox-image test-docker

check: lint types test env-check

lint:
	uv run --locked ruff check .
	uv run --locked ruff format --check .

types:
	uv run --locked mypy

test:
	uv run --locked pytest

env-check:
	./scripts/check-tracked-secrets.sh

fmt:
	uv run --locked ruff check --fix .
	uv run --locked ruff format .

# The sandbox image is built on the host: the Docker socket proxy refuses builds.
sandbox-image:
	docker build -t sandcage-sandbox:latest src/sandcage/sandbox/image

# Tests that need a real Docker daemon (and the sandbox image).
test-docker:
	uv run --locked pytest -m docker
