"""Agent 9 — Performance: measured and static evidence in, a prioritised fix list out.

Two sources of evidence run first, locally:

- a static scan of the syntax trees for patterns that are often slow (candidates ``P1``...),
- optionally a real ``cProfile`` run of a script you name, which measures where the time
  goes (functions with no static candidate become ``H1``...; static ones get their measured
  share attached).

The model must triage **every** candidate (real cost or harmless here, how much, how to fix)
and may add slowdowns the tools cannot see, but only with a quote that really appears in
the code. AgentForge checks all of it (up to two corrections) and builds the report itself:
merged, sorted by impact and measured share, harmless candidates set aside, and the
bottleneck named. Source is parsed, never executed; only the profile script is run.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict

from agentforge.agents.performance.schema import (
    Additional,
    Finding,
    HotspotInfo,
    Impact,
    PerformanceInput,
    PerformanceOutput,
    Triage,
    Verdict,
)
from agentforge.core.agent import RunContext
from agentforge.core.errors import AgentForgeError
from agentforge.core.llm_agent import LLMAgent
from agentforge.core.registry import register_agent
from agentforge.core.structured import OutputCheckError
from agentforge.tools import perf_scan
from agentforge.tools.builtin import ListFilesTool, ReadFileTool, SearchTextTool, Workspace
from agentforge.tools.perf_scan import Candidate, Hotspot
from agentforge.tools.registry import ToolRegistry

MAX_FILE_CHARS = 40_000
MAX_FILES = 300
MAX_FILE_BYTES = 200_000
MAX_CANDIDATES = 80
EVIDENCE_WINDOW = 3
DUPLICATE_WINDOW = 2
MUST_KEEP_SHARE = 0.25  # a candidate this hot cannot be dismissed
MAX_SHOWN = 8
_IMPACT_ORDER = {i: n for n, i in enumerate(Impact)}  # high first
_STATUS_ORDER = {Verdict.CONFIRMED: 0, Verdict.NEEDS_REVIEW: 1, Verdict.FALSE_POSITIVE: 2}


class PerformanceError(AgentForgeError):
    """The analysis cannot run (no files, profile script missing or failing, too many findings)."""


def number_candidates(
    static: list[Candidate], hotspots: list[Hotspot]
) -> dict[str, tuple[Candidate, str]]:
    """Ids for every candidate: ``P1``... for static ones, ``H1``... for measured-only functions.

    The second item is where the candidate came from: ``static`` or ``profile``.
    """
    shared = perf_scan.attach_shares(static, hotspots)
    numbered: dict[str, tuple[Candidate, str]] = {
        f"P{n}": (c, "static") for n, c in enumerate(shared, start=1)
    }
    covered = {(c.file, c.function) for c in static}
    extra = [h for h in hotspots if (h.file, h.function) not in covered]
    for n, h in enumerate(extra, start=1):
        numbered[f"H{n}"] = (
            Candidate(
                "hotspot", h.file, h.line, h.function,
                f"Measured hotspot: {h.share:.0%} of run time in {h.function}",
                "", "high" if h.share >= MUST_KEEP_SHARE else "medium", h.share,
            ),
            "profile",
        )  # fmt: skip
    return numbered


@register_agent
class PerformanceAgent(LLMAgent[PerformanceInput, PerformanceOutput]):
    name = "performance"
    description = (
        "Finds slow code: a static scan for costly patterns plus an optional real cProfile "
        "run, then triages every candidate, ranks by measured impact, gives fixes, and adds "
        "missed slowdowns with verified evidence."
    )
    version = "0.1.0"
    tags = ("performance", "code")
    input_model = PerformanceInput
    output_model = PerformanceOutput
    max_steps = 10
    max_tokens = 8192
    max_repairs = 2
    timeout_s = 600.0

    # ------------------------------------------------------------------ setup

    async def before_run(self, data: PerformanceInput, ctx: RunContext) -> None:
        files = self._collect(data)
        if not files:
            raise PerformanceError("no readable Python files to analyse")
        hotspots: list[Hotspot] = []
        if data.profile_script:
            if data.profile_script not in files:
                raise PerformanceError(
                    f"profile script '{data.profile_script}' is not among the analysed files"
                )
            try:
                hotspots = await perf_scan.profile(files, data.profile_script)
            except perf_scan.PerfScanError as exc:
                raise PerformanceError(str(exc)) from exc
        candidates = number_candidates(perf_scan.scan_python(files), hotspots)
        if len(candidates) > MAX_CANDIDATES:
            raise PerformanceError(
                f"{len(candidates)} candidates is too many to triage at once; narrow the "
                "analysis with 'paths'"
            )
        ctx.metadata.update(files=files, candidates=candidates, hotspots=hotspots)

    # ------------------------------------------------------------------ prompt

    def build_user_message(self, data: PerformanceInput, ctx: RunContext) -> str:
        files: dict[str, str] = ctx.metadata["files"]
        candidates: dict[str, tuple[Candidate, str]] = ctx.metadata["candidates"]
        hotspots: list[Hotspot] = ctx.metadata["hotspots"]
        parts = [f"Analyse {len(files)} file(s) for performance problems."]
        if data.instructions:
            parts.append(f"\n## Focus requested by the user\n\n{data.instructions.strip()}")
        if data.profile_script:
            parts.append(f"\n## Profile of `{data.profile_script}` (measured with cProfile)\n")
            parts.extend(
                f"- {h.function} ({h.file}:{h.line}): {h.share:.0%} of run time, "
                f"{h.calls} call(s), {h.tottime_s:.3f}s own time"
                for h in hotspots
            )
            if not hotspots:
                parts.append("- no function took 5% or more of the run time")
        if candidates:
            parts.append("\n## Candidates (triage every one)\n")
            for cid, (c, _source) in candidates.items():
                measured = f", measured {c.share:.0%} of run time" if c.share is not None else ""
                code = f"\n  code: `{c.code}`" if c.code else ""
                parts.append(
                    f"- {cid}: [{c.kind}] {c.title} - {c.file}:{c.line} in "
                    f"{c.function}, first guess {c.impact}{measured}{code}"
                )
        else:
            parts.append(
                "\nThe tools found no candidates. `triage` must be an empty list; look for "
                "slowdowns they cannot see and put them in `additional`."
            )
        for path, content in sorted(files.items()):
            parts.append(f"\n## {path}\n\n```\n{_numbered(content)}\n```")
        if data.repo_path:
            parts.append(
                "\nThe rest of the repository is available through your tools. "
                "Paths are relative to the repo root."
            )
        return "\n".join(parts)

    def build_tools(self, data: PerformanceInput, ctx: RunContext) -> ToolRegistry | None:
        if not data.repo_path:
            return None
        workspace = Workspace(data.repo_path)
        return ToolRegistry(
            [ReadFileTool(workspace), ListFilesTool(workspace), SearchTextTool(workspace)]
        )

    # ------------------------------------------------------------------ verification

    async def check(
        self, output: PerformanceOutput, data: PerformanceInput, ctx: RunContext
    ) -> None:
        candidates: dict[str, tuple[Candidate, str]] = ctx.metadata["candidates"]
        files: dict[str, str] = ctx.metadata["files"]
        problems = _triage_problems(output.triage, candidates)
        problems.extend(_additional_problems(output.additional, files, candidates, output.triage))
        if problems:
            raise OutputCheckError(
                "The report has problems:\n"
                + "\n".join(f"- {p}" for p in problems[: MAX_SHOWN * 2])
                + "\nFix them and return the complete JSON again."
            )

    def postprocess(
        self, output: PerformanceOutput, data: PerformanceInput, ctx: RunContext
    ) -> PerformanceOutput:
        candidates: dict[str, tuple[Candidate, str]] = ctx.metadata["candidates"]
        files: dict[str, str] = ctx.metadata["files"]
        hotspots: list[Hotspot] = ctx.metadata["hotspots"]
        built: list[Finding] = []
        for t in output.triage:
            c, source = candidates[t.finding_id]
            built.append(
                Finding(
                    id=t.finding_id,
                    source="profile" if source == "profile" else "static",
                    kind=c.kind,
                    impact=t.impact,
                    status=t.verdict,
                    file=c.file,
                    line=c.line,
                    function=c.function,
                    title=c.title,
                    description=t.reason,
                    evidence=c.code or _line(files, c.file, c.line),
                    fix=t.fix,
                    share=c.share,
                )
            )
        for n, a in enumerate(output.additional, start=1):
            built.append(
                Finding(
                    id=f"L{n}",
                    source="llm",
                    kind=a.kind,
                    impact=a.impact,
                    status=Verdict.CONFIRMED if a.confidence == "high" else Verdict.NEEDS_REVIEW,
                    file=a.file,
                    line=a.line,
                    function=_function_at(files[a.file], a.line),
                    title=a.title,
                    description=a.description,
                    evidence=_line(files, a.file, a.line) or a.evidence,
                    fix=a.fix,
                    share=_share_of(hotspots, a.file, _function_at(files[a.file], a.line)),
                )
            )
        dismissed = sorted(
            (f for f in built if f.status is Verdict.FALSE_POSITIVE), key=lambda f: (f.file, f.line)
        )
        open_findings = _merge([f for f in built if f.status is not Verdict.FALSE_POSITIVE])
        open_findings.sort(
            key=lambda f: (_IMPACT_ORDER[f.impact], -(f.share or 0.0), f.file, f.line)
        )
        measured = [f for f in open_findings if f.share is not None]
        top = max(measured, key=lambda f: f.share or 0.0, default=None)
        return output.model_copy(
            update={
                "findings": open_findings,
                "dismissed": dismissed,
                "hotspots": [HotspotInfo(**vars(h)) for h in hotspots],
                "bottleneck": f"{top.file}:{top.function}" if top else None,
                "candidates": len(candidates),
                "profiled": bool(data.profile_script),
            }
        )

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _collect(data: PerformanceInput) -> dict[str, str]:
        if not data.repo_path:
            return {p: c for p, c in data.files.items() if p.endswith(".py")}
        workspace = Workspace(data.repo_path)
        wanted = [*data.paths]
        if data.profile_script and data.profile_script not in wanted and wanted:
            wanted.append(data.profile_script)
        roots = [workspace.resolve(p) for p in wanted] or [workspace.root]
        found: dict[str, str] = {}
        for root in roots:
            candidates = [root] if root.is_file() else sorted(root.rglob("*.py"))
            for path in candidates:
                if len(found) >= MAX_FILES:
                    break
                if path.is_symlink() or not path.is_file() or path.suffix != ".py":
                    continue
                if workspace.is_ignored(path) or workspace.is_denied(path):
                    continue
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                try:
                    found[workspace.relative(path)] = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
        return found


# ---------------------------------------------------------------------- checks


def _triage_problems(
    triage: list[Triage], candidates: dict[str, tuple[Candidate, str]]
) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for t in triage:
        if t.finding_id not in candidates:
            problems.append(f"{t.finding_id} is not a candidate id; remove it")
            continue
        if t.finding_id in seen:
            problems.append(f"{t.finding_id} is triaged more than once")
        seen.add(t.finding_id)
        c = candidates[t.finding_id][0]
        if t.verdict is Verdict.CONFIRMED and not (t.fix and t.fix.strip()):
            problems.append(f"{t.finding_id} is confirmed but has no fix")
        if t.verdict is Verdict.FALSE_POSITIVE and (c.share or 0.0) >= MUST_KEEP_SHARE:
            problems.append(
                f"{t.finding_id} took {c.share:.0%} of the measured run time, so it cannot be "
                "dismissed; use needs_review if unsure"
            )
    missing = [cid for cid in candidates if cid not in seen]
    if missing:
        problems.append("no triage entry for: " + ", ".join(missing))
    return problems


def _additional_problems(
    additional: list[Additional],
    files: dict[str, str],
    candidates: dict[str, tuple[Candidate, str]],
    triage: list[Triage],
) -> list[str]:
    problems: list[str] = []
    dismissed = {t.finding_id for t in triage if t.verdict is Verdict.FALSE_POSITIVE}
    for a in additional:
        where = f"additional finding '{a.title}' ({a.file}:{a.line})"
        source = files.get(a.file)
        if source is None:
            problems.append(f"{where}: file not found in the analysed files")
            continue
        lines = source.splitlines()
        if a.line > len(lines):
            problems.append(f"{where}: the file has only {len(lines)} lines")
            continue
        if not _evidence_found(a.evidence, lines, a.line):
            problems.append(
                f"{where}: the evidence does not appear within {EVIDENCE_WINDOW} lines of "
                "that line. Copy the slow code exactly"
            )
        for cid, (c, _source) in candidates.items():
            near = c.file == a.file and abs(c.line - a.line) <= DUPLICATE_WINDOW
            if cid not in dismissed and near and c.kind == a.kind:
                problems.append(f"{where}: already reported as {cid}; remove it")
    return problems


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _evidence_found(evidence: str, lines: list[str], line: int) -> bool:
    low, high = max(line - 1 - EVIDENCE_WINDOW, 0), min(line + EVIDENCE_WINDOW, len(lines))
    needle = _norm(evidence)
    return bool(needle) and needle in _norm("\n".join(lines[low:high]))


def _line(files: dict[str, str], path: str, number: int) -> str:
    lines = files.get(path, "").splitlines()
    return lines[number - 1].strip()[:200] if 0 < number <= len(lines) else ""


def _function_at(source: str, line: int) -> str:
    """Name of the innermost function containing ``line``, or ``<module>``."""
    best, span = "<module>", 10**9
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return best
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            end = node.end_lineno or node.lineno
            if node.lineno <= line <= end and end - node.lineno < span:
                best, span = node.name, end - node.lineno
    return best


def _share_of(hotspots: list[Hotspot], path: str, function: str) -> float | None:
    for h in hotspots:
        if h.file == path and h.function.rsplit(".", 1)[-1] == function:
            return h.share
    return None


def _merge(findings: list[Finding]) -> list[Finding]:
    """One finding per (file, line, kind): the highest impact and best status kept."""
    groups: dict[tuple[str, int, str], list[Finding]] = defaultdict(list)
    for f in findings:
        groups[(f.file, f.line, f.kind)].append(f)
    merged: list[Finding] = []
    for group in groups.values():
        best = min(group, key=lambda f: (_IMPACT_ORDER[f.impact], _STATUS_ORDER[f.status]))
        status = min((f.status for f in group), key=lambda s: _STATUS_ORDER[s])
        merged.append(best.model_copy(update={"status": status}))
    return merged


def _numbered(text: str) -> str:
    body = text if len(text) <= MAX_FILE_CHARS else text[:MAX_FILE_CHARS] + "\n# ...[truncated]"
    return "\n".join(f"{n:>4}  {line}" for n, line in enumerate(body.splitlines(), start=1))
