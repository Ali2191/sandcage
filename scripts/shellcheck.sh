#!/usr/bin/env bash
# Runs shellcheck over every shell script in the repository (the deploy kit and the scripts).
# Uses the shellcheck on PATH (CI has one), else the pinned image through Docker. With neither
# it fails: a lint that cannot run is not a pass.
set -euo pipefail

main() {
    local root
    root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    cd "$root"
    local files
    files=(deploy/deploy.sh scripts/*.sh)
    if command -v shellcheck >/dev/null 2>&1; then
        shellcheck -x "${files[@]}"
    elif command -v docker >/dev/null 2>&1; then
        docker run --rm -v "$root:/mnt" -w /mnt koalaman/shellcheck:v0.10.0 -x "${files[@]}"
    else
        echo "shellcheck is not installed and Docker is not available" >&2
        exit 1
    fi
    echo "shellcheck: ok (${#files[@]} files)"
}

main "$@"; exit
