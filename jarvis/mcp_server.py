"""MCP stdio server for Jarvis's tool registry.

Exposes the full ContextAware registry (core + mind + learned tools) to any
MCP-compatible client (Claude Code, Claude Desktop, etc.) via stdin/stdout.
"""

import sys
from contextaware.mcp_stdio import serve_stdio
from jarvis.registry import get_registry


def main():
    """Run the MCP stdio server."""
    registry = get_registry()
    # lazy=True exposes only search/get/call meta-tools, keeping large catalogs
    # out of the client's context window. The client discovers tools via search.
    serve_stdio(registry.get_registry(), lazy=True)


if __name__ == "__main__":
    main()