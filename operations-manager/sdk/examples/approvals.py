"""
Invoking a tool that needs a human decision first.

An appliance can put high-risk tools - dropping a table, issuing a refund,
deleting a user - behind human approval. RBAC cannot answer that question,
because the answer depends on the moment rather than the role: what is
being asked is not "may this agent ever do this" but "should this
particular call happen now".

`invoke()` on such a tool returns a pending approval instead of a result,
so the agent is never left holding a connection open while somebody
decides. This example shows both halves: the explicit poll, and the
blocking helper that does it for you.

    python examples/approvals.py
"""
import os
import time

from aom_sdk import AOMApprovalError, AOMClient

BASE_URL = os.environ.get("AOM_BASE_URL", "https://localhost:8090")
API_KEY = os.environ.get("AOM_API_KEY", "demo-admin-4c56")
VERIFY = os.environ.get("AOM_VERIFY_SSL", "false").lower() == "true"

# A critical-risk tool in the bundled sample catalog.
TOOL_ID = os.environ.get("AOM_APPROVAL_TOOL", "snowflake::manage_users")
ARGUMENTS = {"action": "disable", "user": "alice"}


def explicit_poll(client: AOMClient) -> None:
    """The shape most agent frameworks want: never block, come back later."""
    result = client.invoke(TOOL_ID, ARGUMENTS)

    if result.get("status") != "pending_approval":
        print("This tool did not need approval - it ran immediately.")
        return

    approval_id = result["approval"]["approval_id"]
    print(f"held for review: {approval_id}")
    print(f"  {result['detail']}\n")
    print("Approve or deny it on the appliance's Approvals page, then this will continue.\n")

    while True:
        status = client.approval(approval_id)["status"]
        print(f"  status: {status}")
        if status == "approved":
            break
        if status in ("denied", "expired"):
            print("\nNot proceeding.")
            return
        time.sleep(5)

    # The approval is bound to the exact arguments the reviewer saw, and is
    # single-use. Re-invoking with anything else raises rather than running.
    final = client.invoke(TOOL_ID, ARGUMENTS, approval_id=approval_id)
    print("\napproved and executed:", final.get("result"))

    try:
        client.invoke(TOOL_ID, ARGUMENTS, approval_id=approval_id)
    except AOMApprovalError as exc:
        print(f"reusing the same approval is refused, as it should be: {exc.detail}")


def blocking_helper(client: AOMClient) -> None:
    """One call that either returns a result or raises. Convenient, and
    deliberately something you ask for by name - it can block for as long
    as a person takes."""
    try:
        result = client.invoke_with_approval(TOOL_ID, ARGUMENTS, poll_seconds=5, timeout_seconds=300)
        print("approved and executed:", result.get("result"))
    except AOMApprovalError as exc:
        print(f"a reviewer refused it: {exc.detail}")


def main() -> None:
    client = AOMClient(BASE_URL, api_key=API_KEY, verify=VERIFY, agent_id="approvals-demo")
    print("=== explicit poll ===")
    explicit_poll(client)
    print("\n=== blocking helper ===")
    blocking_helper(client)


if __name__ == "__main__":
    main()
