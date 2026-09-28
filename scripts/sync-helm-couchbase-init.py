#!/usr/bin/env python3
"""Regenerate the Helm chart's couchbase-init ConfigMap from
couchbase-init/init.sh, so the two can no longer drift.

They used to be kept in sync by hand, and drifted: the chart's copy was
missing the traces/evals/approvals/knowledge collections and indexes, the
RAM-quota re-assertion, and it used `role`/`namespace` unquoted (both N1QL
reserved words) so three of its covering indexes failed silently on every
install. Run this after any change to couchbase-init/init.sh:

    python3 scripts/sync-helm-couchbase-init.py

Differences from the Docker Compose script, applied here:
  * CB_HOST is the chart's Couchbase Service name, not `couchbase`.
  * The Compose-only "touch a sentinel and idle forever" tail is dropped -
    a Kubernetes Job that exits 0 shows as Completed, which is what we want.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "couchbase-init" / "init.sh"
DEST = ROOT / "helm" / "couchbase-agent-operations-manager" / "templates" / "configmap-couchbase-init.yaml"
TAIL_MARKER = "# Provisioning is idempotent and safe to re-run, so rather than exiting"

HEADER = """{{- if .Values.couchbase.enabled }}
# GENERATED FILE - do not edit by hand. Regenerate from
# couchbase-init/init.sh with scripts/sync-helm-couchbase-init.py.
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ include "aom.fullname" . }}-couchbase-init
  labels:
    {{- include "aom.componentLabels" (merge (dict "component" "couchbase-init") .) | nindent 4 }}
data:
  init.sh: |
"""
FOOTER = "{{- end }}\n"


def main() -> None:
    script = SRC.read_text()
    if "{{" in script or "}}" in script:
        raise SystemExit("init.sh contains '{{' or '}}', which Helm would try to template - escape it first")
    if "\nCB_HOST=couchbase\n" not in script:
        raise SystemExit("could not find the CB_HOST=couchbase line in init.sh")
    script = script.replace(
        "\nCB_HOST=couchbase\n", '\nCB_HOST={{ include "aom.couchbaseServiceName" . }}\n'
    )
    if TAIL_MARKER not in script:
        raise SystemExit("could not find the Compose-only sentinel tail in init.sh")
    script = script[: script.index(TAIL_MARKER)].rstrip() + "\n"
    # The Compose tail ends in `exec tail`, which also hides the exit status
    # of the last `... | grep -q '"errors"' && echo ...` line (non-zero when
    # there was NO error). A Job has to exit 0 explicitly or the Helm
    # post-install/post-upgrade hook is reported as failed.
    script += '\necho "[couchbase-init] Done."\nexit 0\n'
    body = "".join(("    " + line) if line.strip() else "\n" for line in script.splitlines(keepends=True))
    DEST.write_text(HEADER + body + FOOTER)
    print(f"wrote {DEST.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
