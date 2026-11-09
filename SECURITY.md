# Security policy

## Reporting a vulnerability

Use GitHub private vulnerability reporting: open the **Security** tab of this repository and
choose **Report a vulnerability**. Please do not open a public issue for a vulnerability.

A good report has:

- what you found and what an attacker gains (for example: a command that runs outside the gate, a secret that reaches a model or the network, a way past the access code);
- the steps to reproduce it, with the commit you tested and whether gVisor or plain runc was in use;
- a minimal proof of concept, if you have one;
- your suggested fix, if you have one.

Sandcage is maintained by one person. Replies are best effort, and there is no fixed response time.

## Supported versions

Only `main`. There are no releases or maintained branches.

## Scope

In scope:

- **The gate and rulebook:** a way to run an action the policy, the plan or the diff guard should have stopped, or to avoid the circuit breaker.
- **The sandbox:** an escape from the container, a way to give it a network, a secret or an environment variable, or a way to write outside the workspace copy.
- **The Docker socket proxy:** any request that gets past its allowlist or label checks.
- **Authentication:** the access code, its lockout, and the checks on every request that changes state.
- **SSRF protection:** the fetcher that reads web pages for the quarantined reader.
- **Key handling:** the model API key reaching a log, an error, a response, a sandbox, or a host it was not meant for.
- **Masking:** a secret that reaches a model input, an event, a patch or a report unmasked.

Out of scope:

- Denial of service by traffic volume against an exposed instance (see the known limitation on rate limiting).
- Anything that needs a Docker socket you gave the API yourself, or a host you already control.
- The quality of a real model's answers. Sandcage does not claim to make a model honest.
- The mock data in the tracked panel screenshots.
- Vulnerabilities in third-party software (Docker, gVisor, Caddy, Next.js) that are not made worse by how Sandcage uses them. Report those upstream.

## Threat model

Sandcage assumes the agent can be steered. Repo files, tool output, fetched pages and model
output are all untrusted. It defends against that in layers:

- the planner never sees raw file contents, and flagged files are shown as a path only;
- every action passes one gate: the rulebook intersected with the plan, a diff guard on content, taint tracking, budgets, loop detection and a circuit breaker;
- commands run in a container with no network, a read-only root, a non-root user, no capabilities and resource limits;
- secrets are never given to the sandbox and are masked everywhere text goes to a model or a person;
- the API has no Docker socket, only a filtering proxy has;
- the only output is a patch, and the original repo is never written;
- a report shows whether private data, untrusted content and a way out ever existed together.

It does not defend against:

- a compromised host, Docker daemon or kernel. Without gVisor the sandbox boundary is the host kernel (Macs, CI, `--allow-runc-fallback`);
- injected instructions in channels the Bench does not cover (fetched pages, package scripts, installing a package are untested), even though the design intends to handle them;
- a person who approves a harmful plan or patch. Sandcage shows what changed. It does not replace reading it;
- a malicious or compromised model provider, beyond keeping the key from leaving for any other host;
- anyone who can read the event log or the access code on the machine that runs it.

## Known limitations

Medium:

- **Reading is open on a public instance.** Without the access code anyone can read the run list, tasks, events, reports and patches. By design only demo repos and Bench scenarios can run.
- **No rate limiting at the edge.** Only in-app limits exist (per client per hour and day, concurrency, token caps, wrong-code lockout) plus Caddy's header, body and idle timeouts. Put your own limiter in front of a public instance.
- **The Docker proxy holds the real socket.** A bug in it would be host root. The allowlist, label checks, a property test over random requests and Docker-backed tests reduce that risk. They do not remove it.
- **Real-provider gallery runs spend real tokens.** They are bounded by per-run and daily caps.

Low:

- **The API keeps the model key in its own environment** for its whole life. No child process exists today, but a future one would inherit it.
- **The CSP allows inline scripts and styles,** because Next.js inlines its bootstrap code.
- **The access code is stored in the browser's localStorage.** A cross-site scripting bug would expose it. The CSP limits that.
- **Without gVisor the sandbox boundary is the host kernel.** The panel and `/readyz` say which runtime is active.
- **The standard-library HTTP server relies on Caddy** for slow-client protection when exposed. `make api` binds 127.0.0.1 only.
- **The tracked panel screenshots `frontend/screens/bench-real.png` and `gallery-real.png` use mock data,** including invented Bench numbers. The page labels them as test data, not a result.
