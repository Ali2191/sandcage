"""Start the API with a real provider whose key is read from a key file, by this process only.

    SANDCAGE_KEY_FILE=~/.sandcage/vultr-demo.key python scripts/api_with_key_file.py

In the deploy (docker-compose.yml) the file is a mounted secret, ``/run/secrets/llm_api_key``.
With ``LLM_PROVIDER=scripted`` (a local rehearsal) no key is read and none is needed.

The key never appears in a command line, a file this script writes, or its output: it is read
(mode 600, owned by you, not a link; see ``provider_check.read_key``) and put into this
process's own environment as ``LLM_API_KEY``, then the normal API starts. ``LLM_PROVIDER``
defaults to ``vultr`` and ``LLM_MODEL`` to the model the live smoke run used; every other API
setting (``DEMO_ACCESS_CODE``, ``PANEL_ORIGIN``, ``DEMO_RUN_TOKEN_CAP`` ...) is read as usual.
"""

from __future__ import annotations

import os
import sys

from sandcage.api.main import main
from sandcage.api.settings import SCRIPTED_PROVIDERS
from sandcage.provider_check import ProviderCheckError, key_path, read_key

DEFAULT_MODEL = "deepseek-v4.1-flash-1017"


def run() -> int:
    if os.environ.get("LLM_PROVIDER", "").strip().lower() in SCRIPTED_PROVIDERS:
        return main()
    try:
        key = read_key(key_path())
    except ProviderCheckError as problem:
        print(f"api_with_key_file: {problem}", file=sys.stderr)
        return 2
    os.environ["LLM_API_KEY"] = key
    os.environ.setdefault("LLM_PROVIDER", "vultr")
    os.environ.setdefault("LLM_MODEL", DEFAULT_MODEL)
    return main()


if __name__ == "__main__":
    raise SystemExit(run())
