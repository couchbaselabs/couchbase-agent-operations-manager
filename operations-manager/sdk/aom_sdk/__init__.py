"""
Official Python client SDK for the Couchbase Agent Operations Manager.

    from aom_sdk import AOMClient

    client = AOMClient("http://localhost:8090", api_key="demo-support-agent-9f21")
    print(client.health())

Operators (and data pipelines) use the session-based AOMAdminClient to
load the Knowledge Base at scale, run warehouse syncs and manage roles:

    from aom_sdk import AOMAdminClient

    admin = AOMAdminClient("https://localhost:3111", username="admin", password="...", verify=False)
    job = admin.upload_knowledge("orders.csv.gz", allowed_roles=["finance_analyst"],
                                 dump={"source": "snowflake", "table_name": "SALES.PUBLIC.ORDERS"})
    admin.wait_for_job(job["job_id"])

See README.md in this package, the bundled examples/, or the appliance's
Tools -> Developer SDK page for the full quickstart.
"""
from .admin import AOMAdminClient
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

__version__ = "0.7.0"

__all__ = [
    "AOMClient",
    "AOMAdminClient",
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
