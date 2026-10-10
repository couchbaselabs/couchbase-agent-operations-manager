"""SIEM / log-forwarding destinations for the Audit Log.

Six named integrations, each a thin adapter around that vendor's own
documented HTTP ingestion API: Splunk (HEC), Elastic Security
(Elasticsearch Bulk API into a data stream), Sumo Logic (HTTP Source
collector), Microsoft Sentinel (Azure Monitor Logs Ingestion API via a
Data Collection Endpoint/Rule, AAD OAuth2 client-credentials), Google
Security Operations / Chronicle (service-account JWT-bearer OAuth2, then
the unstructured log ingestion API), and CrowdStrike Falcon Next-Gen SIEM
(LogScale/Humio structured HTTP ingest).

Every adapter is a plain synchronous function using `requests` - the same
HTTP client already used elsewhere in this codebase (couchbase_client.py) -
so it is invoked via asyncio.to_thread from dispatch() below, exactly like
this app already wraps the blocking Couchbase SDK calls.

Config is stored in Couchbase at settings::siem (config.SIEM_SETTINGS_DOC),
the same settings::<name> convention as settings::llm_cache / settings::ldap.
Secrets (HEC tokens, API keys, client secrets, service-account keys, and
the Sumo Logic collector URL itself - it *is* the credential) are Fernet-
encrypted at rest via user_auth.encrypt_secret/decrypt_secret, the same
mechanism already protecting the LDAP bind password, and are never returned
to the client in plaintext - the public view exposes only "<field>_set".

Forwarding is fire-and-forget: couchbase_client.log_access() (the single
choke point for every audit log entry - discover, invoke, and dashboard
login/auth decisions alike) schedules dispatch() as a background task via a
provider hook set at startup (see set_siem_config_provider in
couchbase_client.py), so a slow or unreachable SIEM endpoint never adds
latency to the request that generated the log entry, and one destination's
failure never affects another's. Per-destination outcomes (ok/error +
detail + timestamp) are kept in an in-memory dict for the Audit Log page's
status display - resets on process restart, the same tradeoff already made
for llm_config_version and other in-memory-only state in this app.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid

import jwt
import requests

from app import user_auth
from app import metrics

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 8

# Field(s), per vendor, that hold a credential: encrypted at rest under
# "<field>_encrypted", never stored or returned as plaintext.
SECRET_FIELDS: dict[str, list[str]] = {
    "splunk": ["hec_token"],
    "elastic": ["api_key"],
    "sumologic": ["http_source_url"],
    "sentinel": ["client_secret"],
    "chronicle": ["service_account_json"],
    "crowdstrike": ["api_token"],
}

DEFAULT_DESTINATIONS: dict[str, dict] = {
    "splunk": {
        "enabled": False,
        "hec_url": "",  # e.g. https://splunk.example.com:8088
        "index": "",
        "sourcetype": "aom:auditlog",
        "verify_tls": True,
    },
    "elastic": {
        "enabled": False,
        "elasticsearch_url": "",  # e.g. https://elastic.example.com:9200
        "data_stream": "logs-aom.auditlog-default",
        "verify_tls": True,
    },
    "sumologic": {
        "enabled": False,
        "source_category": "",
        "source_name": "aom-audit-log",
    },
    "sentinel": {
        "enabled": False,
        "tenant_id": "",
        "client_id": "",
        "dce_endpoint": "",  # Data Collection Endpoint, e.g. https://xxxx.ingest.monitor.azure.com
        "dcr_immutable_id": "",  # dcr-xxxxxxxxxxxxxxxx
        "stream_name": "Custom-AomAuditLog",
    },
    "chronicle": {
        "enabled": False,
        "customer_id": "",
        "region": "us",  # us | europe | asia-southeast1 ...
        "log_type": "AOM_AUDIT_LOG",
    },
    "crowdstrike": {
        "enabled": False,
        "ingest_url": "",  # Falcon Next-Gen SIEM (LogScale) ingest host, e.g. https://your-cid.cloud.crowdstrike.com
        "tag_source": "aom-audit-log",
    },
}

VENDOR_LABELS: dict[str, str] = {
    "splunk": "Splunk",
    "elastic": "Elastic Security",
    "sumologic": "Sumo Logic",
    "sentinel": "Microsoft Sentinel",
    "chronicle": "Google Security Operations (Chronicle)",
    "crowdstrike": "CrowdStrike Falcon Next-Gen SIEM",
}

# Last-delivery status per vendor, in-memory only (see module docstring).
_last_status: dict[str, dict] = {}

# Cached bearer tokens for the two OAuth2-based vendors: vendor -> (token, expiry_epoch_seconds).
_token_cache: dict[str, tuple[str, float]] = {}


def normalize_config(cfg: dict | None) -> dict:
    """Merge partial input over the defaults, per vendor, coercing types.
    Never sees plaintext secrets - those travel only as "<field>_encrypted",
    already-encrypted by the caller (see apply_secrets)."""
    cfg = cfg or {}
    result: dict[str, dict] = {}
    for vendor, defaults in DEFAULT_DESTINATIONS.items():
        merged = {**defaults, **(cfg.get(vendor) or {})}
        out: dict = {}
        for key, default_val in defaults.items():
            if isinstance(default_val, bool):
                out[key] = bool(merged.get(key, default_val))
            else:
                out[key] = str(merged.get(key) if merged.get(key) is not None else default_val).strip()
        for field in SECRET_FIELDS.get(vendor, []):
            enc_key = f"{field}_encrypted"
            out[enc_key] = str(merged.get(enc_key) or "")
        result[vendor] = out
    return result


def apply_secrets(vendor: str, current: dict, incoming_plain: dict | None) -> dict:
    """Encrypt any non-empty plaintext secret fields the client sent for
    this vendor and overwrite the stored "<field>_encrypted" value; a
    field omitted or sent blank leaves the secret already on file
    untouched, so the settings form never has to round-trip (or even know)
    the current secret - same convention as the LDAP bind password."""
    updated = dict(current)
    for field in SECRET_FIELDS.get(vendor, []):
        plain = (incoming_plain or {}).get(field)
        if plain:
            updated[f"{field}_encrypted"] = user_auth.encrypt_secret(plain)
    return updated


def public_config(cfg: dict) -> dict:
    """Strip encrypted secret material for the API response, replacing each
    with a "<field>_set" boolean - same convention as LDAP's bind_password_set."""
    out: dict = {}
    for vendor, vcfg in (cfg or {}).items():
        v = {k: val for k, val in vcfg.items() if not k.endswith("_encrypted")}
        for field in SECRET_FIELDS.get(vendor, []):
            v[f"{field}_set"] = bool(vcfg.get(f"{field}_encrypted"))
        out[vendor] = v
    return out


def status_snapshot() -> dict:
    return dict(_last_status)


def _decrypt(vendor_cfg: dict, field: str) -> str:
    enc = vendor_cfg.get(f"{field}_encrypted") or ""
    if not enc:
        return ""
    return user_auth.decrypt_secret(enc)


# ---------------------------------------------------------------------------
# Per-vendor adapters. Each returns (success, human-readable detail).
# ---------------------------------------------------------------------------

def _send_splunk(cfg: dict, entry: dict) -> tuple[bool, str]:
    token = _decrypt(cfg, "hec_token")
    if not cfg.get("hec_url") or not token:
        return False, "hec_url and hec_token are required"
    payload = {"event": entry, "sourcetype": cfg.get("sourcetype") or "aom:auditlog"}
    if cfg.get("index"):
        payload["index"] = cfg["index"]
    try:
        resp = requests.post(
            f"{cfg['hec_url'].rstrip('/')}/services/collector/event",
            headers={"Authorization": f"Splunk {token}"},
            json=payload,
            timeout=_TIMEOUT_SECONDS,
            verify=cfg.get("verify_tls", True),
        )
        if resp.status_code == 200:
            return True, "accepted"
        return False, f"HTTP {resp.status_code}: {resp.text[:300]}"
    except requests.RequestException as exc:
        return False, str(exc)


def _send_elastic(cfg: dict, entry: dict) -> tuple[bool, str]:
    api_key = _decrypt(cfg, "api_key")
    if not cfg.get("elasticsearch_url") or not api_key:
        return False, "elasticsearch_url and api_key are required"
    data_stream = cfg.get("data_stream") or "logs-aom.auditlog-default"
    body = (
        json.dumps({"create": {}})
        + "\n"
        + json.dumps({"@timestamp": entry.get("timestamp"), **entry})
        + "\n"
    )
    try:
        resp = requests.post(
            f"{cfg['elasticsearch_url'].rstrip('/')}/{data_stream}/_bulk",
            headers={"Authorization": f"ApiKey {api_key}", "Content-Type": "application/x-ndjson"},
            data=body.encode("utf-8"),
            timeout=_TIMEOUT_SECONDS,
            verify=cfg.get("verify_tls", True),
        )
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:300]}"
        try:
            if (resp.json() or {}).get("errors"):
                return False, f"bulk response reported errors: {resp.text[:300]}"
        except ValueError:
            pass
        return True, "accepted"
    except requests.RequestException as exc:
        return False, str(exc)


def _send_sumologic(cfg: dict, entry: dict) -> tuple[bool, str]:
    url = _decrypt(cfg, "http_source_url")
    if not url:
        return False, "HTTP Source collector URL is required"
    headers = {"Content-Type": "application/json"}
    if cfg.get("source_name"):
        headers["X-Sumo-Name"] = cfg["source_name"]
    if cfg.get("source_category"):
        headers["X-Sumo-Category"] = cfg["source_category"]
    try:
        resp = requests.post(url, headers=headers, json=entry, timeout=_TIMEOUT_SECONDS)
        if resp.status_code in (200, 204):
            return True, "accepted"
        return False, f"HTTP {resp.status_code}: {resp.text[:300]}"
    except requests.RequestException as exc:
        return False, str(exc)


def _get_sentinel_token(cfg: dict) -> str:
    cached = _token_cache.get("sentinel")
    if cached and cached[1] > time.time() + 30:
        return cached[0]
    secret = _decrypt(cfg, "client_secret")
    resp = requests.post(
        f"https://login.microsoftonline.com/{cfg['tenant_id']}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": cfg.get("client_id", ""),
            "client_secret": secret,
            "scope": "https://monitor.azure.com/.default",
        },
        timeout=_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    body = resp.json()
    token = body["access_token"]
    _token_cache["sentinel"] = (token, time.time() + int(body.get("expires_in", 3600)))
    return token


def _send_sentinel(cfg: dict, entry: dict) -> tuple[bool, str]:
    if not all([cfg.get("tenant_id"), cfg.get("client_id"), cfg.get("dce_endpoint"), cfg.get("dcr_immutable_id")]):
        return False, "tenant_id, client_id, client_secret, dce_endpoint and dcr_immutable_id are required"
    try:
        token = _get_sentinel_token(cfg)
    except requests.RequestException as exc:
        _token_cache.pop("sentinel", None)
        return False, f"token request failed: {exc}"
    stream = cfg.get("stream_name") or "Custom-AomAuditLog"
    try:
        resp = requests.post(
            f"{cfg['dce_endpoint'].rstrip('/')}/dataCollectionRules/{cfg['dcr_immutable_id']}/streams/{stream}?api-version=2023-01-01",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=[entry],
            timeout=_TIMEOUT_SECONDS,
        )
        if resp.status_code in (200, 202, 204):
            return True, "accepted"
        return False, f"HTTP {resp.status_code}: {resp.text[:300]}"
    except requests.RequestException as exc:
        _token_cache.pop("sentinel", None)
        return False, str(exc)


def _get_chronicle_token(cfg: dict) -> str:
    cached = _token_cache.get("chronicle")
    if cached and cached[1] > time.time() + 30:
        return cached[0]
    sa_json = _decrypt(cfg, "service_account_json")
    if not sa_json:
        raise ValueError("service_account_json is not configured")
    sa = json.loads(sa_json)
    now = int(time.time())
    claims = {
        "iss": sa["client_email"],
        "scope": "https://www.googleapis.com/auth/malachite-ingestion",
        "aud": "https://oauth2.googleapis.com/token",
        "iat": now,
        "exp": now + 3600,
    }
    assertion = jwt.encode(claims, sa["private_key"], algorithm="RS256")
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
        timeout=_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    body = resp.json()
    token = body["access_token"]
    _token_cache["chronicle"] = (token, time.time() + int(body.get("expires_in", 3600)))
    return token


def _send_chronicle(cfg: dict, entry: dict) -> tuple[bool, str]:
    if not cfg.get("customer_id"):
        return False, "customer_id is required"
    try:
        token = _get_chronicle_token(cfg)
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        return False, f"service_account_json: {exc}"
    except requests.RequestException as exc:
        return False, f"token request failed: {exc}"
    region = (cfg.get("region") or "us").strip()
    host = "chronicle.googleapis.com" if region in ("", "us") else f"{region}-chronicle.googleapis.com"
    body = {
        "customer_id": cfg["customer_id"],
        "log_type": cfg.get("log_type") or "AOM_AUDIT_LOG",
        "entries": [{"log_text": json.dumps(entry), "ts_rfc3339": entry.get("timestamp")}],
    }
    try:
        resp = requests.post(
            f"https://{host}/v2/unstructuredlogentries:batchCreate",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body,
            timeout=_TIMEOUT_SECONDS,
        )
        if resp.status_code == 200:
            return True, "accepted"
        return False, f"HTTP {resp.status_code}: {resp.text[:300]}"
    except requests.RequestException as exc:
        _token_cache.pop("chronicle", None)
        return False, str(exc)


def _send_crowdstrike(cfg: dict, entry: dict) -> tuple[bool, str]:
    token = _decrypt(cfg, "api_token")
    if not cfg.get("ingest_url") or not token:
        return False, "ingest_url and api_token are required"
    payload = [
        {
            "tags": {"source": cfg.get("tag_source") or "aom-audit-log"},
            "events": [{"timestamp": entry.get("timestamp"), "attributes": entry}],
        }
    ]
    try:
        resp = requests.post(
            f"{cfg['ingest_url'].rstrip('/')}/api/v1/ingest/humio-structured",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=_TIMEOUT_SECONDS,
        )
        if resp.status_code == 200:
            return True, "accepted"
        return False, f"HTTP {resp.status_code}: {resp.text[:300]}"
    except requests.RequestException as exc:
        return False, str(exc)


SENDERS = {
    "splunk": _send_splunk,
    "elastic": _send_elastic,
    "sumologic": _send_sumologic,
    "sentinel": _send_sentinel,
    "chronicle": _send_chronicle,
    "crowdstrike": _send_crowdstrike,
}


def send_one(vendor: str, vendor_cfg: dict, entry: dict) -> tuple[bool, str]:
    fn = SENDERS.get(vendor)
    if not fn:
        return False, f"unknown vendor '{vendor}'"
    try:
        return fn(vendor_cfg, entry)
    except Exception as exc:  # noqa: BLE001 - a bad adapter must never break audit logging
        logger.exception("SIEM adapter for %s raised unexpectedly", vendor)
        return False, str(exc)


# Forwarding is fire-and-forget from the audit-log write, so without a
# ceiling a slow or unreachable SIEM endpoint turns every audited request
# into one more parked task holding a worker thread for up to
# _TIMEOUT_SECONDS - and those are the same threads every Couchbase call
# runs on. Past the ceiling, entries are dropped (and counted) rather than
# queued: the audit log in Couchbase remains the system of record, and a
# SIEM outage must never become an appliance outage.
MAX_IN_FLIGHT_FORWARDS = int(os.getenv("SIEM_MAX_IN_FLIGHT", "64"))
_in_flight = 0
_dropped = 0
_last_drop_log = 0.0
_tasks: set = set()


def schedule(entry: dict, config: dict) -> None:
    """Start forwarding one entry in the background, or drop it if too
    many forwards are already outstanding. Must be called from the event
    loop."""
    global _in_flight, _dropped, _last_drop_log
    if _in_flight >= MAX_IN_FLIGHT_FORWARDS:
        _dropped += 1
        now = time.time()
        if now - _last_drop_log > 60:
            _last_drop_log = now
            logger.warning(
                "SIEM forwarding is backed up (%d in flight) - dropped %d audit entr(ies) so far; "
                "they remain in the Couchbase audit log.", _in_flight, _dropped,
            )
        return
    _in_flight += 1
    task = asyncio.create_task(dispatch(entry, config))
    _tasks.add(task)

    def _done(t):
        global _in_flight
        _in_flight -= 1
        _tasks.discard(t)

    task.add_done_callback(_done)


def forwarding_stats() -> dict:
    return {"in_flight": _in_flight, "dropped": _dropped, "max_in_flight": MAX_IN_FLIGHT_FORWARDS}


async def dispatch(entry: dict, config: dict) -> None:
    """Forward one audit log entry to every enabled destination, concurrently
    and independently. Called as a fire-and-forget background task from
    couchbase_client.log_access() - never awaited by the request path."""
    enabled = [vendor for vendor, vcfg in (config or {}).items() if vcfg.get("enabled")]
    if not enabled:
        return

    async def _run(vendor: str):
        ok, detail = await asyncio.to_thread(send_one, vendor, config[vendor], entry)
        metrics.record_siem_forward(vendor, ok)
        _last_status[vendor] = {
            "status": "ok" if ok else "error",
            "detail": detail,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if not ok:
            logger.warning("SIEM forward to %s failed: %s", vendor, detail)

    await asyncio.gather(*(_run(vendor) for vendor in enabled))


def test_one(vendor: str, vendor_cfg: dict) -> dict:
    """Synchronous single-destination test for the settings page's Test
    button - sends one clearly-labelled synthetic event and returns the
    real result immediately."""
    sample_entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "action": "test",
        "role": None,
        "subject": "aom-siem-test",
        "query": None,
        "tool_id": None,
        "server_id": None,
        "decision": "ALLOW",
        "reason": f"Test event from Couchbase Agent Operations Manager ({VENDOR_LABELS.get(vendor, vendor)} forwarding configuration test)",
        "latency_ms": 0,
        "hijack_flagged": False,
        "hijack_severity": None,
        "hijack_signals": [],
        "test_id": uuid.uuid4().hex[:12],
    }
    ok, detail = send_one(vendor, vendor_cfg, sample_entry)
    _last_status[vendor] = {
        "status": "ok" if ok else "error",
        "detail": detail,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return {"success": ok, "detail": detail}
