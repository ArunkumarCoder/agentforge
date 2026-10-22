"""Limits for running code that a model wrote.

``untrusted_limits()`` is what the tools pass to ``run_process`` for generated tests, profile
scripts and SQLAlchemy models. The numbers come from ``AGENTFORGE_SANDBOX__*``; set
``AGENTFORGE_SANDBOX__ENABLED=false`` to turn the CPU, memory and file limits off (the output
cap and process-group kill stay on, they are not optional).

This is a safety net against runaway code (an infinite loop, a huge allocation, a program
that fills the disk or the terminal), not a security boundary. See ``docs/SECURITY.md``.
"""

from __future__ import annotations

from agentforge.config import SandboxSettings, Settings, get_settings
from agentforge.tools.builtin.process import DEFAULT_OUTPUT_MB, Limits


def limits_from(settings: Settings) -> Limits:
    return _from_box(settings.sandbox)


def _from_box(box: SandboxSettings) -> Limits:
    if not box.enabled:
        return Limits(output_mb=box.output_mb)
    return Limits(
        cpu_s=box.cpu_s, memory_mb=box.memory_mb, file_mb=box.file_mb, output_mb=box.output_mb
    )


def untrusted_limits() -> Limits:
    """The limits for running model-written code, from the current settings."""
    try:
        return limits_from(get_settings())
    except Exception:  # a broken .env must not turn the limits off, only use the defaults
        return _from_box(SandboxSettings())


__all__ = ["DEFAULT_OUTPUT_MB", "Limits", "limits_from", "untrusted_limits"]
