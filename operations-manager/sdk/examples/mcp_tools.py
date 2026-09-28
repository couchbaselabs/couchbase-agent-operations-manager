"""
Demonstrates the MCP tool integration: discover tools the way an MCP host
would see them, and invoke one by its MCP name.

Run with:
    AOM_BASE_URL=https://localhost:8090 AOM_API_KEY=demo-support-agent-9f21 python examples/mcp_tools.py
"""
import json
import os

from aom_sdk import AOMClient


def main() -> None:
    client = AOMClient(
        base_url=os.environ.get("AOM_BASE_URL", "https://localhost:8090"),
        api_key=os.environ.get("AOM_API_KEY"),
        # The bundled appliance serves HTTPS with a self-signed certificate
        # by default (see quickstart.py) - AOM_VERIFY_SSL=true once you've
        # installed a real one.
        verify=os.environ.get("AOM_VERIFY_SSL", "false").lower() == "true",
    )

    mcp_tools = client.discover_mcp_tools("look up a customer's open support tickets", top_k=3)
    print(f"Discovered {len(mcp_tools)} MCP-shaped tool(s):")
    for tool in mcp_tools:
        print(f"  - {tool['name']}: {tool['description']}")
        print(f"    inputSchema: {json.dumps(tool['inputSchema'])}")

    if mcp_tools:
        result = client.invoke_mcp_tool(mcp_tools[0]["name"], arguments={})
        print("\nInvoke result:", result["result"])

    print(
        "\nTo expose this appliance as a real local MCP server that any MCP host can "
        "attach to, install the optional extra and run the bundled bridge:\n"
        '    pip install "couchbase-aom-sdk[mcp]"\n'
        "    AOM_BASE_URL=... AOM_API_KEY=... python -m aom_sdk.mcp_server"
    )


if __name__ == "__main__":
    main()
