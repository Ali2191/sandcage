"""The provider check script, on fake transports only (no real call ever runs here)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sandcage import provider_check as pc
from sandcage.agent.llm import HttpReply

KEY = "vk-test-0123456789abcdefSECRET"


def _key_file(tmp_path: Path, mode: int = 0o600, text: str = KEY + "\n") -> Path:
    path = tmp_path / "key"
    path.write_text(text)
    path.chmod(mode)
    return path


def _chat(content: str | None, *, usage: bool = True, **message: object) -> HttpReply:
    body: dict[str, object] = {
        "choices": [{"message": {"content": content, **message}, "finish_reason": "stop"}]
    }
    if usage:
        body["usage"] = {"prompt_tokens": 600, "completion_tokens": 30}
    return HttpReply(200, json.dumps(body).encode())


def _models(*ids: str) -> pc.Getter:
    def get(url: str, headers: dict[str, str], timeout: float) -> HttpReply:
        assert url == "https://api.vultrinference.com/v1/models"
        assert headers == {"Authorization": f"Bearer {KEY}"}
        return HttpReply(200, json.dumps({"data": [{"id": i} for i in ids]}).encode())

    return get


class Script:
    def __init__(self, *replies: HttpReply) -> None:
        self.replies = list(replies)
        self.bodies: list[dict[str, object]] = []

    def __call__(self, url: str, body: bytes, headers: dict[str, str], timeout: float) -> HttpReply:
        assert url == "https://api.vultrinference.com/v1/chat/completions"
        self.bodies.append(json.loads(body))
        return self.replies.pop(0)


GOOD = '{"tool": "list_dir", "args": {"path": "."}, "reason": "look"}'


# --- the key file ------------------------------------------------------------------------


def test_key_is_read_from_a_600_file(tmp_path: Path) -> None:
    assert pc.read_key(_key_file(tmp_path)) == KEY


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o400, 0o700])
def test_key_file_must_be_mode_600(tmp_path: Path, mode: int) -> None:
    with pytest.raises(pc.ProviderCheckError, match="must be 600") as caught:
        pc.read_key(_key_file(tmp_path, mode))
    assert KEY not in str(caught.value)


def test_key_file_problems_never_show_the_contents(tmp_path: Path) -> None:
    with pytest.raises(pc.ProviderCheckError, match="no key file"):
        pc.read_key(tmp_path / "missing")
    link = tmp_path / "link"
    link.symlink_to(_key_file(tmp_path))
    with pytest.raises(pc.ProviderCheckError, match="cannot open"):
        pc.read_key(link)
    with pytest.raises(pc.ProviderCheckError, match="exactly one key") as caught:
        pc.read_key(_key_file(tmp_path, text=f"{KEY} {KEY}\n"))
    assert KEY not in str(caught.value)
    (tmp_path / "dir").mkdir(mode=0o600)
    with pytest.raises(pc.ProviderCheckError):
        pc.read_key(tmp_path / "dir")


def test_key_path_comes_from_the_environment() -> None:
    assert pc.key_path({"SANDCAGE_KEY_FILE": "/x/k"}) == Path("/x/k")
    assert pc.key_path({}) == Path("~/.sandcage/vultr-demo.key").expanduser()


# --- one model -----------------------------------------------------------------------------


def test_first_try_records_usage_and_cost() -> None:
    script = Script(_chat(GOOD))
    budget = pc.Budget()
    result = pc.check_model("deepseek-v4.1-flash-1017", KEY, budget, transport=script)
    assert (result.outcome, result.tool, result.usage_reported, result.calls) == (
        "first try",
        "list_dir",
        True,
        1,
    )
    assert result.cost_measured
    assert result.cost_usd == pytest.approx((600 * 0.15 + 30 * 0.60) / 1e6)
    assert budget.spent == pytest.approx(result.cost_usd)
    sent = script.bodies[0]
    assert sent["max_tokens"] == pc.MAX_TOKENS
    assert sent["temperature"] == 0
    assert "Reply with exactly one JSON object" in json.dumps(sent["messages"])


def test_reasoning_recovery_and_quirks() -> None:
    script = Script(_chat("", usage=False, reasoning_content=f"I will list. {GOOD}"))
    result = pc.check_model("glm-5.3", KEY, pc.Budget(), transport=script)
    assert result.outcome == "after client recovery (reasoning)"
    assert not result.usage_reported
    assert not result.cost_measured
    assert "empty content" in result.quirks
    assert any(q.startswith("reasoning_content field") for q in result.quirks)


def test_fenced_prose_still_parses_but_is_noted() -> None:
    script = Script(_chat(f"Sure!\n```json\n{GOOD}\n```"))
    result = pc.check_model("qwen3.8-27b", KEY, pc.Budget(), transport=script)
    assert result.outcome == "first try"
    assert "markdown fence" in result.quirks
    assert "extra text around the JSON" in result.quirks


def test_one_retry_then_failed() -> None:
    script = Script(_chat("I cannot do that."), _chat(GOOD))
    result = pc.check_model("mimo-v2.6-pro", KEY, pc.Budget(), transport=script)
    assert (result.outcome, result.calls) == ("after one retry", 2)
    retry_messages = json.dumps(script.bodies[1]["messages"])
    assert "could not be used" in retry_messages

    script = Script(_chat("no"), _chat("still no"))
    result = pc.check_model("mimo-v2.6-pro", KEY, pc.Budget(), transport=script)
    assert (result.outcome, result.calls) == ("failed", 2)
    assert "invalid after one retry" in result.note


def test_a_wrong_but_valid_tool_is_noted() -> None:
    result = pc.check_model(
        "qwen3.8-27b", KEY, pc.Budget(), transport=Script(_chat('{"tool": "finish"}'))
    )
    assert result.outcome == "first try"
    assert "chose finish" in result.note


@pytest.mark.parametrize("status", [401, 403, 429])
def test_auth_and_rate_errors_abort_without_retry(status: int) -> None:
    script = Script(HttpReply(status, b'{"error": {"code": "nope"}}'))
    with pytest.raises(pc.ProviderCheckError, match=f"HTTP {status}"):
        pc.check_model("glm-5.3", KEY, pc.Budget(), transport=script)
    assert len(script.bodies) == 1


def test_server_error_is_a_failed_row_not_a_retry_loop() -> None:
    script = Script(HttpReply(500, b'{"error": {"code": "boom"}}'))
    result = pc.check_model("glm-5.3", KEY, pc.Budget(), transport=script)
    assert result.outcome == "failed"
    assert len(script.bodies) == 1


def test_the_spend_cap_stops_before_the_call() -> None:
    script = Script(_chat(GOOD))
    with pytest.raises(pc.ProviderCheckError, match="cross the"):
        pc.check_model("glm-5.3", KEY, pc.Budget(cap=0.0001), transport=script)
    assert script.bodies == []
    with pytest.raises(pc.ProviderCheckError, match="no price"):
        pc.worst_case_cost("unknown-model", pc.messages_for_check(), 10)


def test_whole_check_worst_case_stays_under_the_cap() -> None:
    messages = pc.messages_for_check()
    worst = 2 * sum(pc.worst_case_cost(m, messages, pc.MAX_TOKENS) for m in pc.CANDIDATES)
    assert worst < pc.SPEND_CAP_USD


# --- the whole run -------------------------------------------------------------------------


def test_run_check_never_prints_the_key(tmp_path: Path) -> None:
    key_file = _key_file(tmp_path)
    lines: list[str] = []

    def leaky_get(url: str, headers: dict[str, str], timeout: float) -> HttpReply:
        return HttpReply(200, json.dumps({"data": [{"id": "glm-5.3"}, {"id": KEY}]}).encode())

    code = pc.run_check(
        lines.append,
        environ={"SANDCAGE_KEY_FILE": str(key_file)},
        get=leaky_get,
        transport=Script(_chat(GOOD)),
        candidates=["glm-5.3", "mimo-v2.6-pro"],
    )
    text = "\n".join(lines)
    assert code == 0
    assert KEY not in text
    assert "[MASKED:api_key]" in text
    assert "| glm-5.3 | first try | list_dir | yes" in text
    assert "| mimo-v2.6-pro | not served |" in text
    assert "unverified third-party prices" in text


def test_run_check_stops_on_refusal_and_reports(tmp_path: Path) -> None:
    lines: list[str] = []
    script = Script(HttpReply(429, b"{}"))
    code = pc.run_check(
        lines.append,
        environ={"SANDCAGE_KEY_FILE": str(_key_file(tmp_path))},
        get=_models("glm-5.3", "qwen3.8-27b"),
        transport=script,
        candidates=["glm-5.3", "qwen3.8-27b"],
    )
    assert code == 1
    assert len(script.bodies) == 1
    assert "HTTP 429" in "\n".join(lines)


def test_run_check_refuses_a_loose_key_file(tmp_path: Path) -> None:
    lines: list[str] = []
    calls: list[str] = []

    def get(url: str, headers: dict[str, str], timeout: float) -> HttpReply:
        calls.append(url)
        return HttpReply(200, b"{}")

    code = pc.run_check(
        lines.append, environ={"SANDCAGE_KEY_FILE": str(_key_file(tmp_path, 0o644))}, get=get
    )
    assert code == 1
    assert calls == []
    assert "must be 600" in lines[0]


def test_listing_refusal_aborts(tmp_path: Path) -> None:
    def get(url: str, headers: dict[str, str], timeout: float) -> HttpReply:
        return HttpReply(401, b'{"error": "bad key ' + KEY.encode() + b'"}')

    lines: list[str] = []
    code = pc.run_check(
        lines.append, environ={"SANDCAGE_KEY_FILE": str(_key_file(tmp_path))}, get=get
    )
    assert code == 1
    assert "HTTP 401" in lines[0]
    assert KEY not in "\n".join(lines)


def test_quirks_never_contain_reply_text() -> None:
    body = _chat("SECRET-PROSE " + GOOD).body
    assert all("SECRET-PROSE" not in q for q in pc.reply_quirks(body))
    assert pc.reply_quirks(b"not json") == ["reply body is not JSON"]
    assert "native tool_calls" in pc.reply_quirks(_chat(None, tool_calls=[{"id": "x"}]).body)
