"""The chat client: every model role needs only ``complete(messages) -> Completion``.

:class:`OpenAIChatClient` talks to any OpenAI-compatible chat endpoint (Vultr Serverless
Inference, Groq) with the standard library: one timeout per request, a bounded number of
retries that honour ``Retry-After`` (capped), temperature 0. Usage is reported only when
the provider reported it; callers must not present a missing count as measured.

Two recoveries for reasoning models (History/Bugs #5), both returning plain text that
still goes through the action parser and the policy:

- empty ``content`` with the action left in the reasoning text → that action;
- a 400 ``tool_use_failed`` error carrying the model's native function-call attempt in
  ``failed_generation`` → the same call rewritten as our JSON action.

Error messages never include the API key, the request body or the provider's raw reply.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal, Protocol, TypedDict

from pydantic import JsonValue

from sandcage.agent.protocol import TOOLS, last_action_object
from sandcage.masking import mask

DEFAULT_TIMEOUT: Final = 60.0
DEFAULT_RETRIES: Final = 8
MAX_RETRY_WAIT: Final = 30.0
MAX_COMPLETION_TOKENS: Final = 3000
MAX_RESPONSE_BYTES: Final = 2_000_000
_RETRY_STATUSES: Final = frozenset({408, 409, 429, 500, 502, 503, 504})


class Message(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class Completion:
    text: str
    usage: Usage | None = None
    recovered: str | None = None  # "reasoning" or "tool_call" when a recovery was used


class ChatClient(Protocol):
    def complete(self, messages: Sequence[Message]) -> Completion: ...


class ModelError(Exception):
    """The model call failed. The message is safe to show (masked, no key, no body)."""


# --- recoveries (pure) -------------------------------------------------------------------


def action_from_reasoning(reasoning: str) -> str | None:
    """The last action object in a reasoning text, as JSON text."""
    found = last_action_object(reasoning)
    return json.dumps(found) if found is not None else None


def action_from_tool_call(failed_generation: str) -> str | None:
    """Rewrite a native function-call attempt as our JSON action, or None.

    Handles a single call or a list of calls (the first is used), names with a prefix
    such as ``functions.``, arguments as an object or a JSON string, and a whole action
    passed as the arguments.
    """
    try:
        data: JsonValue = json.loads(failed_generation)
    except (json.JSONDecodeError, RecursionError):
        found = last_action_object(failed_generation)
        return json.dumps(found) if found is not None else None
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("function"), dict):
        data = data["function"]
        if not isinstance(data, dict):
            return None
    name = data.get("name", data.get("tool"))
    arguments: JsonValue = data.get("arguments", data.get("parameters", data.get("args", {})))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except (json.JSONDecodeError, RecursionError):
            return None
    if not isinstance(arguments, dict):
        return None
    if isinstance(arguments.get("tool"), str):  # the whole action came as the arguments
        return json.dumps(arguments)
    if not isinstance(name, str):
        return None
    tool = name.rsplit(".", 1)[-1].strip()
    if tool not in TOOLS:
        return None
    reason = arguments.pop("reason", "") if isinstance(arguments.get("reason"), str) else ""
    return json.dumps({"tool": tool, "args": arguments, "reason": reason})


# --- the HTTP client ---------------------------------------------------------------------


@dataclass(frozen=True)
class HttpReply:
    status: int
    body: bytes
    retry_after: float | None = None


Transport = Callable[[str, bytes, dict[str, str], float], HttpReply]
Sleeper = Callable[[float], None]


def urllib_transport(url: str, body: bytes, headers: dict[str, str], timeout: float) -> HttpReply:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return HttpReply(response.status, response.read(MAX_RESPONSE_BYTES + 1))
    except urllib.error.HTTPError as error:
        retry = error.headers.get("Retry-After") if error.headers else None
        return HttpReply(error.code, error.read(MAX_RESPONSE_BYTES + 1), _seconds(retry))


def _seconds(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


@dataclass
class OpenAIChatClient:
    base_url: str
    api_key: str = field(repr=False)
    model: str
    timeout: float = DEFAULT_TIMEOUT
    retries: int = DEFAULT_RETRIES
    max_tokens: int = MAX_COMPLETION_TOKENS
    transport: Transport = urllib_transport
    sleep: Sleeper = time.sleep

    def complete(self, messages: Sequence[Message]) -> Completion:
        if not self.base_url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise ModelError("the model endpoint must use https")
        url = self.base_url.rstrip("/") + "/chat/completions"
        body = json.dumps(
            {
                "model": self.model,
                "messages": list(messages),
                "temperature": 0,
                "max_tokens": self.max_tokens,
            }
        ).encode()
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        for attempt in range(self.retries + 1):
            try:
                reply = self.transport(url, body, headers, self.timeout)
            except (OSError, TimeoutError) as problem:
                if attempt == self.retries:
                    raise ModelError(
                        f"the model endpoint did not answer: {type(problem).__name__}"
                    ) from None
                self.sleep(min(2.0**attempt, MAX_RETRY_WAIT))
                continue
            if reply.status in _RETRY_STATUSES and attempt < self.retries:
                wait = reply.retry_after if reply.retry_after is not None else 2.0**attempt
                self.sleep(min(max(wait, 0.0), MAX_RETRY_WAIT))
                continue
            return self._read(reply)
        raise ModelError("the model endpoint kept failing")  # pragma: no cover - loop returns

    def _read(self, reply: HttpReply) -> Completion:
        if len(reply.body) > MAX_RESPONSE_BYTES:
            raise ModelError("the model's reply was too large")
        try:
            data = json.loads(reply.body)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            raise ModelError(
                f"the model endpoint sent a non-JSON reply (HTTP {reply.status})"
            ) from None
        if reply.status == 400:
            recovered = _recover_tool_use_failed(data)
            if recovered is not None:
                return Completion(recovered, None, recovered="tool_call")
        if reply.status != 200:
            raise ModelError(f"the model call failed (HTTP {reply.status}){_error_hint(data)}")
        return _completion(data)


def _completion(data: object) -> Completion:
    if not isinstance(data, dict):
        raise ModelError("the model's reply has an unexpected shape")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ModelError("the model's reply has no choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ModelError("the model's reply has no message")
    usage = _usage(data.get("usage"))
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return Completion(content, usage)
    reasoning = message.get("reasoning") or message.get("reasoning_content")
    if isinstance(reasoning, str):
        recovered = action_from_reasoning(reasoning)
        if recovered is not None:
            return Completion(recovered, usage, recovered="reasoning")
    return Completion("", usage)


def _usage(raw: object) -> Usage | None:
    if not isinstance(raw, dict):
        return None
    prompt, completion = raw.get("prompt_tokens"), raw.get("completion_tokens")
    if isinstance(prompt, int) and isinstance(completion, int) and prompt >= 0 and completion >= 0:
        return Usage(prompt, completion)
    return None


def _recover_tool_use_failed(data: object) -> str | None:
    if not isinstance(data, dict) or not isinstance(data.get("error"), dict):
        return None
    error = data["error"]
    if error.get("code") != "tool_use_failed":
        return None
    failed = error.get("failed_generation")
    return action_from_tool_call(failed) if isinstance(failed, str) else None


def _error_hint(data: object) -> str:
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        code = data["error"].get("code") or data["error"].get("type")
        if isinstance(code, str) and code.replace("_", "").isalnum() and len(code) <= 40:
            return f": {mask(code)}"
    return ""
