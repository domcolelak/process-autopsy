import { api } from "@/lib/api";
import { euro, hours, percent } from "@/lib/format";

export const dynamic = "force-dynamic";

const COMPONENT_LABELS: Record<string, string> = {
  frequency: "Frequency",
  time_cost: "Time cost",
  manuality: "Manual share",
  repeatability: "Repeatability",
  stability: "Stability",
  business_impact: "Business impact",
  confidence: "Confidence",
};

export default async function OpportunitiesPage() {
  const opportunities = await api.opportunities();

  if (!opportunities || opportunities.length === 0) {
    return (
      <>
        <h1>Automation opportunities</h1>
        <div className="empty">
          No activity reached the minimum volume needed for a reliable estimate.
        </div>
      </>
    );
  }

  const total = opportunities.reduce((sum, o) => sum + o.estimated_hours_per_month, 0);

  return (
    <>
      <h1>Automation opportunities</h1>
      <p className="subtitle">
        Ranked by a product of named components. Total across all candidates:{" "}
        <strong>{hours(total)}</strong> per month.
      </p>

      {opportunities.map((opportunity) => {
        const components = Object.entries(opportunity.components).filter(
          ([, value]) => typeof value === "number",
        ) as [string, number][];

        return (
          <div className="card" key={opportunity.id} style={{ marginBottom: 14 }}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <h3>{opportunity.activity_name}</h3>
              <span className="pill">score {opportunity.score}</span>
            </div>

            <p className="muted" style={{ margin: "4px 0 14px" }}>
              {hours(opportunity.estimated_hours_per_month)} per month ·{" "}
              {euro(opportunity.estimated_eur_per_month)} per month · status{" "}
              {opportunity.status}
            </p>

            <p style={{ marginTop: 0 }}>{opportunity.recommendation.detail}</p>

            <table style={{ marginTop: 12 }}>
              <tbody>
                {components.map(([key, value]) => (
                  <tr key={key}>
                    <td style={{ width: 160, color: "var(--muted)", fontSize: 13 }}>
                      {COMPONENT_LABELS[key] ?? key}
                    </td>
                    <td style={{ width: 140 }}>
                      <div className="bar">
                        <span style={{ width: `${Math.min(value * 100, 100)}%` }} />
                      </div>
                    </td>
                    <td style={{ fontSize: 13 }}>{percent(value, 1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>

            {opportunity.recommendation.blockers &&
              opportunity.recommendation.blockers.length > 0 && (
                <p style={{ marginBottom: 0 }}>
                  {opportunity.recommendation.blockers.map((blocker) => (
                    <span className="pill" key={blocker}>
                      {blocker}
                    </span>
                  ))}
                </p>
              )}
          </div>
        );
      })}
    </>
  );
}
