"""
MCP Tool Hijacking detection.

MCP Tool Hijacking - usually delivered as a Tool Poisoning Attack - is an
indirect prompt injection vulnerability that's structural to MCP, not a
bug in any one server: an LLM ingests tool *descriptions* and tool *call
results* into the same trusted working context it reasons over, with no
built-in way to tell "text from a vetted source" apart from "text from
anywhere." A malicious or compromised MCP server can exploit that in two
ways this module watches for independently, because they need different
defenses:

  1. Metadata poisoning - a hidden instruction embedded in a tool's
     `description` or its input-schema property descriptions, ingested
     into the catalog at registration time. Caught here at *ingest* time
     (see `scan_tool_metadata`), before the tool is ever discoverable -
     a flagged tool is quarantined automatically (trust_status forced to
     "quarantined", which the RBAC + vector Search pre-filter already
     excludes, exactly like an untrusted server) rather than merely noted
     for later review. The cost of a false positive here is just an admin
     clicking "release" on the Threat Detection page; the cost of a false
     negative is a hidden instruction sitting in the catalog until
     someone reads the raw description by hand.

  2. Response payload poisoning - the tool's *description* is clean, but
     an injected instruction rides along in the data a call actually
     returns (a poisoned web page, a compromised ticket body, and so on).
     This can't be caught at ingest time because it doesn't exist until
     the call happens, so it's scanned on every `invoke` response instead
     (see `scan_response_payload`) and the finding is attached to that
     audit-log entry rather than blocking the response outright - the
     same words that flag an attack (credentials, "always", "before
     responding") show up in a lot of harmless data too, so a live tool
     response gets flagged and logged loudly, not silently dropped.

  3. Cross-tool hijacking - the actual damage: a poisoned response tricks
     the agent into calling a second, higher-privilege tool it wouldn't
     otherwise have reached for. `detect_hijack_chains` correlates
     response-poisoning flags against the audit log itself: a
     response-flagged invoke by some subject, followed within a short
     window by that same subject invoking a materially higher-risk tool,
     is exactly the shape that mechanism takes - and it's the same
     timing-correlation technique the rest of this appliance's insights
     already use, applied to a security signal instead of a performance
     one.
"""
import json
import re
import time
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Pattern bank
# ---------------------------------------------------------------------------
# Every pattern is deliberately broad-strokes and heuristic, not a proof of
# malicious intent on its own - that's why findings are surfaced for human
# review (or auto-quarantine, which is reversible with one click) rather
# than treated as certain. `scope` marks whether a pattern applies to tool
# *metadata* (name/description/schema), a *response* payload, or both.

@dataclass
class Pattern:
    id: str
    category: str
    severity: str  # "critical" | "high" | "medium"
    regex: re.Pattern
    scope: tuple  # subset of ("metadata", "response")


def _p(id_: str, category: str, severity: str, pattern: str, scope=("metadata", "response")) -> Pattern:
    return Pattern(id_, category, severity, re.compile(pattern, re.IGNORECASE | re.DOTALL), scope)


PATTERNS: list[Pattern] = [
    # -- Instruction-override language: the hallmark of a prompt injection --
    _p("override-ignore", "instruction-override", "high",
       r"ignore\s+(all\s+)?(prior|previous|earlier|above)\s+instructions?"),
    _p("override-disregard", "instruction-override", "high",
       r"disregard\s+(the\s+)?(above|prior|previous|system)\b"),
    _p("override-system-note", "instruction-override", "high",
       r"(system note|system override|important system note|new instructions?)\s*[:\-]"),
    _p("override-real-instructions", "instruction-override", "high",
       r"(the\s+)?(real|actual|true)\s+instructions?\s+(are|follow)"),

    # -- Covert / silent execution cues -----------------------------------
    _p("covert-silent", "covert-execution", "high",
       r"\b(secretly|covertly|without (telling|informing|notifying) the user|do not (tell|mention|inform) the user)\b"),
    _p("covert-before-responding", "covert-execution", "medium",
       r"\bbefore (responding|answering|replying) to the user\b.{0,80}\b(call|invoke|run|execute)\b"),
    _p("covert-always-call-first", "covert-execution", "high",
       r"\balways (call|invoke|run) this (tool|function)\s*(first)?\b"),
    _p("covert-chain-tool", "covert-execution", "medium",
       r"\b(then|after (that|calling)|next)\s*,?\s*(call|invoke|run)\s+[`'\"]?[\w:\-]+"),

    # -- Data exfiltration cues ---------------------------------------------
    _p("exfil-credentials", "data-exfiltration", "critical",
       r"\b(ssh key|private key|api[\s_-]?key|access token|credentials?|password|secret)\b.{0,60}\b(include|copy|send|paste|attach|exfiltrate|upload)\b"),
    _p("exfil-include-contents", "data-exfiltration", "critical",
       r"\binclude\s+(the\s+)?(full\s+)?contents?\s+of\s+(any\s+)?(config|credential|environment|\.env|secret)"),
    _p("exfil-env-vars", "data-exfiltration", "critical",
       r"\benvironment\s+variables?\b.{0,60}\b(include|show|reveal|send|dump)\b"),
    _p("exfil-outbound", "data-exfiltration", "high",
       r"\b(send|post|upload)\s+(it|this|the (result|data|output))\s+to\s+https?://"),

    # -- Hidden-content delivery mechanisms ---------------------------------
    _p("hidden-html-comment", "hidden-content", "medium", r"<!--.*?-->"),
    _p("hidden-zero-width", "hidden-content", "medium", r"[\u200b\u200c\u200d\u2060\ufeff]"),
    _p("hidden-system-tag", "hidden-content", "high", r"</?system>"),

    # -- Role / privilege escalation language --------------------------------
    _p("escalation-admin-only", "privilege-escalation", "high",
       r"\b(as an? admin|with admin (rights|privileges)|bypass(ing)? (rbac|authorization|permission))\b"),
]

METADATA_ONLY_IDS = set()  # currently every pattern applies to both scopes


def _matches(text: str, scope: str) -> list[dict]:
    if not text:
        return []
    signals = []
    for pat in PATTERNS:
        if scope not in pat.scope:
            continue
        m = pat.regex.search(text)
        if m:
            snippet = m.group(0)
            if len(snippet) > 140:
                snippet = snippet[:140] + "..."
            signals.append({
                "pattern_id": pat.id,
                "category": pat.category,
                "severity": pat.severity,
                "matched_text": snippet,
            })
    return signals


_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2}


def _overall_severity(signals: list[dict]) -> str | None:
    if not signals:
        return None
    return sorted(signals, key=lambda s: _SEVERITY_RANK.get(s["severity"], 9))[0]["severity"]


def scan_tool_metadata(tool: dict) -> dict:
    """Scan a tool's name, description, and input-schema property
    descriptions for metadata-poisoning signals. Returns
    {flagged, severity, signals}."""
    parts = [tool.get("name", ""), tool.get("description", "")]
    schema = tool.get("input_schema") or {}
    for pname, pinfo in (schema.get("properties") or {}).items():
        if isinstance(pinfo, dict) and pinfo.get("description"):
            parts.append(str(pinfo["description"]))
    text = "\n".join(parts)

    signals = _matches(text, "metadata")
    return {"flagged": bool(signals), "severity": _overall_severity(signals), "signals": signals}


def apply_metadata_scan(tool_doc: dict, existing: dict | None) -> dict:
    """Run the metadata scan against `tool_doc` and decide its
    trust_status, honoring any admin override recorded on `existing` (the
    previously-stored version of this tool, if any) so a manual
    release/quarantine decision survives future re-ingests and background
    rescans rather than being silently reverted the next time the scanner
    runs. Mutates and returns `tool_doc`.
    """
    manual_override = (existing or {}).get("hijack_manual_override")

    scan = scan_tool_metadata(tool_doc)
    tool_doc["hijack_signals"] = scan["signals"]
    tool_doc["hijack_severity"] = scan["severity"]
    tool_doc["hijack_status"] = "flagged" if scan["flagged"] else "clear"
    tool_doc["hijack_scanned_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if manual_override in ("trusted", "quarantined"):
        tool_doc["trust_status"] = manual_override
        tool_doc["hijack_manual_override"] = manual_override
    else:
        tool_doc["trust_status"] = "quarantined" if scan["flagged"] else "trusted"
        tool_doc.pop("hijack_manual_override", None)

    return tool_doc


def scan_response_payload(payload) -> dict:
    """Scan a tool call's response payload for injection signals. Returns
    {flagged, severity, signals}."""
    try:
        text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    except Exception:  # noqa: BLE001
        text = str(payload)

    signals = _matches(text, "response")
    return {"flagged": bool(signals), "severity": _overall_severity(signals), "signals": signals}


# ---------------------------------------------------------------------------
# Cross-tool hijack chain correlation
# ---------------------------------------------------------------------------
CHAIN_WINDOW_SECONDS_DEFAULT = 120
HIGH_RISK_LEVELS = {"critical", "high"}


def _parse_ts(ts: str) -> float | None:
    try:
        return time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None


def detect_hijack_chains(
    log_entries: list[dict],
    tools_by_id: dict[str, dict],
    window_seconds: int = CHAIN_WINDOW_SECONDS_DEFAULT,
) -> list[dict]:
    """Correlate response-poisoning flags against the audit log: a
    response-flagged invoke by some subject, followed within
    `window_seconds` by that same subject successfully invoking a
    materially higher-risk tool, is the actual mechanism a cross-tool
    hijack takes - the poisoned call trains the agent's next action, and
    the next call is where the real damage happens.

    `log_entries` should be ordered newest-first (as store.recent_access_log
    returns); entries need `hijack_flagged`/`hijack_severity` set on the
    ones scan_response_payload flagged at invoke time.
    """
    invokes = [e for e in log_entries if e.get("action") == "invoke" and e.get("decision") == "ALLOW"]
    # Oldest-first makes the "followed by" window logic read naturally.
    invokes = list(reversed(invokes))

    flagged = [e for e in invokes if e.get("hijack_flagged")]
    findings = []
    seen_pairs = set()

    for src in flagged:
        src_ts = _parse_ts(src.get("timestamp", ""))
        if src_ts is None:
            continue
        src_tool = tools_by_id.get(src.get("tool_id") or "", {})
        src_risk = src_tool.get("risk_level", "unknown")

        for candidate in invokes:
            if candidate is src:
                continue
            if candidate.get("subject") != src.get("subject"):
                continue
            cand_ts = _parse_ts(candidate.get("timestamp", ""))
            if cand_ts is None or cand_ts <= src_ts or cand_ts - src_ts > window_seconds:
                continue
            cand_tool = tools_by_id.get(candidate.get("tool_id") or "", {})
            cand_risk = cand_tool.get("risk_level", "unknown")
            if cand_risk not in HIGH_RISK_LEVELS:
                continue
            if candidate.get("tool_id") == src.get("tool_id"):
                continue

            pair_key = (src.get("tool_id"), candidate.get("tool_id"), src.get("subject"), candidate.get("timestamp"))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            delay = round(cand_ts - src_ts, 1)
            findings.append({
                "id": f"hijack-chain-{src.get('tool_id')}-{candidate.get('tool_id')}-{candidate.get('timestamp')}",
                "severity": "critical",
                "title": f"Possible cross-tool hijack chain: `{src.get('tool_id')}` -> `{candidate.get('tool_id')}`",
                "summary": (
                    f"Subject {src.get('subject')} invoked `{src.get('tool_id')}` (risk: {src_risk}), whose response "
                    f"was flagged for a possible prompt-injection pattern, then invoked the materially higher-risk "
                    f"`{candidate.get('tool_id')}` (risk: {cand_risk}) {delay}s later. This is the exact mechanism "
                    f"cross-tool hijacking uses: a poisoned response steering the next tool call. This is a "
                    f"correlation from call timing, not a confirmed compromise - treat it as a lead, and review "
                    f"the flagged response's matched signals on the Threat Detection page."
                ),
                "tags": ["SECURITY", "CROSS-TOOL HIJACK", "SUGGESTED REVIEW"],
            })

    return findings
