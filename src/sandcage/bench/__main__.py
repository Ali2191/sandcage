"""``python -m sandcage.bench``: run the Bench and write the results file.

Needs Docker running: every run executes in the real sandbox. Only the scripted provider
exists in this build, so every record is labelled "scoring check"; the real Bench (a real
model on Vultr credits) is a separate step and writes records with that model's name.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from sandcage.bench import results as bench_results
from sandcage.bench.catalogue import SCENARIOS
from sandcage.bench.runner import run_suite
from sandcage.bench.scenario import Mode
from sandcage.sandbox.container import DockerSandbox
from sandcage.sandbox.container import Limits as SandboxLimits
from sandcage.sandbox.docker_api import DockerError, EngineClient
from sandcage.sandbox.run import RunnerFactory
from sandcage.sandbox.runtime import RuntimeSelector, requested_runtime

DEFAULT_OUT: Final = Path("benchmarks/out")
RESULTS_JSON: Final = "results.json"
RESULTS_MD: Final = "results.md"


def parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m sandcage.bench", description=__doc__)
    parser.add_argument("--mode", choices=["baseline", "full", "both"], default="both")
    parser.add_argument("--provider", choices=["fake"], default="fake")
    parser.add_argument("--only", default="", help="comma-separated scenario ids")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    return parser.parse_args(argv)


def docker_runner() -> tuple[RunnerFactory, str | None]:
    """The real sandbox, and the runtime label for the notes (runc or gVisor)."""
    api = EngineClient.from_env()
    status = RuntimeSelector(api, requested_runtime()).status()

    def factory(workspace: Path) -> DockerSandbox:
        return DockerSandbox(
            api, workspace, runtime=status.active, limits=SandboxLimits(timeout=30.0)
        )

    return factory, status.label


def main(
    argv: Sequence[str] | None = None,
    *,
    out: Callable[[str], object] = print,
    runner: Callable[[], tuple[RunnerFactory, str | None]] = docker_runner,
) -> int:
    args = parse(argv)
    known = {s.id for s in SCENARIOS}
    only = tuple(part.strip() for part in args.only.split(",") if part.strip())
    unknown = [part for part in only if part not in known]
    if unknown:
        out(f"unknown scenario: {', '.join(unknown)}")
        return 2
    modes = (Mode.BASELINE, Mode.FULL) if args.mode == "both" else (Mode(args.mode),)
    try:
        factory, label = runner()
    except DockerError as problem:
        out(f"the Bench needs Docker: {problem}")
        return 2
    started = time.monotonic()
    results = run_suite(runner_factory=factory, modes=modes, only=only, progress=out)
    data = bench_results.record(
        results,
        provider=args.provider,
        model="scripted",
        scripted=True,
        date=time.strftime("%Y-%m-%d", time.gmtime()),
        modes=modes,
        sandbox=label,
    )
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / RESULTS_JSON).write_text(bench_results.dumps(data), encoding="utf-8")
    (args.out / RESULTS_MD).write_text(bench_results.markdown(data), encoding="utf-8")
    out(f"sandbox: {label or 'unknown'}; {time.monotonic() - started:.0f}s; wrote {args.out}")
    out(f"{data['label']}: {len(results)} scenarios, {data['errors']} errors")
    if data["errors"] != 0 or data.get("as_expected") is not True:
        out("the scoring check did not come out as designed; see the results file")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
