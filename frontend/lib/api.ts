/**
 * Typed client for the Process Autopsy API.
 *
 * All calls run on the Next.js server (React server components), so the tenant
 * API key never reaches the browser.
 */

const BASE_URL = process.env.API_BASE_URL ?? "http://localhost:8000";
const API_KEY = process.env.API_KEY ?? "";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${BASE_URL}/v1${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(API_KEY ? { "X-API-Key": API_KEY } : {}),
      ...(init.headers ?? {}),
    },
    // Analytics results change whenever an analysis runs, so never serve stale.
    cache: "no-store",
  });

  if (!response.ok) {
    const detail = await response.text();
    throw new ApiError(`${path} failed: ${detail.slice(0, 300)}`, response.status);
  }
  return (await response.json()) as T;
}

/** Returns `null` instead of throwing when the backend is not running. */
export async function tryRequest<T>(path: string): Promise<T | null> {
  try {
    return await request<T>(path);
  } catch {
    return null;
  }
}

// --- types ---------------------------------------------------------------

export interface ProcessListItem {
  id: string;
  name: string;
  description: string;
  sla_hours: number | null;
  last_analyzed_at: string | null;
  case_count: number;
  open_findings: number;
  median_throughput_hours: number;
}

export interface Finding {
  id: string;
  process_id: string;
  finding_type: string;
  title: string;
  severity: "low" | "medium" | "high" | "critical";
  evidence: Record<string, unknown>;
  affected_case_count: number;
  metric_value: number;
  baseline_value: number | null;
  impact_hours_per_month: number;
  impact_score: number;
  confidence: number;
  detected_at: string;
  status: string;
  narrative: Record<string, unknown>;
}

export interface Opportunity {
  id: string;
  process_id: string;
  activity_name: string;
  score: number;
  components: Record<string, number | Record<string, unknown>>;
  estimated_hours_per_month: number;
  estimated_eur_per_month: number;
  recommendation: { approach?: string; detail?: string; blockers?: string[] };
  status: string;
}

export interface Overview {
  process_count: number;
  case_count: number;
  event_count: number;
  open_findings: number;
  recoverable_hours_per_month: number;
  recoverable_eur_per_month: number;
  top_finding: Finding | null;
  top_opportunity: Opportunity | null;
  worsening_processes: { process_id: string; title: string; change_pct: number | null }[];
}

export interface GraphNode {
  activity: string;
  occurrence_count: number;
  case_count: number;
  median_service_seconds: number;
  manual_share: number;
  is_start: boolean;
  is_end: boolean;
}

export interface GraphEdge {
  source: string;
  target: string;
  occurrence_count: number;
  case_count: number;
  median_wait_seconds: number;
  p90_wait_seconds: number;
  total_wait_seconds: number;
  handoff_rate: number;
  is_loop_edge: boolean;
}

export interface ProcessGraph {
  process_id: string;
  case_count: number;
  nodes: GraphNode[];
  edges: GraphEdge[];
  start_activities: Record<string, number>;
  end_activities: Record<string, number>;
}

export interface Variant {
  variant_key: string;
  sequence: string[];
  case_count: number;
  share: number;
  median_throughput_seconds: number;
  mean_throughput_seconds: number;
  sla_breach_rate: number;
  mean_handoffs: number;
  rework_case_ratio: number;
  example_case_ids: string[];
}

export interface ProcessMetrics {
  case_count: number;
  event_count: number;
  variant_count: number;
  throughput: {
    median_seconds: number;
    mean_seconds: number;
    p90_seconds: number;
    p95_seconds: number;
  };
  median_waiting_seconds: number;
  waiting_share: number;
  mean_handoffs: number;
  rework_case_ratio: number;
  sla_breach_rate: number;
  manual_event_share: number;
  window_start: string | null;
  window_end: string | null;
  activities: {
    activity: string;
    occurrence_count: number;
    case_count: number;
    median_service_seconds: number;
    manual_share: number;
    distinct_actors: number;
  }[];
  transitions: {
    source: string;
    target: string;
    occurrence_count: number;
    median_wait_seconds: number;
    p90_wait_seconds: number;
    handoff_rate: number;
  }[];
}

// --- endpoints -----------------------------------------------------------

export const api = {
  overview: () => tryRequest<Overview>("/overview"),
  processes: () => tryRequest<ProcessListItem[]>("/processes"),
  processMap: (id: string, minShare = 0) =>
    tryRequest<ProcessGraph>(`/processes/${id}/map?min_edge_case_share=${minShare}`),
  processVariants: (id: string) => tryRequest<Variant[]>(`/processes/${id}/variants?limit=25`),
  processMetrics: (id: string) => tryRequest<ProcessMetrics>(`/processes/${id}/metrics`),
  findings: (processId?: string) =>
    tryRequest<Finding[]>(`/findings${processId ? `?process_id=${processId}` : ""}`),
  finding: (id: string) => tryRequest<Finding>(`/findings/${id}`),
  opportunities: (processId?: string) =>
    tryRequest<Opportunity[]>(`/opportunities${processId ? `?process_id=${processId}` : ""}`),
  analyze: (id: string) => request<Record<string, unknown>>(`/processes/${id}/analyze`, { method: "POST" }),
  explainFinding: (id: string) => request<Finding>(`/findings/${id}/explain`, { method: "POST" }),
  setFindingStatus: (id: string, status: string) =>
    request<Finding>(`/findings/${id}/status`, {
      method: "POST",
      body: JSON.stringify({ status }),
    }),
  setOpportunityStatus: (id: string, status: string) =>
    request<Opportunity>(`/opportunities/${id}/status`, {
      method: "POST",
      body: JSON.stringify({ status }),
    }),
};
