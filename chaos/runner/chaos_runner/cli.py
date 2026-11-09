"""chaos-runner: inject the faults in chaos/catalog.yaml and seal the ground truth.

  chaos-runner list                      the catalog
  chaos-runner inject F05 --check        one fault now, and prove its symptom (the dry run)
  chaos-runner run                       a dataset run: the whole catalog at random times
  chaos-runner verify labels.jsonl       check a labels file against its published head
  chaos-runner cleanup                   undo what an interrupted run left behind
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .catalog import Catalog, CatalogError, parse_duration, render
from .cluster import Cluster
from .injectors import cleanup
from .labels import DEFAULT_LABELS_DIR, LabelWriter, SealError, verify
from .runner import Runner, iso
from .schedule import FAULT, Item, plan, summary


def _cluster(args: argparse.Namespace) -> Cluster:
    return Cluster(context=args.context, kubectl=args.kubectl, docker=args.docker, kind=args.kind,
                   kind_cluster=args.kind_cluster, base_tag=args.base_tag)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def dashboard_path(fault, variables: dict[str, str], start: datetime) -> str:
    query = {"orgId": "1", "from": str(int((start - timedelta(minutes=10)).timestamp() * 1000)), "to": "now", "kiosk": ""}
    for k, v in fault.dashboard.get("vars", {}).items():
        query[f"var-{k}"] = render(v, variables)
    return f"/d/{fault.dashboard['uid']}?{urllib.parse.urlencode(query)}"


def cmd_list(args: argparse.Namespace, catalog: Catalog) -> int:
    for f in catalog.faults.values():
        levels = sorted({i for t in f.targets for i in f.intensities_for(t) if i is not None},
                        key=lambda i: (float(i.rstrip("ms")) if i.rstrip("ms").replace(".", "").isdigit() else 0, i))
        print(f"{f.id}  {f.name:<42} {f.injector:<15} {', '.join(f.targets)}"
              + (f"  [{', '.join(levels)}]" if levels else ""))
    return 0


def cmd_plan(args: argparse.Namespace, catalog: Catalog) -> int:
    items = plan(catalog, args.seed, faults=args.faults, repeats=args.repeats)
    print(summary(items))
    if args.out:
        Path(args.out).write_text(json.dumps([i.to_dict() for i in items], indent=1), encoding="utf-8")
        print(f"wrote {args.out}. A plan says when every fault happens: keep it with the labels, not in the repository.")
    return 0


def cmd_inject(args: argparse.Namespace, catalog: Catalog) -> int:
    fault = catalog.fault(args.fault)
    target = args.target or next(iter(fault.targets))
    if target not in fault.targets:
        raise CatalogError(f"{fault.id} has no target {target}; choose one of {', '.join(fault.targets)}")
    levels = fault.intensities_for(target)
    intensity = args.intensity or levels[-1]  # the strongest, so the dry run's symptom is clear
    duration = parse_duration(args.duration) if args.duration else fault.duration[0]
    item = Item(id=f"d{_stamp()[-7:-1]}", kind=FAULT, duration_s=duration, gap_s=0,
                fault=fault.id, target=target, intensity=intensity)

    def on_symptom(label: dict) -> None:
        if not args.on_symptom:
            return
        variables = catalog.variables(fault, target, intensity, duration)
        start = datetime.fromisoformat(label["start"].replace("Z", "+00:00"))
        env = {**os.environ, "CHAOS_FAULT": fault.id, "CHAOS_TARGET": target,
               "CHAOS_DASHBOARD_PATH": dashboard_path(fault, variables, start)}
        subprocess.run(args.on_symptom, shell=True, env=env, check=False)

    runner = Runner(_cluster(args), catalog, poll_s=args.poll)
    label = runner.inject(item, check_symptom=args.check,
                          symptom_timeout_s=parse_duration(args.symptom_timeout),
                          stop_after_symptom_s=parse_duration(args.stop_after_symptom) if args.check else None,
                          on_symptom=on_symptom)
    writer = LabelWriter(Path(args.labels_dir) / f"dryrun-{_stamp()}")
    writer.write(label)
    print(json.dumps(label, indent=1))
    print(f"label written to {writer.path}")
    if not args.check:
        return 0
    failures = []
    if label.get("error"):
        failures.append(f"injection failed: {label['error']}")
    if label.get("symptom_before"):
        failures.append("the symptom was already there before the fault")
    if not label.get("symptom_seen"):
        failures.append("the symptom never showed up")
    if not label.get("recovered"):
        failures.append("the system did not recover")
    for f in failures:
        print(f"FAIL {fault.id}: {f}", file=sys.stderr)
    return 1 if failures else 0


def cmd_run(args: argparse.Namespace, catalog: Catalog) -> int:
    seed = args.seed if args.seed is not None else secrets.randbits(32)
    items = plan(catalog, seed, faults=args.faults, repeats=args.repeats, fault_free=args.fault_free, benign=args.benign)
    run_id = f"run-{_stamp()}"
    writer = LabelWriter(Path(args.labels_dir) / run_id)
    print(f"{run_id}: {summary(items)}")
    print(f"labels: {writer.path} (sealed: keep them away from the AIOps layer)")
    started = iso(datetime.now(timezone.utc))
    try:
        head = Runner(_cluster(args), catalog, poll_s=args.poll).run(items, writer, run_id, seed)
    finally:
        seal = {"run": run_id, "started": started, "ended": iso(datetime.now(timezone.utc)),
                "records": writer.count, "head_sha256": writer.head}
        print(json.dumps(seal, indent=1))
        if args.seal_out:
            Path(args.seal_out).write_text(json.dumps(seal, indent=1) + "\n", encoding="utf-8")
            print(f"wrote the public seal to {args.seal_out}; commit it")
    print(f"done, head {head}")
    return 0


def cmd_verify(args: argparse.Namespace, catalog: Catalog) -> int:
    expected = args.head
    if args.seal:
        expected = json.loads(Path(args.seal).read_text(encoding="utf-8"))["head_sha256"]
    count, head = verify(Path(args.labels), expected)
    print(f"ok: {count} records, head {head}")
    return 0


def cmd_cleanup(args: argparse.Namespace, catalog: Catalog) -> int:
    for line in cleanup(_cluster(args), catalog.rollouts()):
        print(line)
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="chaos-runner", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--catalog", default=None, help="default: chaos/catalog.yaml")
    p.add_argument("--context", default=os.getenv("CONTEXT", "kind-incidentlens"))
    p.add_argument("--kubectl", default=os.getenv("KUBECTL", "kubectl"))
    p.add_argument("--docker", default=os.getenv("DOCKER", "docker"))
    p.add_argument("--kind", default=os.getenv("KIND", "kind"))
    p.add_argument("--kind-cluster", default=os.getenv("CLUSTER", "incidentlens"))
    p.add_argument("--base-tag", default=os.getenv("TAG", "dev"), help="image tag bad releases are built from")
    p.add_argument("--poll", type=float, default=15, help="seconds between checks")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="show the catalog").set_defaults(func=cmd_list)

    s = sub.add_parser("plan", help="draw a schedule and summarise it")
    s.add_argument("--seed", type=int, required=True)
    s.add_argument("--faults", type=lambda v: v.split(","), default=None)
    s.add_argument("--repeats", type=int, default=None)
    s.add_argument("--out")
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser("inject", help="inject one fault now (the dry run with --check)")
    s.add_argument("fault")
    s.add_argument("--target")
    s.add_argument("--intensity")
    s.add_argument("--duration", help="default: the fault's shortest duration")
    s.add_argument("--check", action="store_true", help="require the symptom before/during and recovery after")
    s.add_argument("--symptom-timeout", default="6m")
    s.add_argument("--stop-after-symptom", default="90s", help="with --check: end the fault this long after its symptom")
    s.add_argument("--on-symptom", help="shell command run once the symptom shows (CHAOS_DASHBOARD_PATH is set)")
    s.add_argument("--labels-dir", default=str(DEFAULT_LABELS_DIR))
    s.set_defaults(func=cmd_inject)

    s = sub.add_parser("run", help="a dataset run: the catalog at random times, labels sealed")
    s.add_argument("--seed", type=int, default=None, help="default: random, and recorded in the sealed labels")
    s.add_argument("--faults", type=lambda v: v.split(","), default=None)
    s.add_argument("--repeats", type=int, default=None)
    s.add_argument("--fault-free", type=int, default=None)
    s.add_argument("--benign", type=int, default=None)
    s.add_argument("--labels-dir", default=str(DEFAULT_LABELS_DIR))
    s.add_argument("--seal-out", help="where to write the public seal (run id, record count, head hash)")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("verify", help="check a labels file's hash chain")
    s.add_argument("labels")
    s.add_argument("--head", help="the published head hash")
    s.add_argument("--seal", help="a seal file written by `run --seal-out`")
    s.set_defaults(func=cmd_verify)

    sub.add_parser("cleanup", help="remove chaos objects, release ArgoCD, start stopped nodes").set_defaults(func=cmd_cleanup)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        catalog = Catalog.load(args.catalog) if args.catalog else Catalog.load()
        return args.func(args, catalog)
    except (CatalogError, SealError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
