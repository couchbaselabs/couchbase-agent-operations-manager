"""
Demonstrates agent memory: store a few facts about a user, then recall the
most *relevant* one for a new question rather than just the most recent
one - the same vector-search idea `discover()` runs over the tool catalog,
applied to one user's memory instead.

Run with:
    AOM_BASE_URL=https://localhost:8090 AOM_API_KEY=demo-support-agent-9f21 python examples/agent_memory.py
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

    user_id = "demo-user-42"
    session_id = "session-1"

    # A durable fact about the user (outlives this session).
    client.add_memory(
        user_id, "Prefers responses in metric units, not imperial.",
        memory_type="profile",
    )

    # What was actually said in this session.
    client.add_memory(
        user_id, "Asked how to reset a forgotten account password.",
        session_id=session_id, metadata={"channel": "chat"},
    )
    client.add_memory(
        user_id, "Mentioned their order (#48213) arrived damaged.",
        session_id=session_id, metadata={"channel": "chat"},
    )

    print(f"All memory for {user_id}:")
    for entry in client.list_memory(user_id):
        print(f"  [{entry['memory_type']}] {entry['content']}")

    print("\nSemantic recall for 'the customer's order problem':")
    for entry in client.search_memory(user_id, "the customer's order problem", top_k=2):
        print(f"  similarity={entry['similarity']:.3f}  {entry['content']}")

    removed = client.clear_memory(user_id, session_id=session_id)
    print(f"\nCleared {removed} session-scoped entr(ies); profile memory is untouched:")
    for entry in client.list_memory(user_id):
        print(f"  [{entry['memory_type']}] {entry['content']}")


if __name__ == "__main__":
    main()
