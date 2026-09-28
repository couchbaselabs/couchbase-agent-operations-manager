"""Thin helpers for talking to downstream MCP servers over Streamable HTTP -
the same client machinery a real MCP-aware agent would use, just wrapped so
the operations manager can call it on the caller's behalf after authorization.

Two things ride along with every request beyond the MCP payload itself:

  - The headers built by app/server_auth.py: the registered server's own
    credential, plus a signed assertion of which role and subject this call
    is actually for. Without the latter a downstream server sees only "the
    gateway" and can neither apply its own per-principal policy nor log the
    real caller.
  - A timeout. A downstream that never answers would otherwise hold a
    request here open indefinitely, which is how one unhealthy MCP server
    becomes an outage of the whole gateway. The bound comes from the
    governance policy (see app/governance.py) so it is one operator-visible
    setting rather than a constant buried here.

`read_timeout_seconds` on the session covers the MCP request itself, while
the transport `timeout` covers establishing and writing to the connection;
both are set from the same value so a hung downstream fails at a
predictable moment rather than at whichever bound happens to be lower.
"""
import json
import logging
from datetime import timedelta

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

logger = logging.getLogger("operations-manager.mcp_client")

DEFAULT_TIMEOUT_SECONDS = 30


async def list_tools(mcp_url: str, headers: dict | None = None, timeout_seconds: int | None = None) -> list[dict]:
    """Return [{name, description, input_schema}, ...] for one MCP server."""
    timeout = timeout_seconds or DEFAULT_TIMEOUT_SECONDS
    async with streamablehttp_client(mcp_url, headers=headers or None, timeout=timeout) as (read, write, _):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=timeout)) as session:
            await session.initialize()
            result = await session.list_tools()
            return [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "input_schema": t.inputSchema or {},
                }
                for t in result.tools
            ]


async def call_tool(
    mcp_url: str,
    tool_name: str,
    arguments: dict,
    headers: dict | None = None,
    timeout_seconds: int | None = None,
) -> dict:
    """Invoke one tool on one MCP server and return its structured result
    (falling back to the first text content block if a tool has no
    structured output)."""
    timeout = timeout_seconds or DEFAULT_TIMEOUT_SECONDS
    async with streamablehttp_client(mcp_url, headers=headers or None, timeout=timeout) as (read, write, _):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=timeout)) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
            if result.isError:
                text = result.content[0].text if result.content else "tool call failed"
                raise RuntimeError(text)
            if result.structuredContent is not None:
                return result.structuredContent
            if result.content:
                text = getattr(result.content[0], "text", str(result.content[0]))
                try:
                    return json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    return {"text": text}
            return {}
