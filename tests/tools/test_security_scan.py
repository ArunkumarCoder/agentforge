"""Bandit wrapper and secrets scanner."""

import pytest

from agentforge.tools import security_scan
from agentforge.tools.security_scan import ScanError, scan, scan_bandit, scan_secrets

# Built at runtime so no real-looking key sits in this file (pre-commit scans for them).
AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"
PEM_HEADER = "-----BEGIN " + "RSA PRIVATE KEY-----"
GITHUB_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"

SQL = (
    "import sqlite3\n\n\ndef find(db, name):\n"
    "    return db.execute(\"SELECT * FROM t WHERE n = '%s'\" % name)\n"
)


async def test_bandit_finds_sql_built_by_formatting() -> None:
    found = await scan_bandit({"app/db.py": SQL})
    hit = next(f for f in found if f.rule == "B608")
    assert (hit.file, hit.line, hit.category) == ("app/db.py", 5, "sql_injection")
    assert hit.code.startswith("return db.execute(")
    assert hit.cwe == 89


async def test_bandit_skips_asserts_and_non_python_files() -> None:
    files = {"a.py": "def f(x):\n    assert x\n", "notes.txt": "x = eval(input())"}
    assert await scan_bandit(files) == []
    assert await scan_bandit({"notes.txt": "hi"}) == []


async def test_bandit_refuses_paths_outside_the_scan_directory() -> None:
    with pytest.raises(ScanError, match="outside the scan directory"):
        await scan_bandit({"../evil.py": "x = 1"})


async def test_bandit_garbage_output_is_a_scan_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class Result:
        timed_out = False
        stdout = "not json"
        stderr = "boom"
        exit_code = 2

    async def fake(*_a: object, **_k: object) -> Result:
        return Result()

    monkeypatch.setattr(security_scan, "run_process", fake)
    with pytest.raises(ScanError, match="no report: boom"):
        await scan_bandit({"a.py": "x = 1"})


async def test_bandit_timeout_is_a_scan_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class Result:
        timed_out = True
        stdout = ""
        stderr = ""
        exit_code = -1

    async def fake(*_a: object, **_k: object) -> Result:
        return Result()

    monkeypatch.setattr(security_scan, "run_process", fake)
    with pytest.raises(ScanError, match="longer than"):
        await scan_bandit({"a.py": "x = 1"})


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        (f'key = "{AWS_KEY}"', "SEC001"),
        (PEM_HEADER, "SEC002"),
        (f'token = "{GITHUB_TOKEN}"', "SEC003"),
        ('webhook_token = "xoxb-1234567890-abcdefghij"', "SEC004"),
    ],
)
def test_secret_patterns(text: str, rule: str) -> None:
    (found,) = [f for f in scan_secrets({"config.py": text}) if f.rule == rule]
    assert found.severity == "high"
    assert found.category == "hardcoded_secret"
    assert found.line == 1


def test_generic_credentials_in_config_files_are_found_and_redacted() -> None:
    files = {"config/settings.yaml": 'db:\n  password: "r7Qz!m2Lw9Xp4Ks"\n'}
    (found,) = scan_secrets(files)
    assert (found.rule, found.line, found.severity) == ("SEC005", 2, "medium")
    assert "r7Qz!m2Lw9Xp4Ks" not in found.code
    assert "r7Q…" in found.code


@pytest.mark.parametrize(
    "line",
    [
        "API_KEY=changeme",
        'password = "your-password-here"',
        'token = "${GITHUB_TOKEN}"',
        'secret = "aaaaaaaaaaaa"',
        'password = "short"',
        "password = os.environ['PASSWORD']",
    ],
)
def test_placeholders_and_low_entropy_values_are_ignored(line: str) -> None:
    assert scan_secrets({".env.example": line, "a.py": line}) == []


def test_binary_like_files_are_skipped() -> None:
    assert scan_secrets({"logo.png": f'k = "{AWS_KEY}"'}) == []


async def test_combined_scan_dedupes_secrets_and_filters_by_severity() -> None:
    code = 'PASSWORD = "k9$Tz!w3Qp8vLm2xN7bR4cY6"\n'
    everything = await scan({"a.py": code})
    assert [(f.tool, f.rule) for f in everything] == [("bandit", "B105")]  # no SEC005 twin
    assert await scan({"a.py": code}, min_severity="high") == []
    mixed = await scan({"z.py": SQL, "a.py": code})
    assert [f.file for f in mixed] == ["a.py", "z.py"]  # sorted by file
