#!/usr/bin/env bash
# Fails if git tracks any env file other than the allowed examples.
# Allowed: *.env.example anywhere, and the demo repo's deliberately fake .env.
set -euo pipefail

main() {
    local tracked bad
    tracked="$(git ls-files | grep -Ei '(^|/)(\.env[^/]*|[^/]*\.env)$' || true)"
    bad="$(printf '%s\n' "$tracked" \
        | grep -Ev '(^|/)\.env\.example$' \
        | grep -Ev '^demo-repos/invoice-calc/\.env$' \
        | grep -v '^$' || true)"
    if [[ -n "$bad" ]]; then
        echo "Tracked env files that must not be committed:" >&2
        printf '  %s\n' "$bad" >&2
        exit 1
    fi
    echo "env check: ok"
}

main "$@"
