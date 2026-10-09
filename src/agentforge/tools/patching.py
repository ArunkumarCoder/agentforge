"""Apply a unified diff to files in memory, and say exactly why when it doesn't fit.

Models write patches imperfectly: hunk headers with wrong line counts, hunks
at slightly wrong line numbers, missing ``a/`` / ``b/`` prefixes. This applier
is tolerant where it is safe to be and strict where it matters:

- line counts in ``@@`` headers are ignored; the hunk body is the truth,
- a hunk is placed where its context and removed lines match exactly,
  searching outward from the line number the header gives,
- if no exact match exists, the error names the hunk and shows the first
  line that could not be found, so the model can correct it.

Supports modifying files and creating new ones (``--- /dev/null``).
Nothing is written to disk.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from agentforge.core.errors import AgentForgeError

_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


class PatchError(AgentForgeError):
    """The patch is malformed or does not match the files."""


@dataclass
class Hunk:
    old_start: int
    lines: list[str] = field(default_factory=list)  # each starts with ' ', '-' or '+'

    @property
    def before(self) -> list[str]:
        return [line[1:] for line in self.lines if line[0] in " -"]

    @property
    def after(self) -> list[str]:
        return [line[1:] for line in self.lines if line[0] in " +"]


@dataclass
class FilePatch:
    old_path: str | None  # None for a new file
    new_path: str
    hunks: list[Hunk] = field(default_factory=list)


def _clean_path(raw: str) -> str | None:
    path = raw.split("\t")[0].strip()
    if path == "/dev/null":
        return None
    return path[2:] if path.startswith(("a/", "b/")) else path


def parse_patch(diff: str) -> list[FilePatch]:
    patches: list[FilePatch] = []
    lines = diff.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            old = _clean_path(line[4:])
            new = _clean_path(lines[i + 1][4:])
            if new is None:
                raise PatchError("deleting files is not supported")
            patches.append(FilePatch(old_path=old, new_path=new))
            i += 2
            continue
        if match := _HUNK.match(line):
            if not patches:
                raise PatchError("hunk found before any '--- / +++' file header")
            patches[-1].hunks.append(Hunk(old_start=int(match.group(1))))
        elif patches and patches[-1].hunks and line[:1] in (" ", "-", "+"):
            patches[-1].hunks[-1].lines.append(line)
        elif patches and patches[-1].hunks and line == "":
            # some generators drop the leading space of blank context lines
            patches[-1].hunks[-1].lines.append(" ")
        i += 1
    if not patches:
        raise PatchError("no file headers ('--- a/x' / '+++ b/x') found in the patch")
    for patch in patches:
        if not patch.hunks:
            raise PatchError(f"patch for '{patch.new_path}' has no hunks")
        for hunk in patch.hunks:  # trailing blank context added by the split above
            while hunk.lines and hunk.lines[-1] == " ":
                hunk.lines.pop()
    return patches


def _find(haystack: list[str], needle: list[str], near: int) -> int | None:
    """Index where ``needle`` occurs in ``haystack``, preferring positions close to ``near``."""
    if not needle:
        return min(max(near, 0), len(haystack))
    limit = len(haystack) - len(needle)
    for distance in range(0, max(limit, 0) + 1 + abs(near)):
        for start in (near - distance, near + distance):
            if 0 <= start <= limit and haystack[start : start + len(needle)] == needle:
                return start
    return None


def _apply_hunks(path: str, original: str, hunks: list[Hunk]) -> str:
    lines = original.split("\n")
    trailing_newline = original.endswith("\n")
    if trailing_newline:
        lines.pop()
    offset = 0
    for number, hunk in enumerate(hunks, 1):
        before, after = hunk.before, hunk.after
        start = _find(lines, before, hunk.old_start - 1 + offset)
        if start is None:
            missing = next((b for b in before if b not in lines), before[0] if before else "")
            raise PatchError(
                f"hunk {number} for '{path}' does not match the file "
                f"(could not find: {missing.strip()!r}). Copy context and '-' lines "
                "exactly from the current file."
            )
        lines[start : start + len(before)] = after
        offset += len(after) - len(before)
    return "\n".join(lines) + ("\n" if trailing_newline or not lines else "")


def apply_patch(files: Mapping[str, str], diff: str) -> dict[str, str]:
    """Return only the files the patch changes (or creates), with the patch applied."""
    result: dict[str, str] = {}
    for patch in parse_patch(diff):
        if patch.old_path is None:
            if patch.new_path in files:
                raise PatchError(
                    f"'{patch.new_path}' already exists; patch it instead of creating it"
                )
            body = [line[1:] for hunk in patch.hunks for line in hunk.lines if line[0] == "+"]
            result[patch.new_path] = "\n".join(body) + "\n"
            continue
        source_path = patch.old_path
        current = result.get(source_path, files.get(source_path))
        if current is None:
            known = ", ".join(sorted(files)) or "none"
            raise PatchError(
                f"patch changes '{source_path}', which was not provided (files: {known})"
            )
        result[patch.new_path] = _apply_hunks(
            source_path, current.replace("\r\n", "\n"), patch.hunks
        )
    return result


def touched_files(diff: str) -> list[str]:
    return [p.new_path for p in parse_patch(diff)]
