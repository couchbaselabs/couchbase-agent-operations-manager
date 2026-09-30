"""Local embedding generation (SentenceTransformers, CPU) - no external API
key needed. Used both to embed each tool description at catalog-ingestion
time and to embed each incoming query at discovery time."""
import asyncio
import hashlib
import logging
import os
import threading

import numpy as np

logger = logging.getLogger("operations-manager.embeddings")


class ToolEmbeddings:
    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        from sentence_transformers import SentenceTransformer

        logger.info("Loading embedding model '%s'...", model_name)
        self.model = SentenceTransformer(model_name)
        # sentence-transformers 6 renamed get_sentence_embedding_dimension()
        # to get_embedding_dimension(); fall back for older installs.
        get_dimension = getattr(self.model, "get_embedding_dimension", None) or self.model.get_sentence_embedding_dimension
        self.dimension = get_dimension()
        self._cache: dict[str, list[float]] = {}
        self._max_cache_size = 2000
        # embed() is called from worker threads (see embed_async), so the
        # cache's check-then-evict must not interleave.
        self._cache_lock = threading.Lock()
        # Inference is CPU-bound and torch already parallelizes inside one
        # call; letting every request thread run it at once just thrashes
        # the CPU. A small ceiling keeps latency predictable under load.
        self._inference_slots = asyncio.Semaphore(int(os.getenv("EMBEDDING_CONCURRENCY", "2")))
        logger.info("Embedding model ready (dimension=%d)", self.dimension)

    def embed(self, text: str) -> list[float]:
        """Return an L2-normalized embedding as a plain list of floats.
        Couchbase's Search vector index here uses dot_product similarity, so
        normalizing before storing/querying makes dot_product equivalent to
        cosine similarity."""
        if not text or not text.strip():
            raise ValueError("Cannot embed empty text")

        cache_key = hashlib.md5(text.encode("utf-8")).hexdigest()
        with self._cache_lock:
            cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        vec = self.model.encode(text, convert_to_numpy=True).astype(np.float32)
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        result = vec.tolist()

        with self._cache_lock:
            if cache_key not in self._cache and len(self._cache) >= self._max_cache_size:
                self._cache.pop(next(iter(self._cache)))
            self._cache[cache_key] = result
        return result

    def cached(self, text: str) -> list[float] | None:
        if not text:
            return None
        with self._cache_lock:
            return self._cache.get(hashlib.md5(text.encode("utf-8")).hexdigest())

    async def embed_async(self, text: str) -> list[float]:
        """embed() without blocking the event loop.

        Model inference takes tens of milliseconds per call (far more for a
        long document chunk), and every route that embeds is an async route
        - calling embed() directly there stalls every other request the
        process is serving, health checks included, for as long as the
        model runs. A knowledge-base upload embeds one chunk per ~1KB, so a
        large upload could freeze the appliance long enough for its
        liveness probe to restart it."""
        hit = self.cached(text)
        if hit is not None:
            return hit
        async with self._inference_slots:
            return await asyncio.to_thread(self.embed, text)

    def embed_many(self, texts: list[str], batch_size: int = 32) -> list[list[float]]:
        """Batch form of embed(): one model call per batch_size texts, which
        is several times faster than one call per text for bulk work such
        as a knowledge-base upload. Results are not cached."""
        if any(not t or not t.strip() for t in texts):
            raise ValueError("Cannot embed empty text")
        vecs = self.model.encode(texts, batch_size=batch_size, convert_to_numpy=True).astype(np.float32)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (vecs / norms).tolist()

    async def embed_many_async(self, texts: list[str], batch_size: int = 32) -> list[list[float]]:
        """embed_many() off the event loop, one batch per slot so a large
        upload shares the model with interactive requests instead of
        monopolising it."""
        out: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            async with self._inference_slots:
                out.extend(await asyncio.to_thread(self.embed_many, texts[i:i + batch_size], batch_size))
        return out
