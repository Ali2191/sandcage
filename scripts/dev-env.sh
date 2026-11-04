#!/usr/bin/env bash
# Create .dev.env (git-ignored, mode 600) with a random access code if it does not exist.
# The code is never printed; read it from the file when you need to type it in the panel.
set -euo pipefail
main() {
  cd "$(dirname "$0")/.."
  local file=".dev.env"
  if [ -f "$file" ]; then
    chmod 600 "$file"
    return 0
  fi
  local code tmp
  code="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  tmp="$(mktemp .dev.env.XXXXXX)"
  chmod 600 "$tmp"
  {
    echo "DEMO_ACCESS_CODE=${code}"
    echo "PANEL_ORIGIN=http://127.0.0.1:3000"
    echo "SANDCAGE_API_PORT=8000"
    echo "SANDCAGE_DATA_DIR=.dev-data"
    echo "DEMO_RUNS_PER_IP_PER_HOUR=1000"
    echo "DEMO_RUNS_PER_DAY=1000"
    echo "NEXT_PUBLIC_API_URL=http://127.0.0.1:8000"
  } >"$tmp"
  mv "$tmp" "$file"
  echo "created $file (access code inside; not printed)"
}
main "$@"
