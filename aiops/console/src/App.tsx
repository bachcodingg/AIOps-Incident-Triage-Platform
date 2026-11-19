import { useCallback, useEffect, useState } from "react";

import { api, type AuditEntry, type Config, type IncidentRow, type Proposal } from "./api";
import { actionText, ago, clock, describe, duration, status } from "./format";

const POLL_MS = 5000;

function usePoll<T>(load: () => Promise<T>, deps: unknown[]): [T | null, string | null, () => void] {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(() => {
    load().then((d) => { setData(d); setError(null); }, (e: Error) => setError(e.message));
  }, deps);
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, POLL_MS);
    return () => clearInterval(t);
  }, [refresh]);
  return [data, error, refresh];
}

function Pill({ value }: { value: string }) {
  const s = status(value);
  return <span className={`pill ${s.tone}`}>{s.label}</span>;
}

export default function App() {
  const [config] = usePoll(api.config, []);
  const [rows, listError, refreshList] = usePoll(api.incidents, []);
  const [selected, setSelected] = useState<string | null>(null);
  const [tab, setTab] = useState<"incidents" | "audit">("incidents");

  useEffect(() => {
    if (!selected && rows && rows.length) setSelected(rows[0].id);
  }, [rows, selected]);

  return (
    <div className="app">
      <header>
        <h1>IncidentLens</h1>
        <nav>
          <button className={tab === "incidents" ? "on" : ""} onClick={() => setTab("incidents")}>Incidents</button>
          <button className={tab === "audit" ? "on" : ""} onClick={() => setTab("audit")}>Audit log</button>
        </nav>
        {config && <Status config={config} />}
      </header>
      {listError && <p className="error">Cannot reach the loop: {listError}</p>}
      {tab === "audit" ? (
        <Audit />
      ) : (
        <main>
          <IncidentList rows={rows ?? []} selected={selected} onSelect={setSelected} />
          {selected ? <Incident id={selected} config={config} onChange={refreshList} /> : <p className="empty">No incidents yet.</p>}
        </main>
      )}
    </div>
  );
}

function Status({ config }: { config: Config }) {
  return (
    <div className="status">
      <span>triage: {config.variant}</span>
      {config.dry_run && <span className="pill warn">dry run: nothing changes</span>}
      {!config.loop && <span className="pill off">loop not running: read only</span>}
      <span className={`pill ${config.audit_ok ? "ok" : "act"}`} title={config.audit}>
        audit chain {config.audit_ok ? "intact" : "BROKEN"}
      </span>
    </div>
  );
}

function IncidentList({ rows, selected, onSelect }: { rows: IncidentRow[]; selected: string | null; onSelect: (id: string) => void }) {
  return (
    <aside>
      {rows.length === 0 && <p className="empty">Nothing has gone wrong since the loop started.</p>}
      <ul>
        {rows.map((r) => (
          <li key={r.id} className={r.id === selected ? "on" : ""} onClick={() => onSelect(r.id)}>
            <div className="row">
              <Pill value={r.status} />
              <span className="muted">{ago(r.opened)}</span>
            </div>
            <div className="id">{r.id}</div>
            <div className="muted">{r.root_cause ?? r.components.join(", ")}</div>
            {r.action && <div>→ {actionText(r.action, r.action_target)}</div>}
          </li>
        ))}
      </ul>
    </aside>
  );
}

function Incident({ id, config, onChange }: { id: string; config: Config | null; onChange: () => void }) {
  const [d, error, refresh] = usePoll(() => api.incident(id), [id]);
  if (error) return <section className="error">{error}</section>;
  if (!d) return <section className="empty">Loading…</section>;
  const v = d.verdict;
  const changed = () => { refresh(); onChange(); };
  return (
    <section className="detail">
      <h2>{d.id} <Pill value={d.status} /></h2>
      <p className="muted">
        opened {clock(d.opened)}
        {d.closed ? `, recovered after ${duration(d.closed - d.opened)}` : ""}; {d.components.join(", ")}
      </p>
      {v && <p className="summary">{v.summary}</p>}
      {d.proposals.map((p) => <ProposalCard key={p.id} p={p} loop={!!config?.loop} onDone={changed} />)}

      {v && v.hypotheses.length > 0 && (
        <>
          <h3>Root cause</h3>
          <ol className="hypotheses">
            {v.hypotheses.map((h, i) => (
              <li key={i}><b>{h.component}</b> / {h.category} <span className="muted">({Math.round(h.confidence * 100)}%)</span><br />{h.reason}</li>
            ))}
          </ol>
        </>
      )}
      {v && v.evidence.length > 0 && (
        <>
          <h3>Evidence</h3>
          <table>
            <thead><tr><th>Tool</th><th>Asked</th><th>Showed</th></tr></thead>
            <tbody>
              {v.evidence.map((e, i) => (
                <tr key={i}><td>{e.tool}</td><td><code>{e.query}</code></td><td>{e.finding}</td></tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      <h3>In Grafana</h3>
      <ul className="links">
        {d.grafana.map((g) => <li key={g.url}><a href={g.url} target="_blank" rel="noreferrer">{g.title}</a></li>)}
      </ul>
      <h3>Timeline</h3>
      <Timeline entries={d.timeline} />
      <h3>Alerts ({d.alerts.length})</h3>
      <ul className="alerts">
        {d.alerts.map((a, i) => (
          <li key={i}><span className="muted">{a.start}</span> {a.detector} <b>{a.name}</b> on {a.component}{a.end ? ` (resolved ${a.end})` : ""}</li>
        ))}
      </ul>
      {d.run && (
        <p className="muted">
          {d.run.variant} {d.run.model}: {d.run.valid ? "valid verdict" : "no valid verdict"} after {d.run.attempts} answer(s),
          {" "}{d.tool_calls.length} tool calls, ${d.run.cost_usd.toFixed(4)}, {duration(d.run.seconds)}
        </p>
      )}
      {d.tool_calls.length > 0 && (
        <details>
          <summary>Tool calls</summary>
          {d.tool_calls.map((c, i) => (
            <div key={i} className="call">
              <code>{c.name}({JSON.stringify(c.input)})</code>{c.error && <span className="pill act">error</span>}
              <pre>{c.output}</pre>
            </div>
          ))}
        </details>
      )}
    </section>
  );
}

function ProposalCard({ p, loop, onDone }: { p: Proposal; loop: boolean; onDone: () => void }) {
  const [by, setBy] = useState(() => localStorage.getItem("approver") ?? "");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const act = (approve: boolean) => {
    localStorage.setItem("approver", by);
    setBusy(true);
    (approve ? api.approve(p.id, by) : api.reject(p.id, by, reason))
      .then(() => { setError(null); onDone(); }, (e: Error) => setError(e.message))
      .finally(() => setBusy(false));
  };
  return (
    <div className={`proposal ${p.status}`}>
      <div className="row">
        <b>{actionText(p.action, p.target)}</b>
        <span className="pill off">{p.status}</span>
      </div>
      <p>{p.reason}</p>
      {p.plan?.commands?.length ? <pre>{p.plan.commands.join("\n")}</pre> : null}
      {p.result?.steps?.map((s, i) => <div key={i} className={s.ok ? "ok" : "error"}>{s.ok ? "✓" : "✗"} {s.step}{s.dry_run ? " (dry run)" : ""}</div>)}
      {p.result?.reason && <div className="error">{p.result.reason}</div>}
      {p.decided_by && <div className="muted">by {p.decided_by}{p.decided_at ? `, ${clock(p.decided_at)}` : ""}</div>}
      {p.status === "pending" && (
        <div className="decide">
          <input placeholder="your name" value={by} onChange={(e) => setBy(e.target.value)} />
          <input placeholder="reason (for a rejection)" value={reason} onChange={(e) => setReason(e.target.value)} />
          <button className="approve" disabled={busy || !by || !loop} onClick={() => act(true)}>Approve</button>
          <button className="reject" disabled={busy || !by || !loop} onClick={() => act(false)}>Reject</button>
          {!loop && <span className="muted">start the loop to act</span>}
        </div>
      )}
      {error && <div className="error">{error}</div>}
    </div>
  );
}

function Timeline({ entries }: { entries: AuditEntry[] }) {
  return (
    <ol className="timeline">
      {entries.map((e) => (
        <li key={e.seq}><span className="muted">{clock(e.at)}</span> {describe(e)}</li>
      ))}
    </ol>
  );
}

function Audit() {
  const [data, error] = usePoll(api.audit, []);
  if (error) return <p className="error">{error}</p>;
  if (!data) return <p className="empty">Loading…</p>;
  return (
    <section className="audit">
      <p className={data.ok ? "ok" : "error"}>{data.ok ? "Hash chain intact" : "Hash chain BROKEN"}: {data.message}</p>
      <table>
        <thead><tr><th>#</th><th>When</th><th>Who</th><th>What</th><th>Incident</th><th>Hash</th></tr></thead>
        <tbody>
          {[...data.entries].reverse().map((e) => (
            <tr key={e.seq}>
              <td>{e.seq}</td><td>{clock(e.at)}</td><td>{e.actor}</td><td>{describe(e)}</td>
              <td>{e.incident ?? ""}</td><td><code>{e.hash.slice(0, 12)}</code></td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
