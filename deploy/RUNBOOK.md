# Runbook: self-host Sandcage on a Linux VM

This runbook describes a **self-hosted deployment**: your own Sandcage instance on a VM you
create, control and pay for. There is no hosted public instance of Sandcage. It was written for,
and run on, a Vultr Cloud Compute VM with Ubuntu 24.04; any x86 Ubuntu 24.04 VM behind a cloud
firewall works the same way, only the console steps differ.

Follow it top to bottom. Every command is meant to be pasted as it is; the only things you
type yourself are the values in angle brackets, listed here once.

| You supply | Where it is used | How to get it |
| --- | --- | --- |
| `<VM_IP>` | step (a) onward | the instance page in the Vultr console, "IPv4 address" |
| `<YOUR_IP>` | step (b) | on your Mac: `curl -4 https://api.ipify.org` |
| `<YOUR_EMAIL>` | step (g) | any address you read (Let's Encrypt sends certificate notices there) |
| `<YOUR_DOMAIN_NAME>` | step (h), only if sslip.io fails | a host name you own, for example `demo.yourdomain.com` |

Three rules for the whole runbook:

- **The model provider's API key (Vultr inference by default) is typed only at the hidden prompt
  on the VM** (step g). Never paste it
  into a chat, a command line, a screenshot, a file in the repository or a shell history.
- **The access code is shown once** on the VM's terminal. Copy it into your password manager. Do
  not screenshot it and do not paste it into a chat.
- Stop and ask if any step prints something that is not described here. Do not improvise on the
  VM.

What the VM will run: Caddy (HTTPS, the only public service) in front of the panel and the API; the
API starts sandboxes only through a filtering Docker proxy (the only thing that touches the Docker
socket); sandboxes run under gVisor (`runsc`) when step (e) has been done and verified. `readyz`
and the panel state the runtime that is really in use, so a missing gVisor is visible, never hidden.

---

## (a) Create the VM

In the Vultr console: **Products → Compute → Deploy** (or "Deploy New Server").

1. **Type:** Cloud Compute, **x86 (Intel or AMD)**, not ARM.
2. **Plan:** the 2 vCPU / 8 GB class (`vx1-g-2c-8g`, about $0.06 per hour at the time of writing).
3. **Location:** the region closest to you and your users; the model calls go to the provider's
   API whichever you choose.
4. **Image:** Ubuntu **24.04 LTS** x64.
5. **SSH key:** add your Mac's public key and select it. If you have none yet, on the Mac:

   ```sh
   ls ~/.ssh/id_ed25519.pub || ssh-keygen -t ed25519 -C "sandcage-demo"
   cat ~/.ssh/id_ed25519.pub
   ```

   Paste that one line (the public key, never the file without `.pub`) into Vultr's "Add SSH Key".
   Do **not** enable any password login.
6. **Leave every extra off:** Auto Backups **off**, no extra block storage, no DDoS add-on, no
   managed database, no additional IPv4. IPv6 may stay off (IPv4 only is fine).
7. **Hostname / label:** `sandcage-demo`. Deploy.

Wait until the instance says "Running", then note the **IPv4 address** as `<VM_IP>`.

## (b) The Vultr firewall group (the outer wall)

**Products → Network → Firewall → Add Firewall Group**, name it `sandcage-demo`. Add exactly these
IPv4 rules (the group drops everything else):

| Protocol | Port | Source | Note |
| --- | --- | --- | --- |
| SSH | 22 | `<YOUR_IP>/32` | only your current address |
| HTTP | 80 | `0.0.0.0/0` | needed for certificate issuance and the redirect |
| HTTPS | 443 | `0.0.0.0/0` | the demo |

Then open the instance, **Settings → Firewall**, select `sandcage-demo`, save. Leave the group's
IPv6 rules empty.

**If your IP changes** (different network, phone hotspot, new router): open the firewall group,
edit the SSH rule's source to the new address (or add a second SSH rule), save. That is all: this
wall is the one that limits SSH by address, and changing it needs no login to the VM (the VM's own
`ufw` leaves port 22 open but rate-limited, see step c, for exactly this reason).

Docker note: ports that Docker publishes bypass `ufw`. That is why the compose file publishes only
80 and 443 (checked by a test), and why this Vultr group matters.

## (c) First login and hardening

From the Mac:

```sh
ssh root@<VM_IP>
```

Answer `yes` to the host-key question (first connect). On the VM, as root, create your own user,
with your key and sudo:

```sh
adduser --disabled-password --gecos "" deploy
usermod -aG sudo deploy
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
cp /root/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys
chmod 600 /home/deploy/.ssh/authorized_keys
echo 'deploy ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/90-deploy
chmod 440 /etc/sudoers.d/90-deploy
visudo -cf /etc/sudoers.d/90-deploy
```

`visudo` must print `parsed OK`. **Open a second terminal on the Mac and check the new user works
before going on:**

```sh
ssh deploy@<VM_IP> 'sudo -n true && echo sudo-ok'
```

It must print `sudo-ok`. Only then, back on the root session, make SSH key-only:

```sh
cat > /etc/ssh/sshd_config.d/00-sandcage.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
X11Forwarding no
MaxAuthTries 3
EOF
sshd -t && systemctl reload ssh
sshd -T | grep -E '^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin) '
```

The file is named `00-...` on purpose: sshd uses the first value it reads, and the cloud image's own
file (`50-cloud-init.conf`) may say `PasswordAuthentication yes`. The last command must print
`passwordauthentication no`, `kbdinteractiveauthentication no` and `permitrootlogin no`; if any says
`yes`, stop and fix it before going on.

Check again from the second terminal (`ssh deploy@<VM_IP> true`), then **close the root session and
use `deploy` from now on** (every command below starting with `sudo` runs as `deploy`).

Automatic security updates, the firewall on the VM, and the clock:

```sh
sudo apt-get update
sudo apt-get install -y unattended-upgrades ufw
printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' | sudo tee /etc/apt/apt.conf.d/20auto-upgrades >/dev/null

sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw limit 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable
sudo ufw status verbose

sudo timedatectl set-ntp true
timedatectl | grep -E "System clock synchronized|NTP service"
```

`ufw status` must list 22 (LIMIT), 80 and 443 only. `timedatectl` must say
`System clock synchronized: yes` (Let's Encrypt and the access-code window both need a true clock;
if it says `no`, wait a minute and run it again).

## (d) Docker and the compose plugin, from Docker's own repository

```sh
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin git
sudo docker run --rm hello-world
docker compose version
```

`hello-world` must print "Hello from Docker!". Do **not** add `deploy` to the `docker` group (that
group is root on the machine); the deploy script runs with `sudo`.

## (e) gVisor (`runsc`), from gVisor's official repository

```sh
sudo apt-get update
sudo apt-get install -y apt-transport-https ca-certificates curl gnupg
curl -fsSL https://gvisor.dev/archive.key | sudo gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" | sudo tee /etc/apt/sources.list.d/gvisor.list >/dev/null
sudo apt-get update
sudo apt-get install -y runsc
sudo runsc install
sudo systemctl restart docker
```

Verify that containers really run inside gVisor (the host kernel is 6.x; gVisor reports its own):

```sh
sudo docker run --rm --runtime=runsc alpine:3.20 sh -c 'uname -r; dmesg | head -3'
```

You must see gVisor's own kernel version (`4.19.0-gvisor` on the release this was run with; it is
not the VM's kernel) and a line containing `Starting gVisor`. If you see
the host's `6.x` kernel and no gVisor line, gVisor is **not** active: stop and see
"Troubleshooting 5". The deploy script runs the same banner check and refuses to continue without
it (unless you pass `--allow-runc-fallback`, which prints a warning and records plain Docker).

## (f) A read-only GitHub deploy key

The VM reads the repository with a key that can only read (this also works for a private fork; the
public repository can be cloned over HTTPS instead). On the VM:

```sh
sudo install -d -m 700 /etc/sandcage
sudo ssh-keygen -t ed25519 -N "" -C "sandcage-deploy-vm" -f /etc/sandcage/deploy_key
sudo cat /etc/sandcage/deploy_key.pub
```

Copy the printed **public** line. In GitHub: the repository **Ali2191/sandcage → Settings → Deploy
keys → Add deploy key**, title `sandcage-demo-vm`, paste the line, and **leave "Allow write access"
unticked**. Save.

Pin GitHub's host key (and compare it with GitHub's published fingerprint, listed at
<https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints>):

```sh
ssh-keyscan -t ed25519 github.com 2>/dev/null | sudo tee /etc/sandcage/known_hosts
ssh-keyscan -t ed25519 github.com 2>/dev/null | ssh-keygen -lf -
```

The second command prints `256 SHA256:... github.com (ED25519)`: that `SHA256:` value must equal
the ED25519 one on the GitHub page. If it differs, stop.

Clone the repository (as root, with that key and that pinned host key only):

```sh
sudo GIT_SSH_COMMAND="ssh -i /etc/sandcage/deploy_key -o IdentitiesOnly=yes -o UserKnownHostsFile=/etc/sandcage/known_hosts -o StrictHostKeyChecking=yes" \
  git clone git@github.com:Ali2191/sandcage.git /opt/sandcage
cd /opt/sandcage
git log -1 --oneline
```

## (g) Run the deploy script

```sh
cd /opt/sandcage
sudo ./deploy/deploy.sh --up \
  --email <YOUR_EMAIL> \
  --generate-code \
  --deploy-key /etc/sandcage/deploy_key
```

What happens, in order: prerequisites (Docker, compose plugin, git, curl, 10 GB free) → repository
update → the access code is generated → **a hidden prompt asks for the Vultr inference key: type
or paste it there; nothing is echoed** → directories (owned by uid 10001, mode 700) → the sandbox
image is built on the host → gVisor is verified by its banner → the settings are written
atomically to `/etc/sandcage/sandcage.env` (root only, mode 600) → the stack starts → the script
waits for `readyz`.

Before the stack starts, the terminal shows the access code **once**, in a box:

```
  The access code for the demo (shown once, here only):

      xxxxxxxxxxxxxxxxxxxx
```

Copy it into your password manager now, then clear the screen: `clear`. It is also stored in
`/etc/sandcage/sandcage.env`, readable only by root (`sudo cat` it if you ever lose it; never paste
it anywhere public).

The script ends with a summary: the URL, the runtime (it must read `gVisor (runsc)`), and the
readiness checks, all `ok`. It prints no secret. If it says `not ready`, read the failing check and
go to Troubleshooting. Running the same command again is safe and changes nothing (it never
replaces the code or the key unless you ask with `--rotate-code` or `--replace-key`).

Useful afterwards:

```sh
# logs of every service
sudo docker compose -p sandcage --env-file /etc/sandcage/sandcage.env -f /opt/sandcage/docker-compose.yml logs -f
# what is running
sudo docker compose -p sandcage --env-file /etc/sandcage/sandcage.env -f /opt/sandcage/docker-compose.yml ps
# update to the latest commit on main (safe to repeat)
cd /opt/sandcage && sudo ./deploy/deploy.sh --up --deploy-key /etc/sandcage/deploy_key
```

If the deploy script itself changed in an update it says so: run it once more.

## (h) The name

By default the site is `<VM_IP with dashes>.sslip.io`, for example `203-0-113-7.sslip.io` (sslip.io
answers with the IP in the name; nothing to configure). Caddy gets a real Let's Encrypt
certificate for it on the first request, usually within a minute.

Check from the Mac:

```sh
curl -sS -o /dev/null -w '%{http_code}\n' https://<VM_IP with dashes>.sslip.io/api/healthz
```

`200` means the certificate and the whole path work.

**Fallback if Let's Encrypt refuses sslip.io** (it is a shared domain and its certificate rate
limits are shared with everyone using it; the error shows in the Caddy log, see Troubleshooting 1):

1. At the registrar or DNS host of a domain you own, add an **A record**:
   `<YOUR_DOMAIN_NAME>` → `<VM_IP>` (if you use Cloudflare, set the record to **DNS only**, grey
   cloud: its proxy buffers event streams).
2. Wait until it resolves: `dig +short <YOUR_DOMAIN_NAME>` prints `<VM_IP>` (a few minutes).
3. On the VM:

   ```sh
   cd /opt/sandcage
   sudo ./deploy/deploy.sh --up --no-pull --site <YOUR_DOMAIN_NAME>
   ```

## (i) Check the deployment from the Mac

Save the access code in a private file on the Mac (hidden prompt, mode 600, never echoed):

```sh
umask 077 && read -rs "CODE?Access code (hidden): " && printf '%s\n' "$CODE" > ~/.sandcage-demo-code && unset CODE && echo
chmod 600 ~/.sandcage-demo-code
```

(That is `zsh`, the Mac's default. In bash use `read -rs -p "Access code: " CODE`.) Then, in your
copy of the repository:

```sh
cd ~/Projects/sandcage
python3 scripts/verify_deploy.py https://<VM_IP with dashes>.sslip.io --code-file ~/.sandcage-demo-code
```

It prints PASS / FAIL / WARN / UNTESTED for each check and exits non-zero if anything failed. What
to expect on a healthy deployment: everything `PASS`, plus:

- `kill switch honoured`: **UNTESTED** until you turn the switch on (below).
- `ssh reachability (info)`: **WARN** from your own allowed IP (that is expected); from another
  network it should `PASS` (see "Prove the firewall" below).

The check starts one scripted run (no model call, no tokens), then stops it. It counts as one of
the three runs per hour per client, so wait before testing more by hand.

**Prove the kill switch** (turns the demo off, then back on):

```sh
sudo sed -i 's/^DEMO_PAUSED=.*/DEMO_PAUSED=true/' /etc/sandcage/sandcage.env
cd /opt/sandcage && sudo ./deploy/deploy.sh --up --no-pull
# on the Mac: "kill switch honoured" must now say PASS; the three stream lines say UNTESTED
# (a paused demo cannot start the scripted run), which is expected while it is paused
python3 scripts/verify_deploy.py https://<VM_IP with dashes>.sslip.io --code-file ~/.sandcage-demo-code
# back on the VM: turn it off again
sudo sed -i 's/^DEMO_PAUSED=.*/DEMO_PAUSED=false/' /etc/sandcage/sandcage.env
cd /opt/sandcage && sudo ./deploy/deploy.sh --up --no-pull
```

**Prove the firewall** from a network that is not your allowed IP (tether your phone to the Mac,
then re-run `verify_deploy.py`): `ssh reachability` must be `PASS` ("does not answer"), and every
other line the same as before.

## (j) Checks by hand on a phone

On your phone, on **mobile data, not Wi-Fi** (so you test the real path), open
`https://<VM_IP with dashes>.sslip.io`. Enter the access code when asked. Then:

1. **A whole run.** Gallery → **Run it** on "The README asks for your .env". Approve the plan.
   Two blocked rows appear ("Tried to send your .env to collector.example.net" and "Tried to read
   your secrets in .env"), then the breaker ("Something in README.md is steering the agent, so
   Sandcage paused it."): tap **Resume**. The report says the agent tried to cross the line, was
   blocked 2 times, and the task still got done; **Download patch** works.
2. **Stop.** Start another run. At the plan question tap **Stop** (on a keyboard: Esc). The page
   says "You stopped this run. Nothing was delivered." and there is no patch.
3. **Release.** Run "Usage metrics that phone home". Approve the plan; at the red-team question
   read the findings and choose to release. The report says "Released by you", and the patch
   downloads. Repeat and choose not to release: the patch stays withheld.
4. **Reload.** Start a run and, at the plan question, reload the page. The same question comes
   back and can still be answered. Toggle airplane mode for a few seconds mid-run: the page
   reconnects and continues (the stream resumes after the last event it saw).
5. **Phone width.** On every page (Home, Gallery, Bench, Policy, Runs) nothing scrolls sideways.
6. **The runtime is stated.** The status line / run page names the sandbox runtime; it must say
   gVisor. If it says plain Docker, gVisor is not active: Troubleshooting 5.

Write every defect down exactly as it appears (a screenshot of the page is fine; never of the
access code).

## (k) Tear it all down, and what it costs

The VM costs about **$0.06 per hour** (about $1.44 a day, about $43 for 30 days). A stopped VM
is still billed: destroy it (below) as soon as you no longer need it.

On the VM, optional but tidy:

```sh
cd /opt/sandcage
sudo ./deploy/deploy.sh --down            # stops the stack, keeps the data
sudo ./deploy/deploy.sh --destroy-data    # asks you to type "destroy sandcage data"
sudo shred -u /etc/sandcage/llm-api-key /etc/sandcage/deploy_key 2>/dev/null; sudo rm -rf /etc/sandcage
```

Then in the consoles, in this order:

1. **GitHub:** repository → Settings → Deploy keys → delete `sandcage-demo-vm`.
2. **Vultr inference:** revoke the `sandcage-demo` key.
3. **Vultr instance:** Products → the instance → **Destroy** (Servers → Destroy). Also delete any
   snapshot you took, the firewall group `sandcage-demo`, and the SSH-key entry if you do not need it.
4. **Vultr Billing:** confirm there is no running resource and nothing is still being billed.
5. Your Mac: `rm ~/.sandcage-demo-code`.

## (l) Troubleshooting: the five most likely failures

All commands run on the VM. `COMPOSE` below is shorthand for
`sudo docker compose -p sandcage --env-file /etc/sandcage/sandcage.env -f /opt/sandcage/docker-compose.yml`.

### 1. The certificate is not issued (the browser says the connection is not private, or `curl` says `SSL`)

```sh
COMPOSE logs caddy --tail 80
dig +short <VM_IP with dashes>.sslip.io     # must print <VM_IP>
sudo ufw status                              # 80 and 443 must be ALLOW
```

Check that the Vultr firewall group allows 80 and 443 from anywhere and is attached to the instance.
The Caddy log lines that matter: `obtaining certificate`, `challenge failed`, `rateLimited`. A
`rateLimited` / `too many certificates` error means Let's Encrypt is refusing the shared sslip.io
domain: use the DNS fallback in step (h). Caddy retries by itself; after fixing the cause,
`COMPOSE restart caddy`.

### 2. 502 or "Bad Gateway" from Caddy

```sh
COMPOSE ps
COMPOSE logs api --tail 60
```

A 502 means the API is not answering. If `api` is `restarting` or `unhealthy`, the log's last line
names the cause: a setting (`DEMO_ACCESS_CODE ... at least 12 characters`, a malformed number, the
setting's name only, never its value), the model key file (`mode ... must be 600`, `no key file`),
or the event log already in use. Fix the setting in `/etc/sandcage/sandcage.env` (or rerun the
deploy script with the right option) and run `sudo ./deploy/deploy.sh --up --no-pull`.

### 3. Event streams are buffered (the live page sits on "connecting", or all events arrive at once at the end)

Run `python3 scripts/verify_deploy.py ...` from the Mac: the `event stream streams` line fails when
something buffers the stream. Causes, in order of likelihood: a proxy in front of Caddy (for
example Cloudflare with the orange cloud: switch the DNS record to DNS only); an edited
`deploy/Caddyfile` (the `/api` block must keep `flush_interval -1`, `compression off` and no
`encode`; `git diff deploy/Caddyfile` shows the edit; `git checkout deploy/Caddyfile` restores it);
a browser extension or corporate proxy on the client (test from the phone on mobile data).
After restoring the file: `COMPOSE up -d --force-recreate caddy`.

### 4. The Docker proxy refuses something (runs fail at "starting the sandbox")

```sh
COMPOSE logs dockerproxy --tail 60
```

Each refusal is logged with its reason. The usual ones: the sandbox image is missing (`readyz`
check `image` fails: `cd /opt/sandcage && sudo ./deploy/deploy.sh --up --no-pull` rebuilds it on
the host; the proxy never builds images); a bind mount outside the work directory (the work
directory must be the same path on the host and in the API container, `SANDCAGE_WORK_DIR` in
`/etc/sandcage/sandcage.env`, and it must be owned by uid 10001: `sudo ls -ldn /var/lib/sandcage/work`
should show `10001 10001`); permission denied on the socket (the proxy joins the socket's group:
`sudo stat -c %g /var/run/docker.sock` must equal `DOCKER_GID` in the env file). Never loosen the
proxy to "make it work": a refusal means a request outside what a hardened sandbox needs.

### 5. `runsc` is missing or gVisor is not active (readyz or the panel says plain Docker / runc)

```sh
command -v runsc && runsc --version
sudo docker info | grep -i -A3 runtimes        # must list runsc
cat /etc/docker/daemon.json                      # must contain a "runsc" runtime
sudo docker run --rm --runtime=runsc alpine:3.20 sh -c 'uname -r; dmesg | head -3'
```

If `runsc` is not found, redo step (e). If it is installed but not registered, run
`sudo runsc install && sudo systemctl restart docker`. After that, run
`cd /opt/sandcage && sudo ./deploy/deploy.sh --up --no-pull`; it re-verifies the banner and
records the result. The demo is allowed to run on plain Docker only if you decide so on purpose
(`--allow-runc-fallback`), and then the README and the video must say it is not gVisor.
