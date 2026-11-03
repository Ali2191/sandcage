"""``python -m sandcage.dockerproxy``: run the Docker socket proxy."""

import sys

from sandcage.dockerproxy.server import main

if __name__ == "__main__":
    sys.exit(main())
