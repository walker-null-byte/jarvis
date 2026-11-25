"""Tiny stdio MCP server used by tests/test_mcp_client.py (and manual UI checks)."""

import os

try:
    from mcp.server.mcpserver import MCPServer as Server  # mcp 2.x
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as Server

from mcp.types import ToolAnnotations

server = Server("echo")


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def echo(text: str) -> str:
    """Echo the text back."""
    return f"echo: {text}"


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers (not marked read-only)."""
    return a + b


@server.tool(annotations=ToolAnnotations(destructiveHint=True))
def wipe(name: str) -> str:
    """Pretend to delete something."""
    return f"wiped {name}"


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def env_value(name: str) -> str:
    """Return an environment variable of the server process."""
    return os.environ.get(name, "")


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def boom() -> str:
    """Always fails."""
    raise ValueError("boom failed on purpose")


if __name__ == "__main__":
    server.run("stdio")
