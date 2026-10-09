"""
Everything that talks to Couchbase: connection lifecycle, the RBAC +
vector-search Search (FTS) index, and the KV collections used as the tool
registry, the identity table, and the access audit log.

The centerpiece is `discover_tools()`: a single Couchbase Search request
that combines an RBAC pre-filter (a Conjunction of TermQuerys on
`allowed_roles` and `trust_status`) with a vector kNN query on `embedding`.
The pre-filter narrows the candidate set *before/alongside* the similarity
ranking - a tool outside the caller's role, or from a server that was never
registered as trusted, cannot be returned no matter how well it matches the
query semantically. That is the "RBAC + Couchbase Vector Search
Pre-filtering" this whole appliance is built around.
"""
import asyncio
import hashlib
import logging
import time
import uuid
from datetime import timedelta

import numpy as np
import requests

from app import siem_forwarding
from couchbase.auth import PasswordAuthenticator
from couchbase.cluster import Cluster
from couchbase.exceptions import (
    CasMismatchException,
    CollectionAlreadyExistsException,
    DocumentNotFoundException,
)
from couchbase.options import (
    ClusterOptions,
    ClusterTimeoutOptions,
    DeltaValue,
    IncrementOptions,
    QueryOptions,
    ReplaceOptions,
    SearchOptions,
    SignedInt64,
    UpsertOptions,
)
import couchbase.search as cb_search
from couchbase.vector_search import VectorQuery as CBVectorQuery, VectorSearch as CBVectorSearch

from config import (
    AUDIT_LOG_RETENTION_HOURS,
    CONTEXT_CACHE_LOG_RETENTION_HOURS,
    COUCHBASE_CONFIG,
    EMBEDDING_CONFIG,
    EVAL_RUN_HISTORY,
    LLM_CACHE_LOG_RETENTION_HOURS,
    QUERY_TIMEOUT_SECONDS,
    TRACE_RETENTION_HOURS,
)

logger = logging.getLogger("operations-manager.couchbase")


def hash_key(api_key: str) -> str:
    """Never store raw API keys as document IDs - hash them instead."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


# Every collection this service reads or writes. Created on startup if
# missing (see CouchbaseStore._ensure_collections) so a bring-your-own
# cluster only needs the bucket and scope to exist.
ALL_COLLECTIONS = tuple(
    COUCHBASE_CONFIG[key]
    for key in (
        "servers_collection", "tools_collection", "identities_collection",
        "access_log_collection", "llm_cache_collection", "llm_cache_log_collection",
        "context_cache_collection", "context_cache_log_collection", "settings_collection",
        "agent_memory_collection", "users_collection", "traces_collection",
        "evals_collection", "counters_collection", "approvals_collection",
        "knowledge_collection",
    )
)

# `counters` is read and written by document ID only and takes a write on
# every gateway call, so it deliberately gets no index at all.
PRIMARY_INDEX_COLLECTIONS = tuple(
    c for c in ALL_COLLECTIONS if c != COUCHBASE_CONFIG["counters_collection"]
)

# (index name, collection, index keys, partial-index WHERE or "").
# Keep in sync with couchbase-init/init.sh (and, via
# scripts/sync-helm-couchbase-init.py, the Helm chart's copy of it).
SECONDARY_INDEXES = (
    ("idx_llm_cache_log_timestamp", COUCHBASE_CONFIG["llm_cache_log_collection"], "timestamp", ""),
    ("idx_llm_cache_log_agg", COUCHBASE_CONFIG["llm_cache_log_collection"],
     "timestamp, outcome, provider, model, tokens_saved, total_tokens, cost_saved_usd, cost_usd, "
     "latency_saved_ms, latency_ms", ""),
    ("idx_access_log_topology", COUCHBASE_CONFIG["access_log_collection"],
     "timestamp, action, decision, `role`, server_id", ""),
    ("idx_llm_cache_log_topology", COUCHBASE_CONFIG["llm_cache_log_collection"], "timestamp, `role`, provider", ""),
    ("idx_context_cache_log_timestamp", COUCHBASE_CONFIG["context_cache_log_collection"], "timestamp", ""),
    ("idx_context_cache_log_agg", COUCHBASE_CONFIG["context_cache_log_collection"],
     "timestamp, outcome, `namespace`, subject, latency_saved_ms, latency_ms, value_bytes", ""),
    ("idx_access_log_recent", COUCHBASE_CONFIG["access_log_collection"], "timestamp DESC", ""),
    ("idx_llm_cache_log_recent", COUCHBASE_CONFIG["llm_cache_log_collection"], "timestamp DESC", ""),
    ("idx_context_cache_log_recent", COUCHBASE_CONFIG["context_cache_log_collection"], "timestamp DESC", ""),
    ("idx_llm_cache_created", COUCHBASE_CONFIG["llm_cache_collection"], "created_at DESC", ""),
    ("idx_context_cache_created", COUCHBASE_CONFIG["context_cache_collection"], "created_at DESC", ""),
    ("idx_traces_runs", COUCHBASE_CONFIG["traces_collection"], "doc_type, started_at DESC, `role`, status", ""),
    ("idx_traces_agg", COUCHBASE_CONFIG["traces_collection"],
     "started_at, `role`, error_count, total_tokens, cost_usd, cache_hits, hijack_flags, limit_blocks",
     "doc_type = 'agent_run'"),
    ("idx_traces_spans", COUCHBASE_CONFIG["traces_collection"], "trace_id, started_epoch_ms",
     "doc_type = 'agent_span'"),
    ("idx_eval_runs", COUCHBASE_CONFIG["evals_collection"], "doc_type, dataset_id, started_at DESC", ""),
    ("idx_approvals_queue", COUCHBASE_CONFIG["approvals_collection"], "doc_type, status, requested_at DESC", ""),
    ("idx_knowledge_documents", COUCHBASE_CONFIG["knowledge_collection"], "doc_type, created_at DESC", ""),
    ("idx_knowledge_by_document", COUCHBASE_CONFIG["knowledge_collection"], "document_id", ""),
    # Agent memory is read per user (recall, the Memory page, consolidation)
    # and aggregated per user; without these every one of those is a
    # primary scan that gets slower as memory grows.
    ("idx_agent_memory_user", COUCHBASE_CONFIG["agent_memory_collection"],
     "user_id, created_at DESC, status, session_id, memory_type", ""),
    ("idx_agent_memory_agg", COUCHBASE_CONFIG["agent_memory_collection"],
     "user_id, status, updated_at, session_id, consolidation_kind", ""),
)


# SIEM/log-forwarding provider hook - set once at startup (main.py) so
# log_access() below can schedule a background forward without importing
# main.py itself (avoids a circular import). None means forwarding is off
# or not yet configured; log_access() must tolerate that quietly.
_siem_config_provider = None


def set_siem_config_provider(provider) -> None:
    """provider: a zero-arg callable returning the current SIEM destinations
    config dict (see app/siem_forwarding.normalize_config) - a callable
    rather than a static dict so it always reflects the latest saved config,
    even after an admin edits it via PUT /v1/siem/config."""
    global _siem_config_provider
    _siem_config_provider = provider


class CouchbaseStore:
    def __init__(self):
        self.cluster = None
        self.bucket = None
        self.scope = None
        self.servers = None
        self.tools = None
        self.identities = None
        self.access_log = None
        self.llm_cache = None
        self.llm_cache_log = None
        self.context_cache = None
        self.context_cache_log = None
        self.settings = None
        self.agent_memory = None
        self.users = None
        self.traces = None
        self.evals = None
        self.counters = None
        self.approvals = None
        self.knowledge = None
        self.connected = False

    async def connect(self, retries: int = 30, delay_seconds: float = 5.0):
        for attempt in range(1, retries + 1):
            try:
                await asyncio.to_thread(self._connect_sync)
                self.connected = True
                logger.info("Connected to Couchbase on attempt %d", attempt)
                await self.ensure_search_index()
                await self.ensure_llm_cache_index()
                await self.ensure_agent_memory_index()
                await self.ensure_knowledge_index()
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("Couchbase connect attempt %d/%d failed: %s", attempt, retries, exc)
                await asyncio.sleep(delay_seconds)
        logger.error("Could not connect to Couchbase after %d attempts - running degraded", retries)
        self.connected = False

    def _connect_sync(self):
        auth = PasswordAuthenticator(COUCHBASE_CONFIG["username"], COUCHBASE_CONFIG["password"])
        cluster = Cluster(
            COUCHBASE_CONFIG["connection_string"],
            ClusterOptions(
                auth,
                timeout_options=ClusterTimeoutOptions(
                    kv_timeout=timedelta(seconds=10),
                    # The SDK's default query timeout is 75s. Every N1QL call
                    # here runs in a worker thread (asyncio.to_thread), so a
                    # default-timeout query that has gone bad holds one of
                    # those threads for over a minute - long enough that a
                    # handful of them starve the pool and unrelated requests
                    # (login included) never get served at all. A thread in
                    # the executor cannot be cancelled from outside, so this
                    # timeout - not any request-level deadline - is what
                    # actually hands the thread back. See
                    # config.QUERY_TIMEOUT_SECONDS.
                    query_timeout=timedelta(seconds=QUERY_TIMEOUT_SECONDS),
                    search_timeout=timedelta(seconds=QUERY_TIMEOUT_SECONDS),
                ),
            ),
        )
        cluster.wait_until_ready(timedelta(seconds=20))
        bucket = cluster.bucket(COUCHBASE_CONFIG["bucket"])
        scope = bucket.scope(COUCHBASE_CONFIG["scope"])

        servers = scope.collection(COUCHBASE_CONFIG["servers_collection"])
        tools = scope.collection(COUCHBASE_CONFIG["tools_collection"])
        identities = scope.collection(COUCHBASE_CONFIG["identities_collection"])
        access_log = scope.collection(COUCHBASE_CONFIG["access_log_collection"])
        llm_cache = scope.collection(COUCHBASE_CONFIG["llm_cache_collection"])
        llm_cache_log = scope.collection(COUCHBASE_CONFIG["llm_cache_log_collection"])
        context_cache = scope.collection(COUCHBASE_CONFIG["context_cache_collection"])
        context_cache_log = scope.collection(COUCHBASE_CONFIG["context_cache_log_collection"])
        settings = scope.collection(COUCHBASE_CONFIG["settings_collection"])
        agent_memory = scope.collection(COUCHBASE_CONFIG["agent_memory_collection"])
        users = scope.collection(COUCHBASE_CONFIG["users_collection"])
        traces = scope.collection(COUCHBASE_CONFIG["traces_collection"])
        evals = scope.collection(COUCHBASE_CONFIG["evals_collection"])
        counters = scope.collection(COUCHBASE_CONFIG["counters_collection"])
        approvals = scope.collection(COUCHBASE_CONFIG["approvals_collection"])
        knowledge = scope.collection(COUCHBASE_CONFIG["knowledge_collection"])

        # couchbase-init provisions every collection this service uses, but
        # its script is bind-mounted: upgrading an appliance that is already
        # running brings a new operations-manager image up against a cluster
        # whose init container was never re-run, so a collection added by
        # that upgrade would simply not exist. Rather than fail the startup
        # probe below and idle in a degraded state until someone works out
        # why, create anything missing here. Idempotent, and a no-op on the
        # normal path where couchbase-init already made them.
        # couchbase-init provisions every collection this service uses when
        # the bundled cluster is in play, but against a bring-your-own
        # cluster (the production path) nothing else does: create anything
        # missing here, then make sure every index the app's queries rely on
        # exists. Both steps are idempotent and a near no-op on a cluster
        # that already has them.
        self._ensure_collections(bucket, list(ALL_COLLECTIONS))
        self._ensure_indexes(cluster)

        # Building the four collection handles above is a purely local
        # object in the Couchbase Python SDK - it does NOT confirm the
        # scope or its collections actually exist on the cluster yet.
        # couchbase-init provisions them asynchronously (docker-compose
        # gates operations-manager behind its successful completion, but this
        # probe is defense-in-depth for anyone running the service outside
        # that compose file, or against an already-running cluster that's
        # mid-provisioning). Without this check, connect() would report
        # success and the very first real write - e.g. seeding an identity
        # on startup - would crash with ScopeNotFoundException/
        # CollectionNotFoundException instead of being retried by the
        # backoff loop in connect().
        for collection in (servers, tools, identities, access_log, llm_cache, llm_cache_log,
                           context_cache, context_cache_log, settings,
                           agent_memory, users, traces, evals, counters, approvals, knowledge):
            collection.exists("__startup_probe__")

        self.cluster = cluster
        self.bucket = bucket
        self.scope = scope
        self.servers = servers
        self.tools = tools
        self.identities = identities
        self.access_log = access_log
        self.llm_cache = llm_cache
        self.llm_cache_log = llm_cache_log
        self.context_cache = context_cache
        self.context_cache_log = context_cache_log
        self.settings = settings
        self.agent_memory = agent_memory
        self.users = users
        self.traces = traces
        self.evals = evals
        self.counters = counters
        self.approvals = approvals
        self.knowledge = knowledge

    def _ensure_collections(self, bucket, names: list[str]) -> list[str]:
        """Create any of `names` that do not exist yet in the configured
        scope. Failures are logged, never raised: if the credentials in use
        cannot manage collections, the startup probe that follows is the
        right place to surface that, with a much clearer message than a
        permissions error from here. Returns the names actually created."""
        scope_name = COUCHBASE_CONFIG["scope"]
        created: list[str] = []
        try:
            manager = bucket.collections()
            existing = {
                collection.name
                for scope in manager.get_all_scopes()
                if scope.name == scope_name
                for collection in scope.collections
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not list collections to check for missing ones: %s", exc)
            return created

        for name in names:
            if name in existing:
                continue
            try:
                manager.create_collection(scope_name, name)
                created.append(name)
                logger.info("Created missing collection '%s.%s'", scope_name, name)
            except CollectionAlreadyExistsException:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not create collection '%s.%s': %s", scope_name, name, exc)
        return created

    def _ensure_indexes(self, cluster) -> None:
        """Primary and secondary GSI indexes for every collection (see
        PRIMARY_INDEX_COLLECTIONS / SECONDARY_INDEXES). Same best-effort
        posture as _ensure_collections: a failure is logged, and a query that
        later finds no index logs and returns an empty list rather than
        breaking a page. CREATE ... IF NOT EXISTS makes this cheap when the
        indexes are already there; on a large existing collection the build
        can outlast the query timeout, in which case the server finishes it
        in the background."""
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        statements = [
            f"CREATE PRIMARY INDEX IF NOT EXISTS ON `{bucket}`.`{scope}`.`{name}`"
            for name in PRIMARY_INDEX_COLLECTIONS
        ] + [
            f"CREATE INDEX IF NOT EXISTS {name} ON `{bucket}`.`{scope}`.`{coll}`({keys})"
            + (f" WHERE {where}" if where else "")
            for name, coll, keys, where in SECONDARY_INDEXES
        ]
        for statement in statements:
            try:
                cluster.query(statement, QueryOptions(metrics=False)).execute()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not ensure index (%s): %s", statement, exc)

    # -- Search (FTS) vector index -----------------------------------------

    def _index_definition(self) -> dict:
        bucket = COUCHBASE_CONFIG["bucket"]
        scope = COUCHBASE_CONFIG["scope"]
        collection = COUCHBASE_CONFIG["tools_collection"]
        type_key = f"{scope}.{collection}"

        def text_field(name: str) -> dict:
            return {
                "dynamic": False,
                "enabled": True,
                "fields": [{"name": name, "type": "text", "analyzer": "standard", "index": True, "store": True}],
            }

        properties = {
            "name": text_field("name"),
            "description": text_field("description"),
            "server_id": text_field("server_id"),
            "trust_status": text_field("trust_status"),
            "allowed_roles": text_field("allowed_roles"),
            "risk_level": text_field("risk_level"),
            "embedding": {
                "dynamic": False,
                "enabled": True,
                "fields": [{
                    "name": "embedding",
                    "type": "vector",
                    "dims": EMBEDDING_CONFIG["vector_dim"],
                    "similarity": "dot_product",
                    "index": True,
                    "store": True,
                }],
            },
        }

        return {
            "type": "fulltext-index",
            "name": f"{bucket}.{scope}.{COUCHBASE_CONFIG['tools_index']}",
            "sourceType": "gocbcore",
            "sourceName": bucket,
            "planParams": {"maxPartitionsPerPIndex": 512, "indexPartitions": 1},
            "params": {
                "doc_config": {"mode": "scope.collection.type_field", "type_field": "doc_type"},
                "mapping": {
                    "default_analyzer": "standard",
                    "default_datetime_parser": "dateTimeOptional",
                    "default_field": "_all",
                    "default_mapping": {"dynamic": False, "enabled": False},
                    "default_type": "_default",
                    "docvalues_dynamic": False,
                    "index_dynamic": False,
                    "store_dynamic": False,
                    "type_field": "_type",
                    "types": {type_key: {"dynamic": False, "enabled": True, "properties": properties}},
                },
            },
            "store": {"indexType": "scorch", "segmentVersion": 16},
            "sourceParams": {},
        }

    def _search_admin_url(self, index_name: str) -> str:
        host = COUCHBASE_CONFIG["search_host"]
        port = COUCHBASE_CONFIG["search_port"]
        bucket = COUCHBASE_CONFIG["bucket"]
        scope = COUCHBASE_CONFIG["scope"]
        return f"http://{host}:{port}/api/bucket/{bucket}/scope/{scope}/index/{index_name}"

    async def ensure_search_index(self):
        def _upsert():
            auth = (COUCHBASE_CONFIG["username"], COUCHBASE_CONFIG["password"])
            index_name = COUCHBASE_CONFIG["tools_index"]
            url = self._search_admin_url(index_name)
            existing = requests.get(url, auth=auth, timeout=10)
            if existing.status_code == 200:
                logger.info("Search index '%s' already exists", index_name)
                return
            resp = requests.put(url, auth=auth, json=self._index_definition(), timeout=15)
            if resp.status_code in (200, 201):
                logger.info("Search index '%s' created", index_name)
            else:
                logger.warning("Search index creation returned %s: %s", resp.status_code, resp.text[:300])

        try:
            await asyncio.to_thread(_upsert)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Search index setup failed (will retry on discover): %s", exc)

    # -- Server registry ------------------------------------------------------

    async def upsert_server(self, doc_id: str, doc: dict):
        await asyncio.to_thread(self.servers.upsert, doc_id, doc)

    async def get_server(self, server_id: str) -> dict | None:
        def _get():
            try:
                return self.servers.get(server_id).content_as[dict]
            except DocumentNotFoundException:
                return None

        return await asyncio.to_thread(_get)

    async def delete_server(self, server_id: str) -> bool:
        def _delete():
            try:
                self.servers.remove(server_id)
                return True
            except DocumentNotFoundException:
                return False

        return await asyncio.to_thread(_delete)

    async def list_servers(self) -> list[dict]:
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["servers_collection"]

        def _run():
            q = f"SELECT s.* FROM `{bucket}`.`{scope}`.`{coll}` s"
            return list(self.cluster.query(q, QueryOptions(metrics=False)).rows())

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_servers query failed: %s", exc)
            return []

    # -- Tool registry ----------------------------------------------------------

    async def upsert_tool(self, doc_id: str, doc: dict):
        await asyncio.to_thread(self.tools.upsert, doc_id, doc)

    async def count_tools(self) -> int:
        return await self._count(COUCHBASE_CONFIG["tools_collection"])

    async def list_tools(self) -> list[dict]:
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["tools_collection"]

        def _run():
            # Everything except `embedding` (384 floats/tool - no reason to
            # ship that over the API for a catalog listing) and
            # `input_schema` gets returned, including the hijack-detection
            # fields the Threat Detection page needs.
            field_names = [
                "tool_id", "server_id", "name", "description", "input_schema", "allowed_roles", "risk_level",
                "trust_status", "hijack_status", "hijack_severity", "hijack_signals", "hijack_scanned_at",
                "hijack_manual_override",
            ]
            fields = ", ".join(f"t.{f}" for f in field_names)
            q = f"SELECT {fields} FROM `{bucket}`.`{scope}`.`{coll}` t"
            return list(self.cluster.query(q, QueryOptions(metrics=False)).rows())

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_tools query failed: %s", exc)
            return []

    async def get_tool(self, tool_id: str) -> dict | None:
        def _get():
            try:
                return self.tools.get(tool_id).content_as[dict]
            except DocumentNotFoundException:
                return None

        return await asyncio.to_thread(_get)

    async def delete_tools_by_server(self, server_id: str) -> int:
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["tools_collection"]

        def _run():
            q = (
                f"DELETE FROM `{bucket}`.`{scope}`.`{coll}` t "
                f"WHERE t.server_id = $server_id RETURNING META(t).id"
            )
            rows = list(self.cluster.query(q, QueryOptions(named_parameters={"server_id": server_id}, metrics=False)).rows())
            return len(rows)

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("delete_tools_by_server(%s) failed: %s", server_id, exc)
            return 0

    async def _count(self, collection_name: str) -> int:
        if not self.connected:
            return 0
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]

        def _run():
            q = f"SELECT RAW COUNT(*) FROM `{bucket}`.`{scope}`.`{collection_name}`"
            rows = list(self.cluster.query(q, QueryOptions(metrics=False)).rows())
            return rows[0] if rows else 0

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.debug("count(%s) failed: %s", collection_name, exc)
            return 0

    # -- Identities (API key -> role) ---------------------------------------

    async def upsert_identity(self, api_key: str, role: str, label: str):
        """The original seeding path, kept so a deployment that has never
        issued an agent still boots with working demo keys."""
        doc_id = hash_key(api_key)
        await asyncio.to_thread(self.identities.upsert, doc_id, {"role": role, "label": label})

    async def resolve_role(self, api_key: str) -> str | None:
        doc = await self.get_key_doc(api_key)
        return doc.get("role") if doc else None

    async def get_key_doc(self, api_key: str) -> dict | None:
        """The single KV get every authenticated request begins with. The
        document carries the role and scope denormalized onto it, so this
        one read answers who the caller is and what they may do."""
        doc_id = hash_key(api_key)

        def _get():
            try:
                return self.identities.get(doc_id).content_as[dict]
            except DocumentNotFoundException:
                return None

        try:
            return await asyncio.to_thread(_get)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_key_doc failed: %s", exc)
            return None

    async def upsert_key_doc(self, api_key: str, doc: dict, ttl_seconds: int = 0):
        doc_id = hash_key(api_key)
        options = UpsertOptions(expiry=timedelta(seconds=ttl_seconds)) if ttl_seconds else UpsertOptions()
        await asyncio.to_thread(self.identities.upsert, doc_id, doc, options)

    async def upsert_key_doc_by_hash(self, key_hash: str, doc: dict, ttl_seconds: int = 0):
        """Rotation and revocation rewrite a key document without ever
        having seen the key again - only its hash, which is what the agent
        record stores."""
        options = UpsertOptions(expiry=timedelta(seconds=ttl_seconds)) if ttl_seconds else UpsertOptions()
        await asyncio.to_thread(self.identities.upsert, key_hash, doc, options)

    async def get_key_doc_by_hash(self, key_hash: str) -> dict | None:
        def _get():
            try:
                return self.identities.get(key_hash).content_as[dict]
            except DocumentNotFoundException:
                return None

        try:
            return await asyncio.to_thread(_get)
        except Exception as exc:  # noqa: BLE001
            logger.debug("get_key_doc_by_hash failed: %s", exc)
            return None

    async def delete_key_doc_by_hash(self, key_hash: str) -> bool:
        def _delete():
            try:
                self.identities.remove(key_hash)
                return True
            except DocumentNotFoundException:
                return False

        try:
            return await asyncio.to_thread(_delete)
        except Exception as exc:  # noqa: BLE001
            logger.debug("delete_key_doc_by_hash failed: %s", exc)
            return False

    # -- Agent identity records ----------------------------------------------

    async def upsert_agent(self, agent: dict):
        await asyncio.to_thread(self.identities.upsert, f"agent::{agent['agent_id']}", agent)

    async def get_agent(self, agent_id: str) -> dict | None:
        def _get():
            try:
                return self.identities.get(f"agent::{agent_id}").content_as[dict]
            except DocumentNotFoundException:
                return None

        return await asyncio.to_thread(_get)

    async def delete_agent(self, agent_id: str) -> bool:
        def _delete():
            try:
                self.identities.remove(f"agent::{agent_id}")
                return True
            except DocumentNotFoundException:
                return False

        return await asyncio.to_thread(_delete)

    async def list_agents(self, limit: int = 200) -> list[dict]:
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["identities_collection"]

        def _run():
            q = (
                f"SELECT a.* FROM `{bucket}`.`{scope}`.`{coll}` a "
                f'WHERE a.doc_type = "agent_identity" ORDER BY a.created_at DESC LIMIT $limit'
            )
            return list(self.cluster.query(
                q, QueryOptions(named_parameters={"limit": limit}, metrics=False)
            ).rows())

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_agents query failed: %s", exc)
            return []

    async def touch_agent(self, agent_id: str):
        """Record that an agent was used. Fire-and-forget from the caller's
        side: 'last used' is what tells an operator whether a key can safely
        be turned off, and it must never be able to fail a request."""
        if not agent_id:
            return
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["identities_collection"]

        def _run():
            q = (
                f"UPDATE `{bucket}`.`{scope}`.`{coll}` a "
                f"SET a.last_used_at = $now, a.use_count = IFMISSINGORNULL(a.use_count, 0) + 1 "
                f"WHERE META(a).id = $id"
            )
            self.cluster.query(q, QueryOptions(named_parameters={
                "id": f"agent::{agent_id}", "now": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }, metrics=False, preserve_expiry=True)).execute()

        try:
            await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.debug("touch_agent(%s) failed: %s", agent_id, exc)

    # -- RBAC + vector pre-filtered discovery --------------------------------

    def _run_search_sync(self, role: str, vector: list, top_k: int) -> list[dict]:
        index_name = COUCHBASE_CONFIG["tools_index"]

        vector_query = CBVectorQuery.create("embedding", vector, num_candidates=max(top_k * 4, 25))
        vector_search = CBVectorSearch.from_vector_query(vector_query)

        # The RBAC + trust pre-filter: a Conjunction ("AND") of exact-term
        # matches on the two fields that gate access. Couchbase evaluates
        # this together with the vector query in the same Search request -
        # tools outside the role, or from an unregistered/untrusted server,
        # are excluded from the candidate set the kNN ranks over, not
        # filtered out afterwards.
        prefilter = cb_search.ConjunctionQuery(
            cb_search.TermQuery(role, field="allowed_roles"),
            cb_search.TermQuery("trusted", field="trust_status"),
        )

        request = cb_search.SearchRequest.create(prefilter).with_vector_search(vector_search)
        result = self.scope.search(
            index_name,
            request,
            SearchOptions(limit=top_k, fields=["name", "description", "server_id", "risk_level", "embedding"]),
        )
        rows = []
        for row in result.rows():
            rows.append({"id": row.id, "fields": row.fields or {}})
        return rows

    async def discover_tools(self, role: str, query_vector: list, top_k: int = 5) -> list[dict]:
        try:
            rows = await asyncio.to_thread(self._run_search_sync, role, query_vector, top_k)
        except Exception as exc:  # noqa: BLE001
            logger.warning("RBAC vector search failed (%s) - falling back to a filtered KV scan", exc)
            rows = await self._fallback_scan(role, top_k)

        query_np = np.array(query_vector, dtype=np.float32)
        out = []
        for row in rows:
            fields = row["fields"]
            stored_vector = fields.get("embedding")
            similarity = round(float(np.dot(query_np, np.array(stored_vector, dtype=np.float32))), 3) if stored_vector else 0.0
            out.append({
                "tool_id": row["id"],
                "name": fields.get("name"),
                "description": fields.get("description"),
                "server_id": fields.get("server_id"),
                "risk_level": fields.get("risk_level"),
                "similarity": similarity,
            })
        out.sort(key=lambda t: t["similarity"], reverse=True)
        return out

    async def _fallback_scan(self, role: str, top_k: int) -> list[dict]:
        """Defense-in-depth fallback if the Search service is briefly
        unavailable: a plain N1QL scan that applies the SAME RBAC + trust
        predicate in the WHERE clause, then ranks by cosine similarity in
        Python. Slower, but never less strict than the primary path."""
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["tools_collection"]

        def _run():
            q = (
                f"SELECT META(t).id AS id, t.name, t.description, t.server_id, t.risk_level, t.embedding "
                f"FROM `{bucket}`.`{scope}`.`{coll}` t "
                f"WHERE t.trust_status = $trust AND $role IN t.allowed_roles"
            )
            result = self.cluster.query(q, QueryOptions(named_parameters={"trust": "trusted", "role": role}, metrics=False))
            return list(result.rows())

        try:
            rows = await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.error("Fallback KV scan also failed: %s", exc)
            return []
        return [{"id": r["id"], "fields": r} for r in rows][:top_k]

    # -- Audit log -------------------------------------------------------------

    async def log_access(
        self,
        *,
        action: str,
        role: str | None,
        subject_label: str | None,
        query: str | None,
        tool_id: str | None,
        server_id: str | None,
        decision: str,
        reason: str,
        latency_ms: int,
        hijack_flagged: bool = False,
        hijack_severity: str | None = None,
        hijack_signals: list | None = None,
    ):
        doc_id = f"log::{int(time.time() * 1000)}::{uuid.uuid4().hex[:8]}"
        doc = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "action": action,
            "role": role,
            "subject": subject_label,
            "query": query,
            "tool_id": tool_id,
            "server_id": server_id,
            "decision": decision,
            "reason": reason,
            "latency_ms": latency_ms,
            # Response-payload hijack scan result for this call (invoke
            # only - see app/hijack_detection.py). Always present so the
            # Threat Detection page and chain correlation can filter on it
            # without a schema-dependent WHERE clause.
            "hijack_flagged": hijack_flagged,
            "hijack_severity": hijack_severity,
            "hijack_signals": hijack_signals or [],
        }
        try:
            await asyncio.to_thread(
                self.access_log.upsert, doc_id, doc, UpsertOptions(expiry=timedelta(hours=AUDIT_LOG_RETENTION_HOURS))
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to write audit log entry: %s", exc)
            return

        # Forward to any enabled SIEM destinations - fire-and-forget, never
        # awaited, so a slow/unreachable external endpoint can never add
        # latency to the discover/invoke/authenticate call that produced
        # this entry (see app/siem_forwarding.py).
        if _siem_config_provider is not None:
            try:
                siem_config = _siem_config_provider()
                if siem_config and any(v.get("enabled") for v in siem_config.values()):
                    siem_forwarding.schedule(doc, siem_config)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to schedule SIEM forwarding: %s", exc)

    # The fields the Dashboard, Insights and Threat Detection pages actually
    # read off an audit-log entry (see insights.compute_insights and
    # hijack_detection.detect_hijack_chains). Those three pages re-run this
    # query on every refresh, so they ask for these by name rather than
    # `l.*`: `query` in particular is free text a caller supplied - the
    # largest field on a discover entry, and one none of them look at.
    # The raw Audit Log page still reads whole documents; it is capped at
    # 200 rows and is the one place the full entry is the point.
    _ANALYTICS_LOG_FIELDS = (
        "timestamp", "action", "role", "subject", "tool_id", "server_id",
        "decision", "reason", "latency_ms",
        "hijack_flagged", "hijack_severity", "hijack_signals",
    )

    async def recent_access_log(self, limit: int = 50, *, analytics: bool = False) -> list[dict]:
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["access_log_collection"]
        projection = (
            ", ".join(f"l.`{f}`" for f in self._ANALYTICS_LOG_FIELDS) if analytics else "l.*"
        )

        def _run():
            q = (
                f"SELECT {projection} FROM `{bucket}`.`{scope}`.`{coll}` l USE INDEX (idx_access_log_recent) "
                f"WHERE l.timestamp IS NOT MISSING "
                f"ORDER BY l.timestamp DESC LIMIT $limit"
            )
            return list(self.cluster.query(q, QueryOptions(named_parameters={"limit": limit}, metrics=False)).rows())

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("recent_access_log query failed: %s", exc)
            return []

    async def access_log_role_server_aggregate_since(self, since: str) -> list[dict]:
        """One GROUP BY (role, server_id) aggregate over the audit log
        within a time window - builds the Dashboard topology diagram's
        agent -> MCP server edges. Only a successfully-authorized tool
        invocation counts as a live connection (action=invoke,
        decision=ALLOW, server_id present) - a denied call never reached
        the tool. See idx_access_log_topology (couchbase-init/init.sh) for
        why this is a pure index scan rather than a per-document KV fetch."""
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["access_log_collection"]

        def _run():
            q = (
                f"SELECT e.`role` AS `role`, e.server_id AS server_id, COUNT(1) AS count, MAX(e.timestamp) AS last_at "
                f"FROM `{bucket}`.`{scope}`.`{coll}` e "
                f"WHERE e.timestamp >= $since AND e.action = 'invoke' AND e.decision = 'ALLOW' "
                f"AND e.server_id IS NOT MISSING AND e.`role` IS NOT MISSING "
                f"GROUP BY e.`role`, e.server_id"
            )
            return list(self.cluster.query(q, QueryOptions(named_parameters={"since": since}, metrics=False)).rows())

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("access_log_role_server_aggregate_since query failed: %s", exc)
            return []

    async def access_log_decision_aggregate_since(self, since: str) -> list[dict]:
        """One GROUP BY (decision) aggregate over the *entire* audit log
        within a time window - powers the Dashboard's "Access Events (24h)"
        stat tile and its Allow/Deny/Error breakdown. This is a real
        COUNT(1) over every matching row in the window, unlike
        recent_access_log(limit=N): that call is capped at
        INSIGHTS_LOOKBACK_ENTRIES (default 1000) most-recent rows, so once
        24h of traffic exceeds that cap, counting from it just reports the
        cap back (e.g. a permanently-flat "1000") instead of the true
        total. GROUP BY decision keeps this a single index/aggregate scan
        rather than a per-document fetch, same as
        access_log_role_server_aggregate_since above."""
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["access_log_collection"]

        def _run():
            q = (
                f"SELECT e.decision AS decision, COUNT(1) AS count "
                f"FROM `{bucket}`.`{scope}`.`{coll}` e "
                f"WHERE e.timestamp >= $since "
                f"GROUP BY e.decision"
            )
            return list(self.cluster.query(q, QueryOptions(named_parameters={"since": since}, metrics=False)).rows())

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("access_log_decision_aggregate_since query failed: %s", exc)
            return []

    # -- LLM response cache ----------------------------------------------------
    #
    # The cache is a KV collection with a second Search (FTS) vector index over
    # it. Exact matches are a single KV get on a deterministic document ID -
    # no query, no index, sub-millisecond. Semantic matches reuse exactly the
    # pattern `discover_tools` already uses for the tool catalog: a
    # Conjunction pre-filter (provider + model + scope + namespace) evaluated
    # alongside a vector kNN over the prompt embedding, so an entry belonging
    # to a different model or a different tenant scope can never be returned
    # no matter how similar the prompt is.

    def _llm_cache_index_definition(self) -> dict:
        bucket = COUCHBASE_CONFIG["bucket"]
        scope = COUCHBASE_CONFIG["scope"]
        collection = COUCHBASE_CONFIG["llm_cache_collection"]
        type_key = f"{scope}.{collection}"

        def keyword_field(name: str) -> dict:
            # `keyword`, not `standard`: provider/model/scope values are
            # identifiers ("claude-sonnet-4-5", "role:finance_analyst"), and
            # the standard analyzer would tokenize them apart and turn an
            # exact pre-filter into a fuzzy one.
            return {
                "dynamic": False,
                "enabled": True,
                "fields": [{"name": name, "type": "text", "analyzer": "keyword", "index": True, "store": True}],
            }

        properties = {
            "provider": keyword_field("provider"),
            "model": keyword_field("model"),
            "scope_key": keyword_field("scope_key"),
            "namespace": keyword_field("namespace"),
            "embedding": {
                "dynamic": False,
                "enabled": True,
                "fields": [{
                    "name": "embedding",
                    "type": "vector",
                    "dims": EMBEDDING_CONFIG["vector_dim"],
                    "similarity": "dot_product",
                    "index": True,
                    "store": True,
                }],
            },
        }

        return {
            "type": "fulltext-index",
            "name": f"{bucket}.{scope}.{COUCHBASE_CONFIG['llm_cache_index']}",
            "sourceType": "gocbcore",
            "sourceName": bucket,
            "planParams": {"maxPartitionsPerPIndex": 512, "indexPartitions": 1},
            "params": {
                "doc_config": {"mode": "scope.collection.type_field", "type_field": "doc_type"},
                "mapping": {
                    "default_analyzer": "keyword",
                    "default_datetime_parser": "dateTimeOptional",
                    "default_field": "_all",
                    "default_mapping": {"dynamic": False, "enabled": False},
                    "default_type": "_default",
                    "docvalues_dynamic": False,
                    "index_dynamic": False,
                    "store_dynamic": False,
                    "type_field": "_type",
                    "types": {type_key: {"dynamic": False, "enabled": True, "properties": properties}},
                },
            },
            "store": {"indexType": "scorch", "segmentVersion": 16},
            "sourceParams": {},
        }

    async def ensure_llm_cache_index(self):
        def _upsert():
            auth = (COUCHBASE_CONFIG["username"], COUCHBASE_CONFIG["password"])
            index_name = COUCHBASE_CONFIG["llm_cache_index"]
            url = self._search_admin_url(index_name)
            existing = requests.get(url, auth=auth, timeout=10)
            if existing.status_code == 200:
                logger.info("LLM cache search index '%s' already exists", index_name)
                return
            resp = requests.put(url, auth=auth, json=self._llm_cache_index_definition(), timeout=15)
            if resp.status_code in (200, 201):
                logger.info("LLM cache search index '%s' created", index_name)
            else:
                logger.warning("LLM cache index creation returned %s: %s", resp.status_code, resp.text[:300])

        try:
            await asyncio.to_thread(_upsert)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM cache index setup failed (semantic lookups will fall back to exact): %s", exc)

    # -- Agent memory vector index -------------------------------------------

    def _agent_memory_index_definition(self) -> dict:
        bucket = COUCHBASE_CONFIG["bucket"]
        scope = COUCHBASE_CONFIG["scope"]
        collection = COUCHBASE_CONFIG["agent_memory_collection"]
        type_key = f"{scope}.{collection}"

        def keyword_field(name: str) -> dict:
            # Identifiers (user_id, session_id, memory_type), not prose -
            # `keyword` so a pre-filter term match is exact, not tokenized.
            return {
                "dynamic": False,
                "enabled": True,
                "fields": [{"name": name, "type": "text", "analyzer": "keyword", "index": True, "store": True}],
            }

        properties = {
            "user_id": keyword_field("user_id"),
            "session_id": keyword_field("session_id"),
            "memory_type": keyword_field("memory_type"),
            # Lets recall exclude entries consolidation has superseded. Used
            # as a must_not rather than a must, so an entry written before
            # this field existed - and therefore carrying no status at all -
            # is still returned rather than silently disappearing from
            # recall the moment this index is updated.
            "status": keyword_field("status"),
            "embedding": {
                "dynamic": False,
                "enabled": True,
                "fields": [{
                    "name": "embedding",
                    "type": "vector",
                    "dims": EMBEDDING_CONFIG["vector_dim"],
                    "similarity": "dot_product",
                    "index": True,
                    "store": True,
                }],
            },
        }

        return {
            "type": "fulltext-index",
            "name": f"{bucket}.{scope}.{COUCHBASE_CONFIG['agent_memory_index']}",
            "sourceType": "gocbcore",
            "sourceName": bucket,
            "planParams": {"maxPartitionsPerPIndex": 512, "indexPartitions": 1},
            "params": {
                "doc_config": {"mode": "scope.collection.type_field", "type_field": "doc_type"},
                "mapping": {
                    "default_analyzer": "keyword",
                    "default_datetime_parser": "dateTimeOptional",
                    "default_field": "_all",
                    "default_mapping": {"dynamic": False, "enabled": False},
                    "default_type": "_default",
                    "docvalues_dynamic": False,
                    "index_dynamic": False,
                    "store_dynamic": False,
                    "type_field": "_type",
                    "types": {type_key: {"dynamic": False, "enabled": True, "properties": properties}},
                },
            },
            "store": {"indexType": "scorch", "segmentVersion": 16},
            "sourceParams": {},
        }

    async def ensure_agent_memory_index(self):
        def _upsert():
            auth = (COUCHBASE_CONFIG["username"], COUCHBASE_CONFIG["password"])
            index_name = COUCHBASE_CONFIG["agent_memory_index"]
            url = self._search_admin_url(index_name)
            existing = requests.get(url, auth=auth, timeout=10)
            if existing.status_code == 200:
                logger.info("Agent memory search index '%s' already exists", index_name)
                return
            resp = requests.put(url, auth=auth, json=self._agent_memory_index_definition(), timeout=15)
            if resp.status_code in (200, 201):
                logger.info("Agent memory search index '%s' created", index_name)
            else:
                logger.warning("Agent memory index creation returned %s: %s", resp.status_code, resp.text[:300])

        try:
            await asyncio.to_thread(_upsert)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Agent memory index setup failed (semantic recall will be unavailable until it exists): %s", exc)

    async def get_cache_entry(self, entry_id: str) -> dict | None:
        def _get():
            try:
                return self.llm_cache.get(entry_id).content_as[dict]
            except DocumentNotFoundException:
                return None

        try:
            return await asyncio.to_thread(_get)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_cache_entry(%s) failed: %s", entry_id, exc)
            return None

    async def upsert_cache_entry(self, entry_id: str, doc: dict, ttl_seconds: int = 0):
        """Written with a Couchbase document expiry equal to the configured
        TTL (plus any stale-while-revalidate grace) so the cluster reclaims
        the space even if the background sweeper never runs. The logical TTL
        check in llm_cache.evaluate_entry still runs on read - expiry is the
        floor, not the policy."""
        def _upsert():
            if ttl_seconds and ttl_seconds > 0:
                self.llm_cache.upsert(entry_id, doc, UpsertOptions(expiry=timedelta(seconds=ttl_seconds)))
            else:
                self.llm_cache.upsert(entry_id, doc)

        try:
            await asyncio.to_thread(_upsert)
        except Exception as exc:  # noqa: BLE001
            logger.warning("upsert_cache_entry(%s) failed: %s", entry_id, exc)

    async def delete_cache_entry(self, entry_id: str) -> bool:
        def _delete():
            try:
                self.llm_cache.remove(entry_id)
                return True
            except DocumentNotFoundException:
                return False

        try:
            return await asyncio.to_thread(_delete)
        except Exception as exc:  # noqa: BLE001
            logger.warning("delete_cache_entry(%s) failed: %s", entry_id, exc)
            return False

    async def list_cache_entries(self, limit: int = 200) -> list[dict]:
        """Everything except `embedding` (384 floats/entry) and the full
        `response` body - the table only needs previews."""
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["llm_cache_collection"]

        def _run():
            field_names = [
                "entry_id", "provider", "model", "scope_key", "namespace", "prompt_preview",
                "response_preview", "prompt_tokens", "completion_tokens", "total_tokens",
                "cost_usd", "created_at", "last_hit_at", "hit_count", "exact_hits",
                "semantic_hits", "tokens_saved", "cost_saved_usd", "origin_latency_ms",
                "config_version", "catalog_version", "override", "stub",
            ]
            # Backtick-escape every field, not just `namespace` - it's the
            # one that's a N1QL reserved word today (this exact query used
            # to fail outright with "syntax error ... namespace (reserved
            # word)"), but escaping all of them is cheap insurance against
            # any future field name that collides with a future reserved
            # word. Escaping doesn't change the result set's field names -
            # N1QL's implicit alias for `c.\`namespace\`` is still
            # "namespace", so nothing downstream needs to change.
            fields = ", ".join(f"c.`{f}`" for f in field_names)
            q = (
                f"SELECT {fields} FROM `{bucket}`.`{scope}`.`{coll}` c USE INDEX (idx_llm_cache_created) "
                f"WHERE c.created_at IS NOT MISSING "
                f"ORDER BY c.created_at DESC LIMIT $limit"
            )
            return list(self.cluster.query(q, QueryOptions(named_parameters={"limit": limit}, metrics=False)).rows())

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_cache_entries query failed: %s", exc)
            return []

    async def count_cache_entries(self) -> int:
        return await self._count(COUCHBASE_CONFIG["llm_cache_collection"])

    async def purge_cache(self, provider: str | None = None, model: str | None = None, namespace: str | None = None) -> int:
        """Manual invalidation. With no arguments this is 'purge everything';
        the optional filters are what the LLM Caching page's per-provider and
        per-model purge buttons send."""
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["llm_cache_collection"]
        clauses, params = [], {}
        if provider:
            clauses.append("c.provider = $provider")
            params["provider"] = provider
        if model:
            clauses.append("c.model = $model")
            params["model"] = model
        if namespace:
            # Backtick-escaped: `namespace` is a N1QL reserved word - see
            # the matching fix/comment on list_cache_entries() above,
            # which hit the identical "syntax error ... namespace
            # (reserved word)" failure.
            clauses.append("c.`namespace` = $namespace")
            params["namespace"] = namespace
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

        def _run():
            q = f"DELETE FROM `{bucket}`.`{scope}`.`{coll}` c{where} RETURNING META(c).id"
            rows = list(self.cluster.query(q, QueryOptions(named_parameters=params, metrics=False)).rows())
            return len(rows)

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("purge_cache failed: %s", exc)
            return 0

    def _run_cache_search_sync(self, provider: str, model: str, scope_key: str, namespace: str, vector: list, top_k: int) -> list[dict]:
        index_name = COUCHBASE_CONFIG["llm_cache_index"]
        vector_query = CBVectorQuery.create("embedding", vector, num_candidates=max(top_k * 2, 20))
        vector_search = CBVectorSearch.from_vector_query(vector_query)

        prefilter = cb_search.ConjunctionQuery(
            cb_search.TermQuery(provider, field="provider"),
            cb_search.TermQuery(model, field="model"),
            cb_search.TermQuery(scope_key, field="scope_key"),
            cb_search.TermQuery(namespace, field="namespace"),
        )
        request = cb_search.SearchRequest.create(prefilter).with_vector_search(vector_search)
        result = self.scope.search(
            index_name,
            request,
            SearchOptions(limit=top_k, fields=["provider", "model", "scope_key", "namespace", "embedding"]),
        )
        return [{"id": row.id, "fields": row.fields or {}} for row in result.rows()]

    async def semantic_cache_lookup(
        self, provider: str, model: str, scope_key: str, namespace: str, query_vector: list, top_k: int = 20
    ) -> list[dict]:
        """Return [{entry_id, similarity}] best-first. Similarity is a plain
        dot product, which equals cosine here because ToolEmbeddings.embed
        L2-normalizes every vector it produces."""
        try:
            rows = await asyncio.to_thread(
                self._run_cache_search_sync, provider, model, scope_key, namespace, query_vector, top_k
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Semantic cache lookup failed (%s) - exact matching only for this call", exc)
            return []

        query_np = np.array(query_vector, dtype=np.float32)
        out = []
        for row in rows:
            stored = row["fields"].get("embedding")
            if not stored:
                continue
            similarity = float(np.dot(query_np, np.array(stored, dtype=np.float32)))
            out.append({"entry_id": row["id"], "similarity": round(similarity, 4)})
        out.sort(key=lambda r: r["similarity"], reverse=True)
        return out

    # -- LLM cache event log ---------------------------------------------------

    async def log_llm_event(self, doc: dict):
        doc_id = f"llmevt::{int(time.time() * 1000)}::{uuid.uuid4().hex[:8]}"
        try:
            await asyncio.to_thread(
                self.llm_cache_log.upsert,
                doc_id,
                doc,
                UpsertOptions(expiry=timedelta(hours=LLM_CACHE_LOG_RETENTION_HOURS)),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to write LLM cache event: %s", exc)

    async def recent_llm_events(self, limit: int = 500) -> list[dict]:
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["llm_cache_log_collection"]

        def _run():
            q = (
                f"SELECT e.* FROM `{bucket}`.`{scope}`.`{coll}` e USE INDEX (idx_llm_cache_log_recent) "
                f"WHERE e.timestamp IS NOT MISSING "
                f"ORDER BY e.timestamp DESC LIMIT $limit"
            )
            return list(self.cluster.query(q, QueryOptions(named_parameters={"limit": limit}, metrics=False)).rows())

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("recent_llm_events query failed: %s", exc)
            return []

    async def llm_role_provider_aggregate_since(self, since: str) -> list[dict]:
        """One GROUP BY (role, provider) aggregate over the LLM cache event
        log within a time window - the Dashboard topology diagram's
        agent -> LLM provider edges. Every completion counts, hit or miss -
        a cache hit is still the agent reaching the LLM Caching gateway for
        that provider, even though no tokens left the building. See
        idx_llm_cache_log_topology (couchbase-init/init.sh) for why this is
        a pure index scan."""
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["llm_cache_log_collection"]

        def _run():
            q = (
                f"SELECT e.`role` AS `role`, e.provider AS provider, COUNT(1) AS count, MAX(e.timestamp) AS last_at "
                f"FROM `{bucket}`.`{scope}`.`{coll}` e "
                f"WHERE e.timestamp >= $since AND e.`role` IS NOT MISSING AND e.provider IS NOT MISSING "
                f"GROUP BY e.`role`, e.provider"
            )
            return list(self.cluster.query(q, QueryOptions(named_parameters={"since": since}, metrics=False)).rows())

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm_role_provider_aggregate_since query failed: %s", exc)
            return []

    async def llm_dashboard_aggregate_since(self, since: str) -> list[dict]:
        """One GROUP BY query - by (hour, provider, model, outcome) - that
        supplies everything the LLM Caching dashboard needs: the 24h
        summary/donut, the 'Savings by provider & model' breakdown, and
        (via the trailing N hours of hour buckets) the trend chart. See
        llm_cache.build_dashboard_aggregate for how the rows are reduced
        into those three shapes.

        This replaces three separate full-window GROUP BY queries that
        used to run on every dashboard load/30s auto-refresh (one for
        per-outcome sums, one for per-model sums, one for per-hour
        counts) - each rescanning largely the same 24h of events. At real
        throughput (~230k+ events/24h observed) that was three ~230k-row
        scans instead of one, a meaningful chunk of the multi-second page
        load. A single query, reduced once in Python, removes that
        redundancy.

        Groups by the first 13 characters of the "%Y-%m-%dT%H:%M:%SZ"
        timestamp string (e.g. "2026-09-04T10") for the hour dimension -
        see idx_llm_cache_log_agg (couchbase-init/init.sh) for the
        covering index that lets this run as a pure index scan rather
        than a per-document KV fetch for every matching event."""
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["llm_cache_log_collection"]

        def _run():
            q = (
                f"SELECT SUBSTR(e.timestamp, 0, 13) AS hour_key, e.provider AS provider, "
                f"e.model AS model, e.outcome AS outcome, COUNT(1) AS n, "
                f"SUM(e.tokens_saved) AS tokens_saved, SUM(e.total_tokens) AS total_tokens, "
                f"SUM(e.cost_saved_usd) AS cost_saved_usd, SUM(e.cost_usd) AS cost_usd, "
                f"SUM(e.latency_saved_ms) AS latency_saved_ms, SUM(e.latency_ms) AS latency_ms_sum "
                f"FROM `{bucket}`.`{scope}`.`{coll}` e "
                f"WHERE e.timestamp >= $since "
                f"GROUP BY SUBSTR(e.timestamp, 0, 13), e.provider, e.model, e.outcome"
            )
            return list(self.cluster.query(
                q, QueryOptions(named_parameters={"since": since}, metrics=False)
            ).rows())

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm_dashboard_aggregate_since query failed: %s", exc)
            return []

    # -- Context cache (arbitrary agent-fetched data, not LLM completions) -----
    #
    # Exact-key KV cache, no vector index - see app/context_cache.py's module
    # docstring for why semantic matching doesn't apply here. One KV get on a
    # deterministic document ID, same shape as the LLM cache's exact path
    # without the "and then try semantic" fallback.

    async def get_context_entry(self, entry_id: str) -> dict | None:
        def _get():
            try:
                return self.context_cache.get(entry_id).content_as[dict]
            except DocumentNotFoundException:
                return None

        try:
            return await asyncio.to_thread(_get)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_context_entry(%s) failed: %s", entry_id, exc)
            return None

    async def upsert_context_entry(self, entry_id: str, doc: dict, ttl_seconds: int = 0):
        def _upsert():
            if ttl_seconds and ttl_seconds > 0:
                self.context_cache.upsert(entry_id, doc, UpsertOptions(expiry=timedelta(seconds=ttl_seconds)))
            else:
                self.context_cache.upsert(entry_id, doc)

        try:
            await asyncio.to_thread(_upsert)
        except Exception as exc:  # noqa: BLE001
            logger.warning("upsert_context_entry(%s) failed: %s", entry_id, exc)

    async def delete_context_entry(self, entry_id: str) -> bool:
        def _delete():
            try:
                self.context_cache.remove(entry_id)
                return True
            except DocumentNotFoundException:
                return False

        try:
            return await asyncio.to_thread(_delete)
        except Exception as exc:  # noqa: BLE001
            logger.warning("delete_context_entry(%s) failed: %s", entry_id, exc)
            return False

    async def list_context_entries(self, limit: int = 200) -> list[dict]:
        """Everything except the raw `value` - the table only needs previews."""
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["context_cache_collection"]

        def _run():
            field_names = [
                "entry_id", "namespace", "scope_key", "subject", "role", "key_preview",
                "value_preview", "value_bytes", "created_at", "last_hit_at", "hit_count",
                "ttl_seconds", "origin_latency_ms",
            ]
            fields = ", ".join(f"c.`{f}`" for f in field_names)
            q = (
                f"SELECT {fields} FROM `{bucket}`.`{scope}`.`{coll}` c USE INDEX (idx_context_cache_created) "
                f"WHERE c.created_at IS NOT MISSING "
                f"ORDER BY c.created_at DESC LIMIT $limit"
            )
            return list(self.cluster.query(q, QueryOptions(named_parameters={"limit": limit}, metrics=False)).rows())

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_context_entries query failed: %s", exc)
            return []

    async def count_context_entries(self) -> int:
        return await self._count(COUCHBASE_CONFIG["context_cache_collection"])

    async def purge_context_cache(self, namespace: str | None = None, agent: str | None = None) -> int:
        """Manual invalidation. With no arguments this is 'purge everything';
        the optional filters are what the Context Cache page's per-namespace
        and per-agent purge controls send."""
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["context_cache_collection"]
        clauses, params = [], {}
        if namespace:
            clauses.append("c.`namespace` = $namespace")
            params["namespace"] = namespace
        if agent:
            clauses.append("c.subject = $agent")
            params["agent"] = agent
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

        def _run():
            q = f"DELETE FROM `{bucket}`.`{scope}`.`{coll}` c{where} RETURNING META(c).id"
            rows = list(self.cluster.query(q, QueryOptions(named_parameters=params, metrics=False)).rows())
            return len(rows)

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("purge_context_cache failed: %s", exc)
            return 0

    # -- Context cache event log ------------------------------------------------

    async def log_context_event(self, doc: dict):
        doc_id = f"ctxevt::{int(time.time() * 1000)}::{uuid.uuid4().hex[:8]}"
        try:
            await asyncio.to_thread(
                self.context_cache_log.upsert,
                doc_id,
                doc,
                UpsertOptions(expiry=timedelta(hours=CONTEXT_CACHE_LOG_RETENTION_HOURS)),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to write context cache event: %s", exc)

    async def recent_context_events(self, limit: int = 500) -> list[dict]:
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["context_cache_log_collection"]

        def _run():
            q = (
                f"SELECT e.* FROM `{bucket}`.`{scope}`.`{coll}` e USE INDEX (idx_context_cache_log_recent) "
                f"WHERE e.timestamp IS NOT MISSING "
                f"ORDER BY e.timestamp DESC LIMIT $limit"
            )
            return list(self.cluster.query(q, QueryOptions(named_parameters={"limit": limit}, metrics=False)).rows())

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("recent_context_events query failed: %s", exc)
            return []

    async def context_dashboard_aggregate_since(self, since: str) -> list[dict]:
        """One GROUP BY query - by (hour, namespace, subject, outcome) - that
        supplies everything the Context Cache dashboard needs: the 24h
        summary, the 'Traffic by agent & namespace' breakdown, and (via the
        trailing N hours of hour buckets) the trend chart. See
        context_cache.build_dashboard_aggregate for how the rows are
        reduced. Same reasoning as llm_dashboard_aggregate_since: one scan
        instead of three, and a pure index scan via
        idx_context_cache_log_agg (couchbase-init/init.sh)."""
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["context_cache_log_collection"]

        def _run():
            q = (
                f"SELECT SUBSTR(e.timestamp, 0, 13) AS hour_key, e.`namespace` AS `namespace`, "
                f"e.subject AS subject, e.outcome AS outcome, COUNT(1) AS n, "
                f"SUM(e.latency_saved_ms) AS latency_saved_ms, SUM(e.latency_ms) AS latency_ms_sum, "
                f"SUM(e.value_bytes) AS value_bytes_sum "
                f"FROM `{bucket}`.`{scope}`.`{coll}` e "
                f"WHERE e.timestamp >= $since "
                f"GROUP BY SUBSTR(e.timestamp, 0, 13), e.`namespace`, e.subject, e.outcome"
            )
            return list(self.cluster.query(
                q, QueryOptions(named_parameters={"since": since}, metrics=False)
            ).rows())

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("context_dashboard_aggregate_since query failed: %s", exc)
            return []

    # -- Context cache lifetime stats ----------------------------------------
    #
    # Same never-windowed/CAS-retry convention as the LLM cache's
    # _LIFETIME_STATS_DOC_ID above - see that block's comment for why the
    # headline stat cards need a source that doesn't plateau with the event
    # log's retention window.

    _CONTEXT_LIFETIME_STATS_DOC_ID = "context_lifetime_stats"

    def _bootstrap_context_lifetime_stats(self) -> dict:
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["context_cache_log_collection"]
        baseline_lookups = 0
        baseline_hits = 0
        baseline_latency_saved = 0
        try:
            q = (
                f"SELECT e.outcome AS outcome, COUNT(1) AS n, SUM(e.latency_saved_ms) AS latency_saved_ms "
                f"FROM `{bucket}`.`{scope}`.`{coll}` e "
                f"WHERE e.outcome IN ['hit', 'miss'] GROUP BY e.outcome"
            )
            for row in self.cluster.query(q, QueryOptions(metrics=False)).rows():
                n = int(row.get("n") or 0)
                baseline_lookups += n
                if row.get("outcome") == "hit":
                    baseline_hits += n
                    baseline_latency_saved += int(row.get("latency_saved_ms") or 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Context cache lifetime stats bootstrap scan failed, starting from zero: %s", exc)
        doc = {
            "doc_type": "context_lifetime_stats",
            "lookups_total": baseline_lookups,
            "hits_total": baseline_hits,
            "latency_saved_ms_total": baseline_latency_saved,
        }
        try:
            self.settings.insert(self._CONTEXT_LIFETIME_STATS_DOC_ID, doc)
        except Exception:
            try:
                return self.settings.get(self._CONTEXT_LIFETIME_STATS_DOC_ID).content_as[dict]
            except Exception:  # noqa: BLE001
                pass
        return doc

    async def get_context_lifetime_stats(self) -> dict:
        doc = await self.get_setting(self._CONTEXT_LIFETIME_STATS_DOC_ID)
        if doc is not None:
            return doc
        try:
            return await asyncio.to_thread(self._bootstrap_context_lifetime_stats)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_context_lifetime_stats bootstrap failed: %s", exc)
            return {"lookups_total": 0, "hits_total": 0, "latency_saved_ms_total": 0}

    async def increment_context_lifetime_stats(self, *, hit: bool, latency_saved_ms: int = 0) -> dict:
        """Atomically fold a single get()'s outcome into the all-time
        counters. CAS retry loop since concurrent requests hit this at once."""
        def _apply_delta(doc: dict) -> dict:
            doc["lookups_total"] = int(doc.get("lookups_total") or 0) + 1
            if hit:
                doc["hits_total"] = int(doc.get("hits_total") or 0) + 1
                doc["latency_saved_ms_total"] = int(doc.get("latency_saved_ms_total") or 0) + max(0, latency_saved_ms)
            return doc

        def _apply():
            for _ in range(8):
                try:
                    res = self.settings.get(self._CONTEXT_LIFETIME_STATS_DOC_ID)
                    doc = _apply_delta(res.content_as[dict])
                    self.settings.replace(self._CONTEXT_LIFETIME_STATS_DOC_ID, doc, ReplaceOptions(cas=res.cas))
                    return doc
                except DocumentNotFoundException:
                    seed = _apply_delta(self._bootstrap_context_lifetime_stats())
                    try:
                        self.settings.replace(self._CONTEXT_LIFETIME_STATS_DOC_ID, seed)
                    except Exception:  # noqa: BLE001
                        pass
                    return seed
                except CasMismatchException:
                    continue
            logger.warning("increment_context_lifetime_stats: giving up after CAS retries")
            return {}

        try:
            return await asyncio.to_thread(_apply)
        except Exception as exc:  # noqa: BLE001
            logger.warning("increment_context_lifetime_stats failed: %s", exc)
            return {}

    # -- Settings (user-editable runtime policy) -------------------------------

    async def get_setting(self, doc_id: str) -> dict | None:
        def _get():
            try:
                return self.settings.get(doc_id).content_as[dict]
            except DocumentNotFoundException:
                return None

        try:
            return await asyncio.to_thread(_get)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_setting(%s) failed: %s", doc_id, exc)
            return None

    async def upsert_setting(self, doc_id: str, doc: dict):
        try:
            await asyncio.to_thread(self.settings.upsert, doc_id, doc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("upsert_setting(%s) failed: %s", doc_id, exc)

    # -- LLM cache lifetime stats -------------------------------------------
    #
    # The dashboard's headline "tokens saved" / "cost saved" summary is
    # deliberately derived from `events` (see build_dashboard) - a
    # fixed-count, TTL-bounded window, so it plateaus once traffic outruns
    # that window. These two counters are the answer to a different
    # question - "how much has this cache saved, ever?" - and live in a
    # single settings document that is never windowed and never expires,
    # updated with a CAS retry loop so concurrent requests can't clobber
    # each other's increments.

    _LIFETIME_STATS_DOC_ID = "llm_lifetime_stats"

    @staticmethod
    def _model_key(provider: str | None, model: str | None) -> str:
        return f"{provider or 'unknown'}::{model or 'unknown'}"

    def _bootstrap_lifetime_stats(self) -> dict:
        """First-touch initialization: sum whatever cache-hit/miss history
        is still live in Couchbase (bounded by the event log's own TTL) so
        the counters start from something sensible instead of visibly
        dropping to zero the moment this feature is deployed. Only ever
        runs once - after the settings document exists, every call just
        increments it.

        Populates both the top-level all-time totals (tokens/cost saved,
        never windowed - see the dashboard's "Total ..." stat cards) and a
        per (provider, model) breakdown (cost saved/spent - see the
        'Savings by provider & model' table's Cost saved / Cost spent
        columns, which show all-time amounts rather than the 24h window
        the rest of that table uses)."""
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["llm_cache_log_collection"]
        baseline_tokens = 0
        baseline_cost_saved = 0.0
        by_model: dict[str, dict] = {}
        try:
            q = (
                f"SELECT e.provider AS provider, e.model AS model, e.outcome AS outcome, "
                f"SUM(e.tokens_saved) AS tokens_saved, SUM(e.cost_saved_usd) AS cost_saved_usd, "
                f"SUM(e.cost_usd) AS cost_usd "
                f"FROM `{bucket}`.`{scope}`.`{coll}` e "
                f"WHERE e.outcome IN ['hit_exact', 'hit_semantic', 'miss'] "
                f"GROUP BY e.provider, e.model, e.outcome"
            )
            for row in self.cluster.query(q, QueryOptions(metrics=False)).rows():
                key = self._model_key(row.get("provider"), row.get("model"))
                entry = by_model.setdefault(key, {"cost_saved_usd_total": 0.0, "cost_spent_usd_total": 0.0})
                if row.get("outcome") in ("hit_exact", "hit_semantic"):
                    baseline_tokens += int(row.get("tokens_saved") or 0)
                    cost_saved = float(row.get("cost_saved_usd") or 0.0)
                    baseline_cost_saved += cost_saved
                    entry["cost_saved_usd_total"] += cost_saved
                elif row.get("outcome") == "miss":
                    entry["cost_spent_usd_total"] += float(row.get("cost_usd") or 0.0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Lifetime stats bootstrap scan failed, starting from zero: %s", exc)
        for entry in by_model.values():
            entry["cost_saved_usd_total"] = round(entry["cost_saved_usd_total"], 6)
            entry["cost_spent_usd_total"] = round(entry["cost_spent_usd_total"], 6)
        doc = {
            "doc_type": "llm_lifetime_stats",
            "tokens_saved_total": baseline_tokens,
            "cost_saved_usd_total": round(baseline_cost_saved, 6),
            "by_model": by_model,
        }
        try:
            self.settings.insert(self._LIFETIME_STATS_DOC_ID, doc)
        except Exception:
            # Someone else bootstrapped it concurrently - use whatever they wrote.
            try:
                return self.settings.get(self._LIFETIME_STATS_DOC_ID).content_as[dict]
            except Exception:  # noqa: BLE001
                pass
        return doc

    async def get_lifetime_stats(self) -> dict:
        doc = await self.get_setting(self._LIFETIME_STATS_DOC_ID)
        if doc is not None:
            return doc
        try:
            return await asyncio.to_thread(self._bootstrap_lifetime_stats)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_lifetime_stats bootstrap failed: %s", exc)
            return {"tokens_saved_total": 0, "cost_saved_usd_total": 0.0, "by_model": {}}

    async def increment_lifetime_stats(
        self,
        *,
        tokens_saved_delta: int = 0,
        cost_saved_delta: float = 0.0,
        cost_spent_delta: float = 0.0,
        provider: str | None = None,
        model: str | None = None,
    ) -> dict:
        """Atomically fold a single request's outcome into the all-time
        counters. Hits pass tokens_saved_delta/cost_saved_delta; genuine
        misses (not bypasses) pass cost_spent_delta - both also update the
        per (provider, model) entry keyed by `provider`/`model` so the
        'Savings by provider & model' table's Cost saved/Cost spent columns
        can show all-time totals. CAS retry loop since concurrent requests
        hit this at once."""
        if not tokens_saved_delta and not cost_saved_delta and not cost_spent_delta:
            return await self.get_lifetime_stats()
        model_key = self._model_key(provider, model) if (provider or model) else None

        def _apply_delta(doc: dict) -> dict:
            doc["tokens_saved_total"] = int(doc.get("tokens_saved_total") or 0) + tokens_saved_delta
            doc["cost_saved_usd_total"] = round(
                float(doc.get("cost_saved_usd_total") or 0.0) + cost_saved_delta, 6
            )
            if model_key:
                by_model = doc.setdefault("by_model", {})
                entry = by_model.setdefault(model_key, {"cost_saved_usd_total": 0.0, "cost_spent_usd_total": 0.0})
                entry["cost_saved_usd_total"] = round(
                    float(entry.get("cost_saved_usd_total") or 0.0) + cost_saved_delta, 6
                )
                entry["cost_spent_usd_total"] = round(
                    float(entry.get("cost_spent_usd_total") or 0.0) + cost_spent_delta, 6
                )
            return doc

        def _apply():
            for _ in range(8):
                try:
                    res = self.settings.get(self._LIFETIME_STATS_DOC_ID)
                    doc = _apply_delta(res.content_as[dict])
                    self.settings.replace(self._LIFETIME_STATS_DOC_ID, doc, ReplaceOptions(cas=res.cas))
                    return doc
                except DocumentNotFoundException:
                    seed = _apply_delta(self._bootstrap_lifetime_stats())
                    try:
                        self.settings.replace(self._LIFETIME_STATS_DOC_ID, seed)
                    except Exception:  # noqa: BLE001
                        pass
                    return seed
                except CasMismatchException:
                    continue
            logger.warning("increment_lifetime_stats: giving up after CAS retries")
            return {}

        try:
            return await asyncio.to_thread(_apply)
        except Exception as exc:  # noqa: BLE001
            logger.warning("increment_lifetime_stats failed: %s", exc)
            return {}

    # -- Agent memory ------------------------------------------------------
    #
    # Durable, cross-session recall for agents - the counterpart to the tool
    # catalog's RBAC + vector search, scoped by user_id (and optionally
    # session_id/memory_type) instead of by role. See app/agent_memory.py
    # for what a memory document looks like and how it's validated before
    # it ever reaches here.

    async def upsert_memory(self, memory_id: str, doc: dict, ttl_seconds: int = 0):
        def _upsert():
            if ttl_seconds and ttl_seconds > 0:
                self.agent_memory.upsert(memory_id, doc, UpsertOptions(expiry=timedelta(seconds=ttl_seconds)))
            else:
                # An edit or a consolidation rewrite of an existing entry
                # must keep that entry's retention: a plain upsert resets
                # the document expiry to "never", which silently turns a
                # memory written with a TTL into a permanent one. On a brand
                # new document there is no expiry to preserve.
                self.agent_memory.upsert(memory_id, doc, UpsertOptions(preserve_expiry=True))

        try:
            await asyncio.to_thread(_upsert)
        except Exception as exc:  # noqa: BLE001
            logger.warning("upsert_memory(%s) failed: %s", memory_id, exc)
            raise

    async def get_memory(self, memory_id: str) -> dict | None:
        def _get():
            try:
                return self.agent_memory.get(memory_id).content_as[dict]
            except DocumentNotFoundException:
                return None

        try:
            return await asyncio.to_thread(_get)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_memory(%s) failed: %s", memory_id, exc)
            return None

    async def delete_memory(self, memory_id: str) -> bool:
        def _delete():
            try:
                self.agent_memory.remove(memory_id)
                return True
            except DocumentNotFoundException:
                return False

        try:
            return await asyncio.to_thread(_delete)
        except Exception as exc:  # noqa: BLE001
            logger.warning("delete_memory(%s) failed: %s", memory_id, exc)
            return False

    # Every listing selects this set rather than `m.*`, so the 384-float
    # embedding never leaves the cluster for a view that cannot use it.
    _MEMORY_VIEW_FIELDS = (
        "META(m).id AS memory_id, m.user_id, m.session_id, m.memory_type, m.content, "
        "m.metadata, m.created_at, m.updated_at, m.status, m.importance, m.recall_count, "
        "m.reinforcement_count, m.superseded_by, m.superseded_at, m.consolidated_from, "
        "m.consolidation_kind, m.consolidated_at"
    )

    async def list_memory(
        self,
        user_id: str,
        session_id: str | None = None,
        memory_type: str | None = None,
        limit: int = 100,
        status: str | None = "active",
    ) -> list[dict]:
        """Chronological listing, newest first.

        `status` defaults to "active" so every existing caller - recall's
        recency fallback above all - keeps getting only live memories.
        The operator UI passes None to see superseded entries too, which is
        the whole point of superseding rather than deleting.
        """
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["agent_memory_collection"]
        clauses, params = ["m.user_id = $user_id"], {"user_id": user_id, "limit": min(limit, 500)}
        if session_id:
            clauses.append("m.session_id = $session_id")
            params["session_id"] = session_id
        if memory_type:
            clauses.append("m.memory_type = $memory_type")
            params["memory_type"] = memory_type
        if status == "active":
            # IS MISSING as well as the explicit value: entries written
            # before the status field existed are active.
            clauses.append('(m.status = "active" OR m.status IS MISSING)')
        elif status:
            clauses.append("m.status = $status")
            params["status"] = status
        where = " AND ".join(clauses)

        def _run():
            q = (
                f"SELECT {self._MEMORY_VIEW_FIELDS} "
                f"FROM `{bucket}`.`{scope}`.`{coll}` m "
                f"WHERE {where} ORDER BY m.created_at DESC LIMIT $limit"
            )
            return list(self.cluster.query(q, QueryOptions(named_parameters=params, metrics=False)).rows())

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_memory query failed: %s", exc)
            return []

    async def count_memory_entries(self) -> int:
        return await self._count(COUCHBASE_CONFIG["agent_memory_collection"])

    async def clear_memory(self, user_id: str, session_id: str | None = None) -> int:
        """Bulk-delete everything for a user, or narrowed to one session -
        e.g. an agent wiping short-term conversational memory at the end of
        a session while leaving that user's durable profile memories alone."""
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["agent_memory_collection"]
        clauses, params = ["m.user_id = $user_id"], {"user_id": user_id}
        if session_id:
            clauses.append("m.session_id = $session_id")
            params["session_id"] = session_id
        where = " AND ".join(clauses)

        def _run():
            q = f"DELETE FROM `{bucket}`.`{scope}`.`{coll}` m WHERE {where} RETURNING META(m).id"
            rows = list(self.cluster.query(q, QueryOptions(named_parameters=params, metrics=False)).rows())
            return len(rows)

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("clear_memory failed: %s", exc)
            return 0

    def _run_memory_search_sync(
        self, user_id: str, session_id: str | None, memory_type: str | None, vector: list, top_k: int
    ) -> list[dict]:
        index_name = COUCHBASE_CONFIG["agent_memory_index"]
        vector_query = CBVectorQuery.create("embedding", vector, num_candidates=max(top_k * 4, 25))
        vector_search = CBVectorSearch.from_vector_query(vector_query)

        # user_id always scopes the search - one user's memory is never
        # returned for another's recall, however similar the prompt.
        # session_id/memory_type narrow it further only when the caller asks.
        must = [cb_search.TermQuery(user_id, field="user_id")]
        if session_id:
            must.append(cb_search.TermQuery(session_id, field="session_id"))
        if memory_type:
            must.append(cb_search.TermQuery(memory_type, field="memory_type"))
        # must_not, not a must on status="active": documents written before
        # the status field existed have no status to match, and a positive
        # filter would drop every one of them from recall.
        prefilter = cb_search.BooleanQuery(
            must=cb_search.ConjunctionQuery(*must),
            must_not=cb_search.DisjunctionQuery(cb_search.TermQuery("superseded", field="status")),
        )

        request = cb_search.SearchRequest.create(prefilter).with_vector_search(vector_search)
        result = self.scope.search(
            index_name,
            request,
            SearchOptions(limit=top_k, fields=["user_id", "session_id", "memory_type", "embedding", "status"]),
        )
        return [{"id": row.id, "fields": row.fields or {}} for row in result.rows()]

    async def search_memory(
        self,
        user_id: str,
        query_vector: list,
        session_id: str | None = None,
        memory_type: str | None = None,
        top_k: int = 5,
    ) -> list[dict]:
        """Semantic recall: vector similarity over the memory embedding,
        scoped to this user (and optionally session/type). Falls back to
        the most recent entries (still scoped the same way) if the Search
        index isn't ready yet, so recall degrades to "recency" rather than
        failing outright."""
        try:
            rows = await asyncio.to_thread(
                self._run_memory_search_sync, user_id, session_id, memory_type, query_vector, top_k
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Semantic memory search failed (%s) - falling back to recency", exc)
            recent = await self.list_memory(user_id, session_id=session_id, memory_type=memory_type, limit=top_k)
            return [{**r, "similarity": None} for r in recent]

        if not rows:
            return []

        query_np = np.array(query_vector, dtype=np.float32)
        memory_ids = []
        similarity_by_id = {}
        for row in rows:
            stored = row["fields"].get("embedding")
            if not stored:
                continue
            similarity_by_id[row["id"]] = round(float(np.dot(query_np, np.array(stored, dtype=np.float32))), 4)
            memory_ids.append(row["id"])
        if not memory_ids:
            return []

        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["agent_memory_collection"]

        def _fetch():
            # The index pre-filter already excludes superseded entries; this
            # repeats it against the document itself, so an index that has
            # not caught up with a just-superseded entry cannot surface it.
            q = (
                f"SELECT {self._MEMORY_VIEW_FIELDS} "
                f"FROM `{bucket}`.`{scope}`.`{coll}` m WHERE META(m).id IN $ids "
                f'AND (m.status = "active" OR m.status IS MISSING)'
            )
            return list(self.cluster.query(q, QueryOptions(named_parameters={"ids": memory_ids}, metrics=False)).rows())

        try:
            docs = await asyncio.to_thread(_fetch)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Fetching matched memory documents failed: %s", exc)
            return []

        out = [{**doc, "similarity": similarity_by_id.get(doc["memory_id"])} for doc in docs]
        out.sort(key=lambda m: m["similarity"] or 0.0, reverse=True)
        return out

    # -- Local dashboard accounts (see app/user_auth.py) --------------------
    #
    # Human login for the dashboard UI itself - distinct from the identities
    # collection above, which maps agent API keys to an RBAC role. Username
    # (lowercased) is the document id, same as server_id is for `servers`.

    async def upsert_user(self, username: str, doc: dict):
        await asyncio.to_thread(self.users.upsert, username.lower(), doc)

    async def get_user(self, username: str) -> dict | None:
        def _get():
            try:
                return self.users.get(username.lower()).content_as[dict]
            except DocumentNotFoundException:
                return None

        return await asyncio.to_thread(_get)

    async def delete_user(self, username: str) -> bool:
        def _delete():
            try:
                self.users.remove(username.lower())
                return True
            except DocumentNotFoundException:
                return False

        return await asyncio.to_thread(_delete)

    async def list_users(self) -> list[dict]:
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["users_collection"]

        def _run():
            # password_hash never leaves Couchbase for a list call.
            q = (
                f"SELECT META(u).id AS username, u.`role`, u.source, u.active, u.must_change_password, "
                f"u.password_hash, u.created_at, u.updated_at, u.last_login_at "
                f"FROM `{bucket}`.`{scope}`.`{coll}` u ORDER BY META(u).id"
            )
            return list(self.cluster.query(q, QueryOptions(metrics=False)).rows())

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_users query failed: %s", exc)
            return []

    async def count_users(self) -> int:
        return await self._count(COUCHBASE_CONFIG["users_collection"])

    # -- Rate-limit / budget counters ----------------------------------------
    # A KV atomic counter with the window as its document expiry. The window
    # rolls itself (an expired counter is simply gone, and the next key is a
    # different document - see governance.counter_key), the value is correct
    # across a restart, and two replicas of this service agree without
    # coordinating. That is a distributed token bucket with nothing extra in
    # the deployment, which is the whole argument for doing it here rather
    # than in a process-local dict.

    async def incr_counter(self, key: str, delta: int = 1, expiry_seconds: int = 90) -> int:
        """Advance a counter and return its new value. Returns the delta
        itself if Couchbase is unreachable: a limiter that cannot count must
        not silently report zero usage, because zero means 'plenty of budget
        left' and would turn an outage into an unmetered free-for-all."""
        if not self.connected or self.counters is None:
            return delta

        def _run():
            result = self.counters.binary().increment(
                key,
                IncrementOptions(
                    delta=DeltaValue(max(1, int(delta))),
                    initial=SignedInt64(max(1, int(delta))),
                    expiry=timedelta(seconds=max(1, int(expiry_seconds))),
                ),
            )
            return int(result.content)

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Counter increment failed for %s: %s", key, exc)
            return delta

    async def read_counter(self, key: str) -> int:
        """Current value without advancing it - what the Limits & Budgets
        page shows. A missing counter is an empty window, which is zero."""
        if not self.connected or self.counters is None:
            return 0

        def _get():
            try:
                return int(self.counters.get(key).content_as[int])
            except (DocumentNotFoundException, ValueError, TypeError):
                return 0

        try:
            return await asyncio.to_thread(_get)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Counter read failed for %s: %s", key, exc)
            return 0

    # -- Agent run tracing --------------------------------------------------
    # Spans and their run summaries share one collection, distinguished by
    # doc_type, because every query that wants one wants the other nearby -
    # a run row on the Traces list, then its spans on the detail view.

    async def write_span(self, span: dict):
        doc_id = f"span::{span['trace_id']}::{span['span_id']}"
        try:
            await asyncio.to_thread(
                self.traces.upsert, doc_id, span,
                UpsertOptions(expiry=timedelta(hours=TRACE_RETENTION_HOURS)),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to write span %s: %s", doc_id, exc)

    async def get_run(self, trace_id: str) -> dict | None:
        def _get():
            try:
                return self.traces.get(f"run::{trace_id}").content_as[dict]
            except DocumentNotFoundException:
                return None

        try:
            return await asyncio.to_thread(_get)
        except Exception as exc:  # noqa: BLE001
            logger.debug("get_run(%s) failed: %s", trace_id, exc)
            return None

    async def upsert_run(self, run: dict):
        try:
            await asyncio.to_thread(
                self.traces.upsert, f"run::{run['trace_id']}", run,
                UpsertOptions(expiry=timedelta(hours=TRACE_RETENTION_HOURS)),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to write run summary %s: %s", run.get("trace_id"), exc)

    async def list_runs(self, limit: int = 100, role: str | None = None, status: str | None = None) -> list[dict]:
        """Run summaries, newest first. Reads only the pre-folded summary
        documents - never an aggregate over the span collection, which is
        the query that stops being viable once traces are real."""
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["traces_collection"]
        params = {"limit": limit}
        clauses = ['r.doc_type = "agent_run"']
        if role:
            clauses.append("r.`role` = $role")
            params["role"] = role
        if status:
            clauses.append("r.status = $status")
            params["status"] = status

        def _run():
            q = (
                f"SELECT r.* FROM `{bucket}`.`{scope}`.`{coll}` r "
                f"WHERE {' AND '.join(clauses)} "
                f"ORDER BY r.started_at DESC LIMIT $limit"
            )
            return list(self.cluster.query(q, QueryOptions(named_parameters=params, metrics=False)).rows())

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_runs query failed: %s", exc)
            return []

    async def list_spans(self, trace_id: str, limit: int = 500) -> list[dict]:
        """Every span in one run, oldest first - the order a timeline is
        read in."""
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["traces_collection"]

        def _run():
            q = (
                f"SELECT s.* FROM `{bucket}`.`{scope}`.`{coll}` s "
                f'WHERE s.doc_type = "agent_span" AND s.trace_id = $trace_id '
                f"ORDER BY s.started_epoch_ms ASC LIMIT $limit"
            )
            return list(self.cluster.query(
                q, QueryOptions(named_parameters={"trace_id": trace_id, "limit": limit}, metrics=False)
            ).rows())

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_spans(%s) query failed: %s", trace_id, exc)
            return []

    async def trace_aggregate_since(self, since: str) -> list[dict]:
        """One GROUP BY over run summaries for the Traces page header - runs,
        errors, tokens and spend per role within a window."""
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["traces_collection"]

        def _run():
            q = (
                f"SELECT r.`role` AS `role`, COUNT(1) AS runs, SUM(r.error_count) AS errors, "
                f"SUM(r.total_tokens) AS tokens, SUM(r.cost_usd) AS cost_usd, "
                f"SUM(r.cache_hits) AS cache_hits, SUM(r.hijack_flags) AS hijack_flags, "
                f"SUM(r.limit_blocks) AS limit_blocks "
                f"FROM `{bucket}`.`{scope}`.`{coll}` r "
                f'WHERE r.doc_type = "agent_run" AND r.started_at >= $since '
                f"GROUP BY r.`role`"
            )
            return list(self.cluster.query(q, QueryOptions(named_parameters={"since": since}, metrics=False)).rows())

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("trace_aggregate_since query failed: %s", exc)
            return []

    async def count_runs(self) -> int:
        return await self._count_where(COUCHBASE_CONFIG["traces_collection"], "agent_run")

    async def _count_where(self, collection_name: str, doc_type: str) -> int:
        if not self.connected:
            return 0
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]

        def _run():
            q = (
                f"SELECT RAW COUNT(*) FROM `{bucket}`.`{scope}`.`{collection_name}` d "
                f"WHERE d.doc_type = $doc_type"
            )
            rows = list(self.cluster.query(
                q, QueryOptions(named_parameters={"doc_type": doc_type}, metrics=False)
            ).rows())
            return rows[0] if rows else 0

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.debug("count_where(%s, %s) failed: %s", collection_name, doc_type, exc)
            return 0

    # -- Evaluation datasets and runs ---------------------------------------
    # Same one-collection-two-doc_types shape as traces above, and for the
    # same reason: a dataset and its runs are always read together.

    async def upsert_dataset(self, dataset: dict):
        await asyncio.to_thread(self.evals.upsert, f"dataset::{dataset['dataset_id']}", dataset)

    async def get_dataset(self, dataset_id: str) -> dict | None:
        def _get():
            try:
                return self.evals.get(f"dataset::{dataset_id}").content_as[dict]
            except DocumentNotFoundException:
                return None

        return await asyncio.to_thread(_get)

    async def delete_dataset(self, dataset_id: str) -> bool:
        def _delete():
            try:
                self.evals.remove(f"dataset::{dataset_id}")
                return True
            except DocumentNotFoundException:
                return False

        return await asyncio.to_thread(_delete)

    async def list_datasets(self) -> list[dict]:
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["evals_collection"]

        def _run():
            q = (
                f"SELECT d.* FROM `{bucket}`.`{scope}`.`{coll}` d "
                f'WHERE d.doc_type = "eval_dataset" ORDER BY d.name ASC'
            )
            return list(self.cluster.query(q, QueryOptions(metrics=False)).rows())

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_datasets query failed: %s", exc)
            return []

    async def upsert_eval_run(self, run: dict):
        await asyncio.to_thread(self.evals.upsert, f"evalrun::{run['run_id']}", run)

    async def get_eval_run(self, run_id: str) -> dict | None:
        def _get():
            try:
                return self.evals.get(f"evalrun::{run_id}").content_as[dict]
            except DocumentNotFoundException:
                return None

        return await asyncio.to_thread(_get)

    async def list_eval_runs(self, dataset_id: str | None = None, limit: int = 50, include_results: bool = False) -> list[dict]:
        """Run history, newest first. `include_results` is off by default:
        the per-case results are the bulk of a run document and the history
        list only needs the headline numbers."""
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["evals_collection"]
        params = {"limit": limit}
        clauses = ['r.doc_type = "eval_run"']
        if dataset_id:
            clauses.append("r.dataset_id = $dataset_id")
            params["dataset_id"] = dataset_id

        fields = "r.*" if include_results else (
            "r.run_id, r.dataset_id, r.dataset_name, r.trigger, r.started_at, r.finished_at, "
            "r.duration_ms, r.score, r.case_count, r.passed, r.failed, r.errored, r.status, r.comparison"
        )

        def _run():
            q = (
                f"SELECT {fields} FROM `{bucket}`.`{scope}`.`{coll}` r "
                f"WHERE {' AND '.join(clauses)} "
                f"ORDER BY r.started_at DESC LIMIT $limit"
            )
            return list(self.cluster.query(q, QueryOptions(named_parameters=params, metrics=False)).rows())

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_eval_runs query failed: %s", exc)
            return []

    async def latest_eval_run(self, dataset_id: str) -> dict | None:
        """The previous run, with its per-case results - what the regression
        comparison needs and the only place results are read back."""
        runs = await self.list_eval_runs(dataset_id=dataset_id, limit=1, include_results=True)
        return runs[0] if runs else None

    async def prune_eval_runs(self, dataset_id: str) -> int:
        """Keep only the most recent EVAL_RUN_HISTORY runs for a dataset.
        Runs carry every case result, so an appliance running the gate on
        every catalog change would otherwise accumulate them indefinitely."""
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["evals_collection"]

        def _run():
            q = (
                f"DELETE FROM `{bucket}`.`{scope}`.`{coll}` r "
                f'WHERE r.doc_type = "eval_run" AND r.dataset_id = $dataset_id '
                f"AND r.started_at < ("
                f"  SELECT RAW MIN(x.started_at) FROM ("
                f"    SELECT r2.started_at FROM `{bucket}`.`{scope}`.`{coll}` r2 "
                f'    WHERE r2.doc_type = "eval_run" AND r2.dataset_id = $dataset_id '
                f"    ORDER BY r2.started_at DESC LIMIT $keep"
                f"  ) x"
                f")[0] RETURNING META(r).id"
            )
            rows = list(self.cluster.query(
                q, QueryOptions(named_parameters={"dataset_id": dataset_id, "keep": EVAL_RUN_HISTORY}, metrics=False)
            ).rows())
            return len(rows)

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.debug("prune_eval_runs(%s) failed: %s", dataset_id, exc)
            return 0

    # -- Human approval tier -------------------------------------------------
    # A pending approval is a document with a TTL, so "nobody looked at this"
    # resolves to a denial by the document simply ceasing to exist. There is
    # no sweeper, and no window in which an expired approval is still
    # redeemable.

    async def upsert_approval(self, approval: dict, ttl_seconds: int = 0):
        try:
            options = UpsertOptions(expiry=timedelta(seconds=ttl_seconds)) if ttl_seconds else UpsertOptions()
            await asyncio.to_thread(
                self.approvals.upsert, f"approval::{approval['approval_id']}", approval, options
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to write approval %s: %s", approval.get("approval_id"), exc)
            raise

    async def get_approval(self, approval_id: str) -> dict | None:
        def _get():
            try:
                return self.approvals.get(f"approval::{approval_id}").content_as[dict]
            except DocumentNotFoundException:
                return None

        try:
            return await asyncio.to_thread(_get)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_approval(%s) failed: %s", approval_id, exc)
            return None

    async def list_approvals(self, status: str | None = None, limit: int = 100) -> list[dict]:
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["approvals_collection"]
        params = {"limit": limit}
        clauses = ['a.doc_type = "approval"']
        if status:
            clauses.append("a.status = $status")
            params["status"] = status

        def _run():
            q = (
                f"SELECT a.* FROM `{bucket}`.`{scope}`.`{coll}` a "
                f"WHERE {' AND '.join(clauses)} "
                f"ORDER BY a.requested_at DESC LIMIT $limit"
            )
            return list(self.cluster.query(q, QueryOptions(named_parameters=params, metrics=False)).rows())

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_approvals query failed: %s", exc)
            return []

    async def count_pending_approvals(self) -> int:
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["approvals_collection"]

        def _run():
            q = (
                f"SELECT RAW COUNT(*) FROM `{bucket}`.`{scope}`.`{coll}` a "
                f'WHERE a.doc_type = "approval" AND a.status = "pending"'
            )
            rows = list(self.cluster.query(q, QueryOptions(metrics=False)).rows())
            return rows[0] if rows else 0

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.debug("count_pending_approvals failed: %s", exc)
            return 0

    # -- Knowledge base ------------------------------------------------------
    # Same one-collection-two-doc_types shape as traces and evals: a
    # `knowledge_document` carries the metadata an operator manages, and a
    # `knowledge_chunk` carries the text and the vector retrieval runs over.

    def _knowledge_index_definition(
        self, index_name: str | None = None, vector_field: str = "embedding", dims: int | None = None
    ) -> dict:
        """The default set's index, or - with all three arguments - one
        knowledge set's index: same mapping, its own vector field and
        dimension (see app/knowledge_sets.py)."""
        index_name = index_name or COUCHBASE_CONFIG["knowledge_index"]
        dims = int(dims or EMBEDDING_CONFIG["vector_dim"])
        bucket = COUCHBASE_CONFIG["bucket"]
        scope = COUCHBASE_CONFIG["scope"]
        collection = COUCHBASE_CONFIG["knowledge_collection"]
        type_key = f"{scope}.{collection}"

        def keyword_field(name: str) -> dict:
            return {
                "dynamic": False,
                "enabled": True,
                "fields": [{"name": name, "type": "text", "analyzer": "keyword", "index": True, "store": True}],
            }

        properties = {
            # The RBAC pre-filter field, exactly as the tool catalog uses it:
            # a chunk outside the caller's role cannot be returned however
            # well it matches.
            "allowed_roles": keyword_field("allowed_roles"),
            "document_id": keyword_field("document_id"),
            vector_field: {
                "dynamic": False,
                "enabled": True,
                "fields": [{
                    "name": vector_field,
                    "type": "vector",
                    "dims": dims,
                    "similarity": "dot_product",
                    "index": True,
                    "store": True,
                }],
            },
        }

        return {
            "type": "fulltext-index",
            "name": f"{bucket}.{scope}.{index_name}",
            "sourceType": "gocbcore",
            "sourceName": bucket,
            "planParams": {"maxPartitionsPerPIndex": 512, "indexPartitions": 1},
            "params": {
                "doc_config": {"mode": "scope.collection.type_field", "type_field": "doc_type"},
                "mapping": {
                    "default_analyzer": "keyword",
                    "default_datetime_parser": "dateTimeOptional",
                    "default_field": "_all",
                    "default_mapping": {"dynamic": False, "enabled": False},
                    "default_type": "_default",
                    "docvalues_dynamic": False,
                    "index_dynamic": False,
                    "store_dynamic": False,
                    "type_field": "_type",
                    # Only chunks are indexed - a document metadata row has
                    # no embedding and would only dilute the candidate set.
                    "types": {
                        f"{type_key}.knowledge_chunk": {
                            "dynamic": False, "enabled": True, "properties": properties
                        }
                    },
                },
            },
            "store": {"indexType": "scorch", "segmentVersion": 16},
            "sourceParams": {},
        }

    async def ensure_knowledge_index(
        self, index_name: str | None = None, vector_field: str = "embedding", dims: int | None = None
    ) -> bool:
        index_name = index_name or COUCHBASE_CONFIG["knowledge_index"]
        try:
            resp = await asyncio.to_thread(
                lambda: requests.put(
                    self._search_admin_url(index_name),
                    json=self._knowledge_index_definition(index_name, vector_field, dims),
                    auth=(COUCHBASE_CONFIG["username"], COUCHBASE_CONFIG["password"]),
                    headers={"Content-Type": "application/json"},
                    timeout=30,
                )
            )
            # Re-PUTting an unchanged existing index is refused as "exists";
            # that is the steady state on every restart, not a failure.
            if resp.status_code >= 400 and "exist" not in (resp.text or "").lower():
                logger.warning("Knowledge vector index '%s' not created: %s %s",
                               index_name, resp.status_code, (resp.text or "")[:300])
                return False
            logger.info("Knowledge vector index '%s' ensured", index_name)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not ensure knowledge vector index '%s': %s", index_name, exc)
            return False

    async def delete_knowledge_index(self, index_name: str) -> bool:
        if index_name == COUCHBASE_CONFIG["knowledge_index"]:
            raise ValueError("The default knowledge index is never deleted")
        try:
            resp = await asyncio.to_thread(
                lambda: requests.delete(
                    self._search_admin_url(index_name),
                    auth=(COUCHBASE_CONFIG["username"], COUCHBASE_CONFIG["password"]),
                    timeout=30,
                )
            )
            return resp.status_code < 400
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not delete knowledge vector index '%s': %s", index_name, exc)
            return False

    async def upsert_knowledge_document(self, doc: dict):
        await asyncio.to_thread(self.knowledge.upsert, f"kdoc::{doc['document_id']}", doc)

    async def get_knowledge_document(self, document_id: str) -> dict | None:
        def _get():
            try:
                return self.knowledge.get(f"kdoc::{document_id}").content_as[dict]
            except DocumentNotFoundException:
                return None

        return await asyncio.to_thread(_get)

    async def upsert_knowledge_chunk(self, chunk: dict):
        await asyncio.to_thread(self.knowledge.upsert, f"kchunk::{chunk['chunk_id']}", chunk)

    async def list_knowledge_documents(self, limit: int = 200) -> list[dict]:
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["knowledge_collection"]

        def _run():
            q = (
                f"SELECT d.* FROM `{bucket}`.`{scope}`.`{coll}` d "
                f'WHERE d.doc_type = "knowledge_document" '
                f"ORDER BY d.created_at DESC LIMIT $limit"
            )
            return list(self.cluster.query(
                q, QueryOptions(named_parameters={"limit": limit}, metrics=False)
            ).rows())

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_knowledge_documents query failed: %s", exc)
            return []

    async def delete_knowledge_document(self, document_id: str) -> int:
        """Remove a document and every chunk belonging to it. Returns the
        number of documents deleted (metadata row plus chunks)."""
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["knowledge_collection"]

        def _run():
            q = (
                f"DELETE FROM `{bucket}`.`{scope}`.`{coll}` k "
                f"WHERE k.document_id = $document_id RETURNING META(k).id"
            )
            rows = list(self.cluster.query(
                q, QueryOptions(named_parameters={"document_id": document_id}, metrics=False)
            ).rows())
            return len(rows)

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("delete_knowledge_document(%s) failed: %s", document_id, exc)
            return 0

    async def count_knowledge_chunks(self) -> int:
        return await self._count_where(COUCHBASE_CONFIG["knowledge_collection"], "knowledge_chunk")

    def _run_knowledge_search_sync(
        self, role: str, vector: list, top_k: int, document_id: str | None,
        index_name: str | None = None, vector_field: str = "embedding",
    ) -> list[dict]:
        index_name = index_name or COUCHBASE_CONFIG["knowledge_index"]
        vector_query = CBVectorQuery.create(vector_field, vector, num_candidates=max(top_k * 4, 25))
        vector_search = CBVectorSearch.from_vector_query(vector_query)

        # The same Conjunction pre-filter the tool catalog uses, over the
        # same field name, for the same reason: the role narrows the
        # candidate set inside the one Search request, so a chunk the caller
        # may not see is never a candidate to be ranked at all.
        must = [cb_search.TermQuery(role, field="allowed_roles")]
        if document_id:
            must.append(cb_search.TermQuery(document_id, field="document_id"))
        prefilter = cb_search.ConjunctionQuery(*must)

        request = cb_search.SearchRequest.create(prefilter).with_vector_search(vector_search)
        result = self.scope.search(
            index_name,
            request,
            SearchOptions(limit=top_k, fields=["document_id", "allowed_roles"]),
        )
        return [{"id": row.id, "score": row.score, "fields": row.fields or {}} for row in result.rows()]

    async def search_knowledge(
        self, role: str, query_vector: list, top_k: int = 5, document_id: str | None = None,
        index_name: str | None = None, vector_field: str = "embedding",
    ) -> list[dict]:
        if not self.connected:
            return []
        try:
            rows = await asyncio.to_thread(
                self._run_knowledge_search_sync, role, query_vector, top_k, document_id,
                index_name, vector_field,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Knowledge vector search failed: %s", exc)
            return []

        results = []
        for row in rows:
            chunk = await asyncio.to_thread(self._get_knowledge_chunk_sync, row["id"])
            if not chunk:
                continue
            # Re-check the role against the stored document rather than
            # trusting the index row. The pre-filter is the fast path; this
            # is the same "never trust that the filter was the only thing
            # protecting you" posture invoke takes over discovery.
            if role not in (chunk.get("allowed_roles") or []):
                continue
            results.append({
                "chunk_id": chunk.get("chunk_id"),
                "document_id": chunk.get("document_id"),
                "document_title": chunk.get("document_title"),
                "chunk_index": chunk.get("chunk_index"),
                "content": chunk.get("content"),
                "score": row.get("score"),
                "set_id": chunk.get("set_id") or "default",
            })
        return results

    def _get_knowledge_chunk_sync(self, doc_id: str) -> dict | None:
        try:
            return self.knowledge.get(doc_id).content_as[dict]
        except DocumentNotFoundException:
            return None

    # -- Memory management and consolidation ---------------------------------
    # The operator-facing half of agent memory: who has memories, what they
    # are, and the reads consolidation needs (which, unlike every view
    # above, does want the embeddings - it is comparing them).

    async def list_memory_users(self, limit: int = 500) -> list[dict]:
        """One row per user with memories, with the counts the Memory page
        lists them by. A GROUP BY rather than a fetch of every document:
        the page opens on this query, and it must not get slower as memory
        grows."""
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["agent_memory_collection"]

        def _run():
            q = (
                f"SELECT m.user_id, COUNT(1) AS total, "
                f'SUM(CASE WHEN m.status = "superseded" THEN 1 ELSE 0 END) AS superseded, '
                f"MAX(m.updated_at) AS last_updated_at, "
                f"COUNT(DISTINCT m.session_id) AS sessions "
                f"FROM `{bucket}`.`{scope}`.`{coll}` m "
                f"WHERE m.user_id IS NOT MISSING "
                f"GROUP BY m.user_id ORDER BY MAX(m.updated_at) DESC LIMIT $limit"
            )
            return list(self.cluster.query(
                q, QueryOptions(named_parameters={"limit": limit}, metrics=False)
            ).rows())

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_memory_users query failed: %s", exc)
            return []

    async def list_memory_with_embeddings(self, user_id: str, limit: int = 1000) -> list[dict]:
        """Everything consolidation needs for one user, embeddings included -
        it is comparing vectors, so this is the one read that legitimately
        wants them."""
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["agent_memory_collection"]

        def _run():
            q = (
                f"SELECT META(m).id AS memory_id, m.* "
                f"FROM `{bucket}`.`{scope}`.`{coll}` m "
                f'WHERE m.user_id = $user_id AND (m.status = "active" OR m.status IS MISSING) '
                f"ORDER BY m.created_at ASC LIMIT $limit"
            )
            return list(self.cluster.query(
                q, QueryOptions(named_parameters={"user_id": user_id, "limit": limit}, metrics=False)
            ).rows())

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_memory_with_embeddings(%s) failed: %s", user_id, exc)
            return []

    async def memory_stats(self) -> dict:
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["agent_memory_collection"]

        def _run():
            q = (
                f"SELECT COUNT(1) AS total, "
                f'SUM(CASE WHEN m.status = "superseded" THEN 1 ELSE 0 END) AS superseded, '
                f"COUNT(DISTINCT m.user_id) AS users, "
                f'SUM(CASE WHEN m.consolidation_kind IS NOT MISSING THEN 1 ELSE 0 END) AS consolidated '
                f"FROM `{bucket}`.`{scope}`.`{coll}` m "
                f"WHERE m.user_id IS NOT MISSING"
            )
            rows = list(self.cluster.query(q, QueryOptions(metrics=False)).rows())
            return rows[0] if rows else {}

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.debug("memory_stats failed: %s", exc)
            return {}

    async def bump_recall_counts(self, memory_ids: list[str]):
        """Record that these entries were actually recalled - the strongest
        signal importance scoring has.

        Fire-and-forget from the caller's point of view, and a single
        UPDATE rather than a read-modify-write per entry, so a recall costs
        one extra round-trip regardless of how many entries came back.
        """
        if not memory_ids:
            return
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["agent_memory_collection"]

        def _run():
            q = (
                f"UPDATE `{bucket}`.`{scope}`.`{coll}` m "
                f"SET m.recall_count = IFMISSINGORNULL(m.recall_count, 0) + 1, "
                f"m.last_recalled_at = $now "
                f"WHERE META(m).id IN $ids"
            )
            self.cluster.query(q, QueryOptions(named_parameters={
                "ids": memory_ids,
                "now": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }, metrics=False, preserve_expiry=True)).execute()

        try:
            await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.debug("bump_recall_counts failed: %s", exc)

    async def set_memory_importance(self, scores: dict[str, float]) -> int:
        """Write recomputed importance back. Batched per score value so a
        pass over a few hundred entries is a handful of queries rather than
        one per entry."""
        if not scores:
            return 0
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["agent_memory_collection"]
        by_score: dict[float, list[str]] = {}
        for memory_id, score in scores.items():
            by_score.setdefault(round(float(score), 4), []).append(memory_id)

        def _run():
            updated = 0
            for score, ids in by_score.items():
                q = (
                    f"UPDATE `{bucket}`.`{scope}`.`{coll}` m SET m.importance = $score "
                    f"WHERE META(m).id IN $ids RETURNING META(m).id"
                )
                rows = list(self.cluster.query(
                    q, QueryOptions(named_parameters={"score": score, "ids": ids}, metrics=False, preserve_expiry=True)
                ).rows())
                updated += len(rows)
            return updated

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("set_memory_importance failed: %s", exc)
            return 0

    async def forget_user(self, user_id: str) -> int:
        """Delete every memory belonging to one user, superseded ones
        included. This is what a data-subject erasure request needs, so it
        deliberately ignores the status filter every other read applies -
        a superseded entry is still that person's data."""
        bucket, scope = COUCHBASE_CONFIG["bucket"], COUCHBASE_CONFIG["scope"]
        coll = COUCHBASE_CONFIG["agent_memory_collection"]

        def _run():
            q = (
                f"DELETE FROM `{bucket}`.`{scope}`.`{coll}` m "
                f"WHERE m.user_id = $user_id RETURNING META(m).id"
            )
            rows = list(self.cluster.query(
                q, QueryOptions(named_parameters={"user_id": user_id}, metrics=False)
            ).rows())
            return len(rows)

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("forget_user(%s) failed: %s", user_id, exc)
            return 0
