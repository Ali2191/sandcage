"""A fake VM for the deploy-script tests: a directory of fake commands (docker, id, chown, ip,
curl, df, ...) that record every call, plus scratch ``/etc/sandcage``, state and app
directories. The script under test is the real ``deploy/deploy.sh``, run with ``/bin/bash``
(on a Mac that is bash 3.2, which also checks that the script stays portable). Nothing here
touches the real system, and nothing here is a real secret."""

from __future__ import annotations

import hashlib
import os
import pty
import select
import stat
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from tests.deploy_helpers import DEPLOY_DIR

SCRIPT: Final = DEPLOY_DIR / "deploy.sh"
BASH: Final = "/bin/bash"

READY_JSON: Final = (
    '{"ready": true, "checks": {"store": {"ok": true, "detail": "the event log is writable"},'
    ' "runtime": {"ok": true, "detail": "probe", "active": "%(active)s", "label": "%(label)s",'
    ' "gvisor": %(gvisor)s}, "demo": {"ok": true, "detail": "public mode"}}}'
)

FAKE_DOCKER: Final = r"""#!/bin/bash
echo "docker $*" >> "$FAKE_LOG"
case "$1" in
  info)
    [ "${FAKE_DOCKER_DOWN:-0}" = 1 ] && { echo "Cannot connect to the Docker daemon" >&2; exit 1; }
    exit 0 ;;
  build) echo "sha256:0123456789abcdef"; exit 0 ;;
  run)
    case "$*" in
      *--runtime=runsc*)
        if [ "${FAKE_GVISOR:-1}" = 1 ]; then echo "[    0.000000] Starting gVisor..."; exit 0; fi
        echo "docker: Error response from daemon: unknown or invalid runtime name: runsc." >&2
        exit 125 ;;
    esac
    exit 0 ;;
  ps) printf '%s' "${FAKE_SANDBOXES:-}"; exit 0 ;;
  rm) exit 0 ;;
  compose)
    shift
    if [ "$1" = version ]; then
      [ "${FAKE_NO_COMPOSE:-0}" = 1 ] && exit 1
      echo "Docker Compose version v5.0.0"; exit 0
    fi
    case " $* " in
      *" exec "*)
        if [ "${FAKE_NOT_READY:-0}" = 1 ]; then
          echo '{"ready": false, "checks": {"image": {"ok": false,' \
               '"detail": "sandcage-sandbox:latest is missing: build it on the host"}}}'
          exit 1
        fi
        printf '%s\n' "$FAKE_READY_JSON"; exit 0 ;;
      *" up "*) echo "up" > "$FAKE_STATE/stack-up"; exit 0 ;;
      *" down"*) rm -f "$FAKE_STATE/stack-up"; exit 0 ;;
    esac
    exit 0 ;;
esac
exit 0
"""

FAKES: Final[dict[str, str]] = {
    "docker": FAKE_DOCKER,
    "id": """#!/bin/bash
if [ "$1" = -u ]; then echo "${FAKE_UID:-0}"; else exec /usr/bin/id "$@"; fi
""",
    "chown": '#!/bin/bash\necho "chown $*" >> "$FAKE_LOG"\n',
    "ip": (
        '#!/bin/bash\necho "1.1.1.1 via 10.0.0.1 dev eth0 src ${FAKE_IP_SRC:-203.0.113.7} uid 0"\n'
    ),
    "curl": """#!/bin/bash
echo "curl $*" >> "$FAKE_LOG"
case "$*" in
  *api.ipify.org*) echo "${FAKE_PUBLIC_IP:-198.51.100.9}"; exit 0 ;;
  *gvisor.dev/archive.key*) echo "FAKE-ARCHIVE-KEY"; exit 0 ;;
  *"/api/healthz"*) exit "${FAKE_HTTPS_RC:-0}" ;;
esac
exit 0
""",
    "df": """#!/bin/bash
echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
echo "fake 99999999 1 ${FAKE_FREE_KB:-99999999} 1% /"
""",
    "sleep": "#!/bin/bash\nexit 0\n",
    "runsc": '#!/bin/bash\necho "runsc $*" >> "$FAKE_LOG"\n',
    "apt-get": (
        '#!/bin/bash\necho "apt-get $*" >> "$FAKE_LOG"\n'
        'case "$*" in *install*runsc*)\n'
        '  printf \'#!/bin/bash\\necho "runsc $*" >> "$FAKE_LOG"\\n\' > "$FAKE_BIN/runsc"\n'
        '  chmod +x "$FAKE_BIN/runsc" ;;\n'
        "esac\n"
    ),
    "gpg": (
        '#!/bin/bash\necho "gpg $*" >> "$FAKE_LOG"\n'
        'while [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done\n'
        'cat > "$out"\n'
    ),
    "dpkg": "#!/bin/bash\necho amd64\n",
    "systemctl": '#!/bin/bash\necho "systemctl $*" >> "$FAKE_LOG"\n',
}

ACCESS_CODE: Final = "Fake-Access-Code-4242"
LLM_KEY: Final = "fake-llm-key-ABCDEFGH-0123456789"
NOT_SECRETS: Final = frozenset({"fake"})


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


@dataclass
class FakeHost:
    root: Path
    env_overrides: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.bin = self.root / "bin"
        self.etc = self.root / "etc-sandcage"
        self.state = self.root / "state" / "sandcage"
        self.app = self.root / "app"
        self.keyrings = self.root / "keyrings"
        self.apt_sources = self.root / "apt-sources"
        self.log = self.root / "calls.log"
        self.fake_state = self.root / "fake-state"
        self.socket = self.root / "docker.sock"
        self.os_release = self.root / "os-release"
        self.secrets = self.root / "secrets"
        for directory in (
            self.bin,
            self.keyrings,
            self.apt_sources,
            self.fake_state,
            self.secrets,
            self.state.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.log.write_text("", encoding="utf-8")
        self.socket.write_text("", encoding="utf-8")
        self.os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n', encoding="utf-8")
        for name, body in FAKES.items():
            path = self.bin / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)
        (self.app / "src" / "sandcage" / "sandbox" / "image").mkdir(parents=True)
        (self.app / "src" / "sandcage" / "sandbox" / "image" / "Dockerfile").write_text(
            "FROM scratch\n", encoding="utf-8"
        )
        (self.app / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

    # --- inputs -----------------------------------------------------------------------------

    def private_file(self, name: str, content: str, mode: int = 0o600) -> Path:
        path = self.secrets / name
        path.write_text(content + "\n", encoding="utf-8")
        path.chmod(mode)
        return path

    def code_file(self, code: str = ACCESS_CODE) -> Path:
        return self.private_file("code", code)

    def key_file(self, key: str = LLM_KEY) -> Path:
        return self.private_file("key", key)

    def system_path_without(self, command: str) -> str:
        """A PATH that has the fakes and every system command except ``command`` (a machine
        that never installed it, even if the one running the tests has it in /usr/bin)."""
        hidden = self.root / f"sysbin-without-{command}"
        hidden.mkdir(exist_ok=True)
        for directory in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
            for entry in Path(directory).iterdir():
                link = hidden / entry.name
                if entry.name != command and not link.exists():
                    link.symlink_to(entry)
        return f"{self.bin}:{hidden}"

    # --- running -----------------------------------------------------------------------------

    def environment(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(self.root),
            "LC_ALL": "C",
            # As under sudo: the person who ran it may own the secret files they hand over.
            "SUDO_UID": str(os.getuid()),
            "FAKE_LOG": str(self.log),
            "FAKE_BIN": str(self.bin),
            "FAKE_STATE": str(self.fake_state),
            "FAKE_READY_JSON": READY_JSON
            % {"active": "runsc", "label": "gVisor (runsc)", "gvisor": "true"},
            "SANDCAGE_APP_DIR": str(self.app),
            "SANDCAGE_ETC_DIR": str(self.etc),
            "SANDCAGE_STATE_DIR": str(self.state),
            "SANDCAGE_OS_RELEASE": str(self.os_release),
            "SANDCAGE_KEYRING_DIR": str(self.keyrings),
            "SANDCAGE_APT_SOURCES_DIR": str(self.apt_sources),
            "DOCKER_SOCKET_PATH": str(self.socket),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        }
        env.update(self.env_overrides)
        env.update(extra or {})
        return env

    def run(
        self,
        *args: str,
        stdin: str | None = None,
        env: dict[str, str] | None = None,
        script: Path = SCRIPT,
    ) -> Result:
        # A new session: no controlling terminal, so /dev/tty is unavailable like in a pipeline.
        done = subprocess.run(  # noqa: S603 - the script under test, with fixed arguments
            [BASH, str(script), *args],
            env=self.environment(env),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            start_new_session=True,
        )
        return Result(done.returncode, done.stdout, done.stderr)

    def run_in_terminal(
        self, *args: str, typed: list[str], env: dict[str, str] | None = None
    ) -> Result:
        """Run the script on a pseudo-terminal, typing each answer when a prompt appears.
        Returns everything the terminal showed (which includes anything echoed)."""
        pid, fd = pty.fork()
        if pid == 0:  # the child: become the script
            os.execve(BASH, [BASH, str(SCRIPT), *args], self.environment(env))  # noqa: S606
        shown = b""
        since_answer = b""
        answers = list(typed)
        deadline = time.monotonic() + 120
        status = 0
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.5)
            if ready:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                shown += chunk
                since_answer += chunk
                if answers and since_answer.rstrip().endswith(b":"):
                    os.write(fd, answers.pop(0).encode() + b"\n")
                    since_answer = b""
            else:
                finished, status = os.waitpid(pid, os.WNOHANG)
                if finished:
                    pid = 0
                    break
        if pid:
            _, status = os.waitpid(pid, 0)
        os.close(fd)
        return Result(os.waitstatus_to_exitcode(status), shown.decode(errors="replace"), "")

    # --- what happened -----------------------------------------------------------------------

    def calls(self) -> list[str]:
        return [line for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def calls_matching(self, text: str) -> list[str]:
        return [call for call in self.calls() if text in call]

    @property
    def env_file(self) -> Path:
        return self.etc / "sandcage.env"

    def env_values(self) -> dict[str, str]:
        values: dict[str, str] = {}
        for line in self.env_file.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                key, _, value = line.partition("=")
                values[key] = value
        return values

    def snapshot(self) -> dict[str, tuple[str, int, str]]:
        """Every path under the host's own directories: kind, mode, content hash."""
        found: dict[str, tuple[str, int, str]] = {}
        for base in (self.etc, self.state):
            if not base.exists():
                continue
            for path in sorted([base, *base.rglob("*")]):
                info = path.lstat()
                kind = "dir" if stat.S_ISDIR(info.st_mode) else "file"
                digest = hashlib.sha256(path.read_bytes()).hexdigest() if kind == "file" else ""
                found[str(path.relative_to(self.root))] = (kind, stat.S_IMODE(info.st_mode), digest)
        return found


def everything_secret(*secrets: str) -> list[str]:
    return [secret for secret in secrets if secret]
