import Link from "next/link";
import { api } from "@/lib/api";
import { count, euro, hours, percent, severityClass } from "@/lib/format";

export const dynamic = "force-dynamic";

export default async function OverviewPage() {
  const overview = await api.overview();

  if (!overview) {
    return (
      <>
        <h1>Overview</h1>
        <div className="empty">
          <p>The API is not reachable.</p>
          <p className="muted">
            Start the backend with <code>docker compose up</code>, or run{" "}
            <code>uvicorn app.main:app --reload</code> inside <code>backend/</code>.
          </p>
        </div>
      </>
    );
  }

  return (
    <>
      <h1>Overview</h1>
      <p className="subtitle">
        {count(overview.case_count)} cases reconstructed from {count(overview.event_count)}{" "}
        events across {overview.process_count} process(es).
      </p>

      <div className="grid">
        <div className="card">
          <div className="card-label">Recoverable time</div>
          <div className="card-value">{hours(overview.recoverable_hours_per_month)}</div>
          <div className="card-note">
            per month, {euro(overview.recoverable_eur_per_month)} at the configured hourly cost
          </div>
        </div>
        <div className="card">
          <div className="card-label">Open findings</div>
          <div className="card-value">{overview.open_findings}</div>
          <div className="card-note">ranked by measured time impact</div>
        </div>
        <div className="card">
          <div className="card-label">Cases analysed</div>
          <div className="card-value">{count(overview.case_count)}</div>
          <div className="card-note">{count(overview.event_count)} canonical events</div>
        </div>
        <div className="card">
          <div className="card-label">Processes getting worse</div>
          <div className="card-value">{overview.worsening_processes.length}</div>
          <div className="card-note">cycle time up between window halves</div>
        </div>
      </div>

      {overview.top_finding && (
        <>
          <h2>Highest-impact bottleneck</h2>
          <div className="card">
            <div className="row" style={{ justifyContent: "space-between" }}>
              <h3>{overview.top_finding.title}</h3>
              <span className={severityClass(overview.top_finding.severity)}>
                {overview.top_finding.severity}
              </span>
            </div>
            <p className="muted" style={{ margin: "6px 0 12px" }}>
              {count(overview.top_finding.affected_case_count)} cases affected ·{" "}
              {hours(overview.top_finding.impact_hours_per_month)} per month · confidence{" "}
              {percent(overview.top_finding.confidence)}
            </p>
            <Link href="/findings" className="pill">
              Open findings inbox
            </Link>
          </div>
        </>
      )}

      {overview.top_opportunity && (
        <>
          <h2>Top automation candidate</h2>
          <div className="card">
            <h3>{overview.top_opportunity.activity_name}</h3>
            <p className="muted" style={{ margin: "6px 0 12px" }}>
              Score {overview.top_opportunity.score} ·{" "}
              {hours(overview.top_opportunity.estimated_hours_per_month)} per month ·{" "}
              {euro(overview.top_opportunity.estimated_eur_per_month)} per month
            </p>
            <p style={{ margin: 0 }}>{overview.top_opportunity.recommendation.detail}</p>
          </div>
        </>
      )}

      {overview.worsening_processes.length > 0 && (
        <>
          <h2>Worsening performance</h2>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Finding</th>
                  <th>Change</th>
                </tr>
              </thead>
              <tbody>
                {overview.worsening_processes.map((item) => (
                  <tr key={item.process_id + item.title}>
                    <td>{item.title}</td>
                    <td>{item.change_pct !== null ? percent(item.change_pct) : "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  );
}
