"""Read a recording back: its chunks, in order, and the hashes that pin them."""

from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .record import file_sha256, parse_iso

DEFAULT_RECORDINGS = Path.home() / ".incidentlens" / "recordings"


def resolve(name_or_path: str | Path) -> Path:
    """A recording directory, or the name of one under ~/.incidentlens/recordings."""
    path = Path(name_or_path)
    if not (path / "recording.json").exists() and (DEFAULT_RECORDINGS / str(name_or_path) / "recording.json").exists():
        return DEFAULT_RECORDINGS / str(name_or_path)
    return path


class Recording:
    def __init__(self, path: Path):
        self.path = Path(path)
        meta = self.path / "recording.json"
        if not meta.exists():
            raise FileNotFoundError(f"{self.path} is not a recording (no recording.json)")
        self.meta: dict[str, Any] = json.loads(meta.read_text(encoding="utf-8"))
        self.chunk_seconds: int = self.meta["chunk_seconds"]

    def chunk_paths(self) -> list[Path]:
        return sorted((self.path / "chunks").glob("*.json.gz"))

    @staticmethod
    def load(path: Path) -> dict[str, Any]:
        with gzip.open(path, "rb") as f:
            return json.loads(f.read())

    def chunks(self, start: float | None = None, end: float | None = None) -> Iterator[dict[str, Any]]:
        """Chunks overlapping [start, end), oldest first."""
        for p in self.chunk_paths():
            chunk = self.load(p)
            t0, t1 = parse_iso(chunk["start"]), parse_iso(chunk["end"])
            if (start is None or t1 > start) and (end is None or t0 < end):
                yield chunk

    def span(self) -> tuple[float, float] | None:
        paths = self.chunk_paths()
        if not paths:
            return None
        first, last = self.load(paths[0]), self.load(paths[-1])
        return parse_iso(first["start"]), parse_iso(last["end"])

    def gaps(self) -> list[tuple[float, float]]:
        """Missing stretches between the first and the last chunk."""
        starts = [parse_iso(self.load(p)["start"]) for p in self.chunk_paths()]
        return [(a + self.chunk_seconds, b) for a, b in zip(starts, starts[1:]) if b - a > self.chunk_seconds]

    def covered(self, start: float, end: float) -> bool:
        span = self.span()
        if span is None or start < span[0] or end > span[1]:
            return False
        return not any(g0 < end and g1 > start for g0, g1 in self.gaps())

    def manifest(self) -> dict[str, Any]:
        return {"queries_sha256": self.meta["queries_sha256"], "chunk_seconds": self.chunk_seconds,
                "chunks": [[p.name, file_sha256(p)] for p in self.chunk_paths()]}

    def sha256(self) -> str:
        canonical = json.dumps(self.manifest(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
