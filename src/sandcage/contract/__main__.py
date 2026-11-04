"""Write (or check) the generated contract files: ``python -m sandcage.contract [--check]``."""

from __future__ import annotations

import argparse
import sys

from sandcage.contract.generate import DEFAULT_OUT, stale_files, write_files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sandcage.contract")
    parser.add_argument("--check", action="store_true", help="fail if any file is out of date")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="the contract folder")
    options = parser.parse_args(argv)
    if options.check:
        stale = stale_files(options.out)
        if stale:
            print("contract files are out of date (run `make contract`):", file=sys.stderr)
            for name in stale:
                print(f"  {name}", file=sys.stderr)
            return 1
        print("contract: up to date")
        return 0
    for name in write_files(options.out):
        print(f"wrote {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
