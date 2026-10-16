"""Background ingestion of large Knowledge Base uploads.

The inline path (POST /v1/knowledge) reads a base64 JSON body, parses,
embeds and stores it all inside one request - fine for a 20 MB PDF,
impossible for a 5 GB warehouse unload: the browser would have to hold a
~7 GB string, the process a few copies of it, and the request would
outlive every timeout between them. This module is the other path:

  1. **Streaming upload.** POST /v1/knowledge/uploads streams the raw
     request body straight to a file in the spool directory
     (KNOWLEDGE_SPOOL_DIR) - no base64, no JSON, nothing held in memory
     beyond one 1 MB read. The browser sends a File object as the body,
     which it also streams; curl uses --data-binary @file.
  2. **Cloud import.** POST /v1/knowledge/imports takes a URI instead -
     the usual way a 5 GB export arrives: an S3/GCS/Azure object (Snowflake
     unloads to an external stage, BigQuery EXPORT DATA, Databricks
     df.write to a Volume or cloud path), or an https URL (a presigned
     URL, or the Databricks Files API with a bearer token). The object is
     streamed into the spool the same way.
  3. **A job.** Either route returns a job id at once. One worker per
     KNOWLEDGE_INGEST_CONCURRENCY slot takes the job: streams rows out of
     the spooled file (app/data_dumps.DumpReader) or text out of a
     document, chunks as it goes, embeds in batches of
     KNOWLEDGE_INGEST_BATCH_CHUNKS, bulk-writes each batch, and records
     progress on the job document every couple of seconds so the Knowledge
     page can show it. Memory is one batch of rows plus one batch of
     chunks, whatever the file size.
  4. **Cancel / fail / resume.** A cancelled or failed job deletes the
     chunks it wrote (nothing half-indexed is ever retrievable: the parent
     document is written last). Jobs still queued or running when the
     process restarts are re-queued if their spool file survived, and
     marked failed with a clear reason if it did not - which is why the
     spool should be a persistent volume in Kubernetes (see the chart's
     operationsManager.spool.persistence).

Per-document caps are gone on this path: a single export may produce
millions of chunks. What bounds it instead is KNOWLEDGE_MAX_UPLOAD_MB,
KNOWLEDGE_MAX_CHUNKS_PER_DOCUMENT (a safety ceiling, default 2,000,000)
and the knowledge set's own quota.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
import uuid
from typing import Awaitable, Callable
from urllib.parse import urlparse

import requests

from app import data_dumps, knowledge

logger = logging.getLogger("operations-manager.ingest_jobs")

ACTIVE_STATUSES = ("uploading", "queued", "downloading", "running")
TERMINAL_STATUSES = ("done", "failed", "cancelled")
PROGRESS_FLUSH_SECONDS = 2.0
CHUNK_READ = 1 << 20  # 1 MiB
MAX_RECENT_JOBS = 50
# A PDF has to be parsed whole, so documents (not dumps) are read into
# memory; this caps that path independently of the streaming one.
MAX_DOCUMENT_BYTES_IN_MEMORY = 512 * 1024 * 1024


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_job_id() -> str:
    return f"job-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:6]}"


class JobCancelled(Exception):
    pass


class ImportError_(RuntimeError):
    """A readable reason a cloud import could not fetch the object."""


# ---------------------------------------------------------------------------
# Job documents
# ---------------------------------------------------------------------------
def new_job(*, kind: str, params: dict, filename: str, source_uri: str | None, bytes_total: int | None,
            uploaded_by: str | None) -> dict:
    return {
        "doc_type": "knowledge_job",
        "job_id": new_job_id(),
        "kind": kind,  # "upload" | "import"
        "status": "uploading" if kind == "upload" else "queued",
        "phase": "receiving" if kind == "upload" else "queued",
        "filename": (filename or "")[:300],
        "source_uri": (source_uri or "")[:1000] or None,
        "bytes_total": bytes_total,
        "bytes_received": 0,
        "params": params,
        "progress": {"rows_parsed": 0, "chunks_written": 0, "chunks_estimated": None, "files": 1, "percent": None},
        "document_id": None,
        "error": None,
        "uploaded_by": uploaded_by,
        "created_at": now_iso(),
        "started_at": None,
        "finished_at": None,
        "updated_at": now_iso(),
    }


def public_job(job: dict) -> dict:
    """Strip anything secret (import credentials) before it leaves the API."""
    out = dict(job)
    params = dict(out.get("params") or {})
    params.pop("auth", None)
    out["params"] = params
    return out


# ---------------------------------------------------------------------------
# Spool
# ---------------------------------------------------------------------------
class Spool:
    def __init__(self, directory: str):
        self.directory = directory
        os.makedirs(directory, exist_ok=True)

    def path(self, job_id: str) -> str:
        return os.path.join(self.directory, f"{job_id}.bin")

    def exists(self, job_id: str) -> bool:
        return os.path.exists(self.path(job_id))

    def remove(self, job_id: str) -> None:
        try:
            os.unlink(self.path(job_id))
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("could not remove spool file for %s: %s", job_id, exc)

    def free_bytes(self) -> int | None:
        try:
            st = os.statvfs(self.directory)
            return st.f_bavail * st.f_frsize
        except OSError:
            return None

    def used_bytes(self) -> int:
        total = 0
        try:
            for name in os.listdir(self.directory):
                try:
                    total += os.path.getsize(os.path.join(self.directory, name))
                except OSError:
                    pass
        except OSError:
            pass
        return total


# ---------------------------------------------------------------------------
# Cloud fetch (imports)
# ---------------------------------------------------------------------------
def _stream_to(path: str, iterator, on_progress: Callable[[int], None], limit_bytes: int) -> int:
    received = 0
    with open(path, "wb") as out:
        for block in iterator:
            if not block:
                continue
            received += len(block)
            if received > limit_bytes:
                raise ImportError_(f"the object is larger than the {limit_bytes // (1024 * 1024)} MB upload limit")
            out.write(block)
            on_progress(received)
    return received


def fetch_uri(uri: str, auth: dict | None, dest: str, *, on_progress: Callable[[int], None],
              on_total: Callable[[int | None], None], limit_bytes: int, cancelled: Callable[[], bool]) -> int:
    """Stream one object into `dest`. Returns the byte count.

    Schemes: https:// (optionally with a bearer token or a named header -
    presigned S3/GCS/Azure URLs and the Databricks Files API both work
    this way), s3:// (boto3; credentials from auth, the environment, an
    instance role or IRSA), gs:// (google-cloud-storage; a service-account
    JSON in auth, else Application Default Credentials), az:// or
    abfss:// (azure-storage-blob; a connection string or account key in
    auth, or a SAS token). The cloud SDKs are imported lazily so a build
    without them still serves https."""
    auth = auth or {}
    parsed = urlparse(uri)
    scheme = (parsed.scheme or "").lower()

    def _iter_with_cancel(it):
        for block in it:
            if cancelled():
                raise JobCancelled()
            yield block

    if scheme in ("http", "https"):
        headers = {}
        mode = (auth.get("mode") or "none").lower()
        if mode == "bearer" and auth.get("secret"):
            headers["Authorization"] = f"Bearer {auth['secret']}"
        elif mode == "header" and auth.get("secret"):
            headers[auth.get("header_name") or "X-Api-Key"] = auth["secret"]
        elif mode == "basic" and auth.get("secret"):
            import base64
            headers["Authorization"] = "Basic " + base64.b64encode(f"{auth.get('username', '')}:{auth['secret']}".encode()).decode()
        try:
            resp = requests.get(uri, headers=headers, stream=True, timeout=(15, 120))
        except requests.exceptions.RequestException as exc:
            raise ImportError_(f"could not fetch {uri}: {exc.__class__.__name__}: {exc}") from exc
        if resp.status_code in (401, 403):
            raise ImportError_(f"the server refused the download (HTTP {resp.status_code}) - the URL has expired or "
                               "the credential is wrong")
        if resp.status_code >= 400:
            raise ImportError_(f"the server answered HTTP {resp.status_code} for {uri}")
        length = resp.headers.get("Content-Length")
        on_total(int(length) if length and length.isdigit() else None)
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "text/html" in ctype:
            raise ImportError_("the URL returned an HTML page, not a data file - it is probably a sign-in page or a "
                               "browser download link rather than a direct object URL")
        return _stream_to(dest, _iter_with_cancel(resp.iter_content(CHUNK_READ)), on_progress, limit_bytes)

    if scheme == "s3":
        try:
            import boto3
            from botocore.exceptions import BotoCoreError, ClientError
        except ImportError as exc:
            raise ImportError_("s3:// imports need the 'boto3' package in the operations-manager image") from exc
        kwargs = {}
        if auth.get("access_key_id") and auth.get("secret"):
            kwargs.update(aws_access_key_id=auth["access_key_id"], aws_secret_access_key=auth["secret"])
            if auth.get("session_token"):
                kwargs["aws_session_token"] = auth["session_token"]
        if auth.get("region"):
            kwargs["region_name"] = auth["region"]
        if auth.get("endpoint_url"):
            kwargs["endpoint_url"] = auth["endpoint_url"]
        bucket, key = parsed.netloc, parsed.path.lstrip("/")
        try:
            s3 = boto3.client("s3", **kwargs)
            head = s3.head_object(Bucket=bucket, Key=key)
            on_total(int(head.get("ContentLength") or 0) or None)
            body = s3.get_object(Bucket=bucket, Key=key)["Body"]
            return _stream_to(dest, _iter_with_cancel(body.iter_chunks(CHUNK_READ)), on_progress, limit_bytes)
        except (ClientError, BotoCoreError) as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "") if hasattr(exc, "response") else ""
            if code in ("403", "AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"):
                raise ImportError_(f"S3 refused access to s3://{bucket}/{key} ({code}) - check the credentials or the "
                                   "bucket policy") from exc
            if code in ("404", "NoSuchKey", "NoSuchBucket"):
                raise ImportError_(f"s3://{bucket}/{key} does not exist ({code})") from exc
            raise ImportError_(f"S3 error for s3://{bucket}/{key}: {exc}") from exc

    if scheme == "gs":
        try:
            from google.cloud import storage
            from google.oauth2 import service_account
        except ImportError as exc:
            raise ImportError_("gs:// imports need the 'google-cloud-storage' package in the operations-manager image") from exc
        bucket, key = parsed.netloc, parsed.path.lstrip("/")
        try:
            if auth.get("service_account_json"):
                import json as _json
                creds = service_account.Credentials.from_service_account_info(_json.loads(auth["service_account_json"]))
                client = storage.Client(credentials=creds, project=creds.project_id)
            else:
                client = storage.Client()
            blob = client.bucket(bucket).get_blob(key)
            if blob is None:
                raise ImportError_(f"gs://{bucket}/{key} does not exist")
            on_total(int(blob.size or 0) or None)
            with blob.open("rb", chunk_size=CHUNK_READ) as fh:
                return _stream_to(dest, _iter_with_cancel(iter(lambda: fh.read(CHUNK_READ), b"")), on_progress, limit_bytes)
        except ImportError_:
            raise
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            if "403" in text or "Forbidden" in text:
                raise ImportError_(f"Cloud Storage refused access to gs://{bucket}/{key} - the service account needs "
                                   "storage.objects.get on the bucket") from exc
            raise ImportError_(f"Cloud Storage error for gs://{bucket}/{key}: {text[:300]}") from exc

    if scheme in ("az", "abfss", "wasbs"):
        try:
            from azure.storage.blob import BlobClient, BlobServiceClient
        except ImportError as exc:
            raise ImportError_("az:// imports need the 'azure-storage-blob' package in the operations-manager image") from exc
        # az://<container>/<path>  with auth.account (+ secret = account key / SAS) or auth.connection_string
        # abfss://<container>@<account>.dfs.core.windows.net/<path>
        if scheme == "az":
            container, key = parsed.netloc, parsed.path.lstrip("/")
            account = auth.get("account") or ""
        else:
            container = parsed.username or parsed.netloc.split("@")[0]
            host = parsed.hostname or ""
            account = host.split(".")[0]
            key = parsed.path.lstrip("/")
        try:
            if auth.get("connection_string"):
                client = BlobServiceClient.from_connection_string(auth["connection_string"]).get_blob_client(container, key)
            elif account and auth.get("secret"):
                url = f"https://{account}.blob.core.windows.net"
                client = BlobServiceClient(account_url=url, credential=auth["secret"]).get_blob_client(container, key)
            else:
                raise ImportError_("Azure imports need a connection string, or the storage account name plus an account "
                                   "key or SAS token")
            props = client.get_blob_properties()
            on_total(int(props.size or 0) or None)
            stream = client.download_blob(max_concurrency=2)
            return _stream_to(dest, _iter_with_cancel(stream.chunks()), on_progress, limit_bytes)
        except ImportError_:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ImportError_(f"Azure Blob error for {uri}: {str(exc)[:300]}") from exc

    raise ImportError_(f"unsupported URI scheme '{scheme}' - use https://, s3://, gs://, az:// or abfss://")


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------
class IngestRunner:
    """Owns the queue, the concurrency slots and the cancel flags. Wired
    with callables from main.py so it exercises the same embedder, store
    and expiry logic as the inline path."""

    def __init__(self, *, spool: Spool, store, embedder_for, get_knowledge_set, chunk_chars: int, chunk_overlap: int,
                 batch_chunks: int, max_chunks_per_document: int, max_upload_bytes: int, concurrency: int,
                 on_document_ingested: Callable[[], None], spawn):
        self.spool = spool
        self.store = store
        self.embedder_for = embedder_for
        self.get_knowledge_set = get_knowledge_set
        self.chunk_chars = chunk_chars
        self.chunk_overlap = chunk_overlap
        self.batch_chunks = max(16, batch_chunks)
        self.max_chunks = max_chunks_per_document
        self.max_upload_bytes = max_upload_bytes
        self.semaphore = asyncio.Semaphore(max(1, concurrency))
        self.on_document_ingested = on_document_ingested
        self.spawn = spawn
        self._cancel: set[str] = set()
        self._running: dict[str, asyncio.Task] = {}

    # -- lifecycle -----------------------------------------------------------
    def enqueue(self, job: dict) -> None:
        self._running[job["job_id"]] = self.spawn(self._run(job))

    def cancel(self, job_id: str) -> None:
        self._cancel.add(job_id)

    def is_cancelled(self, job_id: str) -> bool:
        return job_id in self._cancel

    def active_count(self) -> int:
        return sum(1 for t in self._running.values() if not t.done())

    async def resume(self) -> int:
        """At startup: re-queue jobs the previous process left active."""
        jobs = await self.store.list_knowledge_jobs(limit=200, active_only=True)
        resumed = 0
        for job in jobs:
            if job["status"] == "uploading":
                # The upload connection died with the old process.
                job.update(status="failed", phase="failed", error="the upload was interrupted by a restart - upload it again",
                           finished_at=now_iso(), updated_at=now_iso())
                await self.store.upsert_knowledge_job(job)
                self.spool.remove(job["job_id"])
                continue
            if job["kind"] == "upload" and not self.spool.exists(job["job_id"]):
                job.update(status="failed", phase="failed", finished_at=now_iso(), updated_at=now_iso(),
                           error="the spooled file was lost in a restart (the spool directory is not on a persistent "
                                 "volume) - upload it again")
                await self.store.upsert_knowledge_job(job)
                continue
            if job.get("document_id"):
                # Partial chunks from the interrupted attempt: start clean.
                await self.store.delete_knowledge_document(job["document_id"])
            job.update(status="queued", phase="queued", document_id=None, error=None, updated_at=now_iso())
            job["progress"] = {"rows_parsed": 0, "chunks_written": 0, "chunks_estimated": None, "files": 1, "percent": None}
            if job["kind"] == "import":
                job["bytes_received"] = 0
            await self.store.upsert_knowledge_job(job)
            self.enqueue(job)
            resumed += 1
        return resumed

    # -- progress ------------------------------------------------------------
    async def _save(self, job: dict) -> None:
        job["updated_at"] = now_iso()
        await self.store.upsert_knowledge_job(job)

    def _check_cancel(self, job: dict) -> None:
        if job["job_id"] in self._cancel:
            raise JobCancelled()

    # -- run -------------------------------------------------------------------
    async def _run(self, job: dict) -> None:
        job_id = job["job_id"]
        try:
            async with self.semaphore:
                self._check_cancel(job)
                if job["kind"] == "import":
                    await self._download(job)
                job.update(status="running", phase="parsing", started_at=job.get("started_at") or now_iso())
                await self._save(job)
                document_id = await self._ingest(job)
                job.update(status="done", phase="done", document_id=document_id, finished_at=now_iso())
                job["progress"]["percent"] = 100.0
                await self._save(job)
                self.spool.remove(job_id)
                self.on_document_ingested()
                logger.info("Knowledge job %s done: document %s, %d chunk(s)", job_id, document_id, job["progress"]["chunks_written"])
        except JobCancelled:
            await self._abort(job, status="cancelled", error="cancelled by an operator")
        except (data_dumps.DumpError, ImportError_, ValueError) as exc:
            await self._abort(job, status="failed", error=str(exc)[:1000])
        except Exception as exc:  # noqa: BLE001
            logger.exception("Knowledge job %s failed", job_id)
            await self._abort(job, status="failed", error=f"{exc.__class__.__name__}: {str(exc)[:800]}")
        finally:
            self._cancel.discard(job_id)
            self._running.pop(job_id, None)

    async def _abort(self, job: dict, *, status: str, error: str) -> None:
        # _ingest records the document id on the job as soon as it picks
        # one, so partial chunks are found even when it never returned.
        document_id = job.get("document_id")
        job.update(status=status, phase=status, error=error, finished_at=now_iso(), document_id=None)
        if document_id:
            # Nothing half-indexed stays retrievable.
            removed = await self.store.delete_knowledge_document(document_id)
            logger.info("Knowledge job %s %s: removed %d partial chunk(s)", job["job_id"], status, removed)
        if status == "cancelled":
            self.spool.remove(job["job_id"])
        await self._save(job)

    async def _download(self, job: dict) -> None:
        job.update(status="downloading", phase="downloading", started_at=now_iso())
        await self._save(job)
        last_flush = [time.time()]
        loop = asyncio.get_running_loop()

        def on_progress(received: int):
            job["bytes_received"] = received
            if job.get("bytes_total"):
                job["progress"]["percent"] = round(min(99.0, received / job["bytes_total"] * 100), 1)
            if time.time() - last_flush[0] > PROGRESS_FLUSH_SECONDS:
                last_flush[0] = time.time()
                asyncio.run_coroutine_threadsafe(self._save(dict(job)), loop)

        def on_total(total):
            job["bytes_total"] = total

        params = job.get("params") or {}
        received = await asyncio.to_thread(
            fetch_uri, job["source_uri"], params.get("auth"), self.spool.path(job["job_id"]),
            on_progress=on_progress, on_total=on_total, limit_bytes=self.max_upload_bytes,
            cancelled=lambda: job["job_id"] in self._cancel,
        )
        job["bytes_received"] = received
        job["bytes_total"] = job.get("bytes_total") or received
        # The credential was for the download only.
        params.pop("auth", None)

    async def _ingest(self, job: dict) -> str:
        params = job["params"]
        kset = self.get_knowledge_set(params.get("set_id"))
        embedder = self.embedder_for(kset)
        title = params.get("title") or job.get("filename") or "Untitled"
        allowed_roles = list(params.get("allowed_roles") or [])
        expires_at = params.get("expires_at")
        document_id = knowledge.new_document_id(title)
        job["document_id"] = document_id
        path = self.spool.path(job["job_id"])
        progress = job["progress"]
        last_flush = time.time()
        dump = params.get("dump")
        text_format = ""
        dump_meta: dict | None = None
        reader: data_dumps.DumpReader | None = None
        char_count = 0
        fingerprint = ""

        if dump:
            reader = await asyncio.to_thread(
                data_dumps.open_dump, path, job.get("filename") or title,
                header=dump.get("header"), column_names=dump.get("column_names") or [],
            )
            progress["files"] = reader.files
            if reader.total_rows:
                progress["rows_total"] = reader.total_rows
            source = dump.get("source") or "other"
            table_name = dump.get("table_name") or title
            text_format = f"{reader.format} dump"
            chunk_iter = data_dumps.chunk_table_stream(reader, source=source, table_name=table_name, chunk_chars=self.chunk_chars)
            first_index = 1  # index 0 is the schema chunk, written last
        else:
            text, text_format = await asyncio.to_thread(self._read_document_text, path, job.get("filename") or title)
            text = knowledge.normalize_text(text)
            if not text:
                raise ValueError("Nothing readable in that document once it was parsed")
            char_count = len(text)
            fingerprint = knowledge.content_fingerprint(text)
            chunk_iter = iter(knowledge.chunk_text(text, self.chunk_chars, self.chunk_overlap))
            first_index = 0

        job["phase"] = "embedding"
        await self._save(job)

        async def write_batch(batch: list[tuple[int, str]]) -> None:
            vectors = await embedder.embed_documents([knowledge.build_embedding_text(title, c) for _, c in batch])
            docs = []
            for (index, content), embedding in zip(batch, vectors):
                chunk_doc = knowledge.build_chunk_doc(
                    document_id=document_id, document_title=title, index=index, content=content,
                    embedding=embedding, allowed_roles=allowed_roles,
                )
                if kset["set_id"] != "default":
                    chunk_doc[kset["vector_field"]] = chunk_doc.pop("embedding")
                    chunk_doc["set_id"] = kset["set_id"]
                if expires_at:
                    chunk_doc["expires_at"] = expires_at
                docs.append(chunk_doc)
            await self.store.upsert_knowledge_chunks(docs)

        index = first_index
        batch: list[tuple[int, str]] = []
        written = 0
        # Pull chunks off the (blocking) iterator in a thread, a batch at a
        # time, so parsing a 5 GB CSV never stalls the event loop.
        def next_batch() -> list[str]:
            out = []
            try:
                while len(out) < self.batch_chunks:
                    out.append(next(chunk_iter))
            except StopIteration:
                pass
            return out

        while True:
            self._check_cancel(job)
            texts = await asyncio.to_thread(next_batch)
            if not texts:
                break
            for t in texts:
                batch.append((index, t))
                index += 1
            if written + len(batch) > self.max_chunks:
                raise ValueError(
                    f"this upload exceeds the {self.max_chunks:,}-chunk ceiling per document "
                    "(KNOWLEDGE_MAX_CHUNKS_PER_DOCUMENT) - split the export or raise the ceiling"
                )
            await write_batch(batch)
            written += len(batch)
            batch = []
            progress["chunks_written"] = written
            if reader is not None:
                progress["rows_parsed"] = reader.rows_read
                if reader.total_rows:
                    progress["percent"] = round(min(99.0, reader.rows_read / reader.total_rows * 100), 1)
                elif job.get("bytes_total"):
                    progress["percent"] = None  # unknown for streamed CSV/JSON; rows_parsed is the signal
            else:
                est = max(1, char_count // max(1, self.chunk_chars - self.chunk_overlap))
                progress["chunks_estimated"] = est
                progress["percent"] = round(min(99.0, written / est * 100), 1)
            if time.time() - last_flush > PROGRESS_FLUSH_SECONDS:
                last_flush = time.time()
                await self._save(job)

        if reader is not None:
            schema = data_dumps.schema_chunk(reader, source=dump.get("source") or "other", table_name=dump.get("table_name") or title,
                                             chunk_chars=self.chunk_chars, total_rows=reader.rows_read)
            await write_batch([(0, schema)])
            written += 1
            reader.close()
            notes = list(reader.notes)
            dump_meta = {
                "source": dump.get("source") or "other",
                "source_label": data_dumps.SOURCES.get(dump.get("source") or "other", "Other"),
                "table_name": dump.get("table_name") or title,
                "format": reader.format,
                "files": reader.files,
                "columns": reader.columns[:100],
                "column_count": len(reader.columns),
                "total_rows": reader.rows_read,
                "rows_indexed": reader.rows_read,
                "truncated": False,
                "notes": notes,
            }
            char_count = written * self.chunk_chars
            fingerprint = knowledge.content_fingerprint(f"{document_id}:{reader.rows_read}:{written}")
            progress["rows_parsed"] = reader.rows_read

        if written == 0:
            raise ValueError("That file produced no chunks")

        doc = knowledge.build_document_doc(
            document_id=document_id, title=title, source=job.get("source_uri") or job.get("filename") or "upload",
            text_format=text_format, allowed_roles=allowed_roles, chunk_count=written, char_count=char_count,
            fingerprint=fingerprint, uploaded_by=job.get("uploaded_by"), metadata=params.get("metadata") or {},
        )
        doc["set_id"] = kset["set_id"]
        doc["embedding_model"] = kset["model_id"]
        doc["expires_at"] = expires_at
        doc["job_id"] = job["job_id"]
        doc["bytes"] = job.get("bytes_received") or job.get("bytes_total")
        if dump_meta:
            doc["dump"] = dump_meta
        await self.store.upsert_knowledge_document(doc)
        progress["chunks_written"] = written
        return document_id

    @staticmethod
    def _read_document_text(path: str, filename: str) -> tuple[str, str]:
        size = os.path.getsize(path)
        lower = (filename or "").lower()
        if lower.endswith(".pdf") or size <= MAX_DOCUMENT_BYTES_IN_MEMORY:
            if size > MAX_DOCUMENT_BYTES_IN_MEMORY:
                raise ValueError(
                    f"a PDF over {MAX_DOCUMENT_BYTES_IN_MEMORY // (1024 * 1024)} MB cannot be parsed - split it, "
                    "or export the text"
                )
            with open(path, "rb") as fh:
                raw = fh.read()
            return knowledge.extract_text(raw, filename)
        # Very large plain text: read as text without a full copy in bytes.
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(), "text"
