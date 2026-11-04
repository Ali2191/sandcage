#!/usr/bin/env bash
# Sandcage deploy script: set up or update the demo on one Ubuntu 24.04 VM. Safe to run again.
#
#   sudo ./deploy/deploy.sh --up --email you@example.com --generate-code
#   sudo ./deploy/deploy.sh --down
#   sudo ./deploy/deploy.sh --destroy-data
#
# Exactly one action is required (--up, --down or --destroy-data); with none, or with a script
# that was cut off while it downloaded, nothing at all happens.
#
# What it does for --up, in order: checks root, the OS, Docker, the compose plugin, git, curl
# and free disk; clones or updates the repository (read-only deploy key); writes the env file
# (mode 600, atomically, secrets from hidden prompts or mode-600 files, never a command line);
# creates the data and work directories; builds the sandbox image on the host; verifies gVisor
# (or falls back to plain Docker only with --allow-runc-fallback and a loud warning); starts the
# compose stack; waits for /readyz; prints a summary that contains no secret.
#
# The whole body is inside functions and the last line calls main, so a partial download cannot
# run half a script, and `git pull` rewriting this very file mid-run cannot derail it (bash
# reads a script as it executes: Bugs #7).
#
# Paths can be moved with environment variables (used by the tests; the defaults are right on
# the VM): SANDCAGE_APP_DIR, SANDCAGE_ETC_DIR, SANDCAGE_STATE_DIR, SANDCAGE_OS_RELEASE,
# DOCKER_SOCKET_PATH.

set -euo pipefail

readonly API_UID=10001
readonly API_GID=10001
readonly MIN_CODE_LENGTH=12
readonly MIN_FREE_KB=10485760
readonly WAIT_ATTEMPTS=60
readonly WAIT_SECONDS=5
readonly DESTROY_PHRASE="destroy sandcage data"
readonly DEFAULT_MODEL="deepseek-v4.1-flash-1017"
readonly SANDBOX_IMAGE="sandcage-sandbox:latest"
readonly PROJECT="sandcage"

# --- output: only fixed words and non-secret facts ever go through these ------------------

say() { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() {
    printf 'deploy: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'USAGE'
usage: deploy.sh ACTION [options]

actions (exactly one):
  --up                 set up or update the demo (safe to run again)
  --down               stop the stack; data stays on disk
  --destroy-data       stop the stack and delete the event log, work directory and
                       certificates (asks you to type a confirmation phrase)

options for --up:
  --email ADDRESS          contact for certificate notices (needed the first time)
  --site HOST              public host name (default: <ip-with-dashes>.sslip.io of this VM)
  --access-code-file FILE  read the access code from FILE (mode 600)
  --generate-code          make a random 20-character access code and show it once on the terminal
  --rotate-code            replace the saved access code (with --generate-code or a file)
  --llm-key-file FILE      read the Vultr inference key from FILE (mode 600)
  --replace-key            replace the saved key (asks, hidden, unless --llm-key-file is given)
  --model NAME             model for every role (default deepseek-v4.1-flash-1017)
  --runtime gvisor|runc    sandbox runtime (default gvisor; verified with a real probe)
  --allow-runc-fallback    keep going with plain Docker if gVisor cannot be verified (warns)
  --install-gvisor         install runsc from gVisor's official repository first
  --scripted               rehearsal only: scripted models, no key, never the real demo
  --repo URL               clone URL, used only when there is no checkout yet
  --branch NAME            branch to deploy (default main)
  --deploy-key FILE        read-only deploy key for git over ssh (mode 600)
  --no-pull                do not touch the checkout
USAGE
}

# --- small helpers --------------------------------------------------------------------------

file_mode() {
    stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"
}

file_owner() {
    stat -c '%u' "$1" 2>/dev/null || stat -f '%u' "$1"
}

# The first line of a file, without trailing whitespace. The read is a shell builtin: the
# value never appears in a process's argument list.
first_line() {
    local line=""
    IFS= read -r line <"$1" || [ -n "$line" ]
    line="${line%"${line##*[![:space:]]}"}"
    REPLY_VALUE="$line"
}

require_private_file() {
    local path="$1" what="$2" mode owner
    [ -f "$path" ] || die "$what: no such file: $path"
    [ ! -L "$path" ] || die "$what: $path is a link; give the real file"
    mode="$(file_mode "$path")"
    case "$mode" in
        600 | 400) ;;
        *) die "$what: $path has mode $mode; it must be 600 (chmod 600 $path)" ;;
    esac
    owner="$(file_owner "$path")"
    # Owned by root, or by the person who ran sudo (their own mode-600 file).
    case "$owner" in
        0 | "${SUDO_UID:-0}") ;;
        *) die "$what: $path is owned by another user" ;;
    esac
}

valid_code() {
    case "$1" in
        *[!A-Za-z0-9_.~+=:@%-]*) return 1 ;;
    esac
    [ "${#1}" -ge "$MIN_CODE_LENGTH" ] && [ "${#1}" -le 128 ]
}

valid_key() {
    case "$1" in
        *[!A-Za-z0-9_.~+=:@%/-]*) return 1 ;;
    esac
    [ "${#1}" -ge 8 ] && [ "${#1}" -le 512 ]
}

# Ask for a value without echoing it, from the terminal this script runs in.
ask_hidden() {
    local prompt="$1" reply=""
    if [ -t 0 ]; then
        IFS= read -r -s -p "$prompt" reply || true
        printf '\n' >&2
    elif [ -r /dev/tty ] && { : </dev/tty; } 2>/dev/null; then
        IFS= read -r -s -p "$prompt" reply </dev/tty || true
        printf '\n' >&2
    else
        return 1
    fi
    ASKED_VALUE="$reply"
}

random_code() {
    local code
    code="$({ head -c 96 /dev/urandom | base64 | tr -dc 'A-Za-z0-9'; } | cut -c1-20)"
    [ "${#code}" -eq 20 ] || die "could not make a random access code"
    GENERATED_CODE="$code"
}

env_get() {
    # The value of KEY in the env file, or nothing.
    local key="$1" file="$2" line
    [ -f "$file" ] || return 0
    line="$(grep -m1 "^${key}=" "$file" || true)"
    printf '%s' "${line#"${key}="}"
}

# Write a file atomically: the new content is complete before it replaces anything, and the
# file is private from the moment it exists.
write_atomically() {
    local destination="$1" mode="$2" owner="$3" content_file="$4" temp
    temp="$(mktemp_content)"
    cat "$content_file" >"$temp"
    chmod "$mode" "$temp"
    if [ -n "$owner" ]; then
        chown "$owner" "$temp"
    fi
    mv -f "$temp" "$destination"
}

# --- checks ---------------------------------------------------------------------------------

require_root() {
    [ "$(id -u)" = "0" ] || die "run as root (sudo ./deploy/deploy.sh ...)"
}

check_os() {
    local release="${SANDCAGE_OS_RELEASE:-/etc/os-release}" id="" version=""
    if [ -r "$release" ]; then
        id="$(grep -m1 '^ID=' "$release" | cut -d= -f2 | tr -d '"' || true)"
        version="$(grep -m1 '^VERSION_ID=' "$release" | cut -d= -f2 | tr -d '"' || true)"
    fi
    if [ "$id" != "ubuntu" ] || [ "$version" != "24.04" ]; then
        warn "this kit is tested on Ubuntu 24.04; found ${id:-unknown} ${version:-unknown}"
    fi
}

check_prerequisites() {
    local tool
    for tool in docker git curl; do
        command -v "$tool" >/dev/null 2>&1 || die "$tool is not installed (see deploy/RUNBOOK.md)"
    done
    docker compose version >/dev/null 2>&1 || die "the docker compose plugin is not installed (see deploy/RUNBOOK.md)"
    docker info >/dev/null 2>&1 || die "the Docker daemon does not answer (is Docker running, and are you root?)"
    local parent free
    parent="$(dirname "$STATE_DIR")"
    [ -d "$parent" ] || parent="/"
    free="$(df -Pk "$parent" | awk 'NR==2 {print $4}')"
    case "$free" in
        '' | *[!0-9]*) die "could not read the free disk space of $parent" ;;
    esac
    [ "$free" -ge "$MIN_FREE_KB" ] || die "less than 10 GB free on $parent"
    say "prerequisites: docker, compose plugin, git, curl, free disk: ok"
}

# --- the repository --------------------------------------------------------------------------

git_ssh_command() {
    if [ -n "$DEPLOY_KEY" ]; then
        printf 'ssh -i %s -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=%s/known_hosts' \
            "$DEPLOY_KEY" "$ETC_DIR"
    else
        printf 'ssh'
    fi
}

update_repository() {
    local before after
    if [ "$PULL" = "no" ]; then
        [ -f "$APP_DIR/docker-compose.yml" ] || die "$APP_DIR has no docker-compose.yml"
        say "repository: left as it is (--no-pull)"
        return 0
    fi
    if [ -d "$APP_DIR/.git" ]; then
        before="$(sha256_of "$SCRIPT_PATH")"
        GIT_SSH_COMMAND="$(git_ssh_command)" git -c "safe.directory=$APP_DIR" -C "$APP_DIR" fetch --prune origin "$BRANCH"
        git -c "safe.directory=$APP_DIR" -C "$APP_DIR" checkout --quiet "$BRANCH"
        git -c "safe.directory=$APP_DIR" -C "$APP_DIR" merge --ff-only --quiet "origin/$BRANCH"
        say "repository: $BRANCH at $(git -c "safe.directory=$APP_DIR" -C "$APP_DIR" rev-parse --short HEAD)"
        after="$(sha256_of "$SCRIPT_PATH")"
        if [ "$before" != "$after" ]; then
            SCRIPT_CHANGED="yes"
        fi
    elif [ -n "$REPO" ]; then
        GIT_SSH_COMMAND="$(git_ssh_command)" git clone --quiet --branch "$BRANCH" "$REPO" "$APP_DIR"
        say "repository: cloned $BRANCH"
    else
        die "no checkout at $APP_DIR; clone the repository there or give --repo URL"
    fi
    [ -f "$APP_DIR/docker-compose.yml" ] || die "$APP_DIR has no docker-compose.yml"
}

sha256_of() {
    if [ ! -f "$1" ]; then
        printf 'none'
        return 0
    fi
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    else
        shasum -a 256 "$1" | cut -d' ' -f1
    fi
}

# --- the settings ----------------------------------------------------------------------------

private_ipv4() {
    case "$1" in
        10.* | 192.168.* | 127.* | 169.254.* | 172.1[6-9].* | 172.2[0-9].* | 172.3[0-1].* | 100.6[4-9].* | 100.[7-9][0-9].* | 100.1[0-1][0-9].* | 100.12[0-7].*) return 0 ;;
    esac
    return 1
}

public_ipv4() {
    local address
    address="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") {print $(i + 1); exit}}')"
    if [ -z "$address" ] || private_ipv4 "$address"; then
        address="$(curl -4 -fsS --max-time 8 https://api.ipify.org 2>/dev/null || true)"
    fi
    case "$address" in
        '' | *[!0-9.]*) die "could not find this VM's public IPv4 address; give --site HOST" ;;
    esac
    printf '%s' "$address"
}

choose_site() {
    local existing
    existing="$(env_get SITE_ADDRESS "$ENV_FILE")"
    if [ -n "$SITE" ]; then
        CHOSEN_SITE="$SITE"
    elif [ -n "$existing" ]; then
        CHOSEN_SITE="$existing"
    else
        local ip
        ip="$(public_ipv4)"
        CHOSEN_SITE="${ip//./-}.sslip.io"
    fi
    case "$CHOSEN_SITE" in
        *[!A-Za-z0-9.-]* | '' | .* | *..*) die "the site name must be a plain host name" ;;
    esac
}

choose_email() {
    local existing
    existing="$(env_get ACME_EMAIL "$ENV_FILE")"
    if [ -n "$EMAIL" ]; then
        CHOSEN_EMAIL="$EMAIL"
    elif [ -n "$existing" ]; then
        CHOSEN_EMAIL="$existing"
    else
        die "give --email ADDRESS (Let's Encrypt sends certificate notices there)"
    fi
    case "$CHOSEN_EMAIL" in
        *[!A-Za-z0-9._%+@-]* | *@*@* | @* | *@) die "that email address is not usable" ;;
        *@*.*) ;;
        *) die "that email address is not usable" ;;
    esac
}

choose_access_code() {
    local existing
    existing="$(env_get DEMO_ACCESS_CODE "$ENV_FILE")"
    CODE_IS_NEW="no"
    if [ -n "$existing" ] && [ "$ROTATE_CODE" = "no" ]; then
        CHOSEN_CODE="$existing"
        return 0
    fi
    CODE_IS_NEW="yes"
    if [ -n "$CODE_FILE" ]; then
        require_private_file "$CODE_FILE" "access code file"
        first_line "$CODE_FILE"
        CHOSEN_CODE="$REPLY_VALUE"
    elif [ "$GENERATE_CODE" = "yes" ]; then
        # Shown once, on the terminal only: never in a log, a file or a command line.
        { : >/dev/tty; } 2>/dev/null || die "--generate-code shows the code once on the terminal; run this in a terminal"
        random_code
        CHOSEN_CODE="$GENERATED_CODE"
        SHOW_CODE="yes"
    elif ask_hidden "Access code for the demo (12 or more characters, hidden): "; then
        CHOSEN_CODE="$ASKED_VALUE"
    else
        die "no access code: use --generate-code, --access-code-file FILE, or run in a terminal"
    fi
    valid_code "$CHOSEN_CODE" || die "the access code must be ${MIN_CODE_LENGTH}-128 characters from A-Z a-z 0-9 _ . ~ + = : @ % -"
}

show_generated_code() {
    [ "${SHOW_CODE:-no}" = "yes" ] || return 0
    {
        printf '\n'
        printf '  The access code for the demo (shown once, here only):\n\n'
        printf '      %s\n\n' "$CHOSEN_CODE"
        printf '  It is saved in %s (root only). Copy it now.\n\n' "$ENV_FILE"
    } >/dev/tty
}

choose_runtime_request() {
    case "$RUNTIME" in
        gvisor) REQUESTED_RUNTIME="runsc" ;;
        runc) REQUESTED_RUNTIME="runc" ;;
        *) die "--runtime must be gvisor or runc" ;;
    esac
}

store_llm_key() {
    local key_file="$ETC_DIR/llm-api-key" temp key=""
    if [ "$SCRIPTED" = "yes" ]; then
        # A rehearsal reads no key; compose still wants the secret file to exist.
        if [ ! -f "$key_file" ]; then
            temp="$(mktemp_content)"
            printf '%s\n' "scripted-rehearsal-no-key" >"$temp"
            write_atomically "$key_file" 600 "$API_UID:$API_GID" "$temp"
            rm -f "$temp"
        fi
        return 0
    fi
    if [ -f "$key_file" ] && [ "$REPLACE_KEY" = "no" ] && [ -z "$KEY_FILE" ]; then
        first_line "$key_file"
        [ "$REPLY_VALUE" != "scripted-rehearsal-no-key" ] || die "the saved key file is the scripted placeholder; give --llm-key-file FILE or use --replace-key"
        return 0
    fi
    if [ -n "$KEY_FILE" ]; then
        require_private_file "$KEY_FILE" "key file"
        first_line "$KEY_FILE"
        key="$REPLY_VALUE"
    elif ask_hidden "Vultr inference API key (hidden): "; then
        key="$ASKED_VALUE"
    else
        die "no model key: use --llm-key-file FILE, run in a terminal, or --scripted for a rehearsal"
    fi
    valid_key "$key" || die "that does not look like an API key (8-512 characters, no spaces or quotes)"
    if [ -f "$key_file" ]; then
        first_line "$key_file"
        if [ "$REPLY_VALUE" = "$key" ] && [ "$(file_mode "$key_file")" = "600" ]; then
            return 0 # the same key again: nothing to change
        fi
    fi
    temp="$(mktemp_content)"
    printf '%s\n' "$key" >"$temp"
    write_atomically "$key_file" 600 "$API_UID:$API_GID" "$temp"
    rm -f "$temp"
}

# Every temporary file lives next to its destination (same filesystem, so mv is atomic), starts
# private, and is removed by cleanup if the script stops half way.
mktemp_content() {
    (umask 077 && mktemp "$ETC_DIR/.tmp.XXXXXX")
}

# Called by the EXIT trap, which shellcheck cannot see.
# shellcheck disable=SC2317
cleanup() {
    if [ -n "${ETC_DIR:-}" ]; then
        rm -f "$ETC_DIR"/.tmp.* 2>/dev/null || true
    fi
}

# The value for each setting: what the file already holds wins for the settings an operator may
# edit (limits, model); the machine and this run's choices always win.
keep_or() {
    local existing
    existing="$(env_get "$1" "$ENV_FILE")"
    if [ -n "$existing" ]; then
        printf '%s' "$existing"
    else
        printf '%s' "$2"
    fi
}

write_env_file() {
    local content model provider docker_gid
    content="$(mktemp_content)"
    if [ "$SCRIPTED" = "yes" ]; then
        provider="scripted"
    else
        provider="vultr"
    fi
    if [ -n "$MODEL" ]; then
        model="$MODEL"
    else
        model="$(keep_or LLM_MODEL "$DEFAULT_MODEL")"
    fi
    docker_gid="$(docker_group_id)"
    {
        printf '%s\n' "# Written by deploy/deploy.sh. Root only (mode 600). Edit a value, then run the script again."
        printf 'SITE_ADDRESS=%s\n' "$CHOSEN_SITE"
        printf 'ACME_EMAIL=%s\n' "$CHOSEN_EMAIL"
        printf 'DEMO_ACCESS_CODE=%s\n' "$CHOSEN_CODE"
        printf 'DEMO_PAUSED=%s\n' "$(keep_or DEMO_PAUSED false)"
        printf 'DEMO_MAX_CONCURRENT_RUNS=%s\n' "$(keep_or DEMO_MAX_CONCURRENT_RUNS 2)"
        printf 'DEMO_RUNS_PER_IP_PER_HOUR=%s\n' "$(keep_or DEMO_RUNS_PER_IP_PER_HOUR 3)"
        printf 'DEMO_RUNS_PER_DAY=%s\n' "$(keep_or DEMO_RUNS_PER_DAY 40)"
        printf 'DEMO_RUN_TOKEN_CAP=%s\n' "$(keep_or DEMO_RUN_TOKEN_CAP 60000)"
        printf 'DEMO_DAY_TOKEN_CAP=%s\n' "$(keep_or DEMO_DAY_TOKEN_CAP 2000000)"
        printf 'LLM_PROVIDER=%s\n' "$provider"
        printf 'LLM_MODEL=%s\n' "$model"
        printf 'LLM_RETRIES=%s\n' "$(keep_or LLM_RETRIES 2)"
        printf 'SANDCAGE_LLM_KEY_FILE=%s\n' "$ETC_DIR/llm-api-key"
        printf 'SANDBOX_RUNTIME=%s\n' "$FINAL_RUNTIME"
        printf 'DOCKER_GID=%s\n' "$docker_gid"
        printf 'DOCKER_SOCKET_PATH=%s\n' "$SOCKET_PATH"
        printf 'SANDCAGE_WORK_DIR=%s\n' "$WORK_DIR"
        printf 'SANDCAGE_DATA_DIR=%s\n' "$DATA_DIR"
        printf 'DEPLOY_KEY_FILE=%s\n' "$DEPLOY_KEY"
    } >"$content"
    if [ -f "$ENV_FILE" ] && cmp -s "$content" "$ENV_FILE" && [ "$(file_mode "$ENV_FILE")" = "600" ]; then
        rm -f "$content"
        say "settings: $ENV_FILE unchanged"
        return 0
    fi
    write_atomically "$ENV_FILE" 600 "" "$content"
    rm -f "$content"
    say "settings: wrote $ENV_FILE (mode 600)"
}

docker_group_id() {
    local gid
    gid="$(stat -c '%g' "$SOCKET_PATH" 2>/dev/null || stat -f '%g' "$SOCKET_PATH" 2>/dev/null || true)"
    case "$gid" in
        '' | *[!0-9]*) die "could not read the group of $SOCKET_PATH" ;;
    esac
    printf '%s' "$gid"
}

make_directories() {
    local directory
    mkdir -p "$ETC_DIR"
    chmod 700 "$ETC_DIR"
    for directory in "$WORK_DIR" "$DATA_DIR"; do
        mkdir -p "$directory"
        chmod 700 "$directory"
        chown "$API_UID:$API_GID" "$directory"
    done
}

# --- the sandbox image and the runtime --------------------------------------------------------

build_sandbox_image() {
    docker build --quiet -t "$SANDBOX_IMAGE" "$APP_DIR/src/sandcage/sandbox/image" >/dev/null
    say "sandbox image: $SANDBOX_IMAGE built on the host"
}

install_gvisor() {
    command -v runsc >/dev/null 2>&1 && {
        say "gVisor: runsc is already installed"
        return 0
    }
    say "gVisor: installing runsc from gVisor's official repository"
    local keyring_dir="${SANDCAGE_KEYRING_DIR:-/usr/share/keyrings}"
    local sources_dir="${SANDCAGE_APT_SOURCES_DIR:-/etc/apt/sources.list.d}"
    curl -fsSL https://gvisor.dev/archive.key | gpg --dearmor -o "$keyring_dir/gvisor-archive-keyring.gpg"
    printf 'deb [arch=%s signed-by=%s/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main\n' \
        "$(dpkg --print-architecture)" "$keyring_dir" >"$sources_dir/gvisor.list"
    apt-get update -qq
    apt-get install -y -qq runsc
    runsc install
    systemctl restart docker
}

gvisor_banner_present() {
    local output
    output="$(docker run --rm --runtime=runsc --network none --read-only "$SANDBOX_IMAGE" dmesg 2>&1 || true)"
    case "$output" in
        *"Starting gVisor"*) return 0 ;;
    esac
    return 1
}

verify_runtime() {
    FINAL_RUNTIME="runc"
    if [ "$REQUESTED_RUNTIME" = "runc" ]; then
        warn "sandbox runtime: plain Docker (runc). Sandboxes are NOT inside gVisor; /readyz and the panel will say so."
        return 0
    fi
    if [ "$INSTALL_GVISOR" = "yes" ]; then
        install_gvisor
    fi
    if gvisor_banner_present; then
        FINAL_RUNTIME="runsc"
        say "sandbox runtime: gVisor verified (its kernel banner was seen)"
        return 0
    fi
    if [ "$ALLOW_RUNC" = "yes" ]; then
        warn "gVisor could not be verified: falling back to plain Docker (runc) because of --allow-runc-fallback."
        warn "Sandboxes will NOT be inside gVisor; /readyz and the panel will say so."
        return 0
    fi
    die "gVisor (runsc) could not be verified: no 'Starting gVisor' banner. Install it (RUNBOOK step e, or --install-gvisor), or accept plain Docker with --allow-runc-fallback"
}

# --- the stack -------------------------------------------------------------------------------

compose() {
    docker compose -p "$PROJECT" --env-file "$ENV_FILE" -f "$APP_DIR/docker-compose.yml" "$@"
}

start_stack() {
    (cd "$APP_DIR" && compose up -d --build --remove-orphans) >/dev/null
    say "stack: started (docker compose project $PROJECT)"
}

readyz_through_the_api() {
    compose exec -T api python -I -c "
import json, sys, urllib.request, urllib.error
try:
    body = urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=5).read()
except urllib.error.HTTPError as error:
    body = error.read()
data = json.loads(body)
print(json.dumps(data))
sys.exit(0 if data.get('ready') else 1)
" 2>/dev/null
}

wait_until_ready() {
    local attempt=1 answer=""
    while [ "$attempt" -le "$WAIT_ATTEMPTS" ]; do
        if answer="$(readyz_through_the_api)"; then
            READYZ="$answer"
            return 0
        fi
        READYZ="$answer"
        sleep "$WAIT_SECONDS"
        attempt=$((attempt + 1))
    done
    return 1
}

summarise_readyz() {
    # Print each check's name, state and detail: /readyz holds no secret by design.
    printf '%s' "$READYZ" | python3 -I -c "
import json, sys
try:
    data = json.load(sys.stdin)
except ValueError:
    print('  (no readable answer from /readyz)')
    raise SystemExit
for name, check in data.get('checks', {}).items():
    print('  %-9s %-4s %s' % (name, 'ok' if check.get('ok') else 'FAIL', check.get('detail', '')))
" 2>/dev/null || say "  (could not read the /readyz answer)"
}

runtime_label_from_readyz() {
    printf '%s' "$READYZ" | python3 -I -c "
import json, sys
try:
    print(json.load(sys.stdin)['checks']['runtime']['label'])
except (ValueError, KeyError):
    print('unknown')
" 2>/dev/null || printf 'unknown'
}

https_answers() {
    curl -fsS --max-time 10 --resolve "$CHOSEN_SITE:443:127.0.0.1" "https://$CHOSEN_SITE/api/healthz" >/dev/null 2>&1
}

print_summary() {
    local label
    label="$(runtime_label_from_readyz)"
    say ""
    say "Sandcage is up."
    say "  URL:      https://$CHOSEN_SITE/"
    say "  runtime:  $label (what /readyz reports; the panel shows the same)"
    if [ "$SCRIPTED" = "yes" ]; then
        warn "SCRIPTED MODELS: this is a rehearsal, not the real demo."
    fi
    if [ "$FINAL_RUNTIME" = "runc" ]; then
        warn "gVisor is NOT in use: sandboxes run in plain Docker."
    fi
    say "  readiness:"
    summarise_readyz
    if https_answers; then
        say "  https:    the certificate is valid and the site answers"
    else
        warn "https is not answering with a valid certificate yet (Caddy keeps trying; see RUNBOOK troubleshooting)"
    fi
    say ""
    if [ "${CODE_IS_NEW:-no}" = "yes" ] && [ "${SHOW_CODE:-no}" = "no" ]; then
        say "  access code: saved from the file/prompt you gave; it is in $ENV_FILE (root only)"
    else
        say "  access code: in $ENV_FILE (root only); it is never printed here"
    fi
    say "  logs:     docker compose -p $PROJECT --env-file $ENV_FILE -f $APP_DIR/docker-compose.yml logs -f"
    say "  check it: python3 scripts/verify_deploy.py https://$CHOSEN_SITE --code-file <file with the code>"
    if [ "${SCRIPT_CHANGED:-no}" = "yes" ]; then
        warn "deploy.sh itself changed in this update: run it once more to use the new version"
    fi
}

# --- actions ---------------------------------------------------------------------------------

do_up() {
    require_root
    check_os
    check_prerequisites
    mkdir -p "$ETC_DIR"
    chmod 700 "$ETC_DIR"
    update_repository
    choose_runtime_request
    choose_site
    choose_email
    choose_access_code
    store_llm_key
    make_directories
    build_sandbox_image
    verify_runtime
    write_env_file
    show_generated_code
    start_stack
    if ! wait_until_ready; then
        warn "the demo did not become ready in time; what /readyz said:"
        summarise_readyz >&2
        die "not ready: see the failing checks above and RUNBOOK troubleshooting"
    fi
    print_summary
}

remove_sandboxes() {
    local ids
    ids="$(docker ps -aq --filter "label=sandcage=sandbox" 2>/dev/null || true)"
    if [ -n "$ids" ]; then
        # shellcheck disable=SC2086  # container ids are plain words
        docker rm -f $ids >/dev/null
        say "removed leftover sandbox containers"
    fi
}

do_down() {
    require_root
    [ -f "$ENV_FILE" ] || die "no env file at $ENV_FILE: nothing was deployed here"
    [ -f "$APP_DIR/docker-compose.yml" ] || die "no checkout at $APP_DIR"
    compose down --remove-orphans >/dev/null
    remove_sandboxes
    say "stack stopped; data stays on disk ($DATA_DIR)"
}

safe_to_delete() {
    local path="$1"
    case "$path" in
        /*) ;;
        *) return 1 ;;
    esac
    [ ! -L "$path" ] || return 1
    # at least three path components: /var/lib/x, never / or /var
    [ "$(printf '%s' "$path" | tr -cd '/' | wc -c | tr -d ' ')" -ge 3 ]
}

do_destroy() {
    local phrase=""
    require_root
    say "This deletes the event log and learned policy ($DATA_DIR), the work directory"
    say "($WORK_DIR), Caddy's certificates and every leftover sandbox. Secrets in $ETC_DIR stay."
    printf 'Type "%s" to continue: ' "$DESTROY_PHRASE" >&2
    IFS= read -r phrase || true
    [ "$phrase" = "$DESTROY_PHRASE" ] || die "not confirmed: nothing was deleted"
    if [ -f "$ENV_FILE" ] && [ -f "$APP_DIR/docker-compose.yml" ]; then
        compose down --volumes --remove-orphans >/dev/null
    fi
    remove_sandboxes
    local directory
    for directory in "$WORK_DIR" "$DATA_DIR"; do
        safe_to_delete "$directory" || die "refusing to delete $directory"
        rm -rf "$directory"
    done
    say "data destroyed"
}

# --- arguments ---------------------------------------------------------------------------------

main() {
    local action="" script_dir
    SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"
    script_dir="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
    SCRIPT_PATH="$script_dir/$(basename "$SCRIPT_PATH")"

    SITE="" EMAIL="" CODE_FILE="" GENERATE_CODE="no" ROTATE_CODE="no" KEY_FILE="" REPLACE_KEY="no"
    MODEL="" RUNTIME="gvisor" ALLOW_RUNC="no" INSTALL_GVISOR="no" SCRIPTED="no" REPO=""
    BRANCH="main" DEPLOY_KEY="" PULL="yes" SCRIPT_CHANGED="no"

    while [ "$#" -gt 0 ]; do
        case "$1" in
            --up | --down | --destroy-data)
                [ -z "$action" ] || die "give exactly one action"
                action="$1"
                ;;
            --email) EMAIL="${2:?--email needs a value}"; shift ;;
            --site) SITE="${2:?--site needs a value}"; shift ;;
            --access-code-file) CODE_FILE="${2:?--access-code-file needs a file}"; shift ;;
            --generate-code) GENERATE_CODE="yes" ;;
            --rotate-code) ROTATE_CODE="yes" ;;
            --llm-key-file) KEY_FILE="${2:?--llm-key-file needs a file}"; shift ;;
            --replace-key) REPLACE_KEY="yes" ;;
            --model) MODEL="${2:?--model needs a value}"; shift ;;
            --runtime) RUNTIME="${2:?--runtime needs a value}"; shift ;;
            --allow-runc-fallback) ALLOW_RUNC="yes" ;;
            --install-gvisor) INSTALL_GVISOR="yes" ;;
            --scripted) SCRIPTED="yes" ;;
            --repo) REPO="${2:?--repo needs a URL}"; shift ;;
            --branch) BRANCH="${2:?--branch needs a name}"; shift ;;
            --deploy-key) DEPLOY_KEY="${2:?--deploy-key needs a file}"; shift ;;
            --no-pull) PULL="no" ;;
            -h | --help)
                usage
                return 0
                ;;
            *)
                usage >&2
                die "unknown option: $1"
                ;;
        esac
        shift
    done
    if [ -z "$action" ]; then
        usage >&2
        return 2
    fi

    ETC_DIR="${SANDCAGE_ETC_DIR:-/etc/sandcage}"
    STATE_DIR="${SANDCAGE_STATE_DIR:-/var/lib/sandcage}"
    if [ -n "${SANDCAGE_APP_DIR:-}" ]; then
        APP_DIR="$SANDCAGE_APP_DIR"
    elif [ -f "$script_dir/../docker-compose.yml" ]; then
        APP_DIR="$(cd "$script_dir/.." && pwd)"
    else
        APP_DIR="/opt/sandcage"
    fi
    ENV_FILE="$ETC_DIR/sandcage.env"
    trap cleanup EXIT
    WORK_DIR="$STATE_DIR/work"
    DATA_DIR="$STATE_DIR/data"
    SOCKET_PATH="${DOCKER_SOCKET_PATH:-/var/run/docker.sock}"
    if [ -n "$DEPLOY_KEY" ]; then
        require_private_file "$DEPLOY_KEY" "deploy key"
    else
        DEPLOY_KEY="$(env_get DEPLOY_KEY_FILE "$ENV_FILE")"
    fi

    case "$action" in
        --up) do_up ;;
        --down) do_down ;;
        --destroy-data) do_destroy ;;
    esac
}

main "$@"; exit
