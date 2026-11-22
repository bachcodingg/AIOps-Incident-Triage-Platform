// The console's view of aiops/loop/api.py. Times are unix seconds.

export interface Config {
  dry_run: boolean;
  variant: string;
  grafana: string;
  loop: boolean;
  audit_ok: boolean;
  audit: string;
}

export interface IncidentRow {
  id: string;
  opened: number;
  closed: number | null;
  status: string;
  components: string[];
  alerts: number;
  root_cause: string | null;
  action: string | null;
  action_target: string | null;
  pending: boolean;
}

export interface Alert {
  detector: string;
  name: string;
  component: string;
  start: string;
  end: string | null;
  severity: string;
  labels: Record<string, string>;
  score: number | null;
}

export interface Hypothesis {
  component: string;
  category: string;
  confidence: number;
  reason: string;
}

export interface Evidence {
  tool: string;
  query: string;
  finding: string;
}

export interface Verdict {
  hypotheses: Hypothesis[];
  evidence: Evidence[];
  action: string;
  action_target: string | null;
  confidence: number;
  summary: string;
  fallback?: string;
}

export interface Proposal {
  id: number;
  action: string;
  target: string | null;
  mode: string;
  reason: string;
  status: string;
  decided_by: string | null;
  decided_at: number | null;
  created: number;
  plan: { how: string; commands: string[]; before: Record<string, unknown> } | null;
  result: { steps?: { step: string; ok: boolean; output: string; dry_run: boolean }[]; reason?: string } | null;
}

export interface AuditEntry {
  seq: number;
  at: number;
  actor: string;
  event: string;
  incident: string | null;
  data: Record<string, unknown>;
  hash: string;
}

export interface IncidentDetail {
  id: string;
  opened: number;
  closed: number | null;
  status: string;
  components: string[];
  alerts: Alert[];
  verdict: Verdict | null;
  proposals: Proposal[];
  timeline: AuditEntry[];
  grafana: { title: string; url: string }[];
  tool_calls: { name: string; input: unknown; error: boolean; output: string }[];
  run: { variant: string; model: string; valid: boolean; attempts: number; cost_usd: number; seconds: number } | null;
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...init });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : `${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
}

export const api = {
  config: () => call<Config>("/api/config"),
  incidents: () => call<IncidentRow[]>("/api/incidents"),
  incident: (id: string) => call<IncidentDetail>(`/api/incidents/${encodeURIComponent(id)}`),
  audit: () => call<{ ok: boolean; message: string; entries: AuditEntry[] }>("/api/audit"),
  approve: (id: number, by: string) =>
    call<Proposal>(`/api/proposals/${id}/approve`, { method: "POST", body: JSON.stringify({ by }) }),
  reject: (id: number, by: string, reason: string) =>
    call<Proposal>(`/api/proposals/${id}/reject`, { method: "POST", body: JSON.stringify({ by, reason }) }),
};
