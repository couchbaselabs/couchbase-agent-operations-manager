"""
Agent memory storage.

The tool gateway gives an agent a place to *act*; this gives it a place to
*remember* - durable, cross-session recall stored in the same Couchbase
cluster as everything else in this appliance, behind the same bearer-API-key
authentication as discover/invoke/complete.

Three memory types, matching how an agent actually uses them (the same
distinction the wider Couchbase AI Data Plane draws between checkpointer
state, agent memory, and observability spans - see
`claude/aom-distribution-architecture.md` in this repo's history):

  - "conversational" - what was said in a session. Scoped by session_id as
    well as user_id; the default type.
  - "profile"        - durable facts about a user that outlive any one
    session ("prefers metric units", "VIP tier"). Scoped by user_id only.
  - "semantic"        - retrieved knowledge worth remembering across many
    users/sessions (a summarized policy answer, a resolved troubleshooting
    step). Callers choose the scope.

Storage and search live in app/couchbase_client.py, exactly like the tool
catalog and the LLM cache; this module is the validation/normalization
layer both the REST routes and the store share, so the two cannot drift
apart on what a memory document is allowed to look like.
"""
import re
import time
import uuid

MEMORY_TYPES = ("conversational", "profile", "semantic")
DEFAULT_MEMORY_TYPE = "conversational"

MAX_CONTENT_CHARS = 8000
MAX_METADATA_KEYS = 20
MAX_METADATA_VALUE_CHARS = 500


def normalize_memory_type(value: str | None) -> str:
    value = (value or "").strip().lower()
    return value if value in MEMORY_TYPES else DEFAULT_MEMORY_TYPE


def new_memory_id(user_id: str) -> str:
    safe_user = re.sub(r"[^a-zA-Z0-9_.-]", "_", user_id)[:64] or "anonymous"
    return f"memory::{safe_user}::{uuid.uuid4().hex}"


def sanitize_metadata(metadata: dict | None) -> dict:
    """Metadata rides along with a memory entry for the caller's own use
    (e.g. {"channel": "email", "ticket_id": "T-4821"}) - it is never
    searched or interpreted here. Capped so one caller can't bloat a
    document (or the FTS index built over this collection) with an
    arbitrarily large blob."""
    clean: dict = {}
    for key, value in list((metadata or {}).items())[:MAX_METADATA_KEYS]:
        key = str(key)[:120]
        if isinstance(value, (str, int, float, bool)) or value is None:
            clean[key] = str(value)[:MAX_METADATA_VALUE_CHARS] if isinstance(value, str) else value
        else:
            clean[key] = str(value)[:MAX_METADATA_VALUE_CHARS]
    return clean


def build_embedding_text(content: str, metadata: dict) -> str:
    """What actually gets embedded for semantic recall - the content plus
    any metadata values that read as descriptive text, the same
    "join the human-readable bits" approach catalog_ingest.py uses for
    tool descriptions."""
    parts = [content]
    for value in metadata.values():
        if isinstance(value, str) and value.strip():
            parts.append(value)
    return " ".join(parts)[:MAX_CONTENT_CHARS]


def build_memory_doc(
    *,
    user_id: str,
    content: str,
    embedding: list,
    session_id: str | None = None,
    memory_type: str | None = None,
    metadata: dict | None = None,
    role: str | None = None,
    subject_label: str | None = None,
) -> dict:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "doc_type": "agent_memory",
        "user_id": user_id,
        "session_id": session_id or "",
        "memory_type": normalize_memory_type(memory_type),
        "content": (content or "")[:MAX_CONTENT_CHARS],
        "metadata": sanitize_metadata(metadata),
        "embedding": embedding,
        "role": role,
        "subject": subject_label,
        # Consolidation lifecycle (see app/memory_consolidation.py). Written
        # on every entry from the start so recall never has to distinguish
        # "active" from "written before consolidation existed".
        "status": "active",
        "importance": 0.0,
        "recall_count": 0,
        "reinforcement_count": 0,
        "created_at": now,
        "updated_at": now,
    }
