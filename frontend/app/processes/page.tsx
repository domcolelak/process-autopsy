import Link from "next/link";
import { api } from "@/lib/api";
import { count, dateTime } from "@/lib/format";

export const dynamic = "force-dynamic";

export default async function ProcessesPage() {
  const processes = await api.processes();

  if (!processes || processes.length === 0) {
    return (
      <>
        <h1>Processes</h1>
        <div className="empty">
          No process has been discovered yet. Upload an event export from the{" "}
          <Link href="/import" className="pill">
            Import data
          </Link>{" "}
          screen.
        </div>
      </>
    );
  }

  return (
    <>
      <h1>Processes</h1>
      <p className="subtitle">
        A process is a set of cases reconstructed from events that share a case identifier.
      </p>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Process</th>
              <th>Cases</th>
              <th>Avg cycle time</th>
              <th>Open findings</th>
              <th>Last analysed</th>
            </tr>
          </thead>
          <tbody>
            {processes.map((process) => (
              <tr key={process.id}>
                <td>
                  <Link href={`/processes/${process.id}`}>
                    <strong>{process.name}</strong>
                  </Link>
                  {process.description && (
                    <div className="muted" style={{ fontSize: 13 }}>
                      {process.description}
                    </div>
                  )}
                </td>
                <td>{count(process.case_count)}</td>
                <td>{process.median_throughput_hours.toFixed(1)} h</td>
                <td>{process.open_findings}</td>
                <td className="muted">{dateTime(process.last_analyzed_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
