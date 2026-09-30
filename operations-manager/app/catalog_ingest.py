"""
Catalog ingestion: for a given *trusted, registered* server, connect over
MCP, list its tools, embed each tool's description, attach an RBAC policy,
run the metadata-poisoning and definition-drift scans, and upsert the
result into Couchbase's `tools` collection.

This is the one place an MCP server's tool definitions cross the trust
boundary into the centralized catalog. A server that was never registered
(trust_status != "trusted") never runs through this path, so its tools
simply do not exist as far as the operations manager - and therefore RBAC +
vector search discovery - is concerned. That's true whether the server was
left out entirely (like the bundled shadow-diagnostics sample) or was
registered but marked untrusted pending review.

Every tool that does make it through runs two independent checks before
it's stored:

  - `hijack_detection.apply_metadata_scan()` - does this description match
    a known injection pattern? A match quarantines the tool automatically
    (trust_status forced to "quarantined") regardless of how the server
    itself is trusted or how TOOL_POLICY would otherwise classify it.
  - `tool_versioning.apply_drift_scan()` - is this the same definition an
    admin actually approved? A tool whose upstream description or input
    schema has changed since approval is quarantined too, whether or not
    the new text looks malicious: the approval covered text that no longer
    exists. First ingest establishes the baseline rather than reporting
    drift against nothing.

Order matters between the two: the drift scan runs second so that a
definition change clears any standing manual override, which the metadata
scan would otherwise have re-applied to text the operator never saw.

The connection to each downstream server carries the credential and signed
caller identity built by app/server_auth.py, and a timeout from the
governance policy - listing a catalog is a downstream call like any other,
and an unhealthy server must not be able to hang ingestion indefinitely.

On startup, `seed_servers()` upserts the bundled sample servers
(rbac_policy.SEED_SERVERS) into Couchbase *only if they don't already
exist*, so editing or removing them later via the Servers page sticks
across restarts. `ingest_all()` then (re-)ingests every currently
trusted, registered server - seeded or user-added - which is also what
runs when a server is registered or manually re-ingested at runtime.
`rescan_all_tools()` is the lighter-weight pass the background hijack
monitor runs on a timer: it re-scans already-ingested tool descriptions
against the current pattern bank without any MCP round-trip, catching
tools ingested before a pattern-bank update, or before hijack detection
existed at all - without hammering downstream MCP servers to do it.
"""
import logging

from app import hijack_detection, mcp_client, server_auth, tool_versioning
from app.rbac_policy import SEED_SERVERS, policy_for

logger = logging.getLogger("operations-manager.catalog_ingest")

# Set once at startup (main.py) so ingestion can read the current
# downstream timeout without importing main and creating a cycle - the same
# provider-callable convention couchbase_client uses for the SIEM config.
_governance_provider = None


def set_governance_provider(provider) -> None:
    """provider: a zero-arg callable returning the current governance
    config dict (see app/governance.normalize_config)."""
    global _governance_provider
    _governance_provider = provider


def _downstream_timeout() -> int | None:
    if _governance_provider is None:
        return None
    try:
        return int(_governance_provider().get("downstream_timeout_seconds") or 0) or None
    except Exception:  # noqa: BLE001
        return None


async def seed_servers(store, sample_mcp_servers_base_url: str):
    for server_id, meta in SEED_SERVERS.items():
        existing = await store.get_server(server_id)
        if existing:
            continue
        mcp_url = f"{sample_mcp_servers_base_url.rstrip('/')}{meta['mcp_path']}"
        await store.upsert_server(server_id, {
            "server_id": server_id,
            "label": meta["label"],
            "owner": meta["owner"],
            "mcp_url": mcp_url,
            "trust_status": "trusted",
            "default_allowed_roles": [],
            "auth": dict(server_auth.DEFAULT_AUTH),
            "seeded": True,
        })
        logger.info("Seeded sample server '%s' (%s)", server_id, mcp_url)


async def ingest_server(store, embeddings, server_doc: dict) -> dict:
    """Ingest one server's tool catalog. Returns
    {tools, quarantined, drifted}. Raises on connection/listing failure so
    callers (the manual "re-ingest" endpoint especially) can surface the
    error."""
    server_id = server_doc["server_id"]
    mcp_url = server_doc["mcp_url"]

    headers = server_auth.outbound_headers(
        server_doc, role="system", subject="catalog-ingest", tool_id=None
    )
    tools = await mcp_client.list_tools(mcp_url, headers=headers, timeout_seconds=_downstream_timeout())
    default_roles = server_doc.get("default_allowed_roles") or []
    tool_policies = server_doc.get("tool_policies") or {}
    quarantined_count = 0
    drifted_count = 0

    for tool in tools:
        policy = policy_for(server_id, tool["name"], default_allowed_roles=default_roles, tool_policies=tool_policies)
        embedding_text = build_embedding_text(server_id, server_doc, tool)
        embedding = await embeddings.embed_async(embedding_text)

        tool_id = f"{server_id}::{tool['name']}"
        existing = await store.get_tool(tool_id)

        tool_doc = {
            "tool_id": tool_id,
            "server_id": server_id,
            "name": tool["name"],
            "description": tool["description"],
            "input_schema": tool["input_schema"],
            "allowed_roles": policy["allowed_roles"],
            "risk_level": policy["risk_level"],
            "trust_status": "trusted",
            "embedding": embedding,
        }
        tool_doc = hijack_detection.apply_metadata_scan(tool_doc, existing)
        tool_doc = tool_versioning.apply_drift_scan(tool_doc, existing)

        if tool_doc.get("drift_status") == "drifted":
            drifted_count += 1
            logger.warning(
                "Quarantined tool '%s' - its definition changed since it was approved: %s",
                tool_id, [c.get("detail") for c in tool_doc.get("drift_changes", [])],
            )
        if tool_doc["trust_status"] == "quarantined" and tool_doc.get("hijack_status") == "flagged":
            quarantined_count += 1
            logger.warning(
                "Quarantined tool '%s' at ingest - metadata poisoning signal(s): %s",
                tool_id, [s["pattern_id"] for s in tool_doc.get("hijack_signals", [])],
            )

        await store.upsert_tool(tool_id, tool_doc)

    logger.info(
        "Ingested catalog for '%s': %d tool(s), %d quarantined for suspected metadata poisoning, "
        "%d quarantined for definition drift",
        server_id, len(tools), quarantined_count, drifted_count,
    )
    return {"tools": len(tools), "quarantined": quarantined_count, "drifted": drifted_count}


async def ingest_all(store, embeddings):
    """(Re-)ingest every currently trusted, registered server."""
    for server_doc in await store.list_servers():
        if server_doc.get("trust_status") != "trusted":
            continue
        try:
            await ingest_server(store, embeddings, server_doc)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to ingest catalog for registered server '%s' (%s): %s",
                server_doc.get("server_id"), server_doc.get("mcp_url"), exc,
            )


async def rescan_all_tools(store) -> int:
    """Re-run the metadata-poisoning scan against every already-ingested
    tool's stored description, with no MCP round-trip. Only writes back
    documents whose trust_status or hijack_status actually changed, so a
    quiet monitor tick costs one N1QL read plus one KV get per tool and no
    writes at all when nothing changed.

    Drift is deliberately *not* re-evaluated here. This pass reads the
    stored definition, which by construction is the one already compared
    against the approved baseline at ingest time - re-diffing it against
    itself could only ever produce "no change". Drift is only detectable
    when a fresh definition is listed from the server, which is
    `ingest_server`'s job.

    Note: this re-fetches each tool via `get_tool()` (a full KV read)
    rather than reusing `list_tools()`'s rows, because that listing
    intentionally omits the `embedding` vector to keep API responses
    small - upserting a doc built from it would silently strip the
    embedding and break vector search discovery for that tool.
    """
    summaries = await store.list_tools()
    changed = 0
    for summary in summaries:
        tool = await store.get_tool(summary["tool_id"])
        if not tool:
            continue
        updated = dict(tool)
        updated = hijack_detection.apply_metadata_scan(updated, tool)
        # A tool quarantined for drift stays quarantined regardless of what
        # the pattern bank says about it - the metadata scan has no opinion
        # about drift and would happily re-trust it.
        if tool.get("drift_status") == "drifted":
            updated["trust_status"] = "quarantined"
        if updated.get("trust_status") != tool.get("trust_status") or updated.get("hijack_status") != tool.get("hijack_status"):
            await store.upsert_tool(tool["tool_id"], updated)
            changed += 1
            if updated["trust_status"] == "quarantined" and tool.get("trust_status") != "quarantined":
                logger.warning(
                    "Background scan quarantined tool '%s' - metadata poisoning signal(s): %s",
                    tool["tool_id"], [s["pattern_id"] for s in updated.get("hijack_signals", [])],
                )
    return changed


def build_embedding_text(server_id: str, server_meta: dict, tool: dict) -> str:
    parts = [tool["name"], tool["description"], server_meta.get("label", server_id), f"server:{server_id}"]
    schema = tool.get("input_schema") or {}
    props = schema.get("properties") or {}
    for pname, pinfo in props.items():
        if isinstance(pinfo, dict) and pinfo.get("description"):
            parts.append(f"{pname}: {pinfo['description']}")
    return " ".join(parts)
