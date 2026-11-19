"""remediator: plan, dry-run or run one action as the aiops-remediator account.

  remediator plan rollback catalog              what it would do, read from the cluster
  remediator plan scale_out gateway --dry-run   and send it with --dry-run=server: RBAC and the
                                                API server check it, nothing changes
  remediator plan restart redis --execute       do it (the console is the normal way)

The live loop uses the same code; this is for checking it by hand and in CI.
"""

from __future__ import annotations

import argparse
import json
import sys

from .actions import Cluster, PlanError, execute, plan


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="remediator", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("plan")
    s.add_argument("action", choices=["rollback", "restart", "scale_out"])
    s.add_argument("target")
    s.add_argument("--context", default="kind-incidentlens")
    s.add_argument("--kubectl", default="kubectl")
    mode = s.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    args = p.parse_args(argv)
    c = Cluster(args.context, args.kubectl)
    try:
        pl = plan(c, args.action, args.target)
    except PlanError as exc:
        print(f"no plan: {exc}", file=sys.stderr)
        return 3
    d = pl.to_dict()
    print(f"{pl.action} {pl.target}: {pl.how}")
    for cmd in d["commands"]:
        print(f"  {cmd}")
    print(f"  before: {json.dumps(pl.before)}")
    if not (args.dry_run or args.execute):
        return 0
    results = execute(c, pl, dry_run=args.dry_run)
    for r in results:
        print(f"  {'ok  ' if r['ok'] else 'FAIL'} {r['step']}{' (dry run)' if r['dry_run'] else ''}: {r['output']}")
    return 0 if all(r["ok"] for r in results) else 1
