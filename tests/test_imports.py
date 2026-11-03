"""Every module imports on its own in a fresh interpreter (catches import cycles)."""

import pkgutil
import subprocess
import sys

import pytest

import sandcage


def _all_modules() -> list[str]:
    names = ["sandcage"]
    for info in pkgutil.walk_packages(sandcage.__path__, prefix="sandcage."):
        names.append(info.name)
    return sorted(names)


@pytest.mark.parametrize("module", _all_modules())
def test_module_imports_first_in_fresh_interpreter(module: str) -> None:
    result = subprocess.run(  # noqa: S603 - fixed interpreter, module names from our own package
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
