// Pure helpers, tested in format.test.ts.

export function ago(t: number, now: number = Date.now() / 1000): string {
  const s = Math.max(0, Math.round(now - t));
  if (s < 90) return `${s}s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 2 * 86400) return `${(s / 3600).toFixed(1)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

export function duration(seconds: number): string {
  if (seconds < 90) return `${Math.round(seconds)} s`;
  return `${(seconds / 60).toFixed(1)} min`;
}

export function clock(t: number): string {
  return new Date(t * 1000).toISOString().replace("T", " ").slice(0, 19) + "Z";
}

// What an incident's status means for the operator, and how loud to show it.
export const STATUS: Record<string, { label: string; tone: "act" | "wait" | "ok" | "warn" | "off" }> = {
  open: { label: "open", tone: "wait" },
  triaged: { label: "triaged", tone: "wait" },
  "awaiting-approval": { label: "needs approval", tone: "act" },
  acting: { label: "acting", tone: "wait" },
  acted: { label: "acted", tone: "ok" },
  escalated: { label: "escalated to a human", tone: "warn" },
  rejected: { label: "rejected", tone: "off" },
  resolved: { label: "resolved", tone: "ok" },
};

export function status(s: string) {
  return STATUS[s] ?? { label: s, tone: "off" as const };
}

export function actionText(action: string | null, target: string | null): string {
  if (!action) return "–";
  const words: Record<string, string> = {
    rollback: "roll back",
    restart: "restart",
    scale_out: "scale out",
    none_escalate: "escalate to a human",
    none_verify: "verify it recovers",
  };
  return [words[action] ?? action, target].filter(Boolean).join(" ");
}

// One line per audit entry, for the incident's timeline.
export function describe(e: { event: string; actor: string; data: Record<string, unknown> }): string {
  const d = e.data;
  switch (e.event) {
    case "incident-opened":
      return `opened: ${Array.isArray(d.alerts) ? d.alerts.join(", ") : (d.components as string[] | undefined)?.join(", ") ?? ""}`;
    case "triaged":
      return `${e.actor} (${d.variant ?? "?"}): ${d.root_cause}, proposes ${actionText(d.action as string, (d.target as string) ?? null)}`;
    case "proposed":
      return `${d.mode === "auto" ? "runs automatically" : "waits for one approval"}: ${d.reason ?? ""}`;
    case "approved":
      return `approved by ${e.actor}`;
    case "rejected":
      return `rejected by ${e.actor}${d.reason ? `: ${d.reason}` : ""}`;
    case "executed":
      return `done${d.dry_run ? " (dry run: nothing changed)" : ""}, approved by ${d.by}`;
    case "failed":
      return `FAILED: ${JSON.stringify(d.steps)}`;
    case "not-run":
    case "unplannable":
    case "refused":
    case "escalated":
      return `${e.event}: ${d.reason ?? ""}`;
    case "resolved":
      return `resolved after ${duration(Number(d.time_to_recover_s ?? 0))}`;
    default:
      return e.event;
  }
}
