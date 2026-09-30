"""
Official Python client SDK for the Couchbase Agent Operations Manager.

    from aom_sdk import AOMClient

    client = AOMClient("http://localhost:8090", api_key="demo-support-agent-9f21")
    print(client.health())

See README.md in this package, the bundled examples/, or the appliance's
Tools -> Developer SDK page for the full quickstart.
"""
from .client import AgentRun, AOMClient
from .exceptions import (
    AOMApprovalError,
    AOMAuthenticationError,
    AOMAuthorizationError,
    AOMConnectionError,
    AOMError,
    AOMGuardrailError,
    AOMNotFoundError,
    AOMRateLimitError,
    AOMScopeError,
    AOMServerError,
    AOMTimeoutError,
)
from .mcp_tools import to_mcp_tool, to_mcp_tools

__version__ = "0.5.0"

__all__ = [
    "AOMClient",
    "AgentRun",
    "AOMError",
    "AOMAuthenticationError",
    "AOMAuthorizationError",
    "AOMConnectionError",
    "AOMNotFoundError",
    "AOMRateLimitError",
    "AOMGuardrailError",
    "AOMScopeError",
    "AOMApprovalError",
    "AOMTimeoutError",
    "AOMServerError",
    "to_mcp_tool",
    "to_mcp_tools",
    "__version__",
]
