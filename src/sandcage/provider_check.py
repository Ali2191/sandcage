"""The provider smoke test: how real models answer the action protocol.

Run only by hand through ``scripts/check_provider.py`` (never in CI, never by
``make check``). It lists the served models, then sends each candidate one tiny chat
completion with the real system prompt through the real :class:`OpenAIChatClient`, and
records whether the reply parsed as one JSON action (first try, after the client's
recovery, after one protocol retry, or not at all), whether usage was reported, the
latency and any quirk seen in the raw reply.

Safety rules, all enforced here:

- the key is read only from a file (mode 600, a regular file owned by us, never a link);
  it is never an argument, never printed and never logged; every printed line goes
  through :func:`sandcage.masking.mask` and has the key's literal text removed as well;
- one call per model plus at most one retry, ``max_tokens`` small, and a spend cap
  computed from a price table that is clearly third-party and unverified: a call whose
  worst case would cross the cap is not made;
- a 401, 403 or 429 aborts the whole check at once (no loop, no other path tried).
"""

from __future__ import annotations

import json
import os
import stat
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from sandcage.agent import prompts
from sandcage.agent.llm import (
    Completion,
    HttpReply,
    Message,
    ModelError,
    OpenAIChatClient,
    Transport,
    urllib_transport,
)
from sandcage.agent.protocol import ActionParseError, extract_json_object, parse_action
from sandcage.masking import mask

BASE_URL: Final = "https://api.vultrinference.com/v1"
DEFAULT_KEY_FILE: Final = "~/.sandcage/vultr-demo.key"
KEY_FILE_ENV: Final = "SANDCAGE_KEY_FILE"
CANDIDATES: Final = ("deepseek-v4.1-flash-1017", "glm-5.3", "mimo-v2.6-pro", "qwen3.8-27b")
MAX_TOKENS: Final = 1024
SPEND_CAP_USD: Final = 0.05
LIST_TIMEOUT: Final = 20.0
CHAT_TIMEOUT: Final = 60.0
ABORT_STATUSES: Final = frozenset({401, 403, 429})
TASK: Final = "List the files in the root of the workspace. This is the first step."
EXPECTED_TOOL: Final = "list_dir"

# UNVERIFIED THIRD-PARTY PRICES (USD per 1M tokens, input / output), copied from a public
# tracker on 4 Nov 2026 (VULTR CREDITS PLAN). The Vultr API returns no prices. These only
# bound the estimate; the Vultr console is the truth.
UNVERIFIED_PRICES: Final[dict[str, tuple[float, float]]] = {
    "deepseek-v4.1-flash-1017": (0.15, 0.60),
    "glm-5.3": (0.75, 3.00),
    "mimo-v2.6-pro": (0.40, 0.80),
    "qwen3.8-27b": (0.15, 1.00),
}


class ProviderCheckError(Exception):
    """The check must stop. The message is safe to print (masked, no key)."""


# --- the key -----------------------------------------------------------------------------


def key_path(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env.get(KEY_FILE_ENV, DEFAULT_KEY_FILE)).expanduser()


def read_key(path: Path) -> str:
    """The key from a regular file of mode 600 that we own, opened without following a
    link. Errors never contain the file's contents."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise ProviderCheckError(f"no key file at {path}") from None
    except OSError as error:
        raise ProviderCheckError(f"cannot open the key file ({type(error).__name__})") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ProviderCheckError("the key file is not a regular file")
        if info.st_uid != os.getuid():
            raise ProviderCheckError("the key file is not owned by this user")
        mode = stat.S_IMODE(info.st_mode)
        if mode != 0o600:
            raise ProviderCheckError(f"the key file's mode is {mode:o}; it must be 600")
        raw = os.read(fd, 4097)
    finally:
        os.close(fd)
    if len(raw) > 4096:
        raise ProviderCheckError("the key file is too large to be a key")
    try:
        key = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        raise ProviderCheckError("the key file is not plain ASCII text") from None
    if not key or any(char.isspace() for char in key):
        raise ProviderCheckError("the key file must hold exactly one key on one line")
    return key


@dataclass(frozen=True)
class Scrubber:
    """Masks a text and removes the key's literal value, whatever its format."""

    secret: str = field(repr=False)

    def __call__(self, text: str) -> str:
        if self.secret:
            text = text.replace(self.secret, "[MASKED:api_key]")
        return mask(text)


# --- listing models ------------------------------------------------------------------------

Getter = Callable[[str, dict[str, str], float], HttpReply]


def urllib_get(url: str, headers: dict[str, str], timeout: float) -> HttpReply:
    request = urllib.request.Request(url, headers=headers, method="GET")  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return HttpReply(response.status, response.read(2_000_001))
    except urllib.error.HTTPError as error:
        return HttpReply(error.code, error.read(2_000_001))


def list_models(get: Getter, base_url: str, key: str) -> list[str]:
    try:
        reply = get(
            base_url.rstrip("/") + "/models", {"Authorization": f"Bearer {key}"}, LIST_TIMEOUT
        )
    except (OSError, TimeoutError) as error:
        raise ProviderCheckError(f"listing models failed: {type(error).__name__}") from None
    if reply.status in ABORT_STATUSES:
        raise ProviderCheckError(f"listing models was refused (HTTP {reply.status}); stopping")
    if reply.status != 200:
        raise ProviderCheckError(f"listing models failed (HTTP {reply.status})")
    try:
        data = json.loads(reply.body)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        raise ProviderCheckError("the model list is not JSON") from None
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ProviderCheckError("the model list has no 'data' array")
    return sorted(
        item["id"] for item in items if isinstance(item, dict) and isinstance(item.get("id"), str)
    )


# --- one model -----------------------------------------------------------------------------


@dataclass
class Exchange:
    """What the transport saw for one HTTP request (never the body itself)."""

    status: int
    seconds: float
    quirks: list[str]


@dataclass
class ModelResult:
    model: str
    outcome: str = "not run"  # first try | after client recovery (...) | after retry | failed
    tool: str | None = None
    usage_reported: bool = False
    seconds: float = 0.0
    cost_usd: float = 0.0
    cost_measured: bool = False
    calls: int = 0
    quirks: list[str] = field(default_factory=list)
    note: str = ""


def reply_quirks(body: bytes) -> list[str]:
    """Shape notes about a raw chat reply: reasoning text, fences, native tool calls,
    prose around the JSON, a cut-off answer. Never returns any reply text."""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        return ["reply body is not JSON"]
    if not isinstance(data, dict):
        return ["reply is not an object"]
    error = data.get("error")
    if isinstance(error, dict):
        code = error.get("code") or error.get("type")
        return [f"error code {code}" if isinstance(code, str) and code.isascii() else "error"]
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ["no choices"]
    choice = choices[0]
    quirks: list[str] = []
    if choice.get("finish_reason") == "length":
        quirks.append("cut off at max_tokens")
    message = choice.get("message")
    if not isinstance(message, dict):
        return [*quirks, "no message"]
    for name in ("reasoning", "reasoning_content"):
        value = message.get(name)
        if isinstance(value, str) and value.strip():
            quirks.append(f"{name} field ({len(value)} chars)")
    if message.get("tool_calls"):
        quirks.append("native tool_calls")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        quirks.append("empty content")
        return quirks
    text = content.strip()
    if "<think>" in text or "</think>" in text:
        quirks.append("<think> block in content")
    if "```" in text:
        quirks.append("markdown fence")
    try:
        obj = extract_json_object(text)
    except ActionParseError:
        quirks.append("no JSON object in content")
        return quirks
    if text != json.dumps(obj) and not _is_exactly_one_object(text):
        quirks.append("extra text around the JSON")
    if "usage" not in data:
        quirks.append("no usage block")
    return quirks


def _is_exactly_one_object(text: str) -> bool:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, RecursionError):
        return False
    return isinstance(value, dict)


def recording(transport: Transport, log: list[Exchange], clock: Callable[[], float]) -> Transport:
    def send(url: str, body: bytes, headers: dict[str, str], timeout: float) -> HttpReply:
        start = clock()
        reply = transport(url, body, headers, timeout)
        log.append(Exchange(reply.status, clock() - start, reply_quirks(reply.body)))
        return reply

    return send


def messages_for_check() -> list[Message]:
    return [
        {"role": "system", "content": prompts.system_prompt(guard=True)},
        {"role": "user", "content": prompts.task_message(TASK)},
    ]


def worst_case_cost(model: str, messages: Sequence[Message], max_tokens: int) -> float:
    prices = UNVERIFIED_PRICES.get(model)
    if prices is None:
        raise ProviderCheckError(f"no price for {model}, so its cost cannot be bounded")
    prompt_tokens = sum(len(m["content"]) for m in messages) // 3 + 50  # generous: 3 chars/token
    return (prompt_tokens * prices[0] + max_tokens * prices[1]) / 1_000_000


@dataclass
class Budget:
    cap: float = SPEND_CAP_USD
    spent: float = 0.0

    def admit(self, worst: float) -> None:
        if self.spent + worst > self.cap:
            raise ProviderCheckError(
                f"the next call could cost up to ${worst:.4f}; with ${self.spent:.4f} spent "
                f"that would cross the ${self.cap:.2f} cap, so the check stops"
            )


def check_model(
    model: str,
    key: str,
    budget: Budget,
    *,
    transport: Transport = urllib_transport,
    base_url: str = BASE_URL,
    clock: Callable[[], float] = time.monotonic,
) -> ModelResult:
    result = ModelResult(model)
    log: list[Exchange] = []
    client = OpenAIChatClient(
        base_url=base_url,
        api_key=key,
        model=model,
        timeout=CHAT_TIMEOUT,
        retries=0,
        max_tokens=MAX_TOKENS,
        transport=recording(transport, log, clock),
        sleep=lambda _seconds: None,
    )
    messages = messages_for_check()
    for attempt in (0, 1):
        worst = worst_case_cost(model, messages, MAX_TOKENS)
        budget.admit(worst)
        result.calls += 1
        completion: Completion | None = None
        problem: str | None = None
        try:
            completion = client.complete(messages)
        except ModelError as error:
            problem = str(error)
        exchange = log[-1] if log else None
        if exchange is not None:
            result.seconds += exchange.seconds
            result.quirks.extend(q for q in exchange.quirks if q not in result.quirks)
            if exchange.status in ABORT_STATUSES:
                budget.spent += worst
                raise ProviderCheckError(
                    f"{model}: the provider refused the call (HTTP {exchange.status}); stopping"
                )
        cost = _cost(model, completion, worst)
        budget.spent += cost[0]
        result.cost_usd += cost[0]
        result.cost_measured = cost[1] if attempt == 0 else (result.cost_measured and cost[1])
        if completion is None:
            result.outcome = "failed"
            result.note = problem or "the call failed"
            return result
        result.usage_reported = completion.usage is not None
        try:
            action = parse_action(completion.text)
        except ActionParseError as error:
            if attempt == 1:
                result.outcome = "failed"
                result.note = f"invalid after one retry: {error}"
                return result
            messages = [
                *messages,
                {"role": "assistant", "content": mask(completion.text)[:2000]},
                {"role": "user", "content": prompts.invalid_reply_message(str(error))},
            ]
            continue
        result.tool = action.tool
        if completion.recovered is not None:
            result.outcome = f"after client recovery ({completion.recovered})"
        else:
            result.outcome = "first try" if attempt == 0 else "after one retry"
        if action.tool != EXPECTED_TOOL:
            result.note = f"valid action, but chose {action.tool} (expected {EXPECTED_TOOL})"
        return result
    return result  # pragma: no cover - the loop always returns


def _cost(model: str, completion: Completion | None, worst: float) -> tuple[float, bool]:
    """The call's cost from reported usage, or the worst case when usage is missing."""
    if completion is None or completion.usage is None:
        return worst, False
    prices = UNVERIFIED_PRICES[model]
    usage = completion.usage
    return (usage.prompt_tokens * prices[0] + usage.completion_tokens * prices[1]) / 1_000_000, True


# --- the whole check -------------------------------------------------------------------------


def table(results: Sequence[ModelResult]) -> str:
    lines = [
        "| Model | Valid action | Tool | Usage reported | Latency | Calls | Est. cost | Quirks |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        quirks = "; ".join(r.quirks) or "none"
        if r.note:
            quirks = f"{quirks}. {r.note}"
        cost = f"${r.cost_usd:.5f}" + ("" if r.cost_measured else " (worst case)")
        lines.append(
            f"| {r.model} | {r.outcome} | {r.tool or '-'} | {'yes' if r.usage_reported else 'no'}"
            f" | {r.seconds:.1f} s | {r.calls} | {cost} | {quirks} |"
        )
    return "\n".join(lines)


def run_check(
    out: Callable[[str], None],
    *,
    environ: dict[str, str] | None = None,
    get: Getter = urllib_get,
    transport: Transport = urllib_transport,
    candidates: Sequence[str] = CANDIDATES,
) -> int:
    """The whole check; returns the exit code. ``out`` receives only scrubbed text."""
    scrub = Scrubber("")
    try:
        key = read_key(key_path(environ))
        scrub = Scrubber(key)
        served = list_models(get, BASE_URL, key)
        out(scrub(f"Served models ({len(served)}): {', '.join(served)}"))
        budget = Budget()
        results: list[ModelResult] = []
        try:
            for model in candidates:
                if model not in served:
                    results.append(ModelResult(model, outcome="not served"))
                    continue
                results.append(check_model(model, key, budget, transport=transport))
        finally:
            if results:
                out(scrub(table(results)))
            out(
                scrub(
                    f"Estimated spend: ${budget.spent:.5f} (cap ${budget.cap:.2f}, "
                    "unverified third-party prices)"
                )
            )
    except ProviderCheckError as error:
        out(scrub(f"provider check stopped: {error}"))
        return 1
    return 0
