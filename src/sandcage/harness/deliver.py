"""What leaves the cage: ``sandcage.patch`` and ``SUMMARY.md``.

The patch is a git-applicable diff of the final workspace against the untouched original.
Left out, and listed with a reason: secret files, binary or very large files, and files
whose patch would change under secret masking (delivering a masked patch would write
placeholders over real code; delivering it unmasked would leak). A non-empty patch with a
high red-team finding, or one nobody reviewed, is **withheld** until a human releases it.
A withheld patch never reads "Delivered" or "the task got done".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from sandcage.contract.common import Phase
from sandcage.contract.events import DeliverEvent, ExcludedFile, VerifyCheck
from sandcage.masking import mask
from sandcage.report.report import ReportInput, headline, md_text, outcome
from sandcage.sandbox.diff import WorkspaceDiff, file_section

PATCH_NAME: Final = "sandcage.patch"
SUMMARY_NAME: Final = "SUMMARY.md"
REPORT_JSON_NAME: Final = "report.json"
REPORT_MD_NAME: Final = "REPORT.md"
EXCLUDED_SECRET: Final = "secret file: never delivered"  # noqa: S105 - a reason
EXCLUDED_BINARY: Final = "binary or very large file"
EXCLUDED_MASKED: Final = "contains secret-looking text that masking would change"
RELEASE_NOTE: Final = "Released by a human after review: the patch was withheld by the red-team."


@dataclass(frozen=True)
class Patch:
    text: str
    files: tuple[str, ...]
    excluded: tuple[tuple[str, str], ...]


def build_patch(original: Path, workspace: Path, diff: WorkspaceDiff) -> Patch:
    sections: list[str] = []
    files: list[str] = []
    excluded: list[tuple[str, str]] = []
    for change in diff.files:
        if change.secret:
            excluded.append((change.path, EXCLUDED_SECRET))
            continue
        section = file_section(original, workspace, change)
        if section is None:
            excluded.append((change.path, EXCLUDED_BINARY))
            continue
        if mask(section, source=change.path) != section:
            excluded.append((change.path, EXCLUDED_MASKED))
            continue
        sections.append(section)
        files.append(change.path)
    return Patch("".join(sections), tuple(files), tuple(excluded))


@dataclass
class Delivery:
    patch: Patch
    withheld: bool
    released_by_human: bool = False
    summary_md: str = ""

    @property
    def blocked(self) -> bool:
        """Still withheld (not released)."""
        return self.withheld and not self.released_by_human

    def event(self, run_id: str, ts: float) -> DeliverEvent:
        return DeliverEvent(
            run_id=run_id,
            phase=Phase.DELIVER,
            ts=ts,
            files=list(self.patch.files),
            excluded=[ExcludedFile(path=p, reason=r) for p, r in self.patch.excluded],
            blocked=self.blocked,
            released_by_human=self.released_by_human,
            patch_bytes=len(self.patch.text.encode()),
        )


def summary_markdown(
    source: ReportInput,
    *,
    done_criteria: Sequence[str] = (),
    checks: Sequence[VerifyCheck] = (),
    agent_summary: str = "",
) -> str:
    """SUMMARY.md for people. Every untrusted string is cleaned (masked, one line, no HTML,
    no code fences); the agent's own summary is labelled an unverified claim."""
    blocks = [e for e in source.steps if e.blocked and not e.observed]
    pill = outcome(source.status, withheld=source.withheld)
    lines = [
        "# Sandcage summary",
        "",
        f"**{pill}.** {headline(source.status, len(blocks), withheld=source.withheld)}",
        "",
    ]
    if source.withheld:
        lines += [
            "> **The patch is withheld.** The red-team found a high-severity issue or could not "
            "review it. Read the findings below before releasing it.",
            "",
        ]
    verified = source.verified
    lines += [
        f"- Task: {md_text(source.brief, 500)}",
        f"- Status: {source.status.value} ("
        + ("verified" if verified else "NOT VERIFIED" if verified is False else "not checked")
        + ")",
    ]
    if done_criteria:
        lines.append("- Done when: " + "; ".join(md_text(c, 200) for c in done_criteria))
    if agent_summary:
        lines.append(f"- The agent says (unverified claim): {md_text(agent_summary, 500)}")
    if source.scout_flags:
        lines.append("- Files with instructions for AI: " + ", ".join(source.scout_flags))
    if source.plan is not None:
        manifest = source.plan.manifest
        lines.append(
            f"- Plan ({source.plan.risk.value} risk): may write "
            f"{', '.join(manifest.write_paths) or 'nothing'}; may run "
            f"{', '.join(manifest.commands) or 'nothing'}; network "
            f"{', '.join(manifest.network) or 'none'}"
        )
    lines += ["", "## What changed", ""]
    if source.delivered_files:
        lines += [f"- {md_text(p, 200)}" for p in source.delivered_files]
        lines += ["", f"Apply it with `git apply {PATCH_NAME}` in your repository."]
    else:
        lines.append("- No files changed.")
    for path, reason in source.excluded:
        lines.append(f"- Left out: {md_text(path, 200)} ({md_text(reason, 100)})")
    if checks:
        lines += ["", "## Independent check", ""]
        for check in checks:
            mark = "passed" if check.passed else "FAILED"
            detail = f": {md_text(check.detail, 200)}" if check.detail else ""
            lines.append(f"- {check.name}: {mark}{detail}")
    lines += ["", f"## Blocked actions ({len(blocks)})", ""]
    for event in blocks:
        lines.append(
            f"- Step {event.step}: {md_text(event.blocked_sentence or event.tool)} "
            f"(rule {md_text(event.rule_id, 80)})"
        )
    if not blocks:
        lines.append("- None.")
    lines += ["", "## Attacker review", ""]
    if source.unreviewed:
        lines.append("- The patch was not reviewed, so it is withheld.")
    elif not source.findings:
        lines.append("- Nothing found.")
    for finding in source.findings:
        where = md_text(finding.file, 200) + (f":{finding.line}" if finding.line else "")
        lines.append(
            f"- {finding.severity.value}: {where or 'patch'}: {md_text(finding.explanation)}"
        )
    lines += [
        "",
        f"Checkpoints: {source.checkpoints} · rollbacks: {source.rollbacks} · "
        f"breaker trips: {source.breaker_trips}",
    ]
    return "\n".join(lines) + "\n"
