.PHONY: check lint types test env-check fmt

check: lint types test env-check

lint:
	uv run --locked ruff check .
	uv run --locked ruff format --check .

types:
	uv run --locked mypy

test:
	uv run --locked pytest

env-check:
	./scripts/check-env-files.sh

fmt:
	uv run --locked ruff check --fix .
	uv run --locked ruff format .
