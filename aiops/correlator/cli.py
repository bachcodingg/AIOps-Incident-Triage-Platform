"""correlator: group a detector's alerts into incidents.

  correlator --recording RUN --variant B2     RUN/alerts.jsonl -> RUN/incidents-B2.jsonl

The recording supplies Tempo's service graph; --static-graph skips it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from detector.alerts import DETECTORS, read_alerts, write_jsonl
from detector.series import load
from recorder.store import Recording, resolve

from .correlate import HOLD_SECONDS, VARIANTS, WINDOW_SECONDS, correlate, select
from .graph import STATIC_GRAPH, ServiceGraph


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="correlator", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--alerts", help="alerts.jsonl from `detector run` (default: the recording's)")
    p.add_argument("--recording", help="recording to read Tempo's service graph from")
    p.add_argument("--static-graph", action="store_true", help="use only aiops/correlator/graph.yaml")
    p.add_argument("--graph", default=str(STATIC_GRAPH))
    group = p.add_mutually_exclusive_group()
    group.add_argument("--variant", choices=sorted(VARIANTS), default="B2")
    group.add_argument("--detectors", type=lambda v: v.split(","), help=f"any of {', '.join(DETECTORS)}")
    p.add_argument("--window", type=float, default=WINDOW_SECONDS)
    p.add_argument("--hold", type=float, default=HOLD_SECONDS)
    p.add_argument("--out", help="default: incidents-<variant>.jsonl in the recording")
    args = p.parse_args(argv)
    rec_dir = resolve(args.recording) if args.recording else None
    if not args.alerts and not rec_dir:
        p.error("give --alerts, or --recording to use its alerts.jsonl")
    alerts_path = Path(args.alerts) if args.alerts else rec_dir / "alerts.jsonl"
    name = "-".join(args.detectors) if args.detectors else args.variant
    out = Path(args.out) if args.out else (rec_dir or alerts_path.parent) / f"incidents-{name}.jsonl"

    graph = ServiceGraph.load(Path(args.graph))
    if args.recording and not args.static_graph:
        frame, _ = load(Recording(rec_dir).chunks())
        new = graph.add_observed(frame)
        print(f"service graph: {len(graph.edges)} edges, {len(graph.observed)} seen by Tempo"
              + (f", new: {', '.join(' -- '.join(sorted(e)) for e in new)}" if new else ""))
    detectors = args.detectors or VARIANTS[args.variant]
    alerts = select(read_alerts(alerts_path), detectors)
    incidents = correlate(alerts, graph, window_s=args.window, hold_s=args.hold)
    write_jsonl(out, incidents)
    reduction = 1 - len(incidents) / len(alerts) if alerts else 0.0
    print(f"{'+'.join(detectors)}: {len(alerts)} alerts -> {len(incidents)} incidents "
          f"({reduction:.0%} fewer), written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
