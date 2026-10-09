"""
Knowledge sets - groups of Knowledge Base documents that share one
embedding model and one Couchbase vector index.

A query is only meaningful against vectors from the model that embedded
it, so the model is a property of the set, not of a single upload: every
document added to a set is embedded with the set's model, and retrieval
from a set embeds the question with that same model and searches only that
set's index.

The "default" set is the Knowledge Base as it existed before sets: the
appliance's EMBEDDING_MODEL, the original `knowledge_vector_index`, and the
chunk field `embedding`. Every chunk written before sets existed has no
`set_id` and belongs to it, so nothing needs migrating. Each other set gets
its own index (`<knowledge_index>__<key>`) over its own vector field
(`embedding__<key>`), sized to its model's dimension.

Nothing in here talks to Couchbase; persistence is one settings document
(see app/main.py) and the index lifecycle is in app/couchbase_client.py.
"""
import re
import time

DEFAULT_SET_ID = "default"
SET_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug[:32].strip("-") or "set"


def storage_key(set_id: str) -> str:
    """Index names and document field names can't carry '-' everywhere
    Couchbase accepts a set ID, so both are derived from this form."""
    return re.sub(r"[^a-z0-9_]", "_", set_id)


def default_set(model_id: str, dims: int, index_name: str) -> dict:
    return {
        "set_id": DEFAULT_SET_ID,
        "name": "Default",
        "description": "The Knowledge Base's original set, on the appliance's own embedding model.",
        "model_id": model_id,
        "dims": dims,
        "index_name": index_name,
        "vector_field": "embedding",
        "builtin": True,
        "created_at": None,
        "created_by": None,
    }


def new_set(*, name: str, set_id: str | None, model: dict, base_index_name: str, description: str,
            created_by: str | None) -> dict:
    name = (name or "").strip()[:80]
    if not name:
        raise ValueError("name is required")
    set_id = (set_id or slugify(name)).strip().lower()
    if not SET_ID_PATTERN.match(set_id):
        raise ValueError("set_id must be 2-32 characters: lowercase letters, digits and dashes")
    if set_id == DEFAULT_SET_ID:
        raise ValueError("'default' is reserved for the built-in set")
    key = storage_key(set_id)
    return {
        "set_id": set_id,
        "name": name,
        "description": (description or "").strip()[:300],
        "model_id": model["id"],
        "dims": int(model["dims"]),
        "index_name": f"{base_index_name}__{key}",
        "vector_field": f"embedding__{key}",
        "builtin": False,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "created_by": created_by,
    }


def chunk_set_id(chunk_or_doc: dict) -> str:
    return chunk_or_doc.get("set_id") or DEFAULT_SET_ID
