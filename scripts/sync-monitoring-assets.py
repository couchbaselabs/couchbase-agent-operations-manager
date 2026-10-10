#!/usr/bin/env python3
"""Copy the monitoring assets the Helm chart embeds via .Files.Get into
the chart directory, so docker-compose.yml's monitoring profile and the
chart (templates/prometheusrule.yaml, templates/configmap-grafana-dashboard.yaml)
serve the same alert rules and Grafana dashboard without hand-copying.

Helm can only read files inside the chart, hence the copies. Run after
any change to monitoring/prometheus/aom-alerts.yml or
monitoring/grafana/dashboards/aom-overview.json:

    python3 scripts/sync-monitoring-assets.py
"""
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHART = ROOT / "helm" / "couchbase-agent-operations-manager"
COPIES = {
    ROOT / "monitoring" / "prometheus" / "aom-alerts.yml": CHART / "monitoring" / "aom-alerts.yml",
    ROOT / "monitoring" / "grafana" / "dashboards" / "aom-overview.json": CHART / "monitoring" / "aom-overview.json",
}


def main() -> None:
    (CHART / "monitoring").mkdir(exist_ok=True)
    for src, dest in COPIES.items():
        if src.suffix == ".json":
            json.loads(src.read_text())  # fail here, not at helm install
        shutil.copyfile(src, dest)
        print(f"wrote {dest.relative_to(ROOT)}")
    readme = CHART / "monitoring" / "README.md"
    readme.write_text(
        "GENERATED copies - do not edit here. Sources are monitoring/prometheus/aom-alerts.yml "
        "and monitoring/grafana/dashboards/aom-overview.json at the repo root; regenerate with "
        "scripts/sync-monitoring-assets.py.\n"
    )


if __name__ == "__main__":
    main()
