#!/usr/bin/env bash
# Bring the whole deploy stack (docker-compose.yml) up on this machine, check it, and take it
# down: the rehearsal for the real VM. Scripted models (no provider call, no key), Caddy's own
# local certificate for "localhost", real Docker sandboxes (plain runc: no gVisor here).
#
#   scripts/stack_rehearsal.sh up      build everything and start the stack
#   scripts/stack_rehearsal.sh test    scripts/verify_deploy.py, then the browser spec
#   scripts/stack_rehearsal.sh down    stop and remove the stack (--purge also deletes its state)
#
# State lives in $SANDCAGE_REHEARSAL_DIR (default ~/.sandcage-rehearsal, mode 700): the env file,
# a random access code in a mode-600 file, a fake key file, and the work and data directories.
# The access code is never printed.
set -euo pipefail

main() {
    local root dir action="${1:-}"
    root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    dir="${SANDCAGE_REHEARSAL_DIR:-$HOME/.sandcage-rehearsal}"
    cd "$root"
    case "$action" in
        up) stack_up "$root" "$dir" ;;
        test) stack_test "$root" "$dir" ;;
        down) stack_down "$root" "$dir" "${2:-}" ;;
        *)
            echo "usage: stack_rehearsal.sh up | test | down [--purge]" >&2
            return 2
            ;;
    esac
}

compose() {
    docker compose -p sandcage --env-file "$REHEARSAL_ENV" "$@"
}

stack_up() {
    local root="$1" dir="$2" gid
    REHEARSAL_ENV="$dir/secrets/stack.env"
    umask 077
    mkdir -p "$dir/work" "$dir/data" "$dir/secrets"
    # The containers run as uid 10001; this is a scratch directory inside a private parent.
    chmod 777 "$dir/work" "$dir/data"
    if [ ! -f "$dir/secrets/code" ]; then
        { head -c 96 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | cut -c1-24; } >"$dir/secrets/code.tmp"
        mv "$dir/secrets/code.tmp" "$dir/secrets/code"
    fi
    if [ ! -f "$dir/secrets/llm-api-key" ]; then
        { echo "rehearsal-fake-key-$(head -c 16 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')"; } >"$dir/secrets/llm-api-key"
    fi
    chmod 600 "$dir/secrets/code" "$dir/secrets/llm-api-key"
    chmod 644 "$dir/secrets/llm-api-key" # read by uid 10001 in the container; the content is fake
    gid="$(docker run --rm -v /var/run/docker.sock:/var/run/docker.sock alpine:3.20 stat -c '%g' /var/run/docker.sock)"
    local code
    code="$(cat "$dir/secrets/code")"
    {
        printf 'SITE_ADDRESS=localhost\n'
        printf 'ACME_EMAIL=rehearsal@example.com\n'
        printf 'DEMO_ACCESS_CODE=%s\n' "$code"
        printf 'DEMO_RUNS_PER_IP_PER_HOUR=1000\n'
        printf 'DEMO_RUNS_PER_DAY=1000\n'
        printf 'DEMO_MAX_CONCURRENT_RUNS=4\n'
        printf 'LLM_PROVIDER=scripted\n'
        printf 'SANDCAGE_LLM_KEY_FILE=%s\n' "$dir/secrets/llm-api-key"
        printf 'SANDBOX_RUNTIME=runc\n'
        printf 'DOCKER_GID=%s\n' "$gid"
        printf 'SANDCAGE_WORK_DIR=%s\n' "$dir/work"
        printf 'SANDCAGE_DATA_DIR=%s\n' "$dir/data"
    } >"$REHEARSAL_ENV"
    chmod 600 "$REHEARSAL_ENV"
    docker build --quiet -t sandcage-sandbox:latest src/sandcage/sandbox/image >/dev/null
    compose up -d --build --wait --wait-timeout 300
    echo "stack: up at https://localhost (state in $dir; the access code is in $dir/secrets/code)"
}

stack_test() {
    local root="$1" dir="$2"
    REHEARSAL_ENV="$dir/secrets/stack.env"
    uv run --locked python scripts/verify_deploy.py https://localhost --rehearsal \
        --code-file "$dir/secrets/code"
    (
        cd "$root/frontend"
        STACK_REHEARSAL=1 STACK_URL=https://localhost STACK_CODE_FILE="$dir/secrets/code" \
            npx playwright test -c playwright.stack.config.ts
    )
}

stack_down() {
    local dir="$2" purge="${3:-}"
    REHEARSAL_ENV="$dir/secrets/stack.env"
    if [ -f "$REHEARSAL_ENV" ]; then
        compose down --volumes --remove-orphans
    fi
    local leftover
    for leftover in $(docker ps -aq --filter "label=sandcage=sandbox"); do
        docker rm -f "$leftover" >/dev/null 2>&1 || true
    done
    if [ "$purge" = "--purge" ]; then
        rm -rf "$dir"
        echo "state removed"
    fi
}

main "$@"; exit
