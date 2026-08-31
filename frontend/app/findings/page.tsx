import { api } from "@/lib/api";
import { count, hours, percent, severityClass, severityOrder } from "@/lib/format";

export const dynamic = "force-dynamic";

const TYPE_LABELS: Record<string, string> = {
  excessive_waiting: "Waiting time",
  repeated_activity: "Rework loop",
  high_handoff_count: "Fragmented ownership",
  rare_expensive_variant: "Expensive path",
  worsening_cycle_time: "Negative trend",
  high_manual_repetition: "Manual repetition",
  cycle_time_outliers: "Outlier cases",
};

export default async function FindingsPage() {
  const findings = await api.findings();

  if (!findings || findings.length === 0) {
    return (
      <>
        <h1>Findings</h1>
        <div className="empty">
          No finding crossed the detection thresholds. Import more history, or lower the
          thresholds in the finding engine configuration.
        </div>
      </>
    );
  }

  const sorted = [...findings].sort(
    (a, b) =>
      severityOrder.indexOf(a.severity) - severityOrder.indexOf(b.severity) ||
      b.impact_score - a.impact_score,
  );

  return (
    <>
      <h1>Findings</h1>
      <p className="subtitle">
        Each finding is backed by measured evidence: the metric, the baseline it was compared
        against, and how many cases it affects.
      </p>

      {sorted.map((finding) => (
        <div className="card" key={finding.id} style={{ marginBottom: 14 }}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <h3>{finding.title}</h3>
            <div className="row">
              <span className="pill">
                {TYPE_LABELS[finding.finding_type] ?? finding.finding_type}
              </span>
              <span className={severityClass(finding.severity)}>{finding.severity}</span>
            </div>
          </div>

          <p className="muted" style={{ margin: "4px 0 12px" }}>
            {count(finding.affected_case_count)} cases ·{" "}
            {hours(finding.impact_hours_per_month)} per month · impact score{" "}
            {finding.impact_score} · confidence {percent(finding.confidence)}
          </p>

          <details>
            <summary className="muted" style={{ cursor: "pointer", fontSize: 13 }}>
              Evidence
            </summary>
            <pre className="evidence">{JSON.stringify(finding.evidence, null, 2)}</pre>
          </details>

          {finding.narrative && Object.keys(finding.narrative).length > 0 && (
            <details>
              <summary className="muted" style={{ cursor: "pointer", fontSize: 13 }}>
                Narrative
              </summary>
              <pre className="evidence">{JSON.stringify(finding.narrative, null, 2)}</pre>
            </details>
          )}
        </div>
      ))}
    </>
  );
}
