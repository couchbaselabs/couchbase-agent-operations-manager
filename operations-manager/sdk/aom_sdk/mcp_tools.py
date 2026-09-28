"""
Convert AOM catalog tool documents into standard MCP tool definitions.

An AOM tool document already carries everything the Model Context Protocol
needs - it was ingested straight from a real MCP server's `tools/list`
response (see the appliance's `app/mcp_client.py` / `app/catalog_ingest.py`)
and stores the original `input_schema` verbatim. This module just reshapes
the field names, so a tool your role is authorized to see can be handed
directly to any MCP-compatible agent runtime or tool-calling API.

No dependency on the `mcp` package itself - these are plain dicts. For a
real local MCP *server* backed by AOM (so an MCP host can attach to this
appliance directly), see `aom_sdk.mcp_server` (optional `mcp` dependency).
"""
from __future__ import annotations

from typing import Iterable, List


def to_mcp_tool(tool_doc: dict) -> dict:
    """One AOM catalog tool document -> one MCP tool definition:
    ``{"name", "description", "inputSchema"}``.

    `name` is the AOM tool_id (``"<server_id>::<tool name>"``), not the
    bare downstream tool name - that's what `invoke()` / `invoke_mcp_tool()`
    expect, and it's guaranteed unique across every registered server,
    where the bare name alone might collide.
    """
    return {
        "name": tool_doc.get("tool_id") or tool_doc.get("name") or "",
        "description": tool_doc.get("description") or "",
        "inputSchema": tool_doc.get("input_schema") or {"type": "object", "properties": {}},
    }


def to_mcp_tools(tool_docs: Iterable[dict]) -> List[dict]:
    """Convert a list of AOM catalog tool documents in one call - see
    `to_mcp_tool()`."""
    return [to_mcp_tool(doc) for doc in tool_docs]
