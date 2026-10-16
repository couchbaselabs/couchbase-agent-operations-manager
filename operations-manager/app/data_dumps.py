"""
Warehouse data dumps as Knowledge Base documents.

A table export is not prose. The plain-text path would cut it into 1,200
character windows that split rows in half and drop the column names after
the first chunk, so a retrieved chunk reads as a run of bare values. This
module parses the export as a table and chunks it by *row*: every chunk
repeats the source, table and column list, and every row is rendered as
`column: value` pairs, so any single retrieved chunk is self-describing.
A schema chunk (row count, columns and their types where the format
carries them) is indexed first, for questions about the table itself.

Formats - what Snowflake, Databricks and BigQuery actually export:

  * CSV / TSV, optionally gzip-compressed (Snowflake COPY INTO unloads are
    .csv.gz by default; BigQuery extract jobs can gzip too). Header
    detection is automatic unless told otherwise - Snowflake unloads have
    no header row unless HEADER = TRUE was set.
  * Newline-delimited JSON (.json/.jsonl/.ndjson, optionally .gz) - the
    JSON shape of all three; a single top-level JSON array is accepted too.
  * Parquet (pyarrow) - Databricks/Spark's default, and a BigQuery and
    Snowflake export option.
  * Avro (fastavro) - BigQuery's other binary export format.
  * A .zip of any of the above - a Spark output folder (part-00000-...)
    or a multi-file Snowflake/BigQuery unload, merged in order. Spark's
    _SUCCESS/_committed/.crc markers are skipped. A Delta `_delta_log` is
    ignored and flagged: raw Delta data files can include rows that later
    versions removed, so export a snapshot with df.write.parquet instead.

Nothing here talks to Couchbase or embeds anything; app/main.py feeds the
chunks this returns into the normal ingest path (roles, knowledge set,
embedding, expiry).

Two entry points:

  * parse_dump(raw_bytes, ...) / chunk_table(parsed, ...) - the original
    in-memory path, still used by the small inline POST /v1/knowledge
    upload (bounded by KNOWLEDGE_MAX_INLINE_MB).
  * open_dump(path, ...) -> DumpReader, and chunk_table_stream(reader, ...)
    - the streaming path behind background ingestion jobs
    (app/ingest_jobs.py). Rows are read from the spooled file in batches
    and never all held at once, so a multi-gigabyte unload costs the same
    memory as a small one: one batch of rows plus one batch of chunks.
"""
import csv
import datetime as _dt
import decimal
import gzip
import io
import json
import re
import zipfile

SOURCES = {
    "snowflake": "Snowflake",
    "databricks": "Databricks",
    "bigquery": "BigQuery",
    "other": "Other warehouse or database",
}
DUMP_EXTENSIONS = (
    ".csv", ".tsv", ".gz", ".json", ".jsonl", ".ndjson", ".parquet", ".avro", ".zip",
)
MAX_VALUE_CHARS = 300
MAX_COLUMNS = 300
_SKIP_NAMES = re.compile(r"(^|/)(_SUCCESS|_committed_[^/]*|_started_[^/]*|[^/]*\.crc|\.DS_Store)$")


class DumpError(ValueError):
    """A message meant for whoever uploaded the dump."""


def _maybe_gunzip(raw: bytes, name: str) -> tuple[bytes, str]:
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except OSError as exc:
            raise DumpError(f"'{name}' looks gzip-compressed but couldn't be decompressed: {exc}") from exc
        name = re.sub(r"\.gz$", "", name, flags=re.IGNORECASE)
    elif raw[:4] == b"\x28\xb5\x2f\xfd":
        raise DumpError(f"'{name}' is zstd-compressed - re-export with gzip or no compression")
    return raw, name


def _detect_format(raw: bytes, name: str) -> str:
    lower = name.lower()
    if raw[:4] == b"PAR1" or lower.endswith(".parquet"):
        return "parquet"
    if raw[:4] == b"Obj\x01" or lower.endswith(".avro"):
        return "avro"
    if lower.endswith(".tsv"):
        return "tsv"
    if lower.endswith((".json", ".jsonl", ".ndjson")):
        return "json"
    if lower.endswith(".csv"):
        return "csv"
    head = raw[:2048].lstrip()
    if head[:1] in (b"{", b"["):
        return "json"
    return "csv"


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")


def _read_delimited(raw: bytes, delimiter: str | None, header: bool | None, column_names: list[str]):
    text = _decode(raw)
    sample = text[:20000]
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",\t|;").delimiter
        except csv.Error:
            delimiter = ","
    if header is None and not column_names:
        try:
            header = csv.Sniffer().has_header(sample)
        except csv.Error:
            header = True
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    columns: list[str] = []
    if header:
        columns = next(reader, [])
    if column_names:
        columns = column_names
    rows = []
    for row in reader:
        if not row or (len(row) == 1 and not row[0].strip()):
            continue
        rows.append(row)
    width = max([len(columns)] + [len(r) for r in rows[:1000]] or [0])
    columns = list(columns) + [f"column_{i + 1}" for i in range(len(columns), width)]
    types = {}
    return columns, ([dict(zip(columns, r)) for r in rows]), types


def _read_json(raw: bytes):
    text = _decode(raw).strip()
    records = []
    if text.startswith("["):
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise DumpError(f"Not valid JSON: {exc}") from exc
        records = data if isinstance(data, list) else [data]
    else:
        for i, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError as exc:
                raise DumpError(f"Line {i} isn't valid JSON - expected one JSON object per line: {exc}") from exc
    rows = [r if isinstance(r, dict) else {"value": r} for r in records]
    columns: list[str] = []
    for r in rows[:2000]:
        for k in r:
            if k not in columns:
                columns.append(k)
    return columns, rows, {}


def _read_parquet(raw: bytes, max_rows: int):
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise DumpError("Parquet support needs 'pyarrow', which isn't installed in this image") from exc
    try:
        pf = pq.ParquetFile(io.BytesIO(raw))
    except Exception as exc:  # noqa: BLE001
        raise DumpError(f"Couldn't read that Parquet file: {exc}") from exc
    schema = pf.schema_arrow
    columns = list(schema.names)
    types = {f.name: str(f.type) for f in schema}
    rows: list[dict] = []
    for batch in pf.iter_batches(batch_size=5000):
        rows.extend(batch.to_pylist())
        if len(rows) >= max_rows:
            break
    return columns, rows, types, pf.metadata.num_rows


def _read_avro(raw: bytes, max_rows: int):
    try:
        import fastavro
    except ImportError as exc:
        raise DumpError("Avro support needs 'fastavro', which isn't installed in this image") from exc
    try:
        reader = fastavro.reader(io.BytesIO(raw))
        schema = reader.writer_schema or {}
        rows = []
        total = 0
        for rec in reader:
            total += 1
            if len(rows) < max_rows:
                rows.append(rec)
    except Exception as exc:  # noqa: BLE001
        raise DumpError(f"Couldn't read that Avro file: {exc}") from exc
    fields = schema.get("fields") or []
    columns = [f["name"] for f in fields] or (list(rows[0]) if rows else [])
    types = {f["name"]: (f["type"] if isinstance(f["type"], str) else json.dumps(f["type"])[:60]) for f in fields}
    return columns, rows, types, total


def _parse_one(raw: bytes, name: str, header: bool | None, column_names: list[str], max_rows: int):
    raw, name = _maybe_gunzip(raw, name)
    fmt = _detect_format(raw, name)
    if fmt == "parquet":
        columns, rows, types, total = _read_parquet(raw, max_rows)
    elif fmt == "avro":
        columns, rows, types, total = _read_avro(raw, max_rows)
    elif fmt == "json":
        columns, rows, types = _read_json(raw)
        total = len(rows)
    else:
        columns, rows, types = _read_delimited(raw, "\t" if fmt == "tsv" else None, header, column_names)
        total = len(rows)
    return fmt, columns, rows, types, total


def parse_dump(raw: bytes, filename: str, *, header: bool | None = None, column_names: list[str] | None = None,
               max_rows: int = 100_000) -> dict:
    """Parse one export file (or a .zip of part files) into a table.
    Returns {format, columns, column_types, rows, total_rows, files, notes}."""
    if not raw:
        raise DumpError("That file is empty")
    column_names = [c.strip() for c in (column_names or []) if c and c.strip()]
    notes: list[str] = []
    name = filename or "dump"
    if raw[:4] == b"PK\x03\x04" or name.lower().endswith(".zip"):
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile as exc:
            raise DumpError(f"Couldn't open that zip: {exc}") from exc
        members = sorted(n for n in zf.namelist() if not n.endswith("/"))
        if any("_delta_log/" in n for n in members):
            notes.append(
                "A Delta _delta_log was found and ignored. Raw Delta data files can include rows removed by "
                "later table versions - for an exact snapshot, export with df.write.parquet(...) instead."
            )
        parts = [n for n in members if "_delta_log/" not in n and not _SKIP_NAMES.search(n)]
        if not parts:
            raise DumpError("The zip doesn't contain any data files")
        fmt, columns, rows, types, total = None, [], [], {}, 0
        for part in parts:
            p_fmt, p_cols, p_rows, p_types, p_total = _parse_one(
                zf.read(part), part, header, column_names, max(0, max_rows - len(rows)) or 1
            )
            fmt = fmt or p_fmt
            for c in p_cols:
                if c not in columns:
                    columns.append(c)
            types.update(p_types)
            rows.extend(p_rows[: max(0, max_rows - len(rows))])
            total += p_total
        files = len(parts)
    else:
        fmt, columns, rows, types, total = _parse_one(raw, name, header, column_names, max_rows)
        rows = rows[:max_rows]
        files = 1
    if not columns:
        raise DumpError("No columns found in that file")
    if len(columns) > MAX_COLUMNS:
        notes.append(f"Only the first {MAX_COLUMNS} of {len(columns)} columns were indexed.")
        columns = columns[:MAX_COLUMNS]
    if not rows:
        raise DumpError("No rows found in that file")
    return {"format": fmt, "columns": columns, "column_types": types, "rows": rows,
            "total_rows": max(total, len(rows)), "files": files, "notes": notes}


def _render_value(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray)):
        return f"<binary {len(v)} bytes>"
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return format(v, "f")
    if isinstance(v, (dict, list, tuple)):
        v = json.dumps(v, default=str, separators=(",", ":"))
    s = re.sub(r"\s+", " ", str(v)).strip()
    return s[:MAX_VALUE_CHARS] + ("..." if len(s) > MAX_VALUE_CHARS else "")


def chunk_table(parsed: dict, *, source: str, table_name: str, chunk_chars: int,
                max_chunks: int) -> tuple[list[str], int]:
    """Row-aware chunks. Returns (chunks, rows_indexed). The first chunk
    describes the table; each following chunk carries the context header
    and as many complete rows as fit in chunk_chars."""
    label = SOURCES.get(source, source or "Data")
    columns = parsed["columns"]
    types = parsed.get("column_types") or {}
    col_desc = ", ".join(f"{c} ({types[c]})" if c in types else c for c in columns)
    files = parsed.get("files", 1)
    schema = (
        f"{label} table export: {table_name}\n"
        f"Rows: {parsed['total_rows']:,} - format: {parsed['format']}"
        + (f" - {files} files" if files > 1 else "")
        + f"\nColumns ({len(columns)}): {col_desc}"
    )
    chunks = [schema[: max(chunk_chars, 2000)]]
    header = f"{label} table {table_name} - columns: {', '.join(columns)}\n"
    if len(header) > chunk_chars // 2:
        header = f"{label} table {table_name}\n"
    budget = max(chunk_chars - len(header), 200)
    current: list[str] = []
    size = 0
    rows_indexed = 0
    for i, row in enumerate(parsed["rows"], start=1):
        parts = []
        for c in columns:
            val = _render_value(row.get(c) if isinstance(row, dict) else None)
            if val != "":
                parts.append(f"{c}: {val}")
        line = f"Row {i}: " + "; ".join(parts)
        if len(line) > budget:
            line = line[: budget - 3] + "..."
        if current and size + len(line) + 1 > budget:
            chunks.append(header + "\n".join(current))
            current, size = [], 0
            if len(chunks) >= max_chunks:
                break
        current.append(line)
        size += len(line) + 1
        rows_indexed = i
    else:
        if current:
            chunks.append(header + "\n".join(current))
    if len(chunks) > max_chunks:
        chunks = chunks[:max_chunks]
    return chunks, rows_indexed


# ---------------------------------------------------------------------------
# Streaming reader (background ingestion of large exports)
# ---------------------------------------------------------------------------
import os as _os
import tempfile as _tempfile
from typing import Iterator


class DumpReader:
    """One export - a single file, a gzip, or a zip of part files - read
    from disk as an iterator of row dicts. `open()` establishes the format,
    columns and types from the first part (and a row-count estimate where
    the format carries one); `rows()` then streams every part in order.

    Memory stays bounded by the batch size regardless of file size: CSV
    and JSON lines are read line by line, Parquet via row-group batches,
    Avro record by record, gzip through a streaming decompressor, and zip
    members through zipfile's streaming reader (compressed members are
    inflated on the fly; gzip-inside-zip is spooled to a temp file first).
    """

    def __init__(self, path: str, filename: str, *, header: bool | None = None,
                 column_names: list[str] | None = None):
        self.path = path
        self.name = filename or _os.path.basename(path) or "dump"
        self.header = header
        self.column_names = [c.strip() for c in (column_names or []) if c and c.strip()]
        self.format: str = ""
        self.columns: list[str] = []
        self.column_types: dict[str, str] = {}
        self.total_rows: int | None = None  # known up front for Parquet/Avro only
        self.files = 1
        self.notes: list[str] = []
        self._parts: list[tuple[str, str]] = []  # (member name, "zip"|"file")
        self._zip: zipfile.ZipFile | None = None
        self._delimiter: str | None = None
        self.rows_read = 0

    # -- opening --------------------------------------------------------------
    def open(self) -> "DumpReader":
        size = _os.path.getsize(self.path)
        if size == 0:
            raise DumpError("That file is empty")
        with open(self.path, "rb") as fh:
            magic = fh.read(4)
        if magic == b"PK\x03\x04" or self.name.lower().endswith(".zip"):
            try:
                self._zip = zipfile.ZipFile(self.path)
            except zipfile.BadZipFile as exc:
                raise DumpError(f"Couldn't open that zip: {exc}") from exc
            members = sorted(n for n in self._zip.namelist() if not n.endswith("/"))
            if any("_delta_log/" in n for n in members):
                self.notes.append(
                    "A Delta _delta_log was found and ignored. Raw Delta data files can include rows removed by "
                    "later table versions - for an exact snapshot, export with df.write.parquet(...) instead."
                )
            parts = [n for n in members if "_delta_log/" not in n and not _SKIP_NAMES.search(n)]
            if not parts:
                raise DumpError("The zip doesn't contain any data files")
            self._parts = [(n, "zip") for n in parts]
            self.files = len(parts)
        else:
            self._parts = [(self.name, "file")]
        # Format, columns and types come from the first part.
        first_name, kind = self._parts[0]
        with self._open_part(first_name, kind) as (fh, name):
            head = fh.read(4096)
            fmt = _detect_format(head, name)
            self.format = fmt
            if fmt == "parquet":
                self._open_parquet_meta(first_name, kind)
            elif fmt == "avro":
                self._open_avro_meta(first_name, kind)
            elif fmt == "json":
                self._open_json_meta(head, fh)
            else:
                self._open_delimited_meta(head, fh, fmt)
        if not self.columns:
            raise DumpError("No columns found in that file")
        if len(self.columns) > MAX_COLUMNS:
            self.notes.append(f"Only the first {MAX_COLUMNS} of {len(self.columns)} columns were indexed.")
            self.columns = self.columns[:MAX_COLUMNS]
        return self

    class _PartHandle:
        def __init__(self, fh, name, cleanup=None):
            self.fh, self.name, self._cleanup = fh, name, cleanup

        def __enter__(self):
            return self.fh, self.name

        def __exit__(self, *exc):
            try:
                self.fh.close()
            finally:
                if self._cleanup:
                    self._cleanup()

    def _open_part(self, name: str, kind: str):
        """A binary, seekable-enough handle for one part, transparently
        gunzipped. Returns a context manager yielding (fh, logical name)."""
        if kind == "zip":
            raw = self._zip.open(name)  # type: ignore[union-attr]
        else:
            raw = open(self.path, "rb")
        magic = raw.read(2)
        if hasattr(raw, "seek") and kind != "zip":
            raw.seek(0)
        else:
            raw.close()
            raw = self._zip.open(name)  # type: ignore[union-attr]
        if magic == b"\x1f\x8b":
            logical = re.sub(r"\.gz$", "", name, flags=re.IGNORECASE)
            if kind == "zip":
                # zipfile's member stream is not seekable; spool the
                # gunzipped bytes so Parquet/Avro readers can seek.
                tmp = _tempfile.NamedTemporaryFile(delete=False, dir=_os.path.dirname(self.path))
                with gzip.GzipFile(fileobj=raw) as gz:
                    while True:
                        block = gz.read(1 << 20)
                        if not block:
                            break
                        tmp.write(block)
                raw.close()
                tmp.close()
                fh = open(tmp.name, "rb")
                return self._PartHandle(fh, logical, cleanup=lambda: _os.unlink(tmp.name))
            return self._PartHandle(gzip.GzipFile(fileobj=raw), logical, cleanup=raw.close)
        if magic == b"\x28\xb5":
            raise DumpError(f"'{name}' is zstd-compressed - re-export with gzip or no compression")
        return self._PartHandle(raw, name)

    def _open_delimited_meta(self, head: bytes, fh, fmt: str):
        sample = _decode(head + fh.read(20000))
        if fmt == "tsv":
            self._delimiter = "\t"
        else:
            try:
                self._delimiter = csv.Sniffer().sniff(sample, delimiters=",\t|;").delimiter
            except csv.Error:
                self._delimiter = ","
        header = self.header
        if header is None and not self.column_names:
            try:
                header = csv.Sniffer().has_header(sample)
            except csv.Error:
                header = True
        self.header = bool(header)
        reader = csv.reader(io.StringIO(sample), delimiter=self._delimiter)
        first = next(reader, [])
        if self.column_names:
            self.columns = list(self.column_names)
        elif self.header:
            self.columns = [c.strip() for c in first]
        else:
            self.columns = [f"column_{i + 1}" for i in range(len(first))]

    def _open_json_meta(self, head: bytes, fh):
        text = _decode(head).lstrip()
        if text.startswith("["):
            # A single top-level array cannot be streamed line by line; only
            # a small one is accepted (json.load of the whole part).
            raise DumpError(
                "This JSON file is one top-level array. Export newline-delimited JSON (one object per line) "
                "for large tables - BigQuery: NEWLINE_DELIMITED_JSON; Snowflake: FILE_FORMAT TYPE=JSON; "
                "Databricks: df.write.json(...)."
            )
        sample = head + fh.read(1 << 20)
        cols: list[str] = []
        for line in _decode(sample).splitlines()[:2000]:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # the last line of the sample is usually cut
            if isinstance(rec, dict):
                for k in rec:
                    if k not in cols:
                        cols.append(k)
            elif "value" not in cols:
                cols.append("value")
        self.columns = cols

    def _open_parquet_meta(self, name: str, kind: str):
        try:
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise DumpError("Parquet support needs 'pyarrow', which isn't installed in this image") from exc
        total = 0
        for pname, pkind in self._parts:
            with self._open_part(pname, pkind) as (fh, _):
                try:
                    pf = pq.ParquetFile(fh)
                except Exception as exc:  # noqa: BLE001
                    raise DumpError(f"Couldn't read Parquet file '{pname}': {exc}") from exc
                if not self.columns:
                    self.columns = list(pf.schema_arrow.names)
                    self.column_types = {f.name: str(f.type) for f in pf.schema_arrow}
                total += pf.metadata.num_rows
        self.total_rows = total

    def _open_avro_meta(self, name: str, kind: str):
        try:
            import fastavro
        except ImportError as exc:
            raise DumpError("Avro support needs 'fastavro', which isn't installed in this image") from exc
        with self._open_part(name, kind) as (fh, _):
            try:
                reader = fastavro.reader(fh)
            except Exception as exc:  # noqa: BLE001
                raise DumpError(f"Couldn't read that Avro file: {exc}") from exc
            schema = reader.writer_schema or {}
        fields = schema.get("fields") or []
        self.columns = [f["name"] for f in fields]
        self.column_types = {
            f["name"]: (f["type"] if isinstance(f["type"], str) else json.dumps(f["type"])[:60]) for f in fields
        }

    # -- rows ----------------------------------------------------------------
    def rows(self) -> Iterator[dict]:
        for pname, pkind in self._parts:
            with self._open_part(pname, pkind) as (fh, logical):
                fmt = self.format
                if fmt == "parquet":
                    import pyarrow.parquet as pq
                    pf = pq.ParquetFile(fh)
                    for batch in pf.iter_batches(batch_size=4096, columns=[c for c in self.columns if c in pf.schema_arrow.names] or None):
                        for row in batch.to_pylist():
                            self.rows_read += 1
                            yield row
                elif fmt == "avro":
                    import fastavro
                    for rec in fastavro.reader(fh):
                        self.rows_read += 1
                        yield rec if isinstance(rec, dict) else {"value": rec}
                elif fmt == "json":
                    text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace")
                    for lineno, line in enumerate(text, start=1):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except ValueError as exc:
                            raise DumpError(f"{logical} line {lineno} isn't valid JSON - expected one object per line: {exc}") from exc
                        self.rows_read += 1
                        yield rec if isinstance(rec, dict) else {"value": rec}
                else:
                    text = io.TextIOWrapper(fh, encoding="utf-8-sig", errors="replace", newline="")
                    reader = csv.reader(text, delimiter=self._delimiter or ",")
                    if self.header and not self.column_names:
                        next(reader, None)
                    elif self.header and self.column_names:
                        next(reader, None)
                    for row in reader:
                        if not row or (len(row) == 1 and not row[0].strip()):
                            continue
                        self.rows_read += 1
                        yield dict(zip(self.columns, row))

    def close(self):
        if self._zip is not None:
            self._zip.close()


def open_dump(path: str, filename: str, *, header: bool | None = None, column_names: list[str] | None = None) -> DumpReader:
    return DumpReader(path, filename, header=header, column_names=column_names).open()


def render_row(columns: list[str], row: dict, index: int, budget: int) -> str:
    parts = []
    for c in columns:
        val = _render_value(row.get(c) if isinstance(row, dict) else None)
        if val != "":
            parts.append(f"{c}: {val}")
    line = f"Row {index}: " + "; ".join(parts)
    if len(line) > budget:
        line = line[: budget - 3] + "..."
    return line


def table_header(source: str, table_name: str, columns: list[str], chunk_chars: int) -> str:
    label = SOURCES.get(source, source or "Data")
    header = f"{label} table {table_name} - columns: {', '.join(columns)}\n"
    if len(header) > chunk_chars // 2:
        header = f"{label} table {table_name}\n"
    return header


def schema_chunk(reader: DumpReader, *, source: str, table_name: str, chunk_chars: int, total_rows: int) -> str:
    label = SOURCES.get(source, source or "Data")
    types = reader.column_types or {}
    col_desc = ", ".join(f"{c} ({types[c]})" if c in types else c for c in reader.columns)
    text = (
        f"{label} table export: {table_name}\n"
        f"Rows: {total_rows:,} - format: {reader.format}"
        + (f" - {reader.files} files" if reader.files > 1 else "")
        + f"\nColumns ({len(reader.columns)}): {col_desc}"
    )
    return text[: max(chunk_chars, 2000)]


def chunk_table_stream(reader: DumpReader, *, source: str, table_name: str, chunk_chars: int) -> Iterator[str]:
    """Row-aware chunks from a DumpReader, one at a time. The schema chunk
    is NOT included (the row count isn't known until the end for CSV/JSON)
    - the caller adds schema_chunk() once rows_read is final."""
    header = table_header(source, table_name, reader.columns, chunk_chars)
    budget = max(chunk_chars - len(header), 200)
    current: list[str] = []
    size = 0
    for i, row in enumerate(reader.rows(), start=1):
        line = render_row(reader.columns, row, i, budget)
        if current and size + len(line) + 1 > budget:
            yield header + "\n".join(current)
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        yield header + "\n".join(current)
