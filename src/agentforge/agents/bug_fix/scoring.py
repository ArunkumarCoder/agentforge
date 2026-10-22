"""Golden eval cases for the Bug Fix agent, and how a fix is scored against one.

Each case in ``evals/*.json`` has an input (an error plus the source files) and
expectations:

- ``file``: the file the fix must change,
- ``keywords_any``: words one of which must appear in the root cause,
- ``check``: a short Python script that imports the patched code and asserts
  the bug is gone. It fails on the original files and passes on a correct fix.

Scoring is deterministic: no LLM judge. The behaviour check runs in a separate
isolated interpreter (``python -I``) in a temporary directory that holds only
the patched files, with a timeout.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import Field

from agentforge.agents.bug_fix.schema import BugFixInput, BugFixOutput
from agentforge.core.schemas import Contract
from agentforge.evals.scoring import CaseScore, Check
from agentforge.tools.builtin.process import with_limits
from agentforge.tools.patching import PatchError, apply_patch
from agentforge.tools.sandbox import untrusted_limits

CHECK_TIMEOUT_S = 20.0


class Expectations(Contract):
    file: str
    keywords_any: list[str] = Field(default_factory=list)
    check: str = Field(min_length=1, description="Python that asserts the bug is fixed")


class EvalCase(Contract):
    name: str
    description: str
    input: dict[str, Any]
    expect: Expectations

    def fix_input(self) -> BugFixInput:
        return BugFixInput.model_validate(self.input)


def load_cases() -> list[EvalCase]:
    """All golden cases shipped with the agent, sorted by name."""
    folder = resources.files("agentforge.agents.bug_fix").joinpath("evals")
    cases = [
        EvalCase.model_validate(json.loads(entry.read_text(encoding="utf-8")))
        for entry in folder.iterdir()
        if entry.name.endswith(".json")
    ]
    return sorted(cases, key=lambda c: c.name)


def run_check(files: Mapping[str, str], check: str) -> tuple[bool, str]:
    """Write ``files`` to a temp dir and run ``check`` there. Returns (passed, detail)."""
    with tempfile.TemporaryDirectory(prefix="agentforge-eval-") as tmp:
        root = Path(tmp)
        for rel, content in files.items():
            target = (root / rel).resolve()
            if not target.is_relative_to(root.resolve()):
                return False, f"refusing to write outside the eval dir: {rel}"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        script = root / "_agentforge_check.py"
        script.write_text(f"import sys\nsys.path.insert(0, {str(root)!r})\n{check}", "utf-8")
        try:
            proc = subprocess.run(  # noqa: S603 - our interpreter, our generated script
                with_limits([sys.executable, "-I", str(script)], untrusted_limits()),
                cwd=root,
                capture_output=True,
                text=True,
                timeout=CHECK_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False, f"check timed out after {CHECK_TIMEOUT_S:.0f}s"
    if proc.returncode == 0:
        return True, ""
    lines = (proc.stderr or proc.stdout).strip().splitlines()
    return False, lines[-1] if lines else f"exit code {proc.returncode}"


def score(case: EvalCase, output: BugFixOutput) -> CaseScore:
    files: dict[str, str] = dict(case.fix_input().files)
    checks: list[Check] = []

    try:
        patched = apply_patch(files, output.patch)
        checks.append(Check(name="patch applies", passed=True))
    except PatchError as exc:
        patched = {}
        checks.append(Check(name="patch applies", passed=False, detail=str(exc)))

    want = case.expect.file
    checks.append(
        Check(
            name=f"changes {want}",
            passed=want in patched and patched[want] != files.get(want),
            detail=f"changed: {', '.join(sorted(patched)) or 'nothing'}",
        )
    )

    if case.expect.keywords_any:
        text = f"{output.root_cause} {output.explanation}".lower()
        hit = any(k.lower() in text for k in case.expect.keywords_any)
        checks.append(
            Check(
                name="root cause named",
                passed=hit,
                detail="" if hit else f"none of: {', '.join(case.expect.keywords_any)}",
            )
        )

    if patched:
        ok, detail = run_check({**files, **patched}, case.expect.check)
    else:
        ok, detail = False, "no patched code to run"
    checks.append(Check(name="bug is fixed", passed=ok, detail=detail))
    return CaseScore(case=case.name, checks=checks)
