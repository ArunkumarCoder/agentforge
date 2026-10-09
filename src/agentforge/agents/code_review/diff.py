"""A small unified-diff parser: which files and new-file lines a diff touches.

Used to keep the model honest: findings on files that are not in the diff are
dropped, and line numbers outside the changed hunks are cleared rather than
trusted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_FILE = re.compile(r"^\+\+\+ (?:b/)?(.+?)\s*$")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass
class ChangedFile:
    path: str
    added_lines: set[int] = field(default_factory=set)
    hunk_lines: set[int] = field(default_factory=set)  # every new-file line shown in a hunk


def parse_unified_diff(diff: str) -> dict[str, ChangedFile]:
    files: dict[str, ChangedFile] = {}
    current: ChangedFile | None = None
    new_line = 0
    for raw in diff.splitlines():
        if match := _FILE.match(raw):
            path = match.group(1)
            current = None if path == "/dev/null" else files.setdefault(path, ChangedFile(path))
            continue
        if raw.startswith("--- ") or raw.startswith("diff --git"):
            continue
        if match := _HUNK.match(raw):
            new_line = int(match.group(1))
            continue
        if current is None or not raw:
            continue
        marker = raw[0]
        if marker == "+":
            current.added_lines.add(new_line)
            current.hunk_lines.add(new_line)
            new_line += 1
        elif marker == " ":
            current.hunk_lines.add(new_line)
            new_line += 1
        # "-" lines exist only in the old file; "\" is "No newline at end of file"
    return files


def diff_stats(files: dict[str, ChangedFile]) -> str:
    total = sum(len(f.added_lines) for f in files.values())
    return f"{len(files)} file(s), {total} added line(s)"
