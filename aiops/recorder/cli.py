"""recorder: export what the AIOps layer can see, in 5-minute chunks.

  recorder record --name NAME [--hours 72]       live, from now (Ctrl-C stops; the same command resumes)
  recorder record --name NAME --from T --to T    catch up on a past stretch (events only if under an hour old)
  recorder info NAME                             coverage, gaps, failed sections, hash

Recordings live in ~/.incidentlens/recordings/<name> unless --out says otherwise.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

from .kube import AIOPS_IDENTITY, Kube
from .record import CHUNK_SECONDS, LAG_SECONDS, QUERIES, iso, load_queries, parse_iso, record, write_meta
from .store import DEFAULT_RECORDINGS, Recording, resolve


def _time(value: str) -> float:
    """An ISO time, 'now', or 'now-65m' / 'now-2h'."""
    if value == "now":
        return time.time()
    if m := re.fullmatch(r"now-(\d+(?:\.\d+)?)([mh])", value):
        return time.time() - float(m[1]) * (60 if m[2] == "m" else 3600)
    return parse_iso(value)


def cmd_record(args: argparse.Namespace) -> int:
    name = args.name or time.strftime("rec-%Y%m%dT%H%M%SZ", time.gmtime())
    out = Path(args.out) if args.out else DEFAULT_RECORDINGS / name
    start = _time(args.start)
    end = _time(args.end) if args.end else (start + args.hours * 3600 if args.hours else None)
    identity = args.as_identity or None
    write_meta(out, name=out.name, context=args.context, identity=identity, chunk_s=args.chunk_seconds,
               queries_path=QUERIES)
    print(f"recording into {out} as {identity or 'the current user'}, "
          f"from {iso(start)} {'until ' + iso(end) if end else 'until stopped'}")
    kube = Kube(context=args.context, kubectl=args.kubectl, identity=identity)
    try:
        n = record(kube, out, load_queries(), start=start, end=end, chunk_s=args.chunk_seconds, lag_s=args.lag_seconds)
    except KeyboardInterrupt:
        print("stopped; run the same command again to resume")
        return 0
    print(f"{n} chunks written")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    rec = Recording(resolve(args.recording))
    span = rec.span()
    if span is None:
        print(f"{rec.path}: no chunks yet")
        return 0
    failed: Counter[str] = Counter()
    incomplete_events = 0
    for chunk in rec.chunks():
        failed.update(chunk.get("errors", {}).keys())
        incomplete_events += not chunk.get("events_complete", True)
    hours = (span[1] - span[0]) / 3600
    print(f"{rec.path}: {len(rec.chunk_paths())} chunks, {iso(span[0])} to {iso(span[1])} ({hours:.1f} h)")
    for g0, g1 in rec.gaps():
        print(f"  gap {iso(g0)} to {iso(g1)}")
    for section, n in failed.most_common():
        print(f"  {section} failed in {n} chunks")
    if incomplete_events:
        print(f"  {incomplete_events} chunks were recorded too late for complete events")
    print(f"sha256 {rec.sha256()}")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="recorder", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("record", help="record chunks, live or for a past stretch")
    s.add_argument("--out", help=f"recording directory (default: {DEFAULT_RECORDINGS}/<name>)")
    s.add_argument("--name", help="recording name (default: rec-<time>)")
    s.add_argument("--from", dest="start", default="now", help="ISO time, 'now' (default) or relative: now-65m, now-2h")
    s.add_argument("--to", dest="end", help="ISO time or now-5m; default: --hours, or until stopped")
    s.add_argument("--hours", type=float, help="how long to record from --from")
    s.add_argument("--context", default=os.getenv("CONTEXT", "kind-incidentlens"))
    s.add_argument("--kubectl", default=os.getenv("KUBECTL", "kubectl"))
    s.add_argument("--as", dest="as_identity", default=AIOPS_IDENTITY,
                   help="identity to read the cluster as; '' for the current user")
    s.add_argument("--chunk-seconds", type=int, default=CHUNK_SECONDS)
    s.add_argument("--lag-seconds", type=int, default=LAG_SECONDS)
    s.set_defaults(func=cmd_record)

    s = sub.add_parser("info", help="summarise a recording")
    s.add_argument("recording")
    s.set_defaults(func=cmd_info)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
