"""The real API for the panel's end-to-end tests: ``python -m tests.e2e_api``.

Same startup as ``python -m sandcage.api`` (settings, event store, guard, run manager,
HTTP server, CORS), with scripted models and the host runner the HTTP tests use instead
of a Docker sandbox, so the browser specs need no Docker. Scenarios that need a real
container are covered by the Docker-marked tests. Reads the usual API environment
(``SANDCAGE_API_PORT``, ``DEMO_ACCESS_CODE``, ``PANEL_ORIGIN``, ``SANDCAGE_DATA_DIR`` ...).
"""

from __future__ import annotations

import logging
import sys

from sandcage.api.main import StartupError, build, serve
from tests.agent_helpers import LocalRunner


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        built = build(runner_factory=LocalRunner)
    except StartupError as problem:
        print(f"e2e api: not starting: {problem}", file=sys.stderr)
        return 2
    settings = built.app.settings
    server = serve(built, settings.host, settings.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        built.store.close()
        built.lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
