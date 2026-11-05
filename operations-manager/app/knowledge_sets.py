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

Every set also has a storage location - the bucket, scope and collection
its chunks (text + vectors) are written to. The default set uses the
appliance's own `knowledge` collection; any other set may use that too, or
any other bucket/scope/collection on the cluster, chosen or created from
the Knowledge Base page. The set's vector index is created in the
location's bucket and scope and maps only that collection. Document
metadata rows (what the Documents table lists) always stay in the
appliance's own collection, so the operator catalog is one place however
many collections hold chunks.

Nothing in here talks to Couchbase; persistence is one settings document
(see app/main.py) and the index lifecycle is in app/couchbase_client.py.
"""
import re
import time

DEFAULT_SET_ID = "default"
SET_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
# Couchbase bucket names: letters, digits, `.`, `-`, `_`, `%`, up to 100
# chars. Scope and collection names: letters, digits, `-`, `_`, `%`, up to
# 251 chars, and may not start with `_` or `%` (except the built-in
# `_default`).
BUCKET_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._%-]{1,100}$")
SCOPE_COLLECTION_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_%-]{0,250}$")


def validate_location(location: dict | None, default: dict) -> dict:
    """Normalize and validate a storage location, filling missing parts
    from `default` (the appliance's own). Raises ValueError on a name
    Couchbase would refuse."""
    loc = {
        "bucket": ((location or {}).get("bucket") or default["bucket"]).strip(),
        "scope": ((location or {}).get("scope") or default["scope"]).strip(),
        "collection": ((location or {}).get("collection") or default["collection"]).strip(),
    }
    if not BUCKET_NAME_PATTERN.match(loc["bucket"]):
        raise ValueError("bucket name may use letters, digits, '.', '-', '_' and '%' (max 100 characters)")
    for part in ("scope", "collection"):
        if loc[part] != "_default" and not SCOPE_COLLECTION_NAME_PATTERN.match(loc[part]):
            raise ValueError(
                f"{part} name may use letters, digits, '-', '_' and '%', must not start with '_' or '%' "
                f"(max 251 characters)"
            )
    return loc


def location_label(location: dict | None, default: dict) -> str:
    loc = location or default
    return f"{loc['bucket']}.{loc['scope']}.{loc['collection']}"


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug[:32].strip("-") or "set"


def storage_key(set_id: str) -> str:
    """Index names and document field names can't carry '-' everywhere
    Couchbase accepts a set ID, so both are derived from this form."""
    return re.sub(r"[^a-z0-9_]", "_", set_id)


def default_set(model_id: str, dims: int, index_name: str, location: dict) -> dict:
    return {
        "set_id": DEFAULT_SET_ID,
        "name": "Default",
        "description": "The Knowledge Base's original set, on the appliance's own embedding model.",
        "model_id": model_id,
        "dims": dims,
        "index_name": index_name,
        "vector_field": "embedding",
        "location": dict(location),
        "builtin": True,
        "created_at": None,
        "created_by": None,
    }


def new_set(*, name: str, set_id: str | None, model: dict, base_index_name: str, description: str,
            created_by: str | None, location: dict) -> dict:
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
        "location": dict(location),
        "builtin": False,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "created_by": created_by,
    }


def chunk_set_id(chunk_or_doc: dict) -> str:
    return chunk_or_doc.get("set_id") or DEFAULT_SET_ID
