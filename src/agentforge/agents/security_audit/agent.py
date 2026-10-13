"""Agent 8 — Security Audit: scanners find candidates, the model triages, AgentForge verifies.

Two local, offline scanners run first (bandit for Python, a small secrets scanner for any
text file). Their findings are numbered ``S1``, ``S2`` ... and handed to the model together
with the code. The model must

- give a verdict, a real-world severity and a fix for **every** scanner finding, and
- may add vulnerabilities the scanners cannot see (path traversal, SSRF, missing
  authorization) but only with a quote that really appears in the code.

AgentForge checks both before accepting the answer (up to two corrections), then builds
the final report itself: de-duplicated, sorted by risk, false positives set aside.
The code under audit is read, never executed.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from agentforge.agents.security_audit.schema import (
    Additional,
    Finding,
    SecurityAuditInput,
    SecurityAuditOutput,
    Severity,
    Triage,
    Verdict,
)
from agentforge.core.agent import RunContext
from agentforge.core.errors import AgentForgeError
from agentforge.core.llm_agent import LLMAgent
from agentforge.core.registry import register_agent
from agentforge.core.structured import OutputCheckError
from agentforge.tools import security_scan
from agentforge.tools.builtin import ListFilesTool, ReadFileTool, SearchTextTool, Workspace
from agentforge.tools.registry import ToolRegistry
from agentforge.tools.security_scan import ScannerFinding

MAX_FILE_CHARS = 40_000
MAX_FILES = 300
MAX_FILE_BYTES = 200_000
MAX_FINDINGS = 80
EVIDENCE_WINDOW = 3  # lines either side of the reported line
DUPLICATE_WINDOW = 2
MAX_SHOWN = 8
_SEVERITY_ORDER = {s: i for i, s in enumerate(Severity)}  # critical first
_STATUS_ORDER = {Verdict.CONFIRMED: 0, Verdict.NEEDS_REVIEW: 1, Verdict.FALSE_POSITIVE: 2}
_CODE_SUFFIXES = security_scan.TEXT_SUFFIXES


class SecurityAuditError(AgentForgeError):
    """The audit cannot run (no readable files, too many findings, scanner failure)."""


@register_agent
class SecurityAuditAgent(LLMAgent[SecurityAuditInput, SecurityAuditOutput]):
    name = "security-audit"
    description = (
        "Scans code with bandit and a secrets scanner, triages every finding (real or false "
        "positive, real severity, concrete fix), adds missed vulnerabilities with verified "
        "evidence, and returns one prioritised report."
    )
    version = "0.1.0"
    tags = ("security", "code")
    input_model = SecurityAuditInput
    output_model = SecurityAuditOutput
    max_steps = 10
    max_tokens = 8192
    max_repairs = 2
    timeout_s = 600.0

    # ------------------------------------------------------------------ setup

    async def before_run(self, data: SecurityAuditInput, ctx: RunContext) -> None:
        files = self._collect(data)
        if not files:
            raise SecurityAuditError("no readable source or config files to audit")
        try:
            raw = await security_scan.scan(files, min_severity=data.min_severity.value)
        except security_scan.ScanError as exc:
            raise SecurityAuditError(f"the scanner could not run: {exc}") from exc
        if len(raw) > MAX_FINDINGS:
            raise SecurityAuditError(
                f"{len(raw)} scanner findings is too many to triage at once; narrow the audit "
                "with 'paths' or raise 'min_severity'"
            )
        ctx.metadata.update(files=files, scanner={f"S{i}": f for i, f in enumerate(raw, start=1)})

    # ------------------------------------------------------------------ prompt

    def build_user_message(self, data: SecurityAuditInput, ctx: RunContext) -> str:
        files: dict[str, str] = ctx.metadata["files"]
        scanner: dict[str, ScannerFinding] = ctx.metadata["scanner"]
        parts = [f"Audit {len(files)} file(s)."]
        if data.instructions:
            parts.append(f"\n## Focus requested by the user\n\n{data.instructions.strip()}")
        if scanner:
            parts.append("\n## Scanner findings (triage every one)\n")
            parts.extend(
                f"- {fid}: [{f.tool} {f.rule}] {f.title} - scanner severity {f.severity}, "
                f"confidence {f.confidence}, category {f.category}, {f.file}:{f.line}\n"
                f"  code: `{f.code}`"
                for fid, f in scanner.items()
            )
        else:
            parts.append(
                "\nThe scanners found nothing. `triage` must be an empty list; look for "
                "vulnerabilities they cannot see and put them in `additional`."
            )
        for path, content in sorted(files.items()):
            parts.append(f"\n## {path}\n\n```\n{_numbered(content)}\n```")
        if data.repo_path:
            parts.append(
                "\nThe rest of the repository is available through your tools. "
                "Paths are relative to the repo root."
            )
        return "\n".join(parts)

    def build_tools(self, data: SecurityAuditInput, ctx: RunContext) -> ToolRegistry | None:
        if not data.repo_path:
            return None
        workspace = Workspace(data.repo_path)
        return ToolRegistry(
            [ReadFileTool(workspace), ListFilesTool(workspace), SearchTextTool(workspace)]
        )

    # ------------------------------------------------------------------ verification

    async def check(
        self, output: SecurityAuditOutput, data: SecurityAuditInput, ctx: RunContext
    ) -> None:
        scanner: dict[str, ScannerFinding] = ctx.metadata["scanner"]
        files: dict[str, str] = ctx.metadata["files"]
        problems = [*_triage_problems(output.triage, scanner)]
        problems.extend(_additional_problems(output.additional, files, scanner, output.triage))
        if problems:
            raise OutputCheckError(
                "The report has problems:\n"
                + "\n".join(f"- {p}" for p in problems[: MAX_SHOWN * 2])
                + "\nFix them and return the complete JSON again."
            )

    def postprocess(
        self, output: SecurityAuditOutput, data: SecurityAuditInput, ctx: RunContext
    ) -> SecurityAuditOutput:
        scanner: dict[str, ScannerFinding] = ctx.metadata["scanner"]
        files: dict[str, str] = ctx.metadata["files"]
        built: list[Finding] = []
        for t in output.triage:
            f = scanner[t.finding_id]
            built.append(
                Finding(
                    id=t.finding_id,
                    source="scanner",
                    rule=f.rule,
                    category=f.category,
                    severity=t.severity,
                    status=t.verdict,
                    confidence=f.confidence,
                    file=f.file,
                    line=f.line,
                    title=f.title,
                    description=t.reason,
                    evidence=f.code,
                    fix=t.fix,
                    cwe=f.cwe,
                )
            )
        for n, a in enumerate(output.additional, start=1):
            built.append(
                Finding(
                    id=f"L{n}",
                    source="llm",
                    rule="llm",
                    category=a.category,
                    severity=a.severity,
                    status=Verdict.CONFIRMED if a.confidence == "high" else Verdict.NEEDS_REVIEW,
                    confidence=a.confidence,
                    file=a.file,
                    line=a.line,
                    title=a.title,
                    description=a.description,
                    evidence=_evidence_line(files, a),
                    fix=a.fix,
                    cwe=a.cwe,
                )
            )
        dismissed = [f for f in built if f.status is Verdict.FALSE_POSITIVE]
        open_findings = _merge([f for f in built if f.status is not Verdict.FALSE_POSITIVE])
        open_findings.sort(
            key=lambda f: (_SEVERITY_ORDER[f.severity], _STATUS_ORDER[f.status], f.file, f.line)
        )
        dismissed.sort(key=lambda f: (f.file, f.line))
        risk: Any = open_findings[0].severity.value if open_findings else "none"
        return output.model_copy(
            update={
                "findings": open_findings,
                "dismissed": dismissed,
                "risk": risk,
                "scanner_findings": len(scanner),
            }
        )

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _collect(data: SecurityAuditInput) -> dict[str, str]:
        if not data.repo_path:
            return dict(data.files)
        workspace = Workspace(data.repo_path)
        roots = [workspace.resolve(p) for p in data.paths] or [workspace.root]
        found: dict[str, str] = {}
        for root in roots:
            candidates = [root] if root.is_file() else sorted(root.rglob("*"))
            for path in candidates:
                if len(found) >= MAX_FILES:
                    break
                if path.is_symlink() or not path.is_file():
                    continue
                if workspace.is_ignored(path) or workspace.is_denied(path):
                    continue
                if path.suffix.lower() not in _CODE_SUFFIXES and not path.name.startswith(".env"):
                    continue
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                try:
                    found[workspace.relative(path)] = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
        return found


# ---------------------------------------------------------------------- checks


def _triage_problems(triage: list[Triage], scanner: dict[str, ScannerFinding]) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for t in triage:
        if t.finding_id not in scanner:
            problems.append(f"{t.finding_id} is not a scanner finding id; remove it")
            continue
        if t.finding_id in seen:
            problems.append(f"{t.finding_id} is triaged more than once")
        seen.add(t.finding_id)
        f = scanner[t.finding_id]
        if t.verdict is Verdict.CONFIRMED and not (t.fix and t.fix.strip()):
            problems.append(f"{t.finding_id} is confirmed but has no fix")
        if t.verdict is Verdict.FALSE_POSITIVE and f.severity == "high" and f.confidence == "high":
            problems.append(
                f"{t.finding_id} is rated high severity with high confidence by the scanner, "
                "so it cannot be a false positive; use needs_review if unsure"
            )
    missing = [fid for fid in scanner if fid not in seen]
    if missing:
        problems.append("no triage entry for: " + ", ".join(missing))
    return problems


def _additional_problems(
    additional: list[Additional],
    files: dict[str, str],
    scanner: dict[str, ScannerFinding],
    triage: list[Triage],
) -> list[str]:
    problems: list[str] = []
    dropped = {t.finding_id for t in triage if t.verdict is Verdict.FALSE_POSITIVE}
    for a in additional:
        where = f"additional finding '{a.title}' ({a.file}:{a.line})"
        source = files.get(a.file)
        if source is None:
            problems.append(f"{where}: file not found in the audited files")
            continue
        lines = source.splitlines()
        if a.line > len(lines):
            problems.append(f"{where}: the file has only {len(lines)} lines")
            continue
        if not _evidence_found(a.evidence, lines, a.line):
            problems.append(
                f"{where}: the evidence does not appear within {EVIDENCE_WINDOW} lines of "
                "that line. Copy the vulnerable code exactly"
            )
        for fid, f in scanner.items():
            if fid in dropped or f.file != a.file or abs(f.line - a.line) > DUPLICATE_WINDOW:
                continue
            if f.category == a.category:
                problems.append(f"{where}: already reported by the scanner as {fid}; remove it")
    return problems


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _evidence_found(evidence: str, lines: list[str], line: int) -> bool:
    low, high = max(line - 1 - EVIDENCE_WINDOW, 0), min(line + EVIDENCE_WINDOW, len(lines))
    window = _norm("\n".join(lines[low:high]))
    needle = _norm(evidence)
    return bool(needle) and needle in window


def _evidence_line(files: dict[str, str], a: Additional) -> str:
    lines = files[a.file].splitlines()
    return lines[a.line - 1].strip() if 0 < a.line <= len(lines) else a.evidence


def _merge(findings: list[Finding]) -> list[Finding]:
    """One finding per (file, line, category): rules joined, the highest severity kept."""
    groups: dict[tuple[str, int, str], list[Finding]] = defaultdict(list)
    for f in findings:
        groups[(f.file, f.line, f.category)].append(f)
    merged: list[Finding] = []
    for group in groups.values():
        best = min(group, key=lambda f: (_SEVERITY_ORDER[f.severity], _STATUS_ORDER[f.status]))
        rules = sorted({r for f in group for r in f.rule.split(",")})
        status = min((f.status for f in group), key=lambda s: _STATUS_ORDER[s])
        merged.append(best.model_copy(update={"rule": ",".join(rules), "status": status}))
    return merged


def _numbered(text: str) -> str:
    body = text if len(text) <= MAX_FILE_CHARS else text[:MAX_FILE_CHARS] + "\n# ...[truncated]"
    return "\n".join(f"{n:>4}  {line}" for n, line in enumerate(body.splitlines(), start=1))
