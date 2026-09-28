"""
Tool-definition versioning and drift ("rug pull") detection.

The metadata scanner in app/hijack_detection.py answers "does this
description match a known-bad pattern?". It cannot answer "is this the same
description an admin actually reviewed?" - and those are different
questions. A rug pull is a tool that passed review and then *changed*: the
server it came from returns a different description, a different input
schema, or a quietly widened set of parameters on the next `tools/list`.
Nothing about the new definition has to look malicious to a pattern bank
for the change itself to be the thing worth blocking on, because the
approval an operator gave applied to text that no longer exists.

So this module keeps a fingerprint of the definition that was approved and
compares every subsequent ingest against it:

  - `definition_fingerprint()` hashes the parts of a tool that determine
    what an LLM will believe about it and what a caller may pass: name,
    description, and the full input schema including every property
    description. Cosmetic fields (risk level, allowed roles, embeddings)
    are deliberately excluded - those are *our* metadata, not the server's
    claim about itself, and changing them is a local policy decision rather
    than upstream drift.

  - `evaluate_drift()` compares a freshly-listed definition against the
    stored approved fingerprint and classifies what moved. A description or
    schema change on an already-approved tool re-quarantines it: the safe
    default is that a changed tool is an unreviewed tool. A brand-new tool
    has nothing to drift from and is left to the normal ingest path.

  - `record_version()` keeps a bounded history of the definitions a tool
    has presented, so the Threat Detection page can show an operator what
    the tool used to claim it did next to what it claims now, which is the
    only way to judge whether a diff is benign.

An admin's explicit release (`hijack_manual_override`) re-approves the
*current* definition rather than merely clearing a flag - otherwise the
next ingest would immediately re-quarantine on the same diff, and the
operator's decision would be meaningless.
"""
import difflib
import hashlib
import json
import time

# How many historical definitions to keep per tool. Bounded because this
# rides inside the tool document itself - a tool whose upstream server
# rewrites its description hourly must not grow an unbounded document.
MAX_VERSION_HISTORY = 10

# What a diff is called, worst-first. `schema` outranks `description`
# because a widened input schema changes what a caller may actually send,
# whereas a description change only changes what the model believes.
DRIFT_KINDS = ("schema", "description", "name")


def _canonical_schema(schema) -> str:
    try:
        return json.dumps(schema or {}, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return str(schema)


def definition_fingerprint(tool: dict) -> str:
    """A stable hash of everything the *upstream server* asserts about a
    tool. Two definitions with the same fingerprint are interchangeable as
    far as review is concerned."""
    material = "\n".join([
        str(tool.get("name") or ""),
        str(tool.get("description") or ""),
        _canonical_schema(tool.get("input_schema")),
    ])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def definition_snapshot(tool: dict) -> dict:
    """The reviewable form of a definition - what gets stored in history so
    an operator can read the old text, not just a hash of it."""
    return {
        "fingerprint": definition_fingerprint(tool),
        "name": tool.get("name"),
        "description": tool.get("description") or "",
        "input_schema": tool.get("input_schema") or {},
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _schema_property_names(schema) -> set:
    props = ((schema or {}).get("properties") or {})
    return set(props.keys()) if isinstance(props, dict) else set()


def describe_changes(previous: dict, current: dict) -> list[dict]:
    """Classify what actually moved between two definitions. Returns one
    entry per changed facet, worst-first, each carrying enough context for
    the Threat Detection page to explain the change without re-deriving
    it."""
    changes: list[dict] = []

    prev_schema = previous.get("input_schema") or {}
    cur_schema = current.get("input_schema") or {}
    if _canonical_schema(prev_schema) != _canonical_schema(cur_schema):
        before_props = _schema_property_names(prev_schema)
        after_props = _schema_property_names(cur_schema)
        added = sorted(after_props - before_props)
        removed = sorted(before_props - after_props)
        detail = "input schema changed"
        if added:
            detail += f"; new parameter(s): {', '.join(added)}"
        if removed:
            detail += f"; removed parameter(s): {', '.join(removed)}"
        changes.append({
            "kind": "schema",
            "detail": detail,
            "added_properties": added,
            "removed_properties": removed,
        })

    prev_desc = str(previous.get("description") or "")
    cur_desc = str(current.get("description") or "")
    if prev_desc != cur_desc:
        changes.append({
            "kind": "description",
            "detail": (
                f"description changed ({len(prev_desc)} -> {len(cur_desc)} characters)"
            ),
            "diff": summarize_diff(prev_desc, cur_desc),
        })

    if str(previous.get("name") or "") != str(current.get("name") or ""):
        changes.append({
            "kind": "name",
            "detail": f"name changed from '{previous.get('name')}' to '{current.get('name')}'",
        })

    changes.sort(key=lambda c: DRIFT_KINDS.index(c["kind"]) if c["kind"] in DRIFT_KINDS else 99)
    return changes


def summarize_diff(before: str, after: str, max_lines: int = 20) -> list[str]:
    """A compact unified diff of two descriptions, for display. Truncated -
    the point is to let an operator see *what* changed at a glance, with
    the full previous text available in the version history if they need
    it."""
    diff = difflib.unified_diff(
        (before or "").splitlines() or [""],
        (after or "").splitlines() or [""],
        lineterm="", n=1,
    )
    lines = [line for line in diff if not line.startswith(("---", "+++", "@@"))]
    lines = [line[:300] for line in lines if line.strip() not in ("", "-", "+")]
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... {len(lines) - max_lines} more line(s)"]
    return lines


def evaluate_drift(tool_doc: dict, existing: dict | None) -> dict:
    """Compare a freshly-listed definition against the approved one.

    Returns {drifted, changes, approved_fingerprint, current_fingerprint}.
    `drifted` is only ever True for a tool that was *previously approved* -
    a first ingest establishes the baseline rather than reporting drift
    against nothing.
    """
    current_fp = definition_fingerprint(tool_doc)
    approved_fp = (existing or {}).get("approved_fingerprint")
    approved_def = (existing or {}).get("approved_definition")

    if not existing or not approved_fp:
        return {
            "drifted": False,
            "changes": [],
            "approved_fingerprint": approved_fp,
            "current_fingerprint": current_fp,
        }

    if approved_fp == current_fp:
        return {
            "drifted": False,
            "changes": [],
            "approved_fingerprint": approved_fp,
            "current_fingerprint": current_fp,
        }

    baseline = approved_def or {
        "name": existing.get("name"),
        "description": existing.get("description"),
        "input_schema": existing.get("input_schema"),
    }
    return {
        "drifted": True,
        "changes": describe_changes(baseline, tool_doc),
        "approved_fingerprint": approved_fp,
        "current_fingerprint": current_fp,
    }


def record_version(tool_doc: dict, existing: dict | None) -> dict:
    """Append the current definition to the tool's bounded version history,
    if it differs from the most recent entry. Mutates and returns
    `tool_doc`."""
    history = list((existing or {}).get("definition_history") or [])
    snapshot = definition_snapshot(tool_doc)
    if not history or history[-1].get("fingerprint") != snapshot["fingerprint"]:
        history.append(snapshot)
    tool_doc["definition_history"] = history[-MAX_VERSION_HISTORY:]
    tool_doc["definition_version"] = len(tool_doc["definition_history"])
    return tool_doc


def approve_current_definition(tool_doc: dict) -> dict:
    """Pin the current definition as the approved baseline. Called on first
    ingest (nothing to compare against yet) and whenever an admin
    explicitly releases a tool from the Threat Detection page - a release
    that did not move the baseline would be undone by the very next scan.
    Mutates and returns `tool_doc`."""
    tool_doc["approved_fingerprint"] = definition_fingerprint(tool_doc)
    tool_doc["approved_definition"] = {
        "name": tool_doc.get("name"),
        "description": tool_doc.get("description") or "",
        "input_schema": tool_doc.get("input_schema") or {},
    }
    tool_doc["approved_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tool_doc["drift_status"] = "clear"
    tool_doc["drift_changes"] = []
    return tool_doc


def apply_drift_scan(tool_doc: dict, existing: dict | None) -> dict:
    """The ingest-time counterpart to hijack_detection.apply_metadata_scan.

    Runs *after* the metadata scan so an explicit admin override still
    wins: an operator who released a tool released the definition they were
    looking at, and `approve_current_definition` moved the baseline at that
    moment, so a subsequent unchanged ingest produces no drift at all.
    A tool that has genuinely changed since approval is quarantined
    regardless of how its server is trusted - the same strictness metadata
    poisoning gets, and for the same reason: an unreviewed definition
    should not be discoverable while it waits for review.

    Mutates and returns `tool_doc`.
    """
    verdict = evaluate_drift(tool_doc, existing)
    record_version(tool_doc, existing)

    tool_doc["approved_fingerprint"] = verdict["approved_fingerprint"]
    tool_doc["approved_definition"] = (existing or {}).get("approved_definition")
    tool_doc["approved_at"] = (existing or {}).get("approved_at")
    tool_doc["definition_fingerprint"] = verdict["current_fingerprint"]

    if not verdict["approved_fingerprint"]:
        # First time this tool has ever been ingested: establish the
        # baseline. Whatever the metadata scanner already decided about it
        # stands - a first-ingest tool that matched a poisoning pattern is
        # still quarantined by that scan, it just has no drift on top.
        approve_current_definition(tool_doc)
        return tool_doc

    if not verdict["drifted"]:
        tool_doc["drift_status"] = "clear"
        tool_doc["drift_changes"] = []
        return tool_doc

    tool_doc["drift_status"] = "drifted"
    tool_doc["drift_changes"] = verdict["changes"]
    tool_doc["drift_detected_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # A manual release approved a specific definition. This is no longer
    # that definition, so the override does not carry over to it - clear it
    # and quarantine, or the "always allow" the operator granted once would
    # silently apply to text they never saw.
    tool_doc.pop("hijack_manual_override", None)
    tool_doc["trust_status"] = "quarantined"
    return tool_doc


def drift_severity(changes: list[dict]) -> str:
    """Schema drift is `critical` (it changes what a caller may send);
    anything else is `high` (it changes what the model believes)."""
    kinds = {c.get("kind") for c in changes or []}
    return "critical" if "schema" in kinds else "high"
