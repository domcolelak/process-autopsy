"use client";

import { useState } from "react";

/**
 * Three-step import wizard: upload, confirm the column mapping, analyse.
 *
 * The mapping step is deliberately explicit. Source schemas differ between
 * companies, so the backend proposes a mapping and the user confirms it -- a
 * silently guessed case identifier would produce a plausible but wrong process.
 */

interface ColumnProfile {
  name: string;
  non_empty: number;
  distinct: number;
  inferred_type: string;
  samples: string[];
  suggested_field: string | null;
}

interface Profile {
  import_id: string;
  row_count: number;
  columns: ColumnProfile[];
  suggested_mapping: Record<string, string>;
  sample_rows: Record<string, string>[];
  warnings: string[];
}

interface ApplyResult {
  process_id: string;
  accepted: number;
  rejected: number;
  errors: { row: number; problems: string[] }[];
  analysis: { case_count?: number; findings?: number; opportunities?: number } | null;
}

const FIELDS: { key: string; label: string; required: boolean; hint: string }[] = [
  { key: "case_id", label: "Case ID", required: true, hint: "What ties events into one case" },
  { key: "activity_name", label: "Activity", required: true, hint: "The step that happened" },
  { key: "occurred_at", label: "Timestamp", required: true, hint: "When the step started" },
  { key: "completed_at", label: "Completed at", required: false, hint: "Enables service time" },
  { key: "actor_id", label: "Actor", required: false, hint: "Who performed the step" },
  { key: "team", label: "Team", required: false, hint: "Used to count handoffs" },
  {
    key: "source_system",
    label: "Source system",
    required: false,
    hint: "Where the event came from",
  },
  { key: "monetary_value", label: "Amount", required: false, hint: "Weights business impact" },
  { key: "is_manual", label: "Manual flag", required: false, hint: "Drives automation scoring" },
];

export default function ImportWizard() {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [processName, setProcessName] = useState("");
  const [slaHours, setSlaHours] = useState("");
  const [result, setResult] = useState<ApplyResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function upload(file: File) {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const form = new FormData();
      form.append("file", file);
      const response = await fetch("/api/proxy/imports", { method: "POST", body: form });
      if (!response.ok) throw new Error(await response.text());
      const data: Profile = await response.json();
      setProfile(data);
      setMapping(data.suggested_mapping);
      setProcessName(file.name.replace(/\.[^.]+$/, ""));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setBusy(false);
    }
  }

  async function apply() {
    if (!profile) return;
    setBusy(true);
    setError(null);
    try {
      const payload = {
        process_name: processName || "Imported process",
        sla_hours: slaHours ? Number(slaHours) : null,
        mapping,
        analyze: true,
      };
      const response = await fetch(`/api/proxy/imports/${profile.import_id}/mapping`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) throw new Error(await response.text());
      setResult((await response.json()) as ApplyResult);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Mapping failed");
    } finally {
      setBusy(false);
    }
  }

  const missingRequired = FIELDS.filter((field) => field.required && !mapping[field.key]);

  return (
    <>
      <div className="card">
        <h3>1 · Upload an event export</h3>
        <p className="muted" style={{ marginTop: 0, fontSize: 13 }}>
          A CSV where each row is one thing that happened: a case identifier, what happened,
          and when. Ticket exports, ERP status logs and CRM activity logs all work.
        </p>
        <input
          type="file"
          accept=".csv,.tsv,text/csv"
          disabled={busy}
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) void upload(file);
          }}
        />
      </div>

      {error && (
        <div className="card" style={{ borderColor: "var(--danger)", marginTop: 14 }}>
          <strong>Import failed</strong>
          <pre className="evidence">{error}</pre>
        </div>
      )}

      {profile && (
        <div className="card" style={{ marginTop: 14 }}>
          <h3>2 · Confirm the mapping</h3>
          <p className="muted" style={{ marginTop: 0, fontSize: 13 }}>
            {profile.row_count} rows read. Column names were matched automatically, so check
            them before continuing.
          </p>

          {profile.warnings.map((warning) => (
            <p key={warning} style={{ color: "var(--warn)", fontSize: 13 }}>
              {warning}
            </p>
          ))}

          <table>
            <thead>
              <tr>
                <th>Field</th>
                <th>Column</th>
                <th>Sample values</th>
              </tr>
            </thead>
            <tbody>
              {FIELDS.map((field) => {
                const column = profile.columns.find((c) => c.name === mapping[field.key]);
                return (
                  <tr key={field.key}>
                    <td>
                      {field.label}
                      {field.required && <span style={{ color: "var(--danger)" }}> *</span>}
                      <div className="muted" style={{ fontSize: 12 }}>
                        {field.hint}
                      </div>
                    </td>
                    <td>
                      <select
                        value={mapping[field.key] ?? ""}
                        onChange={(event) =>
                          setMapping((current) => {
                            const next = { ...current };
                            if (event.target.value) next[field.key] = event.target.value;
                            else delete next[field.key];
                            return next;
                          })
                        }
                      >
                        <option value="">not mapped</option>
                        {profile.columns.map((c) => (
                          <option key={c.name} value={c.name}>
                            {c.name} ({c.inferred_type})
                          </option>
                        ))}
                      </select>
                    </td>
                    <td className="muted" style={{ fontSize: 12 }}>
                      {column ? column.samples.slice(0, 3).join(", ") : "-"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>

          <div className="row" style={{ marginTop: 16 }}>
            <label>
              <div className="muted" style={{ fontSize: 12 }}>
                Process name
              </div>
              <input
                value={processName}
                onChange={(event) => setProcessName(event.target.value)}
                placeholder="Order to delivery"
              />
            </label>
            <label>
              <div className="muted" style={{ fontSize: 12 }}>
                SLA (hours, optional)
              </div>
              <input
                value={slaHours}
                onChange={(event) => setSlaHours(event.target.value)}
                placeholder="72"
                inputMode="numeric"
              />
            </label>
          </div>

          {missingRequired.length > 0 && (
            <p style={{ color: "var(--warn)", fontSize: 13 }}>
              Still needed: {missingRequired.map((f) => f.label).join(", ")}
            </p>
          )}

          <button
            onClick={() => void apply()}
            disabled={busy || missingRequired.length > 0}
            style={{ marginTop: 12 }}
          >
            {busy ? "Analysing..." : "Import and analyse"}
          </button>
        </div>
      )}

      {result && (
        <div className="card" style={{ marginTop: 14 }}>
          <h3>3 · Result</h3>
          <p>
            {result.accepted} events imported, {result.rejected} rejected.
          </p>
          {result.analysis && (
            <p className="muted">
              {result.analysis.case_count} cases reconstructed · {result.analysis.findings}{" "}
              findings · {result.analysis.opportunities} automation candidates.
            </p>
          )}
          {result.errors.length > 0 && (
            <details>
              <summary className="muted" style={{ cursor: "pointer", fontSize: 13 }}>
                Rejected rows
              </summary>
              <pre className="evidence">{JSON.stringify(result.errors, null, 2)}</pre>
            </details>
          )}
          <a className="pill" href={`/processes/${result.process_id}`}>
            Open the process map
          </a>
        </div>
      )}
    </>
  );
}
