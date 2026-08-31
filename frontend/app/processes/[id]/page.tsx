import { notFound } from "next/navigation";
import ProcessMap from "@/components/ProcessMap";
import { api } from "@/lib/api";
import { count, duration, percent } from "@/lib/format";

export const dynamic = "force-dynamic";

export default async function ProcessDetailPage({ params }: { params: { id: string } }) {
  const [graph, metrics, variants, findings] = await Promise.all([
    api.processMap(params.id),
    api.processMetrics(params.id),
    api.processVariants(params.id),
    api.findings(params.id),
  ]);

  if (!graph || !metrics) notFound();

  const slowest = [...metrics.transitions]
    .sort((a, b) => b.median_wait_seconds - a.median_wait_seconds)
    .slice(0, 6);

  return (
    <>
      <h1>Process map</h1>
      <p className="subtitle">
        {count(metrics.case_count)} cases · {count(metrics.event_count)} events ·{" "}
        {metrics.variant_count} distinct paths · {findings?.length ?? 0} findings
      </p>

      <div className="grid">
        <div className="card">
          <div className="card-label">Median cycle time</div>
          <div className="card-value">{duration(metrics.throughput.median_seconds)}</div>
          <div className="card-note">p90 {duration(metrics.throughput.p90_seconds)}</div>
        </div>
        <div className="card">
          <div className="card-label">Time spent waiting</div>
          <div className="card-value">{percent(metrics.waiting_share)}</div>
          <div className="card-note">
            median {duration(metrics.median_waiting_seconds)} per case
          </div>
        </div>
        <div className="card">
          <div className="card-label">Handoffs per case</div>
          <div className="card-value">{metrics.mean_handoffs.toFixed(1)}</div>
          <div className="card-note">owner changes between consecutive steps</div>
        </div>
        <div className="card">
          <div className="card-label">Cases with rework</div>
          <div className="card-value">{percent(metrics.rework_case_ratio)}</div>
          <div className="card-note">at least one activity repeated</div>
        </div>
      </div>

      <h2>Discovered flow</h2>
      <p className="muted" style={{ marginTop: -6, fontSize: 13 }}>
        Edge thickness is the share of cases taking that path. Dashed amber edges are loops
        back to an earlier step; dashed amber boxes are mostly-manual activities. Hover any
        element for its measured numbers.
      </p>
      <ProcessMap graph={graph} />

      <h2>Longest queues between steps</h2>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Transition</th>
              <th>Median wait</th>
              <th>p90 wait</th>
              <th>Occurrences</th>
              <th>Handoff rate</th>
            </tr>
          </thead>
          <tbody>
            {slowest.map((transition) => (
              <tr key={`${transition.source}->${transition.target}`}>
                <td>
                  {transition.source} <span className="step-arrow">→</span> {transition.target}
                </td>
                <td>{duration(transition.median_wait_seconds)}</td>
                <td>{duration(transition.p90_wait_seconds)}</td>
                <td>{count(transition.occurrence_count)}</td>
                <td>{percent(transition.handoff_rate)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h2>Most common paths</h2>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Path</th>
              <th>Cases</th>
              <th>Share</th>
              <th>Median cycle time</th>
              <th>Rework</th>
            </tr>
          </thead>
          <tbody>
            {(variants ?? []).slice(0, 10).map((variant) => (
              <tr key={variant.variant_key}>
                <td>
                  <div className="steps">
                    {variant.sequence.map((step, index) => (
                      <span key={`${variant.variant_key}-${index}`}>
                        <span className="pill">{step}</span>
                        {index < variant.sequence.length - 1 && (
                          <span className="step-arrow">→</span>
                        )}
                      </span>
                    ))}
                  </div>
                </td>
                <td>{count(variant.case_count)}</td>
                <td>{percent(variant.share, 1)}</td>
                <td>{duration(variant.median_throughput_seconds)}</td>
                <td>{percent(variant.rework_case_ratio)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
