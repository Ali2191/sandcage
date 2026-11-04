"""Shared by the deploy tests: where the kit's files are and how to read the compose file."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Final, cast

import yaml

REPO: Final = Path(__file__).resolve().parents[1]
COMPOSE_FILE: Final = REPO / "docker-compose.yml"
DEPLOY_DIR: Final = REPO / "deploy"
CADDYFILE: Final = DEPLOY_DIR / "Caddyfile"
DOCKERFILES: Final = (
    DEPLOY_DIR / "api.Dockerfile",
    DEPLOY_DIR / "panel.Dockerfile",
    DEPLOY_DIR / "caddy.Dockerfile",
)

Service = dict[str, Any]


def load_compose() -> dict[str, Any]:
    loaded = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return cast("dict[str, Any]", loaded)


def services() -> dict[str, Service]:
    return cast("dict[str, Service]", load_compose()["services"])


def environment(service: Service) -> dict[str, str]:
    raw = service.get("environment", {})
    assert isinstance(raw, dict), "environment must be written as a mapping"
    return {str(key): str(value) for key, value in raw.items()}


def reference_names(text: str) -> set[str]:
    """Variable names referenced as ${NAME...} in a text."""
    return set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", text))
