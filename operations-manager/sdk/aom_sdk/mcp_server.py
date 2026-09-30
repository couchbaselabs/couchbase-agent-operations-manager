"""
Expose the Couchbase Agent Operations Manager as a real MCP server.

Most MCP hosts (Claude Desktop, Claude Code, and other MCP-compatible
agent runtimes) know how to talk to an MCP server, not to this
appliance's own REST API. This module bridges the two: it stands up a
local MCP server (stdio transport) that maps `list_tools` to AOM's
catalog and `call_tool` to AOM's re-checked invoke - so any MCP host can
reach every tool your API key's role is authorized for, still governed by
the same RBAC and audit trail as a direct API integration. Discovery is
intentionally broad here (the whole authorized catalog, not a per-query
top-k) because an MCP host - not this bridge - is what decides which tool
to call for a given task.

Requires the optional `mcp` dependency:

    pip install "couchbase-aom-sdk[mcp]"

Run directly:

    AOM_BASE_URL=https://localhost:8090 AOM_API_KEY=demo-support-agent-9f21 \\
        python -m aom_sdk.mcp_server

Then point any MCP-compatible host at this process over stdio. The bundled
appliance serves HTTPS with a self-signed certificate by default, so this
skips verifying it unless AOM_VERIFY_SSL=true (see AOMClient's own
``verify`` parameter) - set that once you've installed a real certificate.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Dict, List, Optional

from .client import AOMClient
from .mcp_tools import to_mcp_tool

logger = logging.getLogger("aom_sdk.mcp_server")

_IMPORT_ERROR = (
    "The MCP server bridge needs the optional 'mcp' package. Install it with:\n"
    '    pip install "couchbase-aom-sdk[mcp]"\n'
)

DEFAULT_SERVER_NAME = "couchbase-agent-operations-manager"

# MCP tool names are restricted to A-Z a-z 0-9 _ - . ; AOM tool_ids use
# "server_id::tool_name", which isn't valid on its own - see SEP-986.
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_.\-]")


def _require_mcp() -> None:
    try:
        import mcp.server.stdio  # noqa: F401
        import mcp.types  # noqa: F401
        from mcp.server import Server  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised only when mcp isn't installed
        raise ImportError(_IMPORT_ERROR) from exc


def _safe_tool_name(tool_id: str) -> str:
    return _UNSAFE_NAME_CHARS.sub("_", tool_id)


class AOMMCPServer:
    """Wraps an `AOMClient` as an `mcp.server.Server`.

    The catalog is re-fetched on every `list_tools` call, so a tool
    registered, quarantined, or re-authorized after this process started
    is picked up without a restart - the appliance, not this process, is
    the source of truth for what your role can see.
    """

    def __init__(self, client: AOMClient, name: str = DEFAULT_SERVER_NAME):
        _require_mcp()
        self.client = client
        self.name = name
        self._tool_id_by_safe_name: Dict[str, str] = {}
        self._server = self._build_server()

    def _build_server(self):
        from mcp.server import Server
        import mcp.types as types

        server = Server(self.name)

        @server.list_tools()
        async def _list_tools() -> List["types.Tool"]:
            catalog = await asyncio.to_thread(self.client.catalog)
            self._tool_id_by_safe_name.clear()
            tools = []
            for tool_doc in catalog:
                mcp_tool = to_mcp_tool(tool_doc)
                safe_name = _safe_tool_name(mcp_tool["name"])
                self._tool_id_by_safe_name[safe_name] = mcp_tool["name"]
                tools.append(types.Tool(name=safe_name, description=mcp_tool["description"], inputSchema=mcp_tool["inputSchema"]))
            return tools

        @server.call_tool()
        async def _call_tool(name: str, arguments: dict):
            tool_id = self._tool_id_by_safe_name.get(name, name)
            try:
                result = await asyncio.to_thread(self.client.invoke, tool_id, arguments or {})
            except Exception as exc:  # noqa: BLE001 - surfaced to the MCP host as a tool error, not a crash
                logger.warning("AOM invoke('%s') failed: %s", tool_id, exc)
                raise
            payload = result.get("result")
            # dict -> structured content (and auto-generated text content);
            # anything else, wrap as text so call_tool always gets a
            # normalizable return value.
            if isinstance(payload, dict):
                return payload
            return [types.TextContent(type="text", text=json.dumps(payload))]

        return server

    async def run_stdio(self) -> None:
        """Serve over stdio until the client disconnects - the standard
        transport for a local MCP server invoked as a subprocess."""
        import mcp.server.stdio

        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await self._server.run(read_stream, write_stream, self._server.create_initialization_options())


def main(argv: Optional[List[str]] = None) -> None:
    _require_mcp()
    logging.basicConfig(level=os.environ.get("AOM_MCP_LOG_LEVEL", "WARNING"))

    base_url = os.environ.get("AOM_BASE_URL", "https://localhost:8090")
    api_key = os.environ.get("AOM_API_KEY")
    if not api_key:
        raise SystemExit(
            "Set AOM_API_KEY to the RBAC role's API key this MCP server should act as "
            "(e.g. AOM_API_KEY=demo-support-agent-9f21)."
        )
    verify = os.environ.get("AOM_VERIFY_SSL", "false").lower() == "true"

    client = AOMClient(base_url=base_url, api_key=api_key, verify=verify)
    server = AOMMCPServer(client)
    asyncio.run(server.run_stdio())


if __name__ == "__main__":
    main()
