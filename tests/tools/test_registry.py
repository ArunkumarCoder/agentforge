"""Tests for ToolRegistry."""

import pytest

from agentforge.core import ToolCall
from agentforge.tools import DuplicateToolError, ToolContext, ToolPermission, ToolRegistry, tool


@tool
def read_note(title: str) -> str:
    """Read a note."""
    return f"note:{title}"


@tool(permission=ToolPermission.WRITE)
def write_note(title: str, body: str) -> str:
    """Write a note."""
    return "saved"


@tool(permission=ToolPermission.NETWORK)
def fetch_url(url: str) -> str:
    """Fetch a URL."""
    return "<html>"


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry([write_note, read_note, fetch_url])


def test_names_and_specs_are_sorted(registry: ToolRegistry) -> None:
    assert registry.names == ["fetch_url", "read_note", "write_note"]
    assert [s.name for s in registry.specs()] == registry.names
    assert len(registry) == 3
    assert "read_note" in registry
    assert registry.get("read_note") is read_note
    assert registry.get("nope") is None


def test_duplicates_are_rejected(registry: ToolRegistry) -> None:
    with pytest.raises(DuplicateToolError, match="read_note"):
        registry.register(read_note)


def test_subset(registry: ToolRegistry) -> None:
    assert registry.subset(["read_note"]).names == ["read_note"]
    with pytest.raises(KeyError):
        registry.subset(["missing"])


def test_with_permissions(registry: ToolRegistry) -> None:
    safe = registry.with_permissions({ToolPermission.READ})
    assert safe.names == ["read_note"]
    assert {t.name for t in registry} == set(registry.names)


async def test_dispatch_known_tool(registry: ToolRegistry) -> None:
    result = await registry.dispatch(
        ToolCall(id="c1", name="read_note", arguments={"title": "todo"}), ToolContext()
    )
    assert result.content == "note:todo"


async def test_dispatch_unknown_tool_lists_alternatives(registry: ToolRegistry) -> None:
    result = await registry.dispatch(ToolCall(id="c1", name="delete_note"), ToolContext())
    assert result.is_error
    assert result.name == "delete_note"
    assert "Unknown tool 'delete_note'" in result.content
    assert "fetch_url, read_note, write_note" in result.content
