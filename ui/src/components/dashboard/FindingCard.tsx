import type { Finding } from "../../api/types";
import { SeverityBadge, TagBadge } from "../badges/Badges";

export function FindingCard({ finding }: { finding: Finding }) {
  return (
    <div className={`finding-card sev-${finding.severity}`}>
      <h3 className="finding-title">{finding.title}</h3>
      <div className="finding-tags">
        <SeverityBadge severity={finding.severity} solid />
        {finding.tags.map((t) => (
          <TagBadge key={t} label={t} />
        ))}
      </div>
      <p className="finding-summary">{finding.summary}</p>
    </div>
  );
}
