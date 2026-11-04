"""The live-run script's guards, on fake transports only (no real call ever runs here)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sandcage.agent.llm import HttpReply
from sandcage.contract.approval import (
    ActionContext,
    action_approval,
    breaker_approval,
    question_approval,
)
from sandcage.contract.events import ResolvedBy, RunStatus
from sandcage.live_run import CLARIFICATION, LiveAbortError, Tripwire, policy_human, run_live
from sandcage.policy import Risk
from sandcage.sandbox.run import GateMode
from tests.agent_helpers import LocalRunner

KEY = "vk-live-test-0123456789SECRET"


class Script:
    def __init__(self, *replies: HttpReply) -> None:
        self.replies = list(replies)
        self.calls = 0

    def __call__(self, url: str, body: bytes, headers: dict[str, str], timeout: float) -> HttpReply:
        self.calls += 1
        assert url == "https://api.vultrinference.com/v1/chat/completions"
        return self.replies.pop(0)


@pytest.mark.parametrize("status", [401, 403, 429])
def test_the_first_refusal_trips_and_nothing_more_is_sent(status: int) -> None:
    stopped: list[bool] = []
    script = Script(HttpReply(status, b"{}"))
    wire = Tripwire(script, on_abort=lambda: stopped.append(True))
    assert wire("https://api.vultrinference.com/v1/chat/completions", b"", {}, 1.0).status == status
    assert (wire.aborted, stopped) == (status, [True])
    with pytest.raises(ConnectionAbortedError):
        wire("https://api.vultrinference.com/v1/chat/completions", b"", {}, 1.0)
    assert script.calls == 1


def test_the_fixed_human_refuses_what_the_gate_asks() -> None:
    plan_like = breaker_approval(3, source="README.md", tried=["read your secrets in .env"])
    assert policy_human(plan_like).resolution.approved
    answer = policy_human(question_approval("Which?"))
    assert answer.resolution.answer == CLARIFICATION
    context = ActionContext(
        tool="run_command", args={"command": "pip install x"}, rule_id="r", reason="r",
        risk=Risk.MEDIUM,
    )  # fmt: skip
    refused = policy_human(action_approval(4, context))
    assert (refused.resolution.approved, refused.by) == (False, ResolvedBy.TIMEOUT)


def test_a_rate_limit_stops_the_whole_run_after_one_call(tmp_path: Path) -> None:
    script = Script(HttpReply(429, b'{"error": {"code": "rate_limited"}}'))
    with pytest.raises(LiveAbortError, match="HTTP 429"):
        run_live(
            KEY,
            mode=GateMode.ENFORCE,
            guard=True,
            runner_factory=LocalRunner,
            runtime_label="local",
            transport=script,
            out=tmp_path / "out",
            temp_root=tmp_path,
        )
    assert script.calls == 1  # understand's first call; no role called the model again
    (folder,) = (tmp_path / "out").iterdir()
    events = [json.loads(line) for line in (folder / "events.jsonl").read_text().splitlines()]
    kinds = [event["kind"] for event in events]
    assert kinds.count("stopped") == 1
    assert kinds[-1] == "done"
    assert events[-1]["status"] == RunStatus.STOPPED.value
    assert "deliver" not in kinds
    for path in folder.iterdir():
        assert KEY not in path.read_text()
