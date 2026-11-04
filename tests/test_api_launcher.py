"""scripts/api_with_key_file.py: the key is read from a file by the API process, never shown;
a scripted rehearsal needs no key at all."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType

import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / "scripts" / "api_with_key_file.py"
FAKE_KEY = "test-key-not-a-real-credential-1234567890"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("api_with_key_file", LAUNCHER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _forget(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    """Unset variables the launcher may set, so monkeypatch restores them afterwards."""
    for name in names:
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)


def _key_file(tmp_path: Path, mode: int = 0o600) -> Path:
    path = tmp_path / "key"
    path.write_text(FAKE_KEY + "\n", encoding="ascii")
    path.chmod(mode)
    return path


def test_the_key_goes_into_this_process_only_and_the_api_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = _load()
    _forget(monkeypatch, "LLM_API_KEY", "LLM_PROVIDER", "LLM_MODEL")
    monkeypatch.setenv("SANDCAGE_KEY_FILE", str(_key_file(tmp_path)))
    started: list[str | None] = []

    def fake_main() -> int:
        started.append(os.environ.get("LLM_API_KEY"))
        return 0

    monkeypatch.setattr(launcher, "main", fake_main)
    assert launcher.run() == 0
    assert started == [FAKE_KEY]
    assert os.environ["LLM_PROVIDER"] == "vultr"


def test_a_key_file_that_others_can_read_is_refused_and_the_key_is_not_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    launcher = _load()
    _forget(monkeypatch, "LLM_PROVIDER")
    monkeypatch.setenv("SANDCAGE_KEY_FILE", str(_key_file(tmp_path, mode=0o644)))
    monkeypatch.setattr(launcher, "main", lambda: pytest.fail("the API must not start"))
    assert launcher.run() == 2
    seen = capsys.readouterr()
    assert "600" in seen.err
    assert FAKE_KEY not in seen.out + seen.err


def test_a_missing_key_file_stops_the_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    launcher = _load()
    _forget(monkeypatch, "LLM_PROVIDER")
    monkeypatch.setenv("SANDCAGE_KEY_FILE", str(tmp_path / "absent"))
    monkeypatch.setattr(launcher, "main", lambda: pytest.fail("the API must not start"))
    assert launcher.run() == 2
    assert "no key file" in capsys.readouterr().err


@pytest.mark.parametrize("provider", ["scripted", "fake"])
def test_a_scripted_rehearsal_reads_no_key(
    provider: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = _load()
    _forget(monkeypatch, "LLM_API_KEY")
    monkeypatch.setenv("LLM_PROVIDER", provider)
    monkeypatch.setenv("SANDCAGE_KEY_FILE", str(tmp_path / "absent"))
    seen: list[str | None] = []

    def fake_main() -> int:
        seen.append(os.environ.get("LLM_API_KEY"))
        return 0

    monkeypatch.setattr(launcher, "main", fake_main)
    assert launcher.run() == 0
    assert seen == [None]
