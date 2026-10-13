"""Static security scanning: bandit for Python code plus a small scanner for hardcoded secrets.

Both run locally and offline. Bandit runs in its own process (``python -m bandit``) on a
temporary copy of the files, so the code under audit is read but **never executed**. The
secrets scanner is a handful of regular expressions with a placeholder filter, so it also
covers ``.env``, YAML, JSON and other non-Python files.

Findings from both are normalised into :class:`ScannerFinding`, numbered by the caller,
and de-duplicated: a secret reported by bandit on a line is not reported again by the
secrets scanner.
"""

from __future__ import annotations

import json
import math
import re
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from agentforge.tools.builtin.process import run_process
from agentforge.tools.pytest_runner import safe_env

BANDIT_TIMEOUT_S = 120.0
SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}
CATEGORIES = (
    "sql_injection",
    "command_injection",
    "code_injection",
    "path_traversal",
    "ssrf",
    "xss",
    "hardcoded_secret",
    "weak_crypto",
    "insecure_deserialization",
    "insecure_transport",
    "authn",
    "authz",
    "misconfiguration",
    "input_validation",
    "info_disclosure",
    "other",
)

# bandit test id -> category
_BANDIT_CATEGORY = {
    "B608": "sql_injection",
    "B602": "command_injection",
    "B604": "command_injection",
    "B605": "command_injection",
    "B606": "command_injection",
    "B607": "command_injection",
    "B603": "command_injection",
    "B307": "code_injection",
    "B102": "code_injection",
    "B301": "insecure_deserialization",
    "B302": "insecure_deserialization",
    "B506": "insecure_deserialization",
    "B403": "insecure_deserialization",
    "B105": "hardcoded_secret",
    "B106": "hardcoded_secret",
    "B107": "hardcoded_secret",
    "B324": "weak_crypto",
    "B303": "weak_crypto",
    "B304": "weak_crypto",
    "B311": "weak_crypto",
    "B501": "insecure_transport",
    "B504": "insecure_transport",
    "B309": "insecure_transport",
    "B310": "ssrf",
    "B201": "misconfiguration",
    "B104": "misconfiguration",
    "B108": "misconfiguration",
    "B113": "misconfiguration",
    "B701": "xss",
    "B702": "xss",
    "B703": "xss",
}
_SKIPPED_BANDIT = "B101"  # assert: noise in test code, no security meaning on its own
TEXT_SUFFIXES = {
    ".py", ".env", ".yml", ".yaml", ".json", ".toml", ".ini", ".cfg", ".conf", ".txt",
    ".properties", ".sh", ".js", ".ts", ".md",
}  # fmt: skip


@dataclass(frozen=True)
class ScannerFinding:
    """One raw finding from a scanner, before triage."""

    tool: str  # "bandit" or "secrets"
    rule: str  # bandit test id (B608) or secrets rule id (SEC001)
    title: str
    severity: str  # low | medium | high
    confidence: str  # low | medium | high
    file: str
    line: int
    code: str  # the source line, stripped
    category: str
    cwe: int | None = None


# --------------------------------------------------------------------------- bandit


async def scan_bandit(files: Mapping[str, str]) -> list[ScannerFinding]:
    """Run bandit over the Python files. Raises :class:`ScanError` if bandit itself fails."""
    sources = {p: c for p, c in files.items() if p.endswith(".py")}
    if not sources:
        return []
    with tempfile.TemporaryDirectory(prefix="agentforge-bandit-") as tmp:
        root = _write_files(tmp, sources)
        result = await run_process(
            [sys.executable, "-m", "bandit", "-f", "json", "-q", "-r", ".", "-s", _SKIPPED_BANDIT],
            cwd=str(root),
            timeout_s=BANDIT_TIMEOUT_S,
            env=safe_env(),
            inherit_env=False,
        )
    if result.timed_out:
        raise ScanError(f"bandit took longer than {BANDIT_TIMEOUT_S:.0f}s")
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise ScanError(
            f"bandit gave no report: {detail[-1] if detail else result.exit_code}"
        ) from exc
    found = []
    for item in report.get("results", []):
        path = str(item["filename"]).removeprefix("./").replace("\\", "/")
        line = int(item["line_number"])
        found.append(
            ScannerFinding(
                tool="bandit",
                rule=item["test_id"],
                title=item["issue_text"],
                severity=str(item["issue_severity"]).lower(),
                confidence=str(item["issue_confidence"]).lower(),
                file=path,
                line=line,
                code=_line(sources.get(path, ""), line),
                category=_BANDIT_CATEGORY.get(item["test_id"], "other"),
                cwe=(item.get("issue_cwe") or {}).get("id"),
            )
        )
    return found


def _write_files(tmp: str, sources: Mapping[str, str]) -> Path:
    root = Path(tmp).resolve()
    for rel, content in sources.items():
        target = (root / rel).resolve()
        if not target.is_relative_to(root):
            raise ScanError(f"refusing to write outside the scan directory: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


class ScanError(RuntimeError):
    """A scanner could not run."""


# --------------------------------------------------------------------------- secrets

_PLACEHOLDER = re.compile(
    r"change.?me|example|your[-_ ]|<.*>|\$\{|\{\{|xxx|dummy|placeholder|sample|test|todo|none|null",
    re.I,
)
_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("SEC001", "AWS access key id", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("SEC002", "Private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("SEC003", "GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("SEC004", "Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
)
_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?P<name>[\w.-]*(?:secret|password|passwd|api[_-]?key|access[_-]?key|auth[_-]?token|token)[\w.-]*)
    \s*[:=]\s*
    (?P<quote>["'])(?P<value>[^"'\n]{8,})(?P=quote)
    """
)
_MIN_ENTROPY = 3.0


def _entropy(text: str) -> float:
    counts = {c: text.count(c) for c in set(text)}
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


def scan_secrets(files: Mapping[str, str]) -> list[ScannerFinding]:
    """Hardcoded credentials in any text file. Placeholders and low-entropy values are ignored."""
    found: list[ScannerFinding] = []
    for path, content in files.items():
        if Path(path).suffix.lower() not in TEXT_SUFFIXES and not Path(path).name.startswith(
            ".env"
        ):
            continue
        for number, text in enumerate(content.splitlines(), start=1):
            for rule, title, pattern in _RULES:
                if pattern.search(text):
                    found.append(_secret(rule, title, "high", path, number, text))
            match = _ASSIGNMENT.search(text)
            if match and _looks_real(match.group("value")):
                found.append(
                    _secret("SEC005", "Hardcoded credential", "medium", path, number, text)
                )
    return found


def _looks_real(value: str) -> bool:
    return not _PLACEHOLDER.search(value) and _entropy(value) >= _MIN_ENTROPY


def _secret(
    rule: str, title: str, severity: str, path: str, line: int, text: str
) -> ScannerFinding:
    return ScannerFinding(
        tool="secrets",
        rule=rule,
        title=title,
        severity=severity,
        confidence="high" if rule != "SEC005" else "medium",
        file=path,
        line=line,
        code=_redact(text.strip()),
        category="hardcoded_secret",
        cwe=798,
    )


def _redact(text: str) -> str:
    """Keep the shape of the line, hide most of any secret value."""
    return re.sub(
        r"([\"'])([^\"'\n]{8,})\1",
        lambda m: f"{m.group(1)}{m.group(2)[:3]}…{m.group(1)}",
        text,
    )[:200]


# --------------------------------------------------------------------------- combined


async def scan(files: Mapping[str, str], *, min_severity: str = "low") -> list[ScannerFinding]:
    """All scanner findings at or above ``min_severity``, sorted by file and line, no overlaps."""
    bandit = await scan_bandit(files)
    taken = {(f.file, f.line) for f in bandit if f.category == "hardcoded_secret"}
    secrets = [f for f in scan_secrets(files) if (f.file, f.line) not in taken]
    floor = SEVERITY_RANK[min_severity]
    merged = [f for f in [*bandit, *secrets] if SEVERITY_RANK.get(f.severity, 1) >= floor]
    return sorted(merged, key=lambda f: (f.file, f.line, f.rule))


def _line(source: str, number: int) -> str:
    lines = source.splitlines()
    return lines[number - 1].strip() if 0 < number <= len(lines) else ""
