"""Application-log forwarding for the operations manager - the same
capability the Couchbase Relational Database Service's Admin Console has
under Settings -> Log forwarding, ported to this appliance.

Not to be confused with app/siem_forwarding.py, which ships *audit log
entries* (every discover/invoke decision) to a SIEM one event at a time.
This module ships the service's own Python `logging` output - startup,
ingest, cache sweeps, errors, warnings - to a log platform, batched.

Destinations (one or more, each with a "Send test" button):

  Splunk      HTTP Event Collector, gzip batches
  Elastic     Bulk API into a logs-* data stream (ECS fields), gzip batches
  Datadog     Logs intake API v2, gzip batches, <= 1000 per request
  Sumo Logic  HTTP Logs & Metrics Source, gzip batches (the URL is the secret)
  Graylog     GELF 1.1 over TCP, TLS or UDP (gzip + chunking over UDP)
  Syslog      RFC 5424 over UDP, TCP or TLS (RFC 5425), octet-counting or
              newline framing

Why it never slows the appliance down (same design as CRDS):

  - The logging handler does one int compare for the level, copies the
    record into a bounded queue with a non-blocking put, and returns. No
    I/O, no lock beyond the queue's, no unbounded memory.
  - One daemon sender thread drains the queue into batches (batch size or
    flush interval, whichever first) and sends one request per batch per
    destination over kept-alive HTTP sessions / one open socket.
  - A destination that fails backs off exponentially (up to 60 s) and its
    batches are dropped and counted meanwhile; a 4xx is not retried. Other
    destinations are unaffected.
  - When the queue is full the record is dropped and counted
    (aom_log_forwarding_records_total{result="dropped_queue_full"}) - never
    blocking the thread that logged.

Config lives at settings::log_forwarding (config.LOG_FORWARDING_SETTINGS_DOC).
Secret fields are Fernet-encrypted at rest with user_auth.encrypt_secret and
never returned to the browser: they read as "********" and sending that
back unchanged keeps the stored value.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import queue
import socket
import ssl
import struct
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone

import requests

from app import user_auth

logger = logging.getLogger("operations-manager.log_forwarding")

MASK = "********"
LEVELS = ("TRACE", "DEBUG", "INFO", "WARN", "ERROR")
_LEVEL_NUM = {"TRACE": 5, "DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}
_SYSLOG_SEVERITY = {"ERROR": 3, "WARN": 4, "INFO": 6, "DEBUG": 7, "TRACE": 7}
_FACILITIES = {"user": 1, "daemon": 3, **{f"local{i}": 16 + i for i in range(8)}}
SERVICE = "operations-manager"
NODE = os.getenv("HOSTNAME") or socket.gethostname()

_HTTP_TIMEOUT = 10
_MAX_BACKOFF_SECONDS = 60
_UDP_CHUNK = 8192
_UDP_MAX_BYTES = 65000

DATADOG_SITES = ["datadoghq.com", "us3.datadoghq.com", "us5.datadoghq.com", "datadoghq.eu",
                 "ap1.datadoghq.com", "ap2.datadoghq.com", "ddog-gov.com"]


# ---------------------------------------------------------------------------
# Vendor catalog - the console renders its forms from this
# ---------------------------------------------------------------------------
def _f(name, label, input_, default="", required=False, placeholder="", hint=None, options=None):
    return {"name": name, "label": label, "input": input_, "default": default, "required": required,
            "placeholder": placeholder, "hint": hint, "options": options or []}


def _verify_tls():
    return _f("verifyTls", "Verify the server's TLS certificate", "bool", "true", False, "",
              "Turn off only for a test server with a self-signed certificate.")


VENDORS: list[dict] = [
    {
        "id": "splunk", "label": "Splunk", "product": "Splunk Enterprise / Splunk Cloud Platform", "openSource": False,
        "transport": "HTTP Event Collector, gzip-batched",
        "description": "Batches of events to the HTTP Event Collector (HEC) over HTTPS.",
        "fields": [
            _f("url", "HEC URL", "text", "", True, "https://splunk.example.com:8088",
               "Base URL of the HTTP Event Collector; /services/collector/event is added."),
            _f("token", "HEC token", "secret", "", True, "", "Settings > Data inputs > HTTP Event Collector in Splunk."),
            _f("index", "Index", "text", "", False, "main", "Empty = the token's default index."),
            _f("sourcetype", "Source type", "text", "aom:log", False, "aom:log"),
            _verify_tls(),
        ],
    },
    {
        "id": "elastic", "label": "Elastic", "product": "Elasticsearch / Elastic Cloud (ELK)", "openSource": False,
        "transport": "Bulk API into a data stream, gzip-batched",
        "description": "Bulk-indexes ECS-shaped documents into a logs data stream.",
        "fields": [
            _f("url", "Elasticsearch URL", "text", "", True, "https://es.example.com:9200"),
            _f("apiKey", "API key", "secret", "", False, "", "Base64 API key (id:key encoded). Or use user name and password."),
            _f("username", "User name", "text", "", False, "elastic", "Basic authentication when no API key is set."),
            _f("password", "Password", "secret", "", False),
            _f("dataStream", "Data stream", "text", "logs-aom-default", False, "logs-aom-default",
               "logs-<dataset>-<namespace> picks up the built-in logs index template."),
            _verify_tls(),
        ],
    },
    {
        "id": "datadog", "label": "Datadog", "product": "Datadog Log Management", "openSource": False,
        "transport": "Logs intake API v2, gzip-batched",
        "description": "Sends batches to Datadog's HTTP logs intake for your site.",
        "fields": [
            _f("site", "Datadog site", "select", "datadoghq.com", True, "", "The site in your Datadog URL.", DATADOG_SITES),
            _f("apiKey", "API key", "secret", "", True, "", "Organization settings > API keys."),
            _f("tags", "Tags", "text", "", False, "env:prod,team:platform", "Comma-separated, added to every log."),
        ],
    },
    {
        "id": "sumologic", "label": "Sumo Logic", "product": "Sumo Logic", "openSource": False,
        "transport": "HTTP Logs & Metrics Source, gzip-batched",
        "description": "Posts newline-delimited JSON to a hosted collector's HTTP Source.",
        "fields": [
            _f("url", "HTTP Source URL", "secret", "", True, "", "The URL is the credential, so it is stored as a secret."),
            _f("category", "Source category", "text", "aom/logs", False, "aom/logs", "Sent as X-Sumo-Category."),
        ],
    },
    {
        "id": "graylog", "label": "Graylog", "product": "Graylog Open", "openSource": True,
        "transport": "GELF over TCP, TLS or UDP",
        "description": "The leading open-source log management platform. Sends GELF to a GELF TCP or UDP input.",
        "fields": [
            _f("host", "Host", "text", "", True, "graylog.example.com"),
            _f("port", "Port", "number", "12201", True, "12201", "The GELF input's port."),
            _f("protocol", "Protocol", "select", "tcp", True, "",
               "TCP keeps one connection open; UDP is fire-and-forget (chunked above 8 KB).", ["tcp", "tls", "udp"]),
            _verify_tls(),
        ],
    },
    {
        "id": "syslog", "label": "Syslog", "product": "Any RFC 5424 syslog server (rsyslog, syslog-ng)", "openSource": True,
        "transport": "RFC 5424 over UDP, TCP or TLS (RFC 5425)",
        "description": "Standard syslog messages for rsyslog, syslog-ng or any SIEM with a syslog listener.",
        "fields": [
            _f("host", "Host", "text", "", True, "syslog.example.com"),
            _f("port", "Port", "number", "514", True, "514", "514 for UDP/TCP, 6514 for TLS."),
            _f("protocol", "Protocol", "select", "udp", True, "", None, ["udp", "tcp", "tls"]),
            _f("facility", "Facility", "select", "local0", True, "", None, list(_FACILITIES)),
            _f("framing", "TCP framing", "select", "octet-counting", True, "",
               "RFC 6587 octet counting is the default for rsyslog and syslog-ng.", ["octet-counting", "newline"]),
            _verify_tls(),
        ],
    },
]
VENDOR_BY_ID = {v["id"]: v for v in VENDORS}


def _fields(vendor_id: str) -> list[dict]:
    return VENDOR_BY_ID.get(vendor_id, {}).get("fields", [])


def _secret_names(vendor_id: str) -> set[str]:
    return {f["name"] for f in _fields(vendor_id) if f["input"] == "secret"}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "enabled": False,
    "minLevel": "INFO",
    "batchSize": 500,
    "flushIntervalMs": 2000,
    "queueCapacity": 20000,
    "includeStackTraces": True,
    "maxMessageChars": 8000,
    "destinations": {},
}


def _clamp(v, lo, hi, default):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def normalize_config(raw: dict | None, *, previous: dict | None = None) -> dict:
    """Validate a config as stored or as submitted by the console. Secret
    fields: a plaintext value is encrypted; MASK (or empty) keeps whatever
    `previous` held; anything stored is kept under "<name>_encrypted"."""
    raw = raw or {}
    prev = previous or {}
    level = str(raw.get("minLevel") or "INFO").upper()
    cfg = {
        "enabled": bool(raw.get("enabled", False)),
        "minLevel": level if level in LEVELS else "INFO",
        "batchSize": _clamp(raw.get("batchSize"), 1, 5000, 500),
        "flushIntervalMs": _clamp(raw.get("flushIntervalMs"), 200, 60000, 2000),
        "queueCapacity": _clamp(raw.get("queueCapacity"), 100, 1_000_000, 20000),
        "includeStackTraces": bool(raw.get("includeStackTraces", True)),
        "maxMessageChars": _clamp(raw.get("maxMessageChars"), 256, 100_000, 8000),
        "destinations": {},
    }
    prev_dests = prev.get("destinations") or {}
    for vid, vendor in VENDOR_BY_ID.items():
        d = (raw.get("destinations") or {}).get(vid) or {}
        pd = prev_dests.get(vid) or {}
        fields_in = dict(d.get("fields") or {})
        out_fields: dict = {}
        for f in vendor["fields"]:
            name = f["name"]
            val = fields_in.get(name)
            if f["input"] == "secret":
                enc_key = f"{name}_encrypted"
                if val is None or val == "" or val == MASK:
                    if pd.get("fields", {}).get(enc_key):
                        out_fields[enc_key] = pd["fields"][enc_key]
                else:
                    out_fields[enc_key] = user_auth.encrypt_secret(str(val))
                continue
            if val is None or val == "":
                val = f["default"]
            if f["input"] == "bool":
                val = "true" if str(val).lower() in ("true", "1", "yes", "on") else "false"
            elif f["input"] == "number":
                val = str(_clamp(val, 1, 65535, int(f["default"] or 0)))
            elif f["input"] == "select" and f["options"] and str(val) not in f["options"]:
                val = f["default"]
            out_fields[name] = str(val)[:1000]
        cfg["destinations"][vid] = {"type": vid, "enabled": bool(d.get("enabled", False)), "fields": out_fields}
    return cfg


def public_config(cfg: dict) -> dict:
    """What the console sees: secrets masked (or empty when none stored)."""
    out = {k: v for k, v in cfg.items() if k != "destinations"}
    out["destinations"] = {}
    for vid, d in (cfg.get("destinations") or {}).items():
        fields = {}
        for f in _fields(vid):
            if f["input"] == "secret":
                fields[f["name"]] = MASK if d.get("fields", {}).get(f"{f['name']}_encrypted") else ""
            else:
                fields[f["name"]] = d.get("fields", {}).get(f["name"], f["default"])
        out["destinations"][vid] = {"type": vid, "enabled": bool(d.get("enabled")), "fields": fields}
    return out


def missing_required(vid: str, d: dict) -> list[str]:
    out = []
    fields = d.get("fields") or {}
    for f in _fields(vid):
        if not f["required"]:
            continue
        if f["input"] == "secret":
            if not fields.get(f"{f['name']}_encrypted"):
                out.append(f["label"])
        elif not str(fields.get(f["name"]) or f["default"] or "").strip():
            out.append(f["label"])
    return out


class _Dest:
    """A destination's fields with secrets decrypted, for the sinks."""

    def __init__(self, vid: str, d: dict):
        self.type = vid
        self.raw = d.get("fields") or {}

    def get(self, name: str) -> str:
        if name in _secret_names(self.type):
            enc = self.raw.get(f"{name}_encrypted")
            if not enc:
                return ""
            try:
                return user_auth.decrypt_secret(enc)
            except Exception:  # noqa: BLE001
                return ""
        return str(self.raw.get(name) or "")

    def bool(self, name: str) -> bool:
        return str(self.raw.get(name, "true")).lower() == "true"

    def int(self, name: str, default: int = 0) -> int:
        try:
            return int(self.raw.get(name) or default)
        except (TypeError, ValueError):
            return default


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
def _record_from_logging(rec: logging.LogRecord, include_stack: bool, max_chars: int) -> dict:
    level = rec.levelname
    if level == "WARNING":
        level = "WARN"
    elif level == "CRITICAL":
        level = "ERROR"
    elif level not in _LEVEL_NUM:
        level = "INFO"
    try:
        msg = rec.getMessage()
    except Exception:  # noqa: BLE001
        msg = str(rec.msg)
    stack = None
    if rec.exc_info:
        if include_stack:
            stack = "".join(traceback.format_exception(*rec.exc_info))[:max_chars]
        else:
            etype = rec.exc_info[0].__name__ if rec.exc_info[0] else "Exception"
            stack = f"{etype}: {rec.exc_info[1]}"[:max_chars]
    return {
        "ts": rec.created, "level": level, "logger": rec.name, "thread": rec.threadName,
        "message": msg[:max_chars], "stack": stack,
    }


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int((ts % 1) * 1000):03d}Z"


def synthetic_record(level: str = "INFO") -> dict:
    return {
        "ts": time.time(), "level": level, "logger": "operations-manager.log_forwarding", "thread": "test",
        "message": f"Test message from the Couchbase Agent Operations Manager log forwarding settings ({uuid.uuid4().hex[:8]})",
        "stack": None,
    }


# ---------------------------------------------------------------------------
# Sinks
# ---------------------------------------------------------------------------
def _post(session: requests.Session, url: str, headers: dict, body: bytes, gz: bool, verify: bool):
    h = dict(headers)
    if gz:
        body = gzip.compress(body, compresslevel=1)
        h["Content-Encoding"] = "gzip"
    resp = session.post(url, data=body, headers=h, timeout=_HTTP_TIMEOUT, verify=verify)
    if resp.status_code >= 400:
        snippet = (resp.text or "")[:200].replace("\n", " ")
        raise _HttpError(resp.status_code, f"HTTP {resp.status_code}{': ' + snippet if snippet else ''}")
    return resp


class _HttpError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class _Sink:
    id = ""
    retryable_4xx = False

    def send(self, batch: list[dict]) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:
        pass


class _Splunk(_Sink):
    id = "splunk"

    def __init__(self, d: _Dest):
        base = d.get("url").rstrip("/")
        self.url = base if base.endswith("/services/collector/event") else base + "/services/collector/event"
        self.headers = {"Authorization": "Splunk " + d.get("token"), "Content-Type": "application/json"}
        self.index, self.sourcetype, self.verify = d.get("index"), d.get("sourcetype") or "aom:log", d.bool("verifyTls")
        self.session = requests.Session()

    def send(self, batch):
        parts = []
        for r in batch:
            ev = {"level": r["level"], "logger": r["logger"], "thread": r["thread"], "message": r["message"]}
            if r.get("stack"):
                ev["stack_trace"] = r["stack"]
            doc = {"time": round(r["ts"], 3), "host": NODE, "source": SERVICE, "sourcetype": self.sourcetype, "event": ev}
            if self.index:
                doc["index"] = self.index
            parts.append(json.dumps(doc, separators=(",", ":")))
        _post(self.session, self.url, self.headers, "\n".join(parts).encode(), True, self.verify)


class _Elastic(_Sink):
    id = "elastic"

    def __init__(self, d: _Dest):
        self.stream = d.get("dataStream") or "logs-aom-default"
        self.url = d.get("url").rstrip("/") + f"/{self.stream}/_bulk"
        self.headers = {"Content-Type": "application/x-ndjson"}
        if d.get("apiKey"):
            self.headers["Authorization"] = "ApiKey " + d.get("apiKey")
        elif d.get("username"):
            import base64
            self.headers["Authorization"] = "Basic " + base64.b64encode(f"{d.get('username')}:{d.get('password')}".encode()).decode()
        self.verify = d.bool("verifyTls")
        self.session = requests.Session()

    def send(self, batch):
        lines = []
        for r in batch:
            doc = {
                "@timestamp": _iso(r["ts"]), "message": r["message"],
                "log": {"level": r["level"].lower(), "logger": r["logger"]},
                "process": {"thread": {"name": r["thread"]}},
                "service": {"name": SERVICE}, "host": {"name": NODE},
                "event": {"dataset": "aom.log"},
            }
            if r.get("stack"):
                doc["error"] = {"stack_trace": r["stack"]}
            lines.append('{"create":{}}')
            lines.append(json.dumps(doc, separators=(",", ":")))
        resp = _post(self.session, self.url, self.headers, ("\n".join(lines) + "\n").encode(), True, self.verify)
        try:
            body = resp.json()
        except ValueError:
            return
        if body.get("errors"):
            reason = ""
            for item in body.get("items") or []:
                err = (item.get("create") or item.get("index") or {}).get("error")
                if err:
                    reason = err.get("reason") or err.get("type") or ""
                    break
            raise RuntimeError("Elasticsearch rejected documents in the batch" + (f": {reason}" if reason else ""))


class _Datadog(_Sink):
    id = "datadog"
    MAX_PER_REQUEST = 1000

    def __init__(self, d: _Dest):
        self.url = f"https://http-intake.logs.{d.get('site') or 'datadoghq.com'}/api/v2/logs"
        self.headers = {"DD-API-KEY": d.get("apiKey"), "Content-Type": "application/json"}
        self.tags = d.get("tags")
        self.session = requests.Session()

    def send(self, batch):
        for i in range(0, len(batch), self.MAX_PER_REQUEST):
            part = []
            for r in batch[i:i + self.MAX_PER_REQUEST]:
                doc = {
                    "ddsource": "aom", "ddtags": self.tags or f"service:{SERVICE}", "hostname": NODE, "service": SERVICE,
                    "status": r["level"].lower(), "timestamp": _iso(r["ts"]), "logger": {"name": r["logger"], "thread_name": r["thread"]},
                    "message": r["message"],
                }
                if r.get("stack"):
                    doc["error"] = {"stack": r["stack"]}
                part.append(doc)
            _post(self.session, self.url, self.headers, json.dumps(part, separators=(",", ":")).encode(), True, True)


class _Sumo(_Sink):
    id = "sumologic"

    def __init__(self, d: _Dest):
        self.url = d.get("url")
        self.headers = {"Content-Type": "application/json", "X-Sumo-Name": SERVICE, "X-Sumo-Host": NODE}
        if d.get("category"):
            self.headers["X-Sumo-Category"] = d.get("category")
        self.session = requests.Session()

    def send(self, batch):
        lines = []
        for r in batch:
            doc = {"timestamp": _iso(r["ts"]), "level": r["level"], "service": SERVICE, "host": NODE,
                   "logger": r["logger"], "thread": r["thread"], "message": r["message"]}
            if r.get("stack"):
                doc["stack_trace"] = r["stack"]
            lines.append(json.dumps(doc, separators=(",", ":")))
        _post(self.session, self.url, self.headers, ("\n".join(lines) + "\n").encode(), True, True)


class _SocketTransport:
    """One open TCP/TLS socket reused across batches, or a UDP socket."""

    def __init__(self, host: str, port: int, protocol: str, verify: bool):
        self.host, self.port, self.protocol, self.verify = host, port, protocol, verify
        self.sock: socket.socket | None = None

    def _open(self):
        if self.protocol == "udp":
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            return
        s = socket.create_connection((self.host, self.port), timeout=_HTTP_TIMEOUT)
        if self.protocol == "tls":
            ctx = ssl.create_default_context()
            if not self.verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            s = ctx.wrap_socket(s, server_hostname=self.host)
        s.settimeout(_HTTP_TIMEOUT)
        self.sock = s

    def stream(self, data: bytes):
        if self.sock is None:
            self._open()
        try:
            self.sock.sendall(data)
        except OSError:
            self.close()
            self._open()
            self.sock.sendall(data)

    def datagram(self, data: bytes):
        if self.sock is None:
            self._open()
        self.sock.sendto(data, (self.host, self.port))

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


class _Graylog(_Sink):
    id = "graylog"

    def __init__(self, d: _Dest):
        self.t = _SocketTransport(d.get("host"), d.int("port", 12201), d.get("protocol") or "tcp", d.bool("verifyTls"))

    def _gelf(self, r: dict) -> bytes:
        short = (r["message"].split("\n", 1)[0] or "-")[:250]
        doc = {
            "version": "1.1", "host": NODE, "short_message": short, "timestamp": round(r["ts"], 3),
            "level": _SYSLOG_SEVERITY.get(r["level"], 6), "_service": SERVICE, "_logger": r["logger"],
            "_thread": r["thread"], "_level_name": r["level"],
        }
        if r["message"] != short or r.get("stack"):
            doc["full_message"] = r["message"] + (("\n" + r["stack"]) if r.get("stack") else "")
        return json.dumps(doc, separators=(",", ":")).encode()

    def send(self, batch):
        if self.t.protocol == "udp":
            for r in batch:
                gz = gzip.compress(self._gelf(r), compresslevel=1)
                if len(gz) <= _UDP_CHUNK:
                    self.t.datagram(gz)
                else:
                    self._chunked(gz)
        else:
            self.t.stream(b"".join(self._gelf(r) + b"\0" for r in batch))

    def _chunked(self, data: bytes):
        count = (len(data) + _UDP_CHUNK - 1) // _UDP_CHUNK
        if count > 128:
            raise RuntimeError(f"GELF message too large for UDP ({len(data)} bytes compressed); use TCP")
        mid = os.urandom(8)
        for i in range(count):
            part = data[i * _UDP_CHUNK:(i + 1) * _UDP_CHUNK]
            self.t.datagram(b"\x1e\x0f" + mid + struct.pack("BB", i, count) + part)

    def close(self):
        self.t.close()


class _Syslog(_Sink):
    id = "syslog"

    def __init__(self, d: _Dest):
        self.t = _SocketTransport(d.get("host"), d.int("port", 514), d.get("protocol") or "udp", d.bool("verifyTls"))
        self.facility = _FACILITIES.get(d.get("facility") or "local0", 16)
        self.framing = d.get("framing") or "octet-counting"

    @staticmethod
    def _ascii(s: str, max_len: int) -> str:
        s = "".join(c if 32 < ord(c) < 127 else "_" for c in (s or ""))[:max_len]
        return s or "-"

    def _msg(self, r: dict) -> bytes:
        pri = self.facility * 8 + _SYSLOG_SEVERITY.get(r["level"], 6)
        sd = f'[aom@55555 service="{SERVICE}" logger="{self._ascii(r["logger"], 64)}" thread="{self._ascii(r["thread"], 32)}" level="{r["level"]}"]'
        body = r["message"] + (("\n" + r["stack"]) if r.get("stack") else "")
        line = f"<{pri}>1 {_iso(r['ts'])} {self._ascii(NODE, 255)} {self._ascii(SERVICE, 48)} - - {sd} ﻿{body}"
        return line.encode("utf-8")

    def send(self, batch):
        if self.t.protocol == "udp":
            for r in batch:
                m = self._msg(r)
                self.t.datagram(m if len(m) <= _UDP_MAX_BYTES else m[:_UDP_MAX_BYTES])
        else:
            out = []
            for r in batch:
                m = self._msg(r)
                out.append(f"{len(m)} ".encode() + m if self.framing == "octet-counting" else m.replace(b"\n", b" ") + b"\n")
            self.t.stream(b"".join(out))

    def close(self):
        self.t.close()


_SINKS = {"splunk": _Splunk, "elastic": _Elastic, "datadog": _Datadog, "sumologic": _Sumo, "graylog": _Graylog, "syslog": _Syslog}


def build_sink(vid: str, d: dict) -> _Sink:
    return _SINKS[vid](_Dest(vid, d))


# ---------------------------------------------------------------------------
# The forwarder: handler + queue + sender thread
# ---------------------------------------------------------------------------
class _DestState:
    __slots__ = ("sink", "sent", "failed", "last_ok_at", "last_error", "backoff_until", "backoff")

    def __init__(self, sink):
        self.sink = sink
        self.sent = 0
        self.failed = 0
        self.last_ok_at: str | None = None
        self.last_error: str | None = None
        self.backoff_until = 0.0
        self.backoff = 0.0


class LogForwarder(logging.Handler):
    """Install once on the root logger; reconfigure with apply()."""

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self.cfg = dict(DEFAULT_CONFIG)
        self.queue: queue.Queue = queue.Queue(maxsize=self.cfg["queueCapacity"])
        self.received = 0
        self.dropped_queue_full = 0
        self.dests: dict[str, _DestState] = {}
        self._min_level = _LEVEL_NUM["INFO"]
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._stop = False
        self._active = False

    # -- logging.Handler ----------------------------------------------------
    def emit(self, record: logging.LogRecord) -> None:
        if not self._active or record.levelno < self._min_level or record.name.startswith("operations-manager.log_forwarding"):
            return
        self.received += 1
        try:
            self.queue.put_nowait(_record_from_logging(record, self.cfg["includeStackTraces"], self.cfg["maxMessageChars"]))
        except queue.Full:
            self.dropped_queue_full += 1
        if self.queue.qsize() >= self.cfg["batchSize"]:
            self._wake.set()

    # -- configuration --------------------------------------------------------
    def apply(self, cfg: dict) -> None:
        cfg = normalize_config(cfg, previous=cfg)
        with self._lock:
            old = self.dests
            new: dict[str, _DestState] = {}
            if cfg["enabled"]:
                for vid, d in cfg["destinations"].items():
                    if not d.get("enabled") or missing_required(vid, d):
                        continue
                    prev = old.get(vid)
                    # Rebuild the sink when its fields changed; keep counters.
                    if prev and prev.sink is not None and getattr(prev.sink, "_fields", None) == d.get("fields"):
                        new[vid] = prev
                        continue
                    try:
                        sink = build_sink(vid, d)
                        sink._fields = dict(d.get("fields") or {})  # noqa: SLF001
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("log forwarding: could not build %s sink: %s", vid, exc)
                        continue
                    st = _DestState(sink)
                    if prev:
                        st.sent, st.failed, st.last_ok_at = prev.sent, prev.failed, prev.last_ok_at
                    new[vid] = st
            for vid, st in old.items():
                if vid not in new and st.sink is not None:
                    try:
                        st.sink.close()
                    except Exception:  # noqa: BLE001
                        pass
            self.dests = new
            if cfg["queueCapacity"] != self.queue.maxsize:
                fresh: queue.Queue = queue.Queue(maxsize=cfg["queueCapacity"])
                while True:
                    try:
                        fresh.put_nowait(self.queue.get_nowait())
                    except (queue.Empty, queue.Full):
                        break
                self.queue = fresh
            self.cfg = cfg
            self._min_level = _LEVEL_NUM[cfg["minLevel"]]
            self._active = bool(cfg["enabled"] and new)
        if self._active and (self._thread is None or not self._thread.is_alive()):
            self._stop = False
            self._thread = threading.Thread(target=self._run, name="aom-log-forwarder", daemon=True)
            self._thread.start()
        self._wake.set()

    # -- sender thread ---------------------------------------------------------
    def _run(self) -> None:
        while not self._stop:
            self._wake.wait(timeout=self.cfg["flushIntervalMs"] / 1000.0)
            self._wake.clear()
            if not self._active:
                if self.queue.empty():
                    return
            batch: list[dict] = []
            try:
                while len(batch) < self.cfg["batchSize"]:
                    batch.append(self.queue.get_nowait())
            except queue.Empty:
                pass
            if not batch:
                continue
            with self._lock:
                targets = list(self.dests.items())
            now = time.time()
            for vid, st in targets:
                if st.backoff_until > now:
                    st.failed += len(batch)
                    continue
                try:
                    st.sink.send(batch)
                    st.sent += len(batch)
                    st.last_ok_at = _iso(time.time())
                    st.last_error = None
                    st.backoff = 0.0
                    _metric(vid, "sent", len(batch))
                except Exception as exc:  # noqa: BLE001
                    st.failed += len(batch)
                    st.last_error = f"{exc}"[:300]
                    _metric(vid, "failed", len(batch))
                    retry_4xx = isinstance(exc, _HttpError) and 400 <= exc.status < 500 and exc.status not in (408, 429)
                    st.backoff = min(_MAX_BACKOFF_SECONDS, (st.backoff * 2) if st.backoff else 1.0)
                    if retry_4xx:
                        st.backoff = _MAX_BACKOFF_SECONDS
                    st.backoff_until = time.time() + st.backoff
                    try:
                        st.sink.close()
                    except Exception:  # noqa: BLE001
                        pass

    # -- status ----------------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            dests = {
                vid: {
                    "sent": st.sent, "failed": st.failed, "lastOkAt": st.last_ok_at, "lastError": st.last_error,
                    "backingOff": st.backoff_until > time.time(),
                }
                for vid, st in self.dests.items()
            }
        return {
            "active": self._active, "received": self.received, "queued": self.queue.qsize(),
            "droppedQueueFull": self.dropped_queue_full, "destinations": dests,
        }


def _metric(vid: str, result: str, n: int) -> None:
    try:
        from app import metrics
        metrics.record_log_forward(vid, result, n)
    except Exception:  # noqa: BLE001
        pass


forwarder = LogForwarder()
_installed = False


def install() -> None:
    global _installed
    if not _installed:
        logging.getLogger().addHandler(forwarder)
        _installed = True


def service_status() -> dict:
    """Shaped like CRDS's per-service status table, with this one service."""
    return {
        f"{SERVICE}@{NODE}": {
            "service": SERVICE, "node": NODE, "at": _iso(time.time()), "status": forwarder.status(),
        }
    }


def test_destination(vid: str, d: dict) -> dict:
    """Deliver one synthetic record using the destination as given (saved
    or not). Returns {success, detail}."""
    if vid not in VENDOR_BY_ID:
        return {"success": False, "detail": f"unknown destination '{vid}'"}
    missing = missing_required(vid, d)
    if missing:
        return {"success": False, "detail": "missing: " + ", ".join(missing)}
    started = time.time()
    try:
        sink = build_sink(vid, d)
        try:
            sink.send([synthetic_record()])
        finally:
            sink.close()
    except _HttpError as exc:
        hint = ""
        if exc.status in (401, 403):
            hint = " - the token/API key was rejected"
        elif exc.status == 404:
            hint = " - wrong URL path"
        return {"success": False, "detail": f"{exc}{hint}"}
    except (socket.gaierror,) as exc:
        return {"success": False, "detail": f"host does not resolve: {exc}"}
    except (ConnectionRefusedError, socket.timeout, TimeoutError) as exc:
        return {"success": False, "detail": f"could not connect: {exc.__class__.__name__}: {exc}"}
    except ssl.SSLError as exc:
        return {"success": False, "detail": f"TLS error: {exc} (turn off certificate verification only for a test server)"}
    except requests.exceptions.SSLError as exc:
        return {"success": False, "detail": f"TLS error: {exc}"}
    except requests.exceptions.ConnectionError as exc:
        return {"success": False, "detail": f"could not connect: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "detail": f"{exc.__class__.__name__}: {exc}"}
    return {"success": True, "detail": f"one test record delivered to {VENDOR_BY_ID[vid]['label']} in {int((time.time() - started) * 1000)} ms"}
