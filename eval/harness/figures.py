"""The charts of eval/results.md, as plain SVG: no plotting library, and files
GitHub shows inline. Every chart has its numbers in a table in results.md
next to it, and each mark carries a <title> (a tooltip when the SVG is opened
on its own).

Colors: a validated categorical palette (three slots, every pair checked for
colour-vision deficiency in light and dark), one blue ramp for the heat
maps, and a dark variant chosen by the viewer's colour scheme.
"""

from __future__ import annotations

import math
from html import escape
from pathlib import Path
from typing import Any

FONT = "-apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
STYLE = """
<style>
  svg { --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --grid:#e2e1dc; --ref:#8a8984;
        --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; }
  @media (prefers-color-scheme: dark) {
    svg { --surface:#1a1a19; --ink:#f0efec; --ink2:#c3c2b7; --grid:#33332f; --ref:#8f8e88;
          --s1:#3987e5; --s2:#d95926; --s3:#199e70; }
  }
  text { font-family: %s; fill: var(--ink2); font-size: 12px; }
  .title { fill: var(--ink); font-size: 14px; font-weight: 600; }
  .ink { fill: var(--ink); }
  .grid { stroke: var(--grid); stroke-width: 1; }
  .axis { stroke: var(--ink2); stroke-width: 1; }
  .ref { stroke: var(--ref); stroke-width: 1.5; stroke-dasharray: 4 3; }
  .ci { stroke: var(--ink); stroke-width: 1.5; }
  .s1 { fill: var(--s1); } .s2 { fill: var(--s2); } .s3 { fill: var(--s3); }
  .bg { fill: var(--surface); }
  .cell-empty { fill: var(--surface); stroke: var(--grid); }
</style>
""" % FONT
SERIES = ("s1", "s2", "s3")
# Sequential blue, light to dark (the reference ramp's steps 100 to 700).
RAMP = ("#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b")


def _svg(width: int, height: int, body: list[str], title: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">'
            f"<title>{escape(title)}</title>{STYLE}"
            f'<rect class="bg" width="{width}" height="{height}"/>' + "".join(body) + "</svg>\n")


def _bar(x: float, y: float, w: float, h: float, cls: str, tip: str) -> str:
    """A bar on the baseline, its data end rounded (4 px)."""
    if h <= 0:
        return f'<rect class="{cls}" x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="1"><title>{escape(tip)}</title></rect>'
    r = min(4.0, w / 2, h)
    d = (f"M{x:.1f},{y + h:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} H{x + w - r:.1f} "
         f"Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{y + h:.1f} Z")
    return f'<path class="{cls}" d="{d}"><title>{escape(tip)}</title></path>'


def _ci(x: float, y_low: float, y_high: float) -> str:
    return (f'<line class="ci" x1="{x:.1f}" x2="{x:.1f}" y1="{y_low:.1f}" y2="{y_high:.1f}"/>'
            f'<line class="ci" x1="{x - 4:.1f}" x2="{x + 4:.1f}" y1="{y_low:.1f}" y2="{y_low:.1f}"/>'
            f'<line class="ci" x1="{x - 4:.1f}" x2="{x + 4:.1f}" y1="{y_high:.1f}" y2="{y_high:.1f}"/>')


def _pct(v: float | None) -> str:
    return "–" if v is None else f"{v:.0%}"


def grouped_bars(groups: list[str], series: list[tuple[str, list[dict | None]]], *, title: str,
                 unit: str = "%", top: float = 1.0, refs: dict[str, float] | None = None) -> str:
    """Groups on the x axis, up to 3 series per group, each bar with its 95% CI.
    series: (name, [estimate dict per group or None])."""
    w_bar, gap, group_gap = 22, 2, 28
    per_group = len(series) * w_bar + (len(series) - 1) * gap
    left, top_m, bottom = 48, 64, 40
    right = 90 if refs else 16  # reference labels sit right of the plot, clear of the bars
    plot_w = max(len(groups) * per_group + (len(groups) - 1) * group_gap + 24,
                 int(len(title) * 7.6) - right + 8)  # wide enough for the title
    plot_h = 220
    width, height = left + plot_w + right, top_m + plot_h + bottom
    y0 = top_m + plot_h

    def y(v: float) -> float:
        return y0 - max(0.0, min(v, top)) / top * plot_h

    body = [f'<text class="title" x="{left}" y="22">{escape(title)}</text>']
    # legend, one row under the title
    lx = left
    for i, (name, _) in enumerate(series):
        if len(series) == 1:
            break  # one series: the title names it, no legend
        body.append(f'<rect class="{SERIES[i]}" x="{lx}" y="34" width="10" height="10" rx="2"/>'
                    f'<text x="{lx + 14}" y="43">{escape(name)}</text>')
        lx += 14 + 7 * len(name) + 18
    for k in range(5):
        v = top * k / 4
        tick = f"{v:.0%}" if unit == "%" else f"{v:g}"
        body.append(f'<line class="grid" x1="{left}" x2="{left + plot_w}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
                    f'<text x="{left - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{tick}</text>')
    for name, v in (refs or {}).items():
        body.append(f'<line class="ref" x1="{left}" x2="{left + plot_w}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
                    f'<text x="{left + plot_w + 6}" y="{y(v) + 4:.1f}">{escape(name)}</text>')
    body.append(f'<line class="axis" x1="{left}" x2="{left + plot_w}" y1="{y0}" y2="{y0}"/>')
    for g, group in enumerate(groups):
        gx = left + (plot_w - len(groups) * per_group - (len(groups) - 1) * group_gap) / 2 + g * (per_group + group_gap)
        body.append(f'<text class="ink" x="{gx + per_group / 2:.1f}" y="{y0 + 18}" text-anchor="middle">{escape(group)}</text>')
        for i, (name, values) in enumerate(series):
            e = values[g]
            x = gx + i * (w_bar + gap)
            if not e or e.get("value") is None:
                body.append(f'<text x="{x + w_bar / 2:.1f}" y="{y0 - 4}" text-anchor="middle">–</text>')
                continue
            fmt = _pct if unit == "%" else (lambda v: f"{v:.1f} {unit}")
            tip = f"{group}, {name}: {fmt(e['value'])}"
            if e.get("low") is not None:
                tip += f" (95% CI {fmt(e['low'])} to {fmt(e['high'])}, n={e.get('n')})"
            body.append(_bar(x, y(e["value"]), w_bar, y0 - y(e["value"]), SERIES[i], tip))
            if e.get("low") is not None:
                body.append(_ci(x + w_bar / 2, y(e["low"]), y(e["high"])))
    return _svg(width, height, body, title)


def single_bars(groups: list[str], values: list[dict | None], *, title: str, unit: str, top: float,
                ref: tuple[str, float] | None = None) -> str:
    return grouped_bars(groups, [(title, values)], title=title, unit=unit, top=top,
                        refs={ref[0]: ref[1]} if ref else None)


def differences(names: list[str], values: list[dict | None], *, title: str, span: float = 0.5) -> str:
    """Dot and 95% CI per row, around a zero line: how much each ablation loses against A1."""
    left, right, top_m, row_h = 150, 40, 48, 34
    plot_w = 360
    width, height = left + plot_w + right, top_m + row_h * len(names) + 40

    def x(v: float) -> float:
        return left + (max(-span, min(span, v)) + span) / (2 * span) * plot_w

    body = [f'<text class="title" x="16" y="22">{escape(title)}</text>']
    for k in range(-2, 3):
        v = span * k / 2
        body.append(f'<line class="{"axis" if k == 0 else "grid"}" x1="{x(v):.1f}" x2="{x(v):.1f}" y1="{top_m - 8}" '
                    f'y2="{top_m + row_h * len(names)}"/><text x="{x(v):.1f}" y="{top_m + row_h * len(names) + 18}" '
                    f'text-anchor="middle">{v:+.0%}</text>')
    for i, (name, e) in enumerate(zip(names, values)):
        cy = top_m + row_h * i + row_h / 2
        body.append(f'<text class="ink" x="{left - 12}" y="{cy + 4:.1f}" text-anchor="end">{escape(name)}</text>')
        if not e or e.get("value") is None:
            body.append(f'<text x="{x(0) + 8:.1f}" y="{cy + 4:.1f}">not run</text>')
            continue
        tip = f"{name}: {e['value']:+.0%} (95% CI {e['low']:+.0%} to {e['high']:+.0%}, n={e['n']})"
        body.append(f'<line class="ci" x1="{x(e["low"]):.1f}" x2="{x(e["high"]):.1f}" y1="{cy:.1f}" y2="{cy:.1f}"/>'
                    f'<circle class="s1" cx="{x(e["value"]):.1f}" cy="{cy:.1f}" r="5" stroke="var(--surface)" '
                    f'stroke-width="2"><title>{escape(tip)}</title></circle>'
                    f'<text x="{x(e["high"]) + 8:.1f}" y="{cy + 4:.1f}">{e["value"]:+.0%}</text>')
    return _svg(width, height, body, title)


def heatmap(rows: list[str], cols: list[str], counts: dict[str, dict[str, int]], expected: dict[str, str],
            *, title: str) -> str:
    """Counts per (row, column), shaded by share of the row; the expected column
    of each row is outlined."""
    left, top_m, cw, ch = 64, 140, 62, 24
    width, height = left + cw * len(cols) + 48, top_m + ch * len(rows) + 16
    body = [f'<text class="title" x="16" y="22">{escape(title)}</text>',
            f'<text x="16" y="40">Share of each fault\'s runs; outlined: the labelled answer</text>']
    for j, c in enumerate(cols):
        cx = left + j * cw + cw / 2
        body.append(f'<text class="ink" x="{cx - 4:.1f}" y="{top_m - 8}" '
                    f'transform="rotate(-40 {cx - 4:.1f} {top_m - 8})">{escape(c)}</text>')
    for i, r in enumerate(rows):
        y = top_m + i * ch
        body.append(f'<text class="ink" x="{left - 8}" y="{y + ch / 2 + 4:.1f}" text-anchor="end">{escape(r)}</text>')
        total = sum(counts.get(r, {}).values()) or 1
        for j, c in enumerate(cols):
            n = counts.get(r, {}).get(c, 0)
            x = left + j * cw
            tip = f"{r} -> {c}: {n} of {total}"
            if n == 0:
                body.append(f'<rect class="cell-empty" x="{x + 1}" y="{y + 1}" width="{cw - 2}" height="{ch - 2}" rx="2">'
                            f"<title>{escape(tip)}</title></rect>")
            else:
                step = min(len(RAMP) - 1, int(n / total * (len(RAMP) - 1) + 0.5))
                ink = "#ffffff" if step >= 3 else "#0b0b0b"
                body.append(f'<rect x="{x + 1}" y="{y + 1}" width="{cw - 2}" height="{ch - 2}" rx="2" fill="{RAMP[step]}">'
                            f'<title>{escape(tip)}</title></rect><text x="{x + cw / 2:.1f}" y="{y + ch / 2 + 4:.1f}" '
                            f'text-anchor="middle" style="fill:{ink}">{n}</text>')
            if expected.get(r) == c:
                body.append(f'<rect x="{x + 1}" y="{y + 1}" width="{cw - 2}" height="{ch - 2}" rx="2" fill="none" '
                            f'style="stroke:var(--ink)" stroke-width="2"/>')
    return _svg(width, height, body, title)


def write_all(out_dir: Path, report: dict[str, Any]) -> list[Path]:
    """The figures results.md links to. Returns the files written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []

    def put(name: str, svg: str) -> None:
        path = out_dir / name
        path.write_text(svg, encoding="utf-8")
        written.append(path)

    det = report["detection"]
    if det:
        names = list(det)
        put("detection-recall.svg", single_bars(names, [det[v]["recall"] for v in names],
                                                title="Detection recall within 10 minutes", unit="%", top=1.0,
                                                ref=("target 90%", 0.9)))
        mttd = [None if det[v]["mttd_min"]["value"] is None else det[v]["mttd_min"] for v in names]
        top = max([2.5] + [e["high"] for e in mttd if e and e.get("high") is not None])
        top = 4 * math.ceil(top / 4)  # five whole-minute ticks
        put("detection-mttd.svg", single_bars(names, mttd, title="Median time to detect (minutes)", unit="min",
                                              top=float(top), ref=("target 2 min", 2.0)))
    tri = report["triage"]
    if tri:
        names = list(tri)
        put("triage.svg", grouped_bars(names, [("RCA top-1", [tri[v]["top1"] for v in names]),
                                               ("RCA top-3", [tri[v]["top3"] for v in names]),
                                               ("Action", [tri[v]["action"] for v in names])],
                                       title="Triage of the incidents the detectors caught"))
    abl = report.get("ablations") or {}
    if abl:
        put("ablations.svg", differences(list(abl), [abl[k]["top1_end_to_end"] for k in abl],
                                         title="RCA top-1 against A1 (difference, 95% CI)"))
    for variant, m in (report.get("confusion") or {}).items():
        put(f"confusion-{variant}.svg", heatmap(m["rows"], m["cols"], m["counts"], m["expected"],
                                                title=f"{variant}: labelled fault -> first hypothesis's category"))
    return written
