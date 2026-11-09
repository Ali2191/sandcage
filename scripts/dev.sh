#!/usr/bin/env bash
# Start the API (scripted models unless LLM_PROVIDER says otherwise) and the panel together,
# from .dev.env. Ctrl-C stops both. The access code is never printed.
set -euo pipefail
main() {
  cd "$(dirname "$0")/.."
  ./scripts/dev-env.sh
  set -a
  # shellcheck disable=SC1091
  . ./.dev.env
  set +a
  uv run --locked python -m sandcage.api &
  local api=$!
  (cd frontend && exec npm run dev) &
  local panel=$!
  trap 'kill "$api" "$panel" 2>/dev/null || true' EXIT INT TERM
  echo "API    http://127.0.0.1:${SANDCAGE_API_PORT}"
  echo "Panel  http://127.0.0.1:3000   (access code: see .dev.env)"
  # Portable "wait for either": macOS ships bash 3.2, which has no `wait -n`.
  while kill -0 "$api" 2>/dev/null && kill -0 "$panel" 2>/dev/null; do
    sleep 1
  done
}
main "$@"
