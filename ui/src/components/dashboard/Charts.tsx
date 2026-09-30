/** Small dependency-free SVG charts, styled to match the appliance's theme. */

function compactNumber(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

export function BarChart({
  data,
  labelKey = "label",
  valueKey = "value",
  color = "#2dd4c8",
  height = 220,
}: {
  data: Array<Record<string, any>>;
  labelKey?: string;
  valueKey?: string;
  color?: string;
  height?: number;
}) {
  const max = Math.max(1, ...data.map((d) => Number(d[valueKey]) || 0));
  const barWidth = 100 / data.length;

  return (
    <div style={{ width: "100%" }}>
      <svg viewBox={`0 0 100 ${height}`} preserveAspectRatio="none" style={{ width: "100%", height: height - 24, display: "block" }}>
        {[0, 0.25, 0.5, 0.75, 1].map((f) => (
          <line
            key={f}
            x1={0}
            x2={100}
            y1={(height - 24) * (1 - f)}
            y2={(height - 24) * (1 - f)}
            stroke="var(--border-soft)"
            strokeWidth={0.3}
          />
        ))}
        {data.map((d, i) => {
          const v = Number(d[valueKey]) || 0;
          const barH = max > 0 ? (v / max) * (height - 30) : 0;
          const x = i * barWidth + barWidth * 0.18;
          const w = barWidth * 0.64;
          return (
            <rect
              key={i}
              x={x}
              y={height - 24 - barH}
              width={w}
              height={barH}
              rx={1.2}
              fill={color}
              opacity={v === 0 ? 0.15 : 0.9}
            />
          );
        })}
      </svg>
      <div style={{ display: "flex", marginTop: 6 }}>
        {data.map((d, i) => (
          <div
            key={i}
            style={{
              width: `${barWidth}%`,
              textAlign: "center",
              fontSize: 10.5,
              color: "var(--text-faint)",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {d[labelKey]}
          </div>
        ))}
      </div>
    </div>
  );
}

export function DonutChart({
  segments,
  size = 168,
}: {
  segments: Array<{ label: string; value: number; color: string }>;
  size?: number;
}) {
  const total = segments.reduce((a, s) => a + s.value, 0);
  const radius = 42;
  const stroke = 15;
  const circumference = 2 * Math.PI * radius;
  let offsetAcc = 0;

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 22 }}>
      <svg width={size} height={size} viewBox="0 0 100 100">
        <circle cx={50} cy={50} r={radius} fill="none" stroke="var(--border-soft)" strokeWidth={stroke} />
        {total > 0 &&
          segments
            .filter((s) => s.value > 0)
            .map((s, i) => {
              const frac = s.value / total;
              const dash = frac * circumference;
              const gap = circumference - dash;
              const rotation = (offsetAcc / total) * 360 - 90;
              offsetAcc += s.value;
              return (
                <circle
                  key={i}
                  cx={50}
                  cy={50}
                  r={radius}
                  fill="none"
                  stroke={s.color}
                  strokeWidth={stroke}
                  strokeDasharray={`${dash} ${gap}`}
                  strokeLinecap="butt"
                  transform={`rotate(${rotation} 50 50)`}
                />
              );
            })}
        <text x="50" y="47" textAnchor="middle" fontSize="15" fontWeight={700} fill="var(--text)">
          {compactNumber(total)}
        </text>
        <text x="50" y="60" textAnchor="middle" fontSize="7" fill="var(--text-faint)">
          total
        </text>
      </svg>
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {segments.map((s, i) => (
          <div key={i} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12.5 }}>
            <span style={{ width: 9, height: 9, borderRadius: 2, background: s.color, display: "inline-block" }} />
            <span style={{ color: "var(--text-muted)" }}>{s.label}</span>
            <span style={{ marginLeft: "auto", fontWeight: 700 }}>{compactNumber(s.value)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

/** Stacked bars - used for cache hits vs misses per hour, where the total
 * height is "requests that could have been cached" and the split is how
 * many of them never reached a provider. */
export function StackedBarChart({
  data,
  height = 220,
  legend = [],
}: {
  data: Array<{ label: string; segments: Array<{ value: number; color: string }> }>;
  height?: number;
  legend?: Array<{ label: string; color: string }>;
}) {
  const totals = data.map((d) => d.segments.reduce((a, s) => a + (Number(s.value) || 0), 0));
  const max = Math.max(1, ...totals);
  const barWidth = 100 / Math.max(1, data.length);
  const plotH = height - 24;

  return (
    <div style={{ width: "100%" }}>
      <svg viewBox={`0 0 100 ${plotH}`} preserveAspectRatio="none" style={{ width: "100%", height: plotH, display: "block" }}>
        {[0, 0.25, 0.5, 0.75, 1].map((f) => (
          <line key={f} x1={0} x2={100} y1={plotH * (1 - f)} y2={plotH * (1 - f)} stroke="var(--border-soft)" strokeWidth={0.3} />
        ))}
        {data.map((d, i) => {
          const x = i * barWidth + barWidth * 0.18;
          const w = barWidth * 0.64;
          let yCursor = plotH;
          return (
            <g key={i}>
              {d.segments.map((s, j) => {
                const v = Number(s.value) || 0;
                const h = max > 0 ? (v / max) * (plotH - 6) : 0;
                yCursor -= h;
                return <rect key={j} x={x} y={yCursor} width={w} height={h} rx={1.2} fill={s.color} opacity={v === 0 ? 0.12 : 0.9} />;
              })}
            </g>
          );
        })}
      </svg>
      <div style={{ display: "flex", marginTop: 6 }}>
        {data.map((d, i) => (
          <div
            key={i}
            style={{
              width: `${barWidth}%`,
              textAlign: "center",
              fontSize: 10.5,
              color: "var(--text-faint)",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {d.label}
          </div>
        ))}
      </div>
      {legend.length > 0 && (
        <div style={{ display: "flex", gap: 16, marginTop: 12, flexWrap: "wrap" }}>
          {legend.map((l) => (
            <div key={l.label} style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 12.5 }}>
              <span style={{ width: 9, height: 9, borderRadius: 2, background: l.color, display: "inline-block" }} />
              <span style={{ color: "var(--text-muted)" }}>{l.label}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
