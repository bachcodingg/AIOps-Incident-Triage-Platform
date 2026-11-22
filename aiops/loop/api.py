"""The console's API, and the console itself (aiops/console, built to dist/).

  GET  /api/config                    dry run or not, variant, Grafana's address
  GET  /api/incidents                 newest first, with verdict and proposal state
  GET  /api/incidents/{id}            alerts, verdict, evidence with Grafana links, proposals, timeline, tool calls
  POST /api/proposals/{id}/approve    {"by": "name"}  run it
  POST /api/proposals/{id}/reject     {"by": "name", "reason": "..."}
  GET  /api/audit                     the whole audit log and whether its hash chain is intact

It listens on 127.0.0.1 only and has no login: it is the operator's console on
the operator's machine, like `make grafana`. Every approval records the name it
was given; it is not an identity check, and the docs say so.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from detector.alerts import SERVICES
from remediator.store import Store

CONSOLE_DIST = Path(__file__).resolve().parents[1] / "console" / "dist"
DASHBOARDS = {"red": "shopsearch-red", "platform": "shopsearch-platform", "slo": "shopsearch-slo"}


class Decision(BaseModel):
    by: str = Field(min_length=1, max_length=80)
    reason: str = Field(default="", max_length=500)


def grafana_links(base: str, components: list[str], start: float, end: float | None) -> list[dict[str, str]]:
    """Dashboards for the incident's time range: RED per service, Platform for the rest, and the SLOs."""
    frm, to = int((start - 1800) * 1000), int(((end or start) + 900) * 1000)
    links = []

    def link(title: str, uid: str, extra: dict[str, str] | None = None) -> None:
        q = {"from": str(frm), "to": str(to), **(extra or {})}
        links.append({"title": title, "url": f"{base.rstrip('/')}/d/{uid}?{urllib.parse.urlencode(q)}"})

    for c in sorted({p for c in components for p in c.split("->")}):
        if c in SERVICES:
            link(f"{c}: RED", DASHBOARDS["red"], {"var-service": c})
    if any(p not in SERVICES for c in components for p in c.split("->")):
        link("Platform: nodes, DNS, Redis, PostgreSQL", DASHBOARDS["platform"])
    link("SLOs", DASHBOARDS["slo"])
    return links


def create_app(store: Store, loop: Any | None, *, grafana: str = "http://localhost:3000", dry_run: bool = False,
               variant: str = "B2", console_dist: Path = CONSOLE_DIST) -> FastAPI:
    app = FastAPI(title="IncidentLens console", docs_url="/api/docs", openapi_url="/api/openapi.json")

    def proposal_view(p: dict[str, Any]) -> dict[str, Any]:
        return {k: p[k] for k in ("id", "action", "target", "mode", "reason", "status", "decided_by", "decided_at",
                                  "created", "plan", "result")}

    @app.get("/api/config")
    def config() -> dict[str, Any]:
        ok, chain = store.verify_audit()
        return {"dry_run": dry_run, "variant": variant, "grafana": grafana, "loop": loop is not None,
                "audit_ok": ok, "audit": chain}

    @app.get("/api/incidents")
    def incidents() -> list[dict[str, Any]]:
        out = []
        for i in store.incidents():
            props = store.proposals(i["id"])
            v = i.get("verdict") or {}
            top = (v.get("hypotheses") or [{}])[0]
            out.append({"id": i["id"], "opened": i["opened"], "closed": i["closed"], "status": i["status"],
                        "components": i["components"], "alerts": len(i["alerts"]),
                        "root_cause": f"{top['component']} / {top['category']}" if top else None,
                        "action": v.get("action"), "action_target": v.get("action_target"),
                        "pending": any(p["status"] == "pending" for p in props)})
        return out

    @app.get("/api/incidents/{incident_id}")
    def incident(incident_id: str) -> dict[str, Any]:
        i = store.incident(incident_id)
        if i is None:
            raise HTTPException(404, f"no incident {incident_id}")
        v = i.get("verdict") or {}
        run: dict[str, Any] = {}
        if i.get("run_path") and Path(i["run_path"]).exists():
            run = json.loads(Path(i["run_path"]).read_text(encoding="utf-8"))
        named = [h["component"] for h in v.get("hypotheses") or []] + list(i["components"] or [])
        return {**{k: i[k] for k in ("id", "opened", "closed", "status", "components", "alerts")},
                "verdict": v or None,
                "proposals": [proposal_view(p) for p in store.proposals(incident_id)],
                "timeline": store.audit_log(incident_id),
                "grafana": grafana_links(grafana, named, i["opened"], i["closed"]),
                "tool_calls": [{k: c.get(k) for k in ("name", "input", "error", "output")} for c in run.get("tool_calls", [])],
                "run": {k: run.get(k) for k in ("variant", "model", "valid", "attempts", "cost_usd", "seconds",
                                               "stopped_by", "models_answered")} if run else None}

    def _decide(pid: int, body: Decision, approve: bool) -> dict[str, Any]:
        if loop is None:
            raise HTTPException(409, "the loop is not running: start `make console` against a cluster to act")
        try:
            p = loop.approve(pid, body.by) if approve else loop.reject(pid, body.by, body.reason)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return proposal_view(p)

    @app.post("/api/proposals/{pid}/approve")
    def approve(pid: int, body: Decision) -> dict[str, Any]:
        return _decide(pid, body, True)

    @app.post("/api/proposals/{pid}/reject")
    def reject(pid: int, body: Decision) -> dict[str, Any]:
        return _decide(pid, body, False)

    @app.get("/api/audit")
    def audit() -> dict[str, Any]:
        ok, message = store.verify_audit()
        return {"ok": ok, "message": message, "entries": store.audit_log(limit=2000)}

    if (console_dist / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=console_dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:
            return FileResponse(console_dist / "index.html")
    return app
