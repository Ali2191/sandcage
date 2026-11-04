"""Provider smoke test against Vultr Serverless Inference (real calls, costs money).

Run by hand only: ``uv run --locked python scripts/check_provider.py``. Never in CI.
The key is read from the file named by SANDCAGE_KEY_FILE (default
~/.sandcage/vultr-demo.key, mode 600). See ``sandcage.provider_check`` for the limits.
"""

from __future__ import annotations

import sys

from sandcage.provider_check import run_check

if __name__ == "__main__":
    sys.exit(run_check(print))
