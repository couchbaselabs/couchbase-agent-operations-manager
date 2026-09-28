"""
Grouping an agent's work into one traced run.

Every call through the SDK is traced. Left alone, each is its own
single-span trace - visible on the appliance's Traces page, but telling
you nothing about what it belonged to. `client.run()` gives every call
inside the block one trace ID, so the run reads as what the agent actually
did: the query it asked with, the tools it was shown, the one it chose,
what came back, and what it then asked a model.

That is the difference between a trace you can debug from and a pile of
unrelated single-call traces - and because the spans land in the same
Couchbase cluster as the tool catalog, the cache and agent memory, the
run can be read back against the catalog it actually ran against.

    python examples/traced_run.py
"""
import os

from aom_sdk import AOMClient

BASE_URL = os.environ.get("AOM_BASE_URL", "https://localhost:8090")
API_KEY = os.environ.get("AOM_API_KEY", "demo-support-agent-9f21")
VERIFY = os.environ.get("AOM_VERIFY_SSL", "false").lower() == "true"


def main() -> None:
    client = AOMClient(
        BASE_URL,
        api_key=API_KEY,
        verify=VERIFY,
        # Both are optional and both show up on the Traces page. agent_id
        # answers "which agent was this", session_id answers "which
        # conversation" - worth setting even in a script, because a run you
        # cannot attribute is a run you cannot act on.
        agent_id="ticket-triage",
        session_id="demo-session",
    )

    with client.run(name="resolve a customer's open tickets") as run:
        print(f"trace_id = {run.trace_id}\n")

        discovered = client.discover("look up a customer's open support tickets")
        tools = discovered["tools"]
        print(f"discovered {len(tools)} tool(s) this role may use:")
        for tool in tools:
            print(f"  - {tool['tool_id']}")
        if not tools:
            print("\nNothing discoverable for this role - nothing else to trace.")
            return

        chosen = tools[0]["tool_id"]
        print(f"\ninvoking {chosen}")
        result = client.invoke(chosen, arguments={})

        # A held tool returns a pending approval rather than a result. The
        # run records that too - "waited for a person" is part of what the
        # agent did.
        if result.get("status") == "pending_approval":
            print(f"  held for approval: {result['approval']['approval_id']}")
        else:
            if result.get("hijack_warning"):
                print("  response was flagged by the hijack detector - do not trust it blindly")
            print("  returned a result")

        answer = client.complete("Summarise that customer's situation in two sentences.")
        print(f"\ncompletion: cache {answer['cache']['status']}")
        if answer["cache"].get("reason"):
            print(f"  reason: {answer['cache']['reason']}")

    print(
        f"\nOpen Traces in the dashboard and find {run.trace_id[:10]} to see all of that "
        f"as one run, with the catalog state of every tool it touched."
    )


if __name__ == "__main__":
    main()
