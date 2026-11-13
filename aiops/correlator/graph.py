"""The service graph the correlator groups by: static edges plus Tempo's."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import yaml

from detector.alerts import COMPONENTS
from detector.series import Frame

STATIC_GRAPH = Path(__file__).with_name("graph.yaml")


class ServiceGraph:
    def __init__(self, edges: set[frozenset[str]], infrastructure: set[str], aliases: dict[str, str]):
        self.edges = set(edges)
        self.infrastructure = set(infrastructure)
        self.aliases = dict(aliases)
        self.observed: set[frozenset[str]] = set()

    @classmethod
    def load(cls, path: Path = STATIC_GRAPH) -> ServiceGraph:
        spec = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls({frozenset(e) for e in spec["edges"]}, set(spec["infrastructure"]), spec.get("aliases", {}))

    def add_observed(self, frame: Frame, min_rate: float = 0.01) -> set[frozenset[str]]:
        """Add every client->server pair Tempo saw at a meaningful rate. Returns the new edges."""
        new = set()
        for labels, values in frame.series("graph_requests"):
            client = self.aliases.get(labels.get("client", ""), labels.get("client", ""))
            server = self.aliases.get(labels.get("server", ""), labels.get("server", ""))
            if client == server or client not in COMPONENTS or server not in COMPONENTS:
                continue
            if np.all(np.isnan(values)) or np.nanmean(values) < min_rate:
                continue
            edge = frozenset((client, server))
            self.observed.add(edge)
            if edge not in self.edges:
                new.add(edge)
                self.edges.add(edge)
        return new

    def related(self, a: str, b: str) -> bool:
        """Same component, direct neighbours, or one of them is infrastructure."""
        return a == b or a in self.infrastructure or b in self.infrastructure or frozenset((a, b)) in self.edges

    def describe(self) -> list[str]:
        return sorted(" -- ".join(sorted(e)) + ("" if e in self.observed else " (static only)") for e in self.edges)
