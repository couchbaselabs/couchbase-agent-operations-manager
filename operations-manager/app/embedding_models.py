"""
Selectable embedding models for the Knowledge Base.

The appliance's own embedding model (EMBEDDING_MODEL, all-MiniLM-L6-v2 by
default) still embeds the tool catalog, agent memory and the LLM cache.
This module adds a choice for the Knowledge Base, made per *knowledge set*:
every document in a set is embedded with that set's model and searched
through that set's own Couchbase vector index. Vectors from two different
models are not comparable - even at the same dimension - so a set never
mixes models, and a query is always embedded with the model of the set it
searches.

Two kinds of model:

  * local  - sentence-transformers models run on the operations manager's
             CPU. No API key; the weights download from Hugging Face the
             first time a set using the model is created or queried, so the
             appliance needs outbound access to huggingface.co once (or a
             pre-populated HF cache). Larger models are noticeably slower on
             CPU and hold their weights in RAM while loaded.
  * hosted - provider embedding APIs (OpenAI, Google Gemini, Voyage AI,
             Cohere, Mistral, Jina). Need that provider's API key in the
             operations manager's environment and outbound HTTPS to it.

Every vector is L2-normalized before it is stored or searched, so the
indexes' dot_product similarity is cosine similarity whatever the model.
Couchbase Server 7.6.2+ vector fields accept up to 4096 dimensions; the
largest model here is 3072.

Models that distinguish queries from documents get the right input type or
prefix for each side (e5's "query: "/"passage: ", BGE's retrieval
instruction, Voyage/Cohere/Gemini/Jina input types) - using the same form
for both measurably hurts retrieval with those models.
"""
import asyncio
import logging
import os
import re
import threading
import time
from collections import OrderedDict

import numpy as np
import requests

logger = logging.getLogger("operations-manager.embedding-models")

BGE_QUERY = "Represent this sentence for searching relevant passages: "

# provider id -> (label, env var holding its key or "" for local)
PROVIDERS: dict[str, tuple[str, str]] = {
    "local": ("Local (runs in AOM)", ""),
    "openai": ("OpenAI", "OPENAI_API_KEY"),
    "google": ("Google Gemini", "GEMINI_API_KEY"),
    "voyage": ("Voyage AI", "VOYAGE_API_KEY"),
    "cohere": ("Cohere", "COHERE_API_KEY"),
    "mistral": ("Mistral AI", "MISTRAL_API_KEY"),
    "jina": ("Jina AI", "JINA_API_KEY"),
    # Models an admin imports (see CUSTOM below). Keys for imported
    # endpoints are stored encrypted, not read from the environment.
    "custom_hf": ("Imported (Hugging Face)", ""),
    "custom_openai": ("Imported (OpenAI-compatible endpoint)", ""),
}


def _m(model_id, label, provider, dims, *, size="", multilingual=False, query_prefix="", doc_prefix="", notes=""):
    return {
        "id": model_id, "label": label, "provider": provider, "dims": dims, "size": size,
        "multilingual": multilingual, "query_prefix": query_prefix, "doc_prefix": doc_prefix, "notes": notes,
    }


CATALOG: list[dict] = [
    # -- local (sentence-transformers) -----------------------------------------
    _m("sentence-transformers/all-MiniLM-L6-v2", "all-MiniLM-L6-v2", "local", 384, size="22M params",
       notes="AOM's built-in default: fast on CPU, good general English retrieval."),
    _m("sentence-transformers/all-MiniLM-L12-v2", "all-MiniLM-L12-v2", "local", 384, size="33M params",
       notes="Deeper MiniLM; slightly better quality, still fast."),
    _m("sentence-transformers/all-mpnet-base-v2", "all-mpnet-base-v2", "local", 768, size="110M params",
       notes="Strong general-purpose English model."),
    _m("sentence-transformers/multi-qa-MiniLM-L6-cos-v1", "multi-qa-MiniLM-L6-cos-v1", "local", 384,
       size="22M params", notes="Tuned for question-to-passage search."),
    _m("sentence-transformers/multi-qa-mpnet-base-cos-v1", "multi-qa-mpnet-base-cos-v1", "local", 768,
       size="110M params", notes="Question-to-passage search, higher quality."),
    _m("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", "paraphrase-multilingual-MiniLM-L12-v2",
       "local", 384, size="118M params", multilingual=True, notes="50+ languages, fast."),
    _m("sentence-transformers/paraphrase-multilingual-mpnet-base-v2", "paraphrase-multilingual-mpnet-base-v2",
       "local", 768, size="278M params", multilingual=True, notes="50+ languages, higher quality."),
    _m("BAAI/bge-small-en-v1.5", "BGE small en v1.5", "local", 384, size="33M params", query_prefix=BGE_QUERY,
       notes="Strong retrieval for its size."),
    _m("BAAI/bge-base-en-v1.5", "BGE base en v1.5", "local", 768, size="110M params", query_prefix=BGE_QUERY,
       notes="Strong English retrieval."),
    _m("BAAI/bge-large-en-v1.5", "BGE large en v1.5", "local", 1024, size="335M params", query_prefix=BGE_QUERY,
       notes="Best BGE English quality; slower on CPU."),
    _m("BAAI/bge-m3", "BGE-M3", "local", 1024, size="568M params", multilingual=True,
       notes="100+ languages, long inputs (8K tokens); heavy on CPU and RAM."),
    _m("intfloat/e5-base-v2", "E5 base v2", "local", 768, size="110M params",
       query_prefix="query: ", doc_prefix="passage: ", notes="Strong English retrieval."),
    _m("intfloat/multilingual-e5-large", "Multilingual E5 large", "local", 1024, size="560M params",
       multilingual=True, query_prefix="query: ", doc_prefix="passage: ",
       notes="100 languages; heavy on CPU and RAM."),
    _m("mixedbread-ai/mxbai-embed-large-v1", "mxbai-embed-large-v1", "local", 1024, size="335M params",
       query_prefix=BGE_QUERY, notes="High-quality English retrieval; slower on CPU."),
    _m("Snowflake/snowflake-arctic-embed-m-v1.5", "Snowflake Arctic Embed M v1.5", "local", 768,
       size="109M params", query_prefix=BGE_QUERY, notes="Retrieval-tuned, good quality for its size."),
    # -- hosted ----------------------------------------------------------------
    _m("text-embedding-3-small", "text-embedding-3-small", "openai", 1536, notes="Low cost, good quality."),
    _m("text-embedding-3-large", "text-embedding-3-large", "openai", 3072, multilingual=True,
       notes="OpenAI's highest-quality embedding model."),
    _m("gemini-embedding-001", "gemini-embedding-001", "google", 3072, multilingual=True,
       notes="Google's Gemini embedding model, 100+ languages."),
    _m("voyage-4-large", "voyage-4-large", "voyage", 1024, multilingual=True, notes="Voyage's highest quality."),
    _m("voyage-4", "voyage-4", "voyage", 1024, multilingual=True, notes="Balanced quality and cost."),
    _m("voyage-4-lite", "voyage-4-lite", "voyage", 1024, multilingual=True, notes="Lowest latency and cost."),
    _m("embed-v5.0-pro", "Embed v5.0 Pro", "cohere", 2048, multilingual=True, notes="Cohere's newest, highest quality."),
    _m("embed-v4.0", "Embed v4.0", "cohere", 1536, multilingual=True, notes="Multilingual, long inputs."),
    _m("mistral-embed", "mistral-embed", "mistral", 1024, notes="Mistral's general text embedding model."),
    _m("jina-embeddings-v5-text-small", "jina-embeddings-v5-text-small", "jina", 1024, multilingual=True,
       notes="Multilingual, 32K-token inputs."),
]
BY_ID = {m["id"]: m for m in CATALOG}

# Admin-imported models, keyed by "custom:<slug>". Persisted by app/main.py
# (settings::custom_embedding_models) and pushed in with set_custom(); the
# plaintext API keys for imported endpoints live only in memory here.
CUSTOM: dict[str, dict] = {}
CUSTOM_SECRETS: dict[str, str] = {}
HF_SOURCE_PATTERN = re.compile(r"^(/[\w./-]+|[A-Za-z0-9][\w.-]*(/[\w.-]+)?)$")


def set_custom(models: dict, secrets: dict | None = None):
    CUSTOM.clear()
    CUSTOM.update(models)
    CUSTOM_SECRETS.clear()
    CUSTOM_SECRETS.update(secrets or {})


def custom_model(*, kind: str, source: str, label: str = "", base_url: str = "", query_prefix: str = "",
                 doc_prefix: str = "", created_by: str | None = None) -> dict:
    """Validate one import. Dimensions are not taken on trust - they are
    measured by embedding a probe text (see probe_dims) before the model can
    back a knowledge set."""
    source = (source or "").strip()
    if kind == "huggingface":
        if not HF_SOURCE_PATTERN.match(source) or ".." in source:
            raise ValueError("Enter a Hugging Face model ID like 'org/model-name', or an absolute local path")
        provider = "custom_hf"
    elif kind == "openai_compatible":
        base_url = (base_url or "").strip().rstrip("/")
        if not re.match(r"^https?://[^\s/]+", base_url):
            raise ValueError("base_url must be an http(s) URL, e.g. https://llm.internal:8000/v1")
        if not source or len(source) > 200:
            raise ValueError("Enter the model name the endpoint expects")
        provider = "custom_openai"
    else:
        raise ValueError("kind must be 'huggingface' or 'openai_compatible'")
    slug = re.sub(r"[^a-z0-9]+", "-", (label or source).lower()).strip("-")[:40] or "model"
    return {
        "id": f"custom:{slug}",
        "label": (label or source).strip()[:80],
        "provider": provider,
        "source": source,
        "base_url": base_url if provider == "custom_openai" else "",
        "dims": None,
        "size": "",
        "multilingual": False,
        "query_prefix": (query_prefix or "")[:200],
        "doc_prefix": (doc_prefix or "")[:200],
        "notes": f"Imported from {'Hugging Face' if provider == 'custom_hf' else base_url}: {source}",
        "custom": True,
        "status": "pending",
        "error": None,
        "created_by": created_by,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
# The appliance default may be configured by its short name.
ALIASES = {m["id"].split("/", 1)[1]: m["id"] for m in CATALOG if "/" in m["id"]}
MAX_DIMS = 4096


def resolve(model_id: str) -> dict | None:
    return BY_ID.get(model_id) or BY_ID.get(ALIASES.get(model_id or "", "")) or CUSTOM.get(model_id or "")


def api_key_for(provider: str, keys: dict | None = None) -> str:
    env = PROVIDERS.get(provider, ("", ""))[1]
    if not env:
        return ""
    return ((keys or {}).get(env) or os.getenv(env, "")).strip()


def is_available(model: dict, keys: dict | None = None) -> bool:
    if model.get("custom"):
        return model.get("status") == "ready"
    return model["provider"] == "local" or bool(api_key_for(model["provider"], keys))


def public_catalog(keys: dict | None = None, default_model: str | None = None) -> list[dict]:
    default = resolve(default_model or "") or {}
    out = []
    for m in CATALOG + list(CUSTOM.values()):
        label, env = PROVIDERS[m["provider"]]
        row = {
            **{k: m[k] for k in ("id", "label", "provider", "dims", "size", "multilingual", "notes")},
            "provider_label": label,
            "requires": env or None,
            "available": is_available(m, keys),
            "is_default": m["id"] == default.get("id"),
            "custom": bool(m.get("custom")),
        }
        if m.get("custom"):
            row.update({
                "status": m.get("status"), "error": m.get("error"), "source": m.get("source"),
                "base_url": m.get("base_url") or None, "has_api_key": m["id"] in CUSTOM_SECRETS,
                "query_prefix": m.get("query_prefix"), "doc_prefix": m.get("doc_prefix"),
            })
        out.append(row)
    return out


def _normalize(vectors) -> list[list[float]]:
    arr = np.asarray(vectors, dtype=np.float32)
    if arr.ndim == 1:
        arr = arr[None, :]
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (arr / norms).tolist()


# -- local models -----------------------------------------------------------------
_local_lock = threading.Lock()
_local_models: "OrderedDict[str, object]" = OrderedDict()
MAX_LOADED_LOCAL_MODELS = max(1, int(os.getenv("EMBEDDING_MAX_LOADED_MODELS", "2")))


def _load_local(model_id: str):
    with _local_lock:
        model = _local_models.get(model_id)
        if model is not None:
            _local_models.move_to_end(model_id)
            return model
    from sentence_transformers import SentenceTransformer

    started = time.time()
    logger.info("Loading local embedding model '%s' (first use downloads it from Hugging Face)...", model_id)
    model = SentenceTransformer(model_id)
    logger.info("Local embedding model '%s' ready in %.1fs", model_id, time.time() - started)
    with _local_lock:
        _local_models[model_id] = model
        while len(_local_models) > MAX_LOADED_LOCAL_MODELS:
            evicted, _ = _local_models.popitem(last=False)
            logger.info("Unloaded local embedding model '%s' (EMBEDDING_MAX_LOADED_MODELS=%d)",
                        evicted, MAX_LOADED_LOCAL_MODELS)
    return model


def unload_local(model_id: str):
    with _local_lock:
        _local_models.pop(model_id, None)


def _embed_local(model: dict, texts: list[str], is_query: bool) -> list[list[float]]:
    prefix = model["query_prefix"] if is_query else model["doc_prefix"]
    # Imported models load from their Hugging Face ID or local path. Remote
    # code is never trusted: a model that needs trust_remote_code fails here
    # with the library's own explanation instead of running arbitrary code.
    st = _load_local(model.get("source") or model["id"])
    vecs = st.encode([prefix + t for t in texts], batch_size=32, convert_to_numpy=True)
    return _normalize(vecs)


# -- hosted models ----------------------------------------------------------------
HOSTED_TIMEOUT = int(os.getenv("EMBEDDING_API_TIMEOUT_SECONDS", "60"))
BATCH_SIZES = {"openai": 256, "google": 100, "voyage": 128, "cohere": 96, "mistral": 64, "jina": 128}


def _post(url: str, headers: dict, body: dict) -> dict:
    """POST with one retry on rate limiting or a provider-side error."""
    for attempt in (1, 2):
        resp = requests.post(url, headers=headers, json=body, timeout=HOSTED_TIMEOUT)
        if resp.status_code in (429, 500, 502, 503, 504) and attempt == 1:
            time.sleep(float(resp.headers.get("Retry-After") or 2))
            continue
        if resp.status_code >= 400:
            detail = resp.text[:300]
            raise RuntimeError(f"{url.split('/')[2]} returned {resp.status_code}: {detail}")
        return resp.json()
    raise RuntimeError("unreachable")


def _embed_hosted_batch(model: dict, texts: list[str], is_query: bool, key: str) -> list[list[float]]:
    p, mid = model["provider"], model["id"]
    bearer = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if p == "custom_openai":
        headers = bearer if key else {"Content-Type": "application/json"}
        data = _post(f"{model['base_url']}/embeddings", headers, {"model": model["source"], "input": texts})
        return [d["embedding"] for d in sorted(data["data"], key=lambda d: d.get("index", 0))]
    if p == "openai":
        data = _post("https://api.openai.com/v1/embeddings", bearer,
                     {"model": mid, "input": texts, "encoding_format": "float"})
        return [d["embedding"] for d in sorted(data["data"], key=lambda d: d.get("index", 0))]
    if p == "mistral":
        data = _post("https://api.mistral.ai/v1/embeddings", bearer, {"model": mid, "input": texts})
        return [d["embedding"] for d in sorted(data["data"], key=lambda d: d.get("index", 0))]
    if p == "voyage":
        data = _post("https://api.voyageai.com/v1/embeddings", bearer,
                     {"model": mid, "input": texts, "input_type": "query" if is_query else "document"})
        rows = data.get("data") or [{"embedding": e} for e in data.get("embeddings", [])]
        return [d["embedding"] for d in sorted(rows, key=lambda d: d.get("index", 0))]
    if p == "jina":
        data = _post("https://api.jina.ai/v1/embeddings", bearer,
                     {"model": mid, "input": texts, "task": "retrieval.query" if is_query else "retrieval.passage"})
        return [d["embedding"] for d in sorted(data["data"], key=lambda d: d.get("index", 0))]
    if p == "cohere":
        data = _post("https://api.cohere.com/v2/embed", bearer, {
            "model": mid, "texts": texts, "embedding_types": ["float"],
            "input_type": "search_query" if is_query else "search_document",
        })
        return data["embeddings"]["float"]
    if p == "google":
        task = "RETRIEVAL_QUERY" if is_query else "RETRIEVAL_DOCUMENT"
        data = _post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{mid}:batchEmbedContents",
            {"x-goog-api-key": key, "Content-Type": "application/json"},
            {"requests": [
                {"model": f"models/{mid}", "content": {"parts": [{"text": t}]}, "taskType": task}
                for t in texts
            ]},
        )
        return [e["values"] for e in data["embeddings"]]
    raise ValueError(f"Unknown embedding provider '{p}'")


def _embed_hosted(model: dict, texts: list[str], is_query: bool, keys: dict | None) -> list[list[float]]:
    if model["provider"] == "custom_openai":
        key = CUSTOM_SECRETS.get(model["id"], "")
        out: list[list[float]] = []
        for i in range(0, len(texts), 64):
            out.extend(_embed_hosted_batch(model, texts[i:i + 64], is_query, key))
        return _normalize(out)
    key = api_key_for(model["provider"], keys)
    if not key:
        env = PROVIDERS[model["provider"]][1]
        raise RuntimeError(f"{model['label']} needs {env} set on the operations manager")
    size = BATCH_SIZES.get(model["provider"], 64)
    out: list[list[float]] = []
    for i in range(0, len(texts), size):
        out.extend(_embed_hosted_batch(model, texts[i:i + size], is_query, key))
    return _normalize(out)


# -- public interface ---------------------------------------------------------------
class SetEmbedder:
    """Embeds documents and queries for one knowledge set's model. The
    appliance's default model is served by the already-loaded ToolEmbeddings
    instance rather than a second copy."""

    def __init__(self, model_id: str, default_embeddings=None, default_model_id: str | None = None,
                 keys: dict | None = None, concurrency: int | None = None):
        model = resolve(model_id)
        if not model:
            raise ValueError(f"Unknown embedding model '{model_id}'")
        self.model = model
        self.keys = keys
        default = resolve(default_model_id or "")
        self._shared = default_embeddings if (default and default["id"] == model["id"]) else None
        self._slots = asyncio.Semaphore(concurrency or int(os.getenv("EMBEDDING_CONCURRENCY", "2")))

    def _embed_sync(self, texts: list[str], is_query: bool) -> list[list[float]]:
        if self.model["provider"] in ("local", "custom_hf"):
            vectors = _embed_local(self.model, texts, is_query)
        else:
            vectors = _embed_hosted(self.model, texts, is_query, self.keys)
        if len(vectors) != len(texts):
            raise RuntimeError(f"{self.model['label']} returned {len(vectors)} vectors for {len(texts)} inputs")
        if vectors and len(vectors[0]) != self.model["dims"]:
            raise RuntimeError(
                f"{self.model['label']} returned {len(vectors[0])}-dimension vectors, expected "
                f"{self.model['dims']} - refusing to write them into this set's index"
            )
        return vectors

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if any(not t or not t.strip() for t in texts):
            raise ValueError("Cannot embed empty text")
        if self._shared is not None:
            return await self._shared.embed_many_async(texts)
        out: list[list[float]] = []
        batch = 32 if self.model["provider"] in ("local", "custom_hf") else BATCH_SIZES.get(self.model["provider"], 64)
        for i in range(0, len(texts), batch):
            async with self._slots:
                out.extend(await asyncio.to_thread(self._embed_sync, texts[i:i + batch], False))
        return out

    async def embed_query(self, text: str) -> list[float]:
        if not text or not text.strip():
            raise ValueError("Cannot embed empty text")
        if self._shared is not None:
            return await self._shared.embed_async(text)
        async with self._slots:
            return (await asyncio.to_thread(self._embed_sync, [text], True))[0]


def probe_dims(model: dict) -> int:
    """Embed one probe text with an imported model and return its real
    dimension. Blocking - run it in a thread. Raises with a readable reason
    when the model can't be loaded or called, or doesn't fit a Couchbase
    vector field."""
    if model["provider"] == "custom_hf":
        vectors = _embed_local(model, ["dimension probe"], False)
    else:
        vectors = _embed_hosted(model, ["dimension probe"], False, None)
    dims = len(vectors[0]) if vectors else 0
    if dims < 1:
        raise RuntimeError("the model returned an empty vector")
    if dims > MAX_DIMS:
        raise RuntimeError(f"{dims} dimensions exceeds Couchbase's {MAX_DIMS}-dimension vector limit")
    return dims
