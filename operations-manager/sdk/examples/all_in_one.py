"""
Everything an agent does through the Couchbase Agent Operations Manager, in
one script - the single file to hand a developer who is wiring the SDK into
their agent for the first time.

It walks the whole client surface in the order an agent actually uses it:

    1. health                    - is the appliance up
    2. catalog -> discover       - find a tool, read its input_schema
    3. invoke                    - call it with *real* arguments (not {})
    4. complete                  - a cached LLM completion, twice, to see a hit
    5. cached_context            - the get-or-compute cache for fetched data
    6. agent memory              - add / search / list / clear
    7. knowledge search          - RBAC-filtered retrieval
    8. rag_query                 - a registered RAG application (optional)

All of it runs inside one `client.run()` block, so the appliance's Traces
page shows it as a single run rather than nine unrelated calls. Each stage
is independent: a stage that isn't set up on your appliance (no RAG app, no
knowledge documents, a tool that needs human approval) is reported and
skipped, and the rest still runs. Errors are caught by SDK exception type so
you can see which one each situation raises.

Run with:
    AOM_BASE_URL=https://localhost:8090 AOM_API_KEY=demo-support-agent-9f21 python examples/all_in_one.py

Optional environment:
    AOM_VERIFY_SSL=true        once a real certificate is installed (default: false, for the
                               self-signed cert the bundled appliance ships with)
    AOM_QUERY="..."            the natural-language task to discover a tool for
    AOM_TOOL_ID=server::tool   invoke this exact tool instead of the top discover hit
    AOM_TOOL_ARGS='{"k": 1}'   JSON arguments for the invoke. Read the tool's input_schema
                               printed by stage 2 - most tools have required fields, and an
                               invoke with {} fails *downstream*, which surfaces here as a 502.
    AOM_RAG_APP=app-id         a RAG application registered on the appliance (stage 8)
    AOM_LLM_PROVIDER=openai    pin the provider (and optionally AOM_LLM_MODEL) for every complete()
    AOM_LLM_MODEL=gpt-4o-mini  this client makes. Unset, the appliance's policy decides - the
                               global default or the per-role / per-agent override for this key -
                               and stage 4 prints which one applied.
"""
import json
import os
import time
from typing import Optional

from aom_sdk import (
    AOMApprovalError,
    AOMAuthenticationError,
    AOMAuthorizationError,
    AOMClient,
    AOMConnectionError,
    AOMError,
    AOMGuardrailError,
    AOMNotFoundError,
    AOMRateLimitError,
    AOMServerError,
)

BASE_URL = os.environ.get("AOM_BASE_URL", "https://localhost:8090")
API_KEY = os.environ.get("AOM_API_KEY", "demo-support-agent-9f21")
VERIFY = os.environ.get("AOM_VERIFY_SSL", "false").lower() == "true"
QUERY = os.environ.get("AOM_QUERY", "look up a customer's open support tickets")
TOOL_ID = os.environ.get("AOM_TOOL_ID")
TOOL_ARGS = json.loads(os.environ.get("AOM_TOOL_ARGS", "{}"))
RAG_APP = os.environ.get("AOM_RAG_APP")
LLM_PROVIDER = os.environ.get("AOM_LLM_PROVIDER") or None
LLM_MODEL = os.environ.get("AOM_LLM_MODEL") or None

USER_ID = "user-42"
SESSION_ID = f"all-in-one-{int(time.time())}"


def stage(title: str):
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------------------
# 1. health
# ---------------------------------------------------------------------------
def health(client: AOMClient) -> None:
    stage("1. health")
    # No API key needed - a good startup check before an agent does
    # anything else. (This key's own role comes back from discover() and
    # catalog() below; the full role list is an operator view.)
    print("health:", client.health())


# ---------------------------------------------------------------------------
# 2. catalog -> discover (read the input_schema before you invoke anything)
# ---------------------------------------------------------------------------
def pick_tool(client: AOMClient) -> Optional[dict]:
    stage("2. catalog -> discover")

    # catalog() is every tool this key can invoke, *with* input_schema.
    # discover() ranks by vector similarity but carries no schema, so the
    # two are joined here - that join is what tells you which arguments a
    # tool needs before you spend a call finding out the hard way.
    schemas = {t["tool_id"]: t for t in client.catalog()}
    print(f"catalog: {len(schemas)} tool(s) invokable by this key")

    if TOOL_ID:
        chosen = schemas.get(TOOL_ID)
        if not chosen:
            print(f"AOM_TOOL_ID={TOOL_ID!r} is not in this key's catalog - skipping invoke")
            return None
    else:
        discovered = client.discover(QUERY, top_k=3)
        tools = discovered["tools"]
        print(f"discover({QUERY!r}) -> {len(tools)} match(es) for role {discovered['role']!r}:")
        for t in tools:
            print(f"  - {t['tool_id']}")
        if not tools:
            print("nothing matched - set AOM_QUERY to something in your catalog")
            return None
        # The top hit is the closest *description*, not necessarily the
        # right tool. A real agent reasons over the candidates; this script
        # takes the first, and prints its schema so you can see whether
        # that was a sensible choice.
        chosen = schemas.get(tools[0]["tool_id"], tools[0])

    schema = chosen.get("input_schema") or {}
    required = schema.get("required") or []
    print(f"\nchosen: {chosen['tool_id']}")
    print(f"  description: {chosen.get('description', '')[:160]}")
    print(f"  input_schema: {json.dumps(schema, indent=2)}")
    if required:
        print(f"  required arguments: {required}")
    return chosen


# ---------------------------------------------------------------------------
# 3. invoke - with real arguments
# ---------------------------------------------------------------------------
def invoke(client: AOMClient, tool: dict) -> None:
    stage("3. invoke")
    required = (tool.get("input_schema") or {}).get("required") or []
    missing = [r for r in required if r not in TOOL_ARGS]
    if missing:
        print(f"not invoking {tool['tool_id']}: required argument(s) {missing} not provided.")
        print("  Set AOM_TOOL_ARGS to a JSON object with those keys and run again.")
        print("  (Invoking with {} would pass authorization here and then fail inside the")
        print("   downstream MCP server - which the SDK reports as a 502 AOMServerError.)")
        return

    try:
        result = client.invoke(tool["tool_id"], arguments=TOOL_ARGS)
    except AOMApprovalError as exc:
        print(f"approval refused/expired: {exc.detail}")
        return
    except AOMGuardrailError as exc:
        print(f"guardrails refused these arguments: {exc.detail}")
        return
    except AOMAuthorizationError as exc:
        print(f"this key may not invoke that tool: {exc.detail}")
        return
    except AOMServerError as exc:
        # The gateway reached the tool's MCP server and the call failed
        # there. The detail carries the downstream cause (401, connection
        # refused, a tool error) - that is where to look, not at AOM.
        print(f"downstream failure: {exc}")
        return

    if result.get("status") == "pending_approval":
        print(f"held for human approval: {result['approval']['approval_id']}")
        print("  approve it on the Approvals page, then re-invoke with approval_id=...,")
        print("  or use client.invoke_with_approval() to block until a decision. See examples/approvals.py")
        return

    print(f"result ({result['latency_ms']} ms): {json.dumps(result['result'])[:400]}")
    if result.get("hijack_warning"):
        # Never trust `result` blindly: the live payload was scanned for
        # prompt-injection signals and flagged.
        print("WARNING - response flagged for possible prompt injection:", result["hijack_warning"])


# ---------------------------------------------------------------------------
# 4. cached LLM completion - the second call is served from Couchbase
# ---------------------------------------------------------------------------
def completion(client: AOMClient) -> None:
    stage("4. cached LLM completion")
    prompt = "In one sentence, what does an agent operations manager do?"
    for n in (1, 2):
        try:
            ans = client.complete(prompt, namespace="all-in-one")
        except AOMRateLimitError as exc:
            print(f"rate limited ({exc.limit}); retry in {exc.retry_after}s")
            return
        except AOMError as exc:
            print(f"completion unavailable (no LLM provider configured?): {exc}")
            return
        print(f"call {n}: cache={ans['cache']['status']:<13} tokens={ans['usage']['total_tokens']:<5} "
              f"cost=${ans['cost_usd']:.5f}")
    # Who answered, and why: the client default if one was set, otherwise
    # the appliance's policy - `policy` names a per-role / per-agent
    # override when one applied, and is None under the global default.
    pol = ans.get("policy")
    print(f"served by {ans['provider']} / {ans['model']}"
          + (f" via override '{pol['name']}' ({pol['match']['type']} {pol['match']['value']})" if pol
             else " via the global policy" if not LLM_PROVIDER else " (client default)"))
    print("answer:", ans["response"][:200])


# ---------------------------------------------------------------------------
# 5. context cache - for data the agent fetches itself
# ---------------------------------------------------------------------------
def context_cache(client: AOMClient) -> None:
    stage("5. context cache (cached_context)")

    def expensive_lookup() -> dict:
        time.sleep(0.5)  # stand-in for a warehouse query / external API
        return {"customer": USER_ID, "tier": "gold", "open_tickets": 2}

    key = f"customer-profile::{USER_ID}"
    try:
        t0 = time.time()
        first = client.cached_context(key, expensive_lookup, namespace="all-in-one", ttl_seconds=300)
        t1 = time.time()
        second = client.cached_context(key, expensive_lookup, namespace="all-in-one")
        t2 = time.time()
    except AOMError as exc:
        print(f"context cache unavailable: {exc}")
        return
    print(f"first  (miss, ran fetch): {int((t1 - t0) * 1000)} ms -> {first}")
    print(f"second (hit, no fetch):   {int((t2 - t1) * 1000)} ms -> {second}")


# ---------------------------------------------------------------------------
# 6. agent memory
# ---------------------------------------------------------------------------
def memory(client: AOMClient) -> None:
    stage("6. agent memory")
    try:
        client.add_memory(USER_ID, "Customer prefers email over phone.", session_id=SESSION_ID,
                          memory_type="preference")
        client.add_memory(USER_ID, "Asked about a damaged order (#48213).", session_id=SESSION_ID)
        hits = client.search_memory(USER_ID, "how should we contact them?", top_k=2)
        print("search_memory('how should we contact them?'):")
        for h in hits:
            print(f"  {h.get('score', 0):.3f}  {h.get('content')}")
        print(f"list_memory: {len(client.list_memory(USER_ID, session_id=SESSION_ID))} entries this session")
        cleared = client.clear_memory(USER_ID, session_id=SESSION_ID)
        print(f"clear_memory: removed {cleared} (session only; long-term memory untouched)")
    except AOMError as exc:
        print(f"memory unavailable: {exc}")


# ---------------------------------------------------------------------------
# 7. knowledge search
# ---------------------------------------------------------------------------
def knowledge(client: AOMClient) -> None:
    stage("7. knowledge search")
    try:
        chunks = client.search_knowledge("refund policy", top_k=3)
    except AOMError as exc:
        print(f"knowledge search unavailable: {exc}")
        return
    if not chunks:
        print("no results - upload a document under Registry -> Knowledge Base and grant this role access")
        return
    for c in chunks:
        print(f"  {c.get('score', 0):.3f}  {c.get('document_title')}  -  {str(c.get('text', ''))[:80]}")


# ---------------------------------------------------------------------------
# 8. RAG application (optional)
# ---------------------------------------------------------------------------
def rag(client: AOMClient) -> None:
    stage("8. RAG application")
    if not RAG_APP:
        print("skipped - set AOM_RAG_APP to an app registered under Registry -> RAG Applications")
        return
    try:
        res = client.rag_query(RAG_APP, "What is our refund window?")
    except AOMNotFoundError:
        print(f"no RAG application {RAG_APP!r} on this appliance")
        return
    except AOMError as exc:
        print(f"rag_query failed: {exc}")
        return
    print("answer:", res["answer"][:300])
    for s in res.get("sources", []):
        print(f"  [{s['n']}] {s['document_title']}")
    print("retrieval cache:", res["retrieval"]["cache"], "| llm cache:", res["llm"]["cache"]["status"])


# ---------------------------------------------------------------------------
def main() -> None:
    client = AOMClient(
        BASE_URL,
        api_key=API_KEY,
        verify=VERIFY,
        agent_id="all-in-one-example",
        session_id=SESSION_ID,
        provider=LLM_PROVIDER,
        model=LLM_MODEL,
    )
    print(client)

    try:
        with client.run(name="all-in-one walkthrough") as run:
            print(f"trace_id = {run.trace_id}  (Traces page shows every call below as one run)")
            health(client)
            tool = pick_tool(client)
            if tool:
                invoke(client, tool)
            completion(client)
            context_cache(client)
            memory(client)
            knowledge(client)
            rag(client)
    except AOMConnectionError as exc:
        print(f"\ncannot reach the appliance: {exc}")
        print("check AOM_BASE_URL, and AOM_VERIFY_SSL=false for a self-signed certificate")
        raise SystemExit(1)
    except AOMAuthenticationError as exc:
        print(f"\nkey refused: {exc}")
        print("issue an agent key under Settings -> Agent Identities and pass it as AOM_API_KEY")
        raise SystemExit(1)

    print(f"\ndone - open the appliance's Traces page and search for trace {run.trace_id}")


if __name__ == "__main__":
    main()
