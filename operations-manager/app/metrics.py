"""Prometheus metrics for the appliance.

One module owns every metric so the names stay consistent and the rest of
the code only ever calls a small set of record_* helpers. Everything here is
a plain in-process counter/gauge/histogram: a Prometheus scrape of
GET /metrics never touches Couchbase, so it is safe to point a 15s scrape
interval at an appliance under load.

What is measured, and where it is recorded from:

  aom_http_requests_total / aom_http_request_duration_seconds /
  aom_http_requests_in_flight   - every HTTP request, by route template
                                  (not raw path, so IDs don't explode
                                  cardinality), from the middleware in main.py
  aom_gateway_decisions_total   - every discover/invoke decision, from
                                  CouchbaseStore.log_access() - the same
                                  choke point the Audit Log and SIEM
                                  forwarding hang off
  aom_gateway_latency_seconds   - latency of those calls, by action
  aom_hijack_flags_total        - tool responses flagged by hijack detection
  aom_llm_cache_events_total, aom_llm_tokens_saved_total,
  aom_llm_cost_saved_usd_total, aom_llm_cost_usd_total,
  aom_llm_latency_seconds       - from CouchbaseStore.log_llm_event()
  aom_context_cache_events_total,
  aom_context_latency_saved_seconds_total
                                - from CouchbaseStore.log_context_event()
  aom_trace_spans_total         - every span written, by kind/status
  aom_siem_forward_total        - per-vendor delivery outcomes
  aom_couchbase_connected       - 1 while the SDK connection is up
  aom_build_info                - version label for dashboards

Label values are bounded: role is one of the code-defined RBAC roles,
provider/model come from the small provider catalog, server_id from the
registry, vendor from the six SIEM adapters. Nothing user-typed (queries,
subjects, tool IDs beyond server) is ever used as a label.

The exporter is optional: with METRICS_ENABLED=false the helpers are
no-ops and /metrics answers 404, and if prometheus_client is somehow not
installed the module degrades the same way instead of taking the
appliance down with it.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Callable

logger = logging.getLogger("operations-manager.metrics")

METRICS_ENABLED = os.getenv("METRICS_ENABLED", "true").strip().lower() not in ("0", "false", "no")
# Optional bearer token Prometheus must present. Empty = no auth on
# /metrics (fine when the port is only reachable inside the cluster/compose
# network); set it when 8090 is exposed more widely.
METRICS_TOKEN = os.getenv("METRICS_TOKEN", "")

try:
    from prometheus_client import (  # type: ignore
        CONTENT_TYPE_LATEST,
        REGISTRY,
        Counter,
        Gauge,
        Histogram,
        Info,
        generate_latest,
    )

    _AVAILABLE = True
except Exception as exc:  # noqa: BLE001 - keep the appliance up without the exporter
    _AVAILABLE = False
    logger.warning("prometheus_client unavailable, /metrics disabled: %s", exc)

ENABLED = METRICS_ENABLED and _AVAILABLE

if ENABLED:
    _LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)

    BUILD_INFO = Info("aom_build", "Couchbase Agent Operations Manager build information")

    HTTP_REQUESTS = Counter(
        "aom_http_requests_total", "HTTP requests handled", ["method", "route", "status"]
    )
    HTTP_DURATION = Histogram(
        "aom_http_request_duration_seconds", "HTTP request latency", ["method", "route"],
        buckets=_LATENCY_BUCKETS,
    )
    HTTP_IN_FLIGHT = Gauge("aom_http_requests_in_flight", "HTTP requests currently being handled")

    GATEWAY_DECISIONS = Counter(
        "aom_gateway_decisions_total",
        "Tool discovery/invocation decisions made by the gateway",
        ["action", "decision", "role", "server_id"],
    )
    GATEWAY_LATENCY = Histogram(
        "aom_gateway_latency_seconds", "Gateway call latency by action", ["action"],
        buckets=_LATENCY_BUCKETS,
    )
    HIJACK_FLAGS = Counter(
        "aom_hijack_flags_total", "Tool responses flagged by hijack detection", ["severity", "role"]
    )

    LLM_EVENTS = Counter(
        "aom_llm_cache_events_total",
        "LLM completion requests by cache outcome",
        ["outcome", "provider", "model", "role"],
    )
    LLM_TOKENS_SAVED = Counter("aom_llm_tokens_saved_total", "Tokens not sent to a provider thanks to the cache", ["provider", "model"])
    LLM_COST_SAVED = Counter("aom_llm_cost_saved_usd_total", "Estimated provider spend avoided (USD)", ["provider", "model"])
    LLM_COST = Counter("aom_llm_cost_usd_total", "Estimated provider spend incurred (USD)", ["provider", "model"])
    LLM_LATENCY = Histogram(
        "aom_llm_latency_seconds", "LLM completion latency as seen by the agent", ["outcome"],
        buckets=_LATENCY_BUCKETS,
    )

    CONTEXT_EVENTS = Counter(
        "aom_context_cache_events_total", "Context cache lookups/writes by outcome", ["outcome", "namespace"]
    )
    CONTEXT_LATENCY_SAVED = Counter(
        "aom_context_latency_saved_seconds_total", "Source-fetch latency avoided by context cache hits", ["namespace"]
    )

    TRACE_SPANS = Counter("aom_trace_spans_total", "Agent trace spans recorded", ["kind", "status"])

    SIEM_FORWARDS = Counter("aom_siem_forward_total", "Audit entries forwarded to SIEM destinations", ["vendor", "status"])

    COUCHBASE_CONNECTED = Gauge("aom_couchbase_connected", "1 while the Couchbase SDK connection is established")


def _label(value, fallback: str = "unknown") -> str:
    if value is None or value == "":
        return fallback
    return str(value)[:120]


# --- recorders (all no-ops when disabled) -----------------------------------

def set_build_info(version: str, appliance_name: str) -> None:
    if ENABLED:
        BUILD_INFO.info({"version": version, "appliance": appliance_name})


def set_couchbase_connected(connected: bool) -> None:
    if ENABLED:
        COUCHBASE_CONNECTED.set(1 if connected else 0)


def record_http(method: str, route: str, status: int, seconds: float) -> None:
    if not ENABLED:
        return
    HTTP_REQUESTS.labels(method, route, str(status)).inc()
    HTTP_DURATION.labels(method, route).observe(seconds)


def record_gateway_decision(
    *, action: str, decision: str, role, server_id, latency_ms: int,
    hijack_flagged: bool = False, hijack_severity=None,
) -> None:
    if not ENABLED:
        return
    GATEWAY_DECISIONS.labels(_label(action), _label(decision), _label(role, "anonymous"), _label(server_id, "none")).inc()
    GATEWAY_LATENCY.labels(_label(action)).observe(max(0, int(latency_ms or 0)) / 1000.0)
    if hijack_flagged:
        HIJACK_FLAGS.labels(_label(hijack_severity), _label(role, "anonymous")).inc()


def record_llm_event(doc: dict) -> None:
    if not ENABLED:
        return
    provider = _label(doc.get("provider"))
    model = _label(doc.get("model"))
    LLM_EVENTS.labels(_label(doc.get("outcome")), provider, model, _label(doc.get("role"), "anonymous")).inc()
    LLM_TOKENS_SAVED.labels(provider, model).inc(max(0, float(doc.get("tokens_saved") or 0)))
    LLM_COST_SAVED.labels(provider, model).inc(max(0.0, float(doc.get("cost_saved_usd") or 0)))
    LLM_COST.labels(provider, model).inc(max(0.0, float(doc.get("cost_usd") or 0)))
    LLM_LATENCY.labels(_label(doc.get("outcome"))).observe(max(0, float(doc.get("latency_ms") or 0)) / 1000.0)


def record_context_event(doc: dict) -> None:
    if not ENABLED:
        return
    namespace = _label(doc.get("namespace"), "default")
    CONTEXT_EVENTS.labels(_label(doc.get("outcome")), namespace).inc()
    CONTEXT_LATENCY_SAVED.labels(namespace).inc(max(0, float(doc.get("latency_saved_ms") or 0)) / 1000.0)


def record_span(span: dict) -> None:
    if ENABLED:
        TRACE_SPANS.labels(_label(span.get("kind")), _label(span.get("status"), "ok")).inc()


def record_siem_forward(vendor: str, ok: bool) -> None:
    if ENABLED:
        SIEM_FORWARDS.labels(_label(vendor), "ok" if ok else "error").inc()


def in_flight() -> Callable[[], None]:
    """Increment the in-flight gauge and return the matching decrement."""
    if not ENABLED:
        return lambda: None
    HTTP_IN_FLIGHT.inc()
    return HTTP_IN_FLIGHT.dec


def exposition() -> tuple[bytes, str]:
    """Body + content type for GET /metrics."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


class Timer:
    __slots__ = ("start",)

    def __init__(self) -> None:
        self.start = time.perf_counter()

    def elapsed(self) -> float:
        return time.perf_counter() - self.start
