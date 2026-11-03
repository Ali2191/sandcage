# Sandcage

A containment-first harness for AI coding agents: the agent works inside a throwaway
sandbox, every action passes one policy gate, and the only output is a patch a human reads.

> Built during the Vultr Agent Rush (Nov 3–8, 2026). Design and planning were prepared before the event.

## Status

Work in progress. Built so far: secret masking and the policy engine (rulebook, command
parser, taint tracking, diff guard).

## Development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11.

```sh
uv sync --locked   # create .venv with the pinned tools
make check         # lint, format check, strict types, tests, tracked-env-file check
```

## License

MIT, see [LICENSE](LICENSE).
