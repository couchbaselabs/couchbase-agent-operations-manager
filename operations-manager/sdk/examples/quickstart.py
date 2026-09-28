"""
Minimal end-to-end example: check the appliance is up, discover a tool for
a task, invoke it, and run one cached LLM completion - the handful of
things almost every agent integration needs from the Couchbase Agent
Operations Manager.

Run with:
    AOM_BASE_URL=https://localhost:8090 AOM_API_KEY=demo-support-agent-9f21 python examples/quickstart.py

The bundled Docker Compose stack serves HTTPS with a self-signed
certificate out of the box, which AOMClient's default verify=True would
reject - these examples pass AOM_VERIFY_SSL=false (the default below) to
skip that check. Once you've installed a real certificate (see
docker-compose.yml), set AOM_VERIFY_SSL=true, or leave it unset and rely
on AOMClient's own secure-by-default verify=True.
"""
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

    print("Health:", client.health())

    discovered = client.discover("look up a customer's open support tickets", top_k=3)
    print(f"Discovered {len(discovered['tools'])} tool(s) for role '{discovered['role']}':")
    for tool in discovered["tools"]:
        print(f"  - {tool['tool_id']}")

    if discovered["tools"]:
        top_tool = discovered["tools"][0]
        result = client.invoke(top_tool["tool_id"], arguments={})
        print("Invoke result:", result["result"])
        if result["hijack_warning"]:
            print("WARNING - response flagged:", result["hijack_warning"])
    else:
        print("No tool matched - nothing to invoke.")

    answer = client.complete("Summarize the AOM appliance's threat model in two sentences.")
    print("LLM completion:", answer["response"])
    print("Cache status:", answer["cache"]["status"], "| tokens spent:", answer["usage"]["total_tokens"])


if __name__ == "__main__":
    main()
