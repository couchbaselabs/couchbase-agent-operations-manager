"""
The knowledge base: governed retrieval over the customer's own documents.

Until now an agent built on this appliance got governed tools, cached
completions and durable memory - and still needed an entirely separate RAG
stack for the knowledge it actually reasons over. That is the largest hole
in a product whose pitch is one governed data layer, and it sits on the
part of the problem Couchbase is best at.

The design is deliberately the same shape as the tool catalog, because the
tool catalog already proves the pattern works: documents are chunked and
embedded at ingest, stored in Couchbase with an `allowed_roles` list, and
retrieved with a single Search request that combines vector kNN with a
Conjunction pre-filter on role. A chunk outside the caller's role cannot be
returned however well it matches the query - the same guarantee discovery
gives for tools, applied to text.

That symmetry is the point. An operator who understands why
`snowflake::manage_users` is invisible to a support agent already
understands why the salary bands document is, and there is one mechanism to
review rather than two.

Chunking
--------
Fixed-size character windows with an overlap, split on paragraph and
sentence boundaries where one is available near the target size. Not the
most sophisticated strategy available, and deliberately so: the appliance
embeds with a 384-dimension MiniLM whose useful context is a few hundred
words, and a clever structural chunker that produces chunks the embedder
cannot represent is worse than a plain one that produces chunks it can.
Overlap exists so an answer that straddles a boundary is still retrievable
from at least one chunk.

Formats
-------
Text, Markdown, JSON, CSV and HTML are parsed here with no dependency.
PDF goes through `pypdf` when it is installed, and reports a clear error
when it is not, rather than silently indexing an empty document - a
knowledge base that quietly contains nothing is worse than one that refused
the upload.
"""
import hashlib
import html
import io
import json
import re
import time
import uuid

# Chunking. 1200 characters is roughly 200-300 words: comfortably inside
# what all-MiniLM-L6-v2 represents well, and large enough that a chunk
# usually carries a complete thought.
DEFAULT_CHUNK_CHARS = 1200
DEFAULT_CHUNK_OVERLAP = 200
MIN_CHUNK_CHARS = 200
MAX_CHUNK_CHARS = 4000
MAX_DOCUMENT_CHARS = 4_000_000
MAX_CHUNKS_PER_DOCUMENT = 2000

SUPPORTED_EXTENSIONS = (".txt", ".md", ".markdown", ".json", ".csv", ".tsv", ".html", ".htm", ".pdf", ".log", ".rst")

# Where a paragraph break is preferred over a sentence break, which is
# preferred over a hard cut.
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def new_document_id(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:48] or "document"
    return f"{slug}-{uuid.uuid4().hex[:8]}"


def content_fingerprint(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _strip_html(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    return html.unescape(raw)


def _flatten_json(value, depth: int = 0) -> str:
    """JSON becomes readable lines rather than raw syntax - an embedding of
    `{"a": 1}` is mostly an embedding of braces."""
    if depth > 8:
        return ""
    if isinstance(value, dict):
        return "\n".join(f"{k}: {_flatten_json(v, depth + 1)}" for k, v in value.items())
    if isinstance(value, list):
        return "\n".join(_flatten_json(v, depth + 1) for v in value[:500])
    return str(value)


def extract_text(raw: bytes | str, filename: str = "") -> tuple[str, str]:
    """Return (text, format). Raises ValueError with a message meant to be
    shown to whoever attempted the upload."""
    name = (filename or "").lower()
    extension = "." + name.rsplit(".", 1)[-1] if "." in name else ""

    if extension == ".pdf":
        if not isinstance(raw, bytes):
            raise ValueError("A PDF must be uploaded as binary content, not text")
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # noqa: BLE001
            raise ValueError(
                "PDF support needs the 'pypdf' package, which is not installed in this image. "
                "Install it, or convert the file to text or Markdown before uploading."
            ) from exc
        try:
            reader = PdfReader(io.BytesIO(raw))
            pages = [(page.extract_text() or "") for page in reader.pages]
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Could not read that PDF: {exc}") from exc
        text = "\n\n".join(p for p in pages if p.strip())
        if not text.strip():
            raise ValueError(
                "No selectable text found in that PDF - it is most likely a scan. "
                "Run it through OCR first; indexing it as-is would add an empty document."
            )
        return text, "pdf"

    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1", errors="replace")
    else:
        text = raw

    if extension in (".html", ".htm"):
        return _strip_html(text), "html"
    if extension == ".json":
        try:
            return _flatten_json(json.loads(text)), "json"
        except (ValueError, TypeError):
            return text, "json"
    if extension in (".csv", ".tsv"):
        return text, "csv"
    if extension in (".md", ".markdown"):
        return text, "markdown"
    return text, "text"


def normalize_text(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    # Collapse runs of blank lines but keep paragraph structure, which the
    # chunker splits on.
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()[:MAX_DOCUMENT_CHARS]


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def _split_point(window: str, target: int) -> int:
    """Prefer a paragraph break, then a sentence end, then a hard cut. Only
    looks in the back third of the window, so a break near the start cannot
    produce a chunk of two words."""
    floor = int(target * 0.6)
    candidates = [m.end() for m in _PARAGRAPH_BREAK.finditer(window) if m.end() >= floor]
    if candidates:
        return candidates[-1]
    candidates = [m.end() for m in _SENTENCE_END.finditer(window) if m.end() >= floor]
    if candidates:
        return candidates[-1]
    return len(window)


def chunk_text(text: str, chunk_chars: int = DEFAULT_CHUNK_CHARS, overlap: int = DEFAULT_CHUNK_OVERLAP) -> list[str]:
    text = normalize_text(text)
    if not text:
        return []

    chunk_chars = max(MIN_CHUNK_CHARS, min(MAX_CHUNK_CHARS, int(chunk_chars)))
    overlap = max(0, min(chunk_chars // 2, int(overlap)))

    chunks: list[str] = []
    position = 0
    while position < len(text) and len(chunks) < MAX_CHUNKS_PER_DOCUMENT:
        window = text[position:position + chunk_chars]
        if position + chunk_chars >= len(text):
            chunk = window
            position = len(text)
        else:
            cut = _split_point(window, chunk_chars)
            chunk = window[:cut]
            # Advance by the chunk minus the overlap, never by less than a
            # third of the target - otherwise a document with no break
            # points could loop producing near-identical chunks.
            position += max(cut - overlap, chunk_chars // 3)
        chunk = chunk.strip()
        if chunk:
            chunks.append(chunk)
    return chunks


# ---------------------------------------------------------------------------
# Documents and chunks
# ---------------------------------------------------------------------------

def build_document_doc(
    *,
    document_id: str,
    title: str,
    source: str,
    text_format: str,
    allowed_roles: list[str],
    chunk_count: int,
    char_count: int,
    fingerprint: str,
    uploaded_by: str | None,
    metadata: dict | None = None,
) -> dict:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "doc_type": "knowledge_document",
        "document_id": document_id,
        "title": (title or "Untitled")[:300],
        "source": (source or "")[:500],
        "format": text_format,
        "allowed_roles": list(allowed_roles or []),
        "chunk_count": chunk_count,
        "char_count": char_count,
        "fingerprint": fingerprint,
        "uploaded_by": uploaded_by,
        "metadata": {str(k)[:120]: str(v)[:500] for k, v in list((metadata or {}).items())[:20]},
        "created_at": now,
        "updated_at": now,
    }


def build_chunk_doc(
    *,
    document_id: str,
    document_title: str,
    index: int,
    content: str,
    embedding: list,
    allowed_roles: list[str],
) -> dict:
    return {
        "doc_type": "knowledge_chunk",
        "chunk_id": f"{document_id}::{index:04d}",
        "document_id": document_id,
        "document_title": document_title,
        "chunk_index": index,
        "content": content,
        "embedding": embedding,
        # Denormalized onto every chunk on purpose: the retrieval pre-filter
        # runs against the chunk, so the role list has to be on the chunk.
        # Re-scoping a document rewrites its chunks, which is the cost of
        # making the filter a single Search request instead of a join.
        "allowed_roles": list(allowed_roles or []),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def build_embedding_text(title: str, content: str) -> str:
    """The document's title rides along with each chunk's own text, so a
    chunk that never repeats the subject is still findable by it."""
    title = (title or "").strip()
    return f"{title}\n\n{content}" if title else content


def preview(content: str, limit: int = 280) -> str:
    content = re.sub(r"\s+", " ", content or "").strip()
    return content[:limit] + ("..." if len(content) > limit else "")
