"""In-memory unified-diff application: tolerant of sloppy headers, strict about content."""

import pytest

from agentforge.tools.patching import PatchError, apply_patch, parse_patch, touched_files

SOURCE = "def add(a, b):\n    return a - b\n\n\ndef sub(a, b):\n    return a - b\n"

FIX = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""


def test_applies_a_simple_hunk() -> None:
    patched = apply_patch({"calc.py": SOURCE, "other.py": "x = 1\n"}, FIX)
    assert list(patched) == ["calc.py"]  # only changed files come back
    assert patched["calc.py"].startswith("def add(a, b):\n    return a + b\n")
    assert patched["calc.py"].endswith("def sub(a, b):\n    return a - b\n")


def test_wrong_counts_and_line_numbers_are_tolerated() -> None:
    sloppy = FIX.replace("@@ -1,2 +1,2 @@", "@@ -40,7 +40,9 @@").replace("a/calc", "calc")
    assert apply_patch({"calc.py": SOURCE}, sloppy)["calc.py"].count("a + b") == 1


def test_picks_the_occurrence_nearest_the_header_line() -> None:
    second = """--- a/calc.py
+++ b/calc.py
@@ -5,2 +5,2 @@
 def sub(a, b):
-    return a - b
+    return a - b  # checked
"""
    patched = apply_patch({"calc.py": SOURCE}, second)["calc.py"]
    assert patched.splitlines()[1] == "    return a - b"
    assert patched.splitlines()[5] == "    return a - b  # checked"


def test_multiple_hunks_track_the_line_offset() -> None:
    diff = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,3 @@
+# calculator
 def add(a, b):
     return a - b
@@ -5,2 +6,2 @@
 def sub(a, b):
-    return a - b
+    return b - a
"""
    patched = apply_patch({"calc.py": SOURCE}, diff)["calc.py"]
    assert patched.splitlines()[0] == "# calculator"
    assert patched.splitlines()[-1] == "    return b - a"


def test_blank_context_lines_without_a_leading_space() -> None:
    diff = (
        "--- a/calc.py\n+++ b/calc.py\n@@ -2,4 +2,4 @@\n"
        "     return a - b\n\n\n-def sub(a, b):\n+def subtract(a, b):\n"
    )
    assert "def subtract(a, b):" in apply_patch({"calc.py": SOURCE}, diff)["calc.py"]


def test_crlf_files_and_new_files() -> None:
    diff = FIX + "--- /dev/null\n+++ b/test_calc.py\n@@ -0,0 +1,2 @@\n+def test_add():\n+    pass\n"
    patched = apply_patch({"calc.py": SOURCE.replace("\n", "\r\n")}, diff)
    assert "return a + b" in patched["calc.py"]
    assert patched["test_calc.py"] == "def test_add():\n    pass\n"
    assert touched_files(diff) == ["calc.py", "test_calc.py"]


@pytest.mark.parametrize(
    ("files", "diff", "message"),
    [
        ({"calc.py": SOURCE}, "just some text", "no file headers"),
        ({"calc.py": SOURCE}, "--- a/calc.py\n+++ b/calc.py\n", "has no hunks"),
        ({"calc.py": SOURCE}, "@@ -1 +1 @@\n-x\n+y\n", "before any"),
        ({"calc.py": SOURCE}, "--- a/calc.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n", "deleting"),
        ({}, FIX, "which was not provided"),
        ({"calc.py": SOURCE}, FIX.replace(" def add", " def plus"), "could not find: 'def plus"),
        (
            {"test_calc.py": ""},
            "--- /dev/null\n+++ b/test_calc.py\n@@ -0,0 +1 @@\n+x = 1\n",
            "already exists",
        ),
    ],
)
def test_errors_say_what_is_wrong(files: dict[str, str], diff: str, message: str) -> None:
    with pytest.raises(PatchError, match=message):
        apply_patch(files, diff)


def test_parse_patch_strips_prefixes_and_timestamps() -> None:
    (patch,) = parse_patch("--- a/x.py\t2026-01-01\n+++ b/x.py\t2026-01-02\n@@ -1 +1 @@\n-a\n+b\n")
    assert (patch.old_path, patch.new_path) == ("x.py", "x.py")
    assert patch.hunks[0].before == ["a"]
    assert patch.hunks[0].after == ["b"]
