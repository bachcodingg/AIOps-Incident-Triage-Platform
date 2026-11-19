"""The live loop's state: incidents, their triage, proposals, and the audit log.

One SQLite file (~/.incidentlens/live/state.db). Incidents and proposals are
current state and change; the audit log is append-only and hash-chained like
the chaos labels: each entry carries the hash of the one before, so
`verify_audit()` notices an entry that was edited or removed. Everything a
human or the loop did is in it: incident opened, triage verdict, policy
decision, approval or rejection with the approver's name, each command run
and its result, recovery.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_DB = Path.home() / ".incidentlens" / "live" / "state.db"
GENESIS = "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
  id TEXT PRIMARY KEY, opened REAL NOT NULL, closed REAL, status TEXT NOT NULL,
  components TEXT NOT NULL, alerts TEXT NOT NULL, verdict TEXT, run_path TEXT, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, incident TEXT NOT NULL, action TEXT NOT NULL, target TEXT,
  mode TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL, plan TEXT, result TEXT,
  decided_by TEXT, decided_at REAL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY, at REAL NOT NULL, actor TEXT NOT NULL, event TEXT NOT NULL,
  incident TEXT, data TEXT NOT NULL, prev TEXT NOT NULL, hash TEXT NOT NULL
);
"""

# Incident status, in the order an incident moves through them.
STATUSES = ("open", "triaged", "awaiting-approval", "acting", "acted", "escalated", "rejected", "resolved")


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


class Store:
    def __init__(self, path: Path | str = DEFAULT_DB):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()  # the loop and the API share the connection
        with self.lock:
            self.db.executescript(SCHEMA)

    # --- audit ----------------------------------------------------------------------
    def audit(self, actor: str, event: str, incident: str | None = None, data: dict[str, Any] | None = None,
              at: float | None = None) -> int:
        with self.lock:
            row = self.db.execute("SELECT seq, hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
            seq, prev = (row["seq"] + 1, row["hash"]) if row else (1, GENESIS)
            at = time.time() if at is None else at
            body = {"seq": seq, "at": round(at, 3), "actor": actor, "event": event, "incident": incident,
                    "data": data or {}, "prev": prev}
            digest = hashlib.sha256(_canonical(body).encode()).hexdigest()
            self.db.execute("INSERT INTO audit VALUES (?,?,?,?,?,?,?,?)",
                            (seq, body["at"], actor, event, incident, _canonical(data or {}), prev, digest))
            self.db.commit()
            return seq

    def audit_log(self, incident: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM audit", ()
        if incident:
            q, args = q + " WHERE incident = ?", (incident,)
        with self.lock:
            rows = self.db.execute(q + " ORDER BY seq DESC LIMIT ?", (*args, limit)).fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in reversed(rows)]

    def verify_audit(self) -> tuple[bool, str]:
        prev = GENESIS
        with self.lock:
            rows = self.db.execute("SELECT * FROM audit ORDER BY seq").fetchall()
        for n, r in enumerate(rows, start=1):
            body = {"seq": r["seq"], "at": r["at"], "actor": r["actor"], "event": r["event"],
                    "incident": r["incident"], "data": json.loads(r["data"]), "prev": r["prev"]}
            if r["seq"] != n or r["prev"] != prev or hashlib.sha256(_canonical(body).encode()).hexdigest() != r["hash"]:
                return False, f"entry {r['seq']} was changed or one before it is missing"
            prev = r["hash"]
        return True, f"{len(rows)} entries, chain intact"

    # --- incidents --------------------------------------------------------------------
    def upsert_incident(self, inc: dict[str, Any], now: float) -> bool:
        """Insert a new incident or refresh its alerts. True if it is new."""
        with self.lock:
            old = self.db.execute("SELECT id FROM incidents WHERE id = ?", (inc["id"],)).fetchone()
            if old:
                self.db.execute("UPDATE incidents SET alerts = ?, components = ?, updated = ? WHERE id = ?",
                                (_canonical(inc["alerts"]), _canonical(inc["components"]), now, inc["id"]))
            else:
                self.db.execute("INSERT INTO incidents (id, opened, status, components, alerts, updated) VALUES (?,?,?,?,?,?)",
                                (inc["id"], inc["opened"], "open", _canonical(inc["components"]), _canonical(inc["alerts"]), now))
            self.db.commit()
            return old is None

    def set_incident(self, incident_id: str, **fields: Any) -> None:
        fields = {k: (_canonical(v) if isinstance(v, (dict, list)) else v) for k, v in fields.items()}
        fields["updated"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.lock:
            self.db.execute(f"UPDATE incidents SET {cols} WHERE id = ?", (*fields.values(), incident_id))
            self.db.commit()

    def incident(self, incident_id: str) -> dict[str, Any] | None:
        with self.lock:
            r = self.db.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        return self._incident(r) if r else None

    def incidents(self, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM incidents", ()
        if status:
            q, args = q + " WHERE status = ?", (status,)
        with self.lock:
            rows = self.db.execute(q + " ORDER BY opened DESC LIMIT ?", (*args, limit)).fetchall()
        return [self._incident(r) for r in rows]

    @staticmethod
    def _incident(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        for k in ("components", "alerts", "verdict"):
            d[k] = json.loads(d[k]) if d.get(k) else None
        return d

    # --- proposals ----------------------------------------------------------------------
    def add_proposal(self, incident: str, action: str, target: str | None, mode: str, reason: str,
                     status: str, plan: dict[str, Any] | None = None) -> int:
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO proposals (incident, action, target, mode, reason, status, plan, created) VALUES (?,?,?,?,?,?,?,?)",
                (incident, action, target, mode, reason, status, _canonical(plan) if plan else None, time.time()))
            self.db.commit()
            return int(cur.lastrowid)

    def set_proposal(self, pid: int, **fields: Any) -> None:
        fields = {k: (_canonical(v) if isinstance(v, (dict, list)) else v) for k, v in fields.items()}
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.lock:
            self.db.execute(f"UPDATE proposals SET {cols} WHERE id = ?", (*fields.values(), pid))
            self.db.commit()

    def proposal(self, pid: int) -> dict[str, Any] | None:
        with self.lock:
            r = self.db.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
        return self._proposal(r) if r else None

    def proposals(self, incident: str | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM proposals", ()
        if incident:
            q, args = q + " WHERE incident = ?", (incident,)
        with self.lock:
            rows = self.db.execute(q + " ORDER BY id", args).fetchall()
        return [self._proposal(r) for r in rows]

    @staticmethod
    def _proposal(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        for k in ("plan", "result"):
            d[k] = json.loads(d[k]) if d.get(k) else None
        return d

    def executed(self) -> list[dict[str, Any]]:
        """Actions that ran, for the policy's cooldown and rate limit."""
        with self.lock:
            rows = self.db.execute("SELECT action, target, decided_at FROM proposals WHERE status = 'executed'").fetchall()
        return [{"action": r["action"], "target": r["target"], "at": r["decided_at"] or 0} for r in rows]
