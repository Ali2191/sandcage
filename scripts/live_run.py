"""The live smoke: the demo through the whole harness on a real Vultr model and real
Docker, guard on, then observe mode with the guard off. Costs money; run by hand only:
``uv run --locked python scripts/live_run.py``. Never in CI. The key is read from the file
named by SANDCAGE_KEY_FILE (default ~/.sandcage/vultr-demo.key, mode 600). Event streams
and reports are saved under live-runs/ (git-ignored). See ``sandcage.live_run``.
"""

from __future__ import annotations

import sys

from sandcage.live_run import main

if __name__ == "__main__":
    sys.exit(main())
