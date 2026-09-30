"""
Input guardrails and PII handling.

The hijacking detector in app/hijack_detection.py watches text arriving from
*servers* - a tool's description at ingest, a tool's response at invoke. It
has nothing to say about text arriving from the *caller*, which is the other
half of the same problem and the half that actually carries personal data.
Two distinct concerns live here, and they want different answers:

  1. **Injection in inbound text.** A prompt or a tool argument can carry the
     same instruction-override payload a poisoned tool description carries.
     The pattern bank that already exists is the right detector; what is new
     is running it on the way in, and being able to refuse rather than only
     note - an injected *argument* is going straight to a downstream system,
     so unlike a flagged response there is something still worth stopping.

  2. **Personal data in anything this appliance stores.** Prompts, completions,
     tool arguments and memory content are written to Couchbase and, in the
     audit log's case, forwarded to a customer's SIEM. Retention is thirty
     days by default. That combination - user-supplied text, long retention,
     onward forwarding - is the compliance exposure, and it exists whether or
     not anyone is attacking anything.

The caller always receives the original text. Redaction applies to what is
*persisted*: the audit log, trace attributes, memory documents and cache
event previews. An agent asking about a real customer still gets a real
answer; what survives the request is scrubbed.

Why PII means "do not cache" rather than "cache the redacted version"
---------------------------------------------------------------------
Redacting a cache entry breaks the cache's central promise, which is that a
hit and a miss return the same answer. Store the redacted response and the
second caller gets something different from the first; store the original and
one caller's personal data is served to another the moment a semantic match
is close enough. Neither is acceptable, so a prompt or completion carrying
PII is simply never cached - `bypass_reason` covers it exactly as the
existing never-cache rules do. It costs a cache entry on the rare request
that names a real person, and it removes the entire class of problem.

Detector design
---------------
Every pattern here is deliberately conservative: it matches shapes that are
expensive to get wrong in only one direction. A missed detection is a
redaction that did not happen; a false positive silently corrupts stored
text that an operator may later need. So the card detector checks the Luhn
digit rather than trusting sixteen digits in a row, and there is no
"name" or "address" detector at all - those cannot be done with a regex at
an acceptable false-positive rate, and pretending otherwise would produce
audit logs full of holes where ordinary words used to be.
"""
import hashlib
import re

from app import hijack_detection

# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------
# `label` is what replaces the match. Keeping the class visible - [EMAIL]
# rather than [REDACTED] - is what makes a redacted audit log still useful
# for investigating an incident.


class Detector:
    def __init__(self, id_: str, label: str, severity: str, pattern: str, validator=None):
        self.id = id_
        self.label = label
        self.severity = severity
        self.regex = re.compile(pattern)
        self.validator = validator


def _luhn_ok(value: str) -> bool:
    """Card numbers have a check digit. Verifying it turns 'sixteen digits'
    - which matches order numbers, tracking IDs and serial numbers - into
    something that is almost always an actual card."""
    digits = [int(c) for c in re.sub(r"[^0-9]", "", value)]
    if not 12 <= len(digits) <= 19:
        return False
    checksum = 0
    parity = len(digits) % 2
    for i, digit in enumerate(digits):
        if i % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def _ssn_ok(value: str) -> bool:
    """Exclude the ranges the SSA never issues, which is what most
    false positives (dates, IDs, version strings) fall into."""
    digits = re.sub(r"[^0-9]", "", value)
    if len(digits) != 9:
        return False
    area, group, serial = digits[:3], digits[3:5], digits[5:]
    if area in ("000", "666") or area.startswith("9"):
        return False
    return group != "00" and serial != "0000"


DETECTORS: list[Detector] = [
    Detector("email", "[EMAIL]", "medium",
             r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    # Greedy on purpose. A lazy quantifier here stops at the twelfth digit
    # of a sixteen-digit number, hands Luhn a truncated string, fails the
    # check and lets the real card through - the detector appears to work
    # on unspaced numbers and silently misses every spaced one.
    Detector("credit_card", "[CARD]", "critical",
             r"\b\d(?:[ -]?\d){11,18}\b", _luhn_ok),
    Detector("ssn", "[SSN]", "critical",
             r"\b\d{3}-\d{2}-\d{4}\b", _ssn_ok),
    Detector("iban", "[IBAN]", "critical",
             r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
    Detector("phone", "[PHONE]", "medium",
             r"(?<![\d.])(?:\+\d{1,3}[ -]?)?(?:\(\d{3}\)|\d{3})[ -]\d{3}[ -]\d{4}(?![\d.])"),
    Detector("ip_address", "[IP]", "low",
             r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b"),
    # Secrets are not personal data, but they are the thing you least want
    # sitting in a thirty-day audit log, and they have unambiguous shapes.
    Detector("aws_key", "[AWS_KEY]", "critical", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    Detector("bearer_token", "[TOKEN]", "critical",
             r"\b(?:sk|pk|rk)[-_](?:live|test|prod)?[-_]?[A-Za-z0-9]{16,}\b"),
    Detector("private_key", "[PRIVATE_KEY]", "critical",
             r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
]

DETECTOR_IDS = tuple(d.id for d in DETECTORS)

DEFAULT_GUARDRAILS_CONFIG = {
    "enabled": True,
    # Which detectors run. All on by default except ip_address, which fires
    # on version numbers and internal hostnames often enough to be noise in
    # most deployments - it is offered, not assumed.
    "detectors": [d.id for d in DETECTORS if d.id != "ip_address"],
    # What gets redacted before it is written. The caller's response is never
    # touched by any of these.
    "redact_audit_log": True,
    "redact_traces": True,
    "redact_memory": True,
    # A prompt or completion carrying PII is never cached. See the module
    # docstring for why this is not "cache the redacted version".
    "never_cache_pii": True,
    # Injection screening of inbound text.
    "scan_prompts": True,
    "scan_tool_arguments": True,
    # Refuse an inbound payload whose injection signal is at or above this
    # severity. "off" scans and records without refusing - the same
    # measure-before-enforce posture the limits policy takes, and for the
    # same reason.
    "block_injection_at": "off",
    # Extra caller-supplied regexes, redacted as [CUSTOM].
    "custom_patterns": [],
}

BLOCK_LEVELS = ("off", "critical", "high", "medium")
_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}

MAX_CUSTOM_PATTERNS = 20


def normalize_config(cfg: dict | None) -> dict:
    merged = dict(DEFAULT_GUARDRAILS_CONFIG)
    for key, value in (cfg or {}).items():
        if key in merged:
            merged[key] = value

    for flag in ("enabled", "redact_audit_log", "redact_traces", "redact_memory",
                 "never_cache_pii", "scan_prompts", "scan_tool_arguments"):
        merged[flag] = bool(merged[flag])

    detectors = merged.get("detectors") or []
    if not isinstance(detectors, (list, tuple)):
        detectors = []
    merged["detectors"] = [d for d in DETECTOR_IDS if d in detectors]

    level = str(merged.get("block_injection_at") or "off").strip().lower()
    merged["block_injection_at"] = level if level in BLOCK_LEVELS else "off"

    patterns = merged.get("custom_patterns") or []
    if not isinstance(patterns, (list, tuple)):
        patterns = []
    valid = []
    for pattern in list(patterns)[:MAX_CUSTOM_PATTERNS]:
        pattern = str(pattern)[:400]
        try:
            re.compile(pattern)
        except re.error:
            continue
        valid.append(pattern)
    merged["custom_patterns"] = valid

    return merged


# ---------------------------------------------------------------------------
# Detection and redaction
# ---------------------------------------------------------------------------

def _fingerprint(value: str) -> str:
    """Four hex characters of a hash of the matched text. Two occurrences of
    the same email redact to the same [EMAIL:1f3a], so an investigator can
    still tell 'the same address appears in both of these entries' without
    the address being recoverable from the log."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:4]


def _active_detectors(cfg: dict) -> list[Detector]:
    enabled = set(cfg.get("detectors") or [])
    return [d for d in DETECTORS if d.id in enabled]


def scan_pii(text: str, cfg: dict) -> dict:
    """Find personal data and secrets in `text`. Returns
    {found, severity, matches} where each match names its detector and the
    fingerprint of what matched - never the matched text itself, because
    this result is itself written to documents and shown in the UI."""
    if not text or not cfg.get("enabled"):
        return {"found": False, "severity": None, "matches": []}

    matches = []
    for detector in _active_detectors(cfg):
        for found in detector.regex.finditer(text):
            value = found.group(0)
            if detector.validator and not detector.validator(value):
                continue
            matches.append({
                "detector": detector.id,
                "label": detector.label,
                "severity": detector.severity,
                "fingerprint": _fingerprint(value),
            })

    for index, pattern in enumerate(cfg.get("custom_patterns") or []):
        try:
            for found in re.finditer(pattern, text, re.IGNORECASE):
                matches.append({
                    "detector": f"custom_{index + 1}",
                    "label": "[CUSTOM]",
                    "severity": "high",
                    "fingerprint": _fingerprint(found.group(0)),
                })
        except re.error:
            continue

    severity = None
    if matches:
        severity = sorted(matches, key=lambda m: _SEVERITY_RANK.get(m["severity"], 9))[0]["severity"]
    return {"found": bool(matches), "severity": severity, "matches": matches}


def redact(text: str, cfg: dict) -> str:
    """Replace every detected value with its labelled placeholder. Safe to
    call on text with nothing to redact - it returns the input unchanged."""
    if not text or not cfg.get("enabled"):
        return text

    result = text
    for detector in _active_detectors(cfg):
        def _replace(match, _d=detector):
            value = match.group(0)
            if _d.validator and not _d.validator(value):
                return value
            return f"{_d.label[:-1]}:{_fingerprint(value)}]"

        result = detector.regex.sub(_replace, result)

    for pattern in cfg.get("custom_patterns") or []:
        try:
            result = re.sub(
                pattern,
                lambda m: f"[CUSTOM:{_fingerprint(m.group(0))}]",
                result,
                flags=re.IGNORECASE,
            )
        except re.error:
            continue

    return result


def redact_structure(value, cfg: dict, _depth: int = 0):
    """Redact every string inside a nested dict/list - what tool arguments
    and trace attributes actually are. Depth-bounded because the input is
    caller-supplied and a pathological nesting should cost a truncated
    redaction, not a recursion error."""
    if _depth > 6:
        return value
    if isinstance(value, str):
        return redact(value, cfg)
    if isinstance(value, dict):
        return {k: redact_structure(v, cfg, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_structure(v, cfg, _depth + 1) for v in value]
    return value


# ---------------------------------------------------------------------------
# Inbound injection screening
# ---------------------------------------------------------------------------

def scan_injection(text: str, cfg: dict) -> dict:
    """Run the existing pattern bank against caller-supplied text. Reuses
    hijack_detection rather than forking a second bank, so a pattern added
    for poisoned tool descriptions immediately also covers prompts."""
    if not text or not cfg.get("enabled"):
        return {"flagged": False, "severity": None, "signals": []}
    return hijack_detection.scan_response_payload(text)


def should_block(severity: str | None, cfg: dict) -> bool:
    """Whether an injection signal at `severity` is refused outright. `off`
    (the default) records without refusing."""
    level = cfg.get("block_injection_at", "off")
    if level == "off" or not severity:
        return False
    return _SEVERITY_RANK.get(severity, 9) <= _SEVERITY_RANK.get(level, 9)


def inspect_input(text: str, cfg: dict, *, scan_injection_too: bool = True) -> dict:
    """One pass over inbound text: what personal data is in it, whether it
    carries an injection signal, whether that signal should stop the call,
    and the redacted form to persist. The single entry point the routes
    use, so prompts and tool arguments cannot drift apart on what they
    check."""
    pii = scan_pii(text, cfg)
    injection = scan_injection(text, cfg) if scan_injection_too else {
        "flagged": False, "severity": None, "signals": []
    }
    return {
        "pii": pii,
        "injection": injection,
        "blocked": should_block(injection.get("severity"), cfg),
        "redacted": redact(text, cfg) if pii["found"] else text,
    }


def summarize(result: dict) -> str:
    """A short human-readable reason, for an audit-log entry or a 400."""
    parts = []
    if result["pii"]["found"]:
        kinds = sorted({m["detector"] for m in result["pii"]["matches"]})
        parts.append(f"personal data detected ({', '.join(kinds)})")
    if result["injection"]["flagged"]:
        patterns = sorted({s["pattern_id"] for s in result["injection"]["signals"]})
        parts.append(
            f"possible prompt injection ({result['injection']['severity']}: {', '.join(patterns)})"
        )
    return "; ".join(parts) or "no guardrail findings"
