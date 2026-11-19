"""The system prompt and the incident brief.

The system prompt is the same for every incident of a variant, so the provider
can cache it (with the tool definitions in front of it). It holds no time, no
incident and nothing from the labels: what the system looks like, the action
policy, the runbook index, and the verdict schema. Everything about the
incident is in the first user message, the brief.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from correlator.graph import ServiceGraph
from detector.alerts import Alert, iso

from .tools import RUNBOOKS, runbook_names
from .verdict import CATEGORIES, schema

SYSTEM = """\
You are the on-call triage agent for ShopSearch, a small shop-search application on Kubernetes.
An incident has opened. Find its root cause from the telemetry, then propose one action.

## The system

- gateway (TypeScript): the public API. /api/search goes to search-api, /api/products/:id to catalog.
- search-api (Python): query parsing and ranking; reads its index and cache from Redis.
- catalog (Java): product data in PostgreSQL; pushes changes to a Redis queue.
- indexer (Python worker): reads the queue, refreshes the search index in Redis, sometimes reindexes from catalog.
- redis and postgres: the data stores. node: a Kubernetes node. cluster: cluster services such as DNS.
- Call graph (caller -> callee): {edges}.
- Every service is an Argo Rollout deployed by ArgoCD; a release is a new image tag or new Helm parameters.

## How to work

- Use the tools. They are read-only and show telemetry up to the moment of triage, never later.
- An error or slowdown spreads to the callers of the component that causes it. Look for what changed
  first and furthest down the call graph, and check it against the alternatives the runbooks name.
- A release shortly before the symptoms is a strong lead, not proof: harmless releases happen all the time.
- Prefer a few targeted queries to many broad ones. Stop when the evidence settles it.
- Root-cause categories: {categories}. Components: a service, redis, postgres, node, cluster, or a
  link between two components written caller->callee when the network between them is at fault.

## Actions (exactly one)

- rollback <service>: a recent release of that service caused it. Also when a release changed the
  database and the database is now the problem: roll back the service that released.
- restart <component>: a process is stuck or leaking, or a data store went down and needs a restart.
- scale_out <service>: the service is saturated (CPU) or receives more traffic than it can serve.
- none_escalate: a human has to act (network, DNS, a filling disk, or no clear cause).
- none_verify: infrastructure that heals by itself (a lost node); someone only has to confirm it did.
An action on the wrong component makes an incident worse. When in doubt, none_escalate.
{runbooks}
## Answer

When you are done, reply with only a JSON object, no prose around it, matching this schema:
{schema}
Up to 3 hypotheses, most likely first. Each piece of evidence names the tool, its arguments and what it showed.
"""

RUNBOOK_SECTION = """
## Runbooks

One per known failure; read the full text with read_runbook(name) to check symptoms and look-alikes.
{index}
"""


def runbook_index(root: Path = RUNBOOKS) -> str:
    lines = []
    for name in runbook_names(root):
        text = (root / f"{name}.md").read_text(encoding="utf-8").splitlines()
        title = text[0].lstrip("# ").strip()
        summary = next((line.strip() for line in text[1:] if line.strip() and not line.startswith("![")), "")
        lines.append(f"- {name}: {title}. {summary}")
    return "\n".join(lines)


def system_prompt(*, runbooks: bool = True, graph: ServiceGraph | None = None) -> str:
    graph = graph or ServiceGraph.load()
    edges = ", ".join(f"{a} -> {b}" for a, b in graph.directed())
    return SYSTEM.format(
        edges=edges,
        categories=", ".join(CATEGORIES),
        runbooks=RUNBOOK_SECTION.format(index=runbook_index()) if runbooks else "",
        schema=json.dumps(schema(), separators=(",", ":")),
    )


def prompt_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def brief(incident_id: str, alerts: list[Alert], now: float) -> str:
    """The incident as the correlator handed it over, as far as it was known at `now`."""
    opened = min(a.start for a in alerts)
    rows = []
    for a in sorted(alerts, key=lambda a: a.start):
        state = "firing" if a.end is None else f"resolved at {iso(a.end)}"
        labels = ", ".join(f"{k}={v}" for k, v in sorted(a.labels.items()))
        score = f", score {a.score}" if a.score is not None else ""
        rows.append(f"- {iso(a.start)} {a.detector} {a.name} on {a.component} ({a.severity or 'no severity'}{score}), "
                    f"{state}{'; ' + labels if labels else ''}")
    return (f"Incident {incident_id} opened at {iso(opened)}. It is now {iso(now)}.\n"
            f"Its alerts so far ({len(alerts)}):\n" + "\n".join(rows) +
            "\n\nFind the root cause and propose one action.")
