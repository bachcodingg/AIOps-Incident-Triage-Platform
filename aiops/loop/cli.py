"""loop: the live AIOps loop and its console.

  loop serve [--variant B2|A1|...] [--dry-run] [--port 8090]   loop + console on http://127.0.0.1:8090
  loop serve --no-loop --db FILE                              the console over a stored state, nothing runs
  loop demo --db FILE                                         a made-up incident waiting for approval, no cluster

`serve` needs a running cluster (make up). It reads as the `aiops` account and
acts as `aiops-remediator`, both by impersonation from your kubeconfig, like
the recorder. --dry-run sends every change with --dry-run=server: the API
server checks it, RBAC included, and changes nothing.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

from recorder.kube import Kube, KubeError
from remediator.actions import Cluster
from remediator.store import DEFAULT_DB, Store

from .api import create_app
from .live import LiveSource, Loop

DEFAULT_RUNS = Path.home() / ".incidentlens" / "live" / "runs"


def _run_loop(loop: Loop, interval: float, stop: threading.Event) -> None:
    while not stop.is_set():
        started = time.monotonic()
        try:
            loop.tick()
        except (KubeError, ValueError) as exc:  # a backend restarting must not stop the loop
            loop.log(f"tick failed: {exc}")
        stop.wait(max(1.0, interval - (time.monotonic() - started)))


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    store = Store(args.db)
    loop = None
    if not args.no_loop:
        models = None
        if args.model:
            from detector.anomaly import load_model
            from detector.cli import resolve_model
            models, _ = load_model(resolve_model(args.model))
        source = LiveSource(Kube(args.context, args.kubectl), directory=Path(args.record_dir) if args.record_dir else None)
        loop = Loop(source, store, Cluster(args.context, args.kubectl), variant=args.variant, dry_run=args.dry_run,
                    runs_dir=Path(args.runs), models=models)
        store.audit("loop", "started", None, {"variant": args.variant, "dry_run": args.dry_run, "context": args.context})
        stop = threading.Event()
        threading.Thread(target=_run_loop, args=(loop, args.interval, stop), daemon=True).start()
        print(f"loop: {args.variant}, every {args.interval:.0f}s{' (dry run)' if args.dry_run else ''}; "
              f"it acts only on incidents that open from now on")
    app = create_app(store, loop, grafana=args.grafana, dry_run=args.dry_run, variant=args.variant)
    print(f"console: http://127.0.0.1:{args.port}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """A store with one incident at each stage, for trying the console offline."""
    store = Store(args.db)
    if store.incident("inc-demo-catalog"):
        print(f"demo state already in {args.db}")
        return 0
    now = time.time()
    opened = now - 420
    alerts = [
        {"detector": "anomaly", "name": "AnomalyMAD", "component": "catalog", "start": opened, "end": None,
         "severity": "anomaly", "labels": {"metric": "error_ratio", "direction": "up"}, "score": 41.2},
        {"detector": "burn-rate", "name": "ProductsAvailabilityBudgetBurn", "component": "gateway", "start": opened + 75,
         "end": None, "severity": "page", "labels": {"sloth_slo": "products-availability"}, "score": None},
    ]
    from detector.alerts import iso
    alerts = [{**a, "start": iso(a["start"])} for a in alerts]
    store.upsert_incident({"id": "inc-demo-catalog", "opened": opened, "components": ["catalog", "gateway"], "alerts": alerts}, now)
    verdict = {"hypotheses": [
        {"component": "catalog", "category": "bad-release", "confidence": 0.82,
         "reason": "catalog 5xx started 40 s after release 7 of catalog; nothing below it changed"},
        {"component": "postgres", "category": "resource", "confidence": 0.1, "reason": "PostgreSQL CPU and connections are normal"}],
        "evidence": [
            {"tool": "query_metric", "query": '{"metric": "errors", "match": {"job": "shopsearch/catalog"}}',
             "finding": "0.4 req/s of 5xx from -6m, 0 in the hour before"},
            {"tool": "deploy_history", "query": '{"app": "catalog"}', "finding": "release 7 of catalog at -7m: image.tag=3f9a1c2"}],
        "action": "rollback", "action_target": "catalog", "confidence": 0.8,
        "summary": "A catalog release answers 5% of product lookups with 500s; roll it back."}
    store.audit("loop", "incident-opened", "inc-demo-catalog", {"components": ["catalog", "gateway"]}, at=opened)
    store.set_incident("inc-demo-catalog", verdict=verdict, status="awaiting-approval")
    store.audit("agent", "triaged", "inc-demo-catalog", {"variant": "A1", "root_cause": "catalog / bad-release",
                                                         "action": "rollback", "target": "catalog"}, at=opened + 150)
    plan = {"action": "rollback", "target": "catalog", "how": "argocd-previous-release", "steps": [],
            "before": {"history_id": 7, "back_to": 6},
            "commands": ['kubectl -n argocd patch applications.argoproj.io catalog --type json -p [{"op":"remove","path":"/spec/source/helm/parameters"}]']}
    pid = store.add_proposal("inc-demo-catalog", "rollback", "catalog", "approval",
                             "rollback catalog changes a running system: one approval needed", "pending", plan)
    store.audit("policy", "proposed", "inc-demo-catalog", {"proposal": pid, "mode": "approval"}, at=opened + 151)
    print(f"demo state in {args.db}; `loop serve --no-loop --db {args.db}` shows it (approving needs the loop)")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="loop", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("serve", help="run the loop and the console")
    s.add_argument("--context", default="kind-incidentlens")
    s.add_argument("--kubectl", default="kubectl")
    s.add_argument("--variant", default="B2", choices=["B2", "A1", "A2", "A3", "A4"])
    s.add_argument("--dry-run", action="store_true", help="changes go to the API server with --dry-run=server")
    s.add_argument("--interval", type=float, default=30)
    s.add_argument("--model", help="Isolation Forest model for the anomaly detector (file or name)")
    s.add_argument("--record-dir", help="also keep the live telemetry as a recording here")
    s.add_argument("--db", default=str(DEFAULT_DB))
    s.add_argument("--runs", default=str(DEFAULT_RUNS), help="where triage runs are written")
    s.add_argument("--grafana", default="http://localhost:3000")
    s.add_argument("--port", type=int, default=8090)
    s.add_argument("--no-loop", action="store_true", help="only the console, over the stored state")
    s.set_defaults(func=cmd_serve)
    s = sub.add_parser("demo", help="write a made-up incident to a store, to try the console")
    s.add_argument("--db", required=True)
    s.set_defaults(func=cmd_demo)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return args.func(args)
