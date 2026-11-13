import json
from pathlib import Path

import pytest

from chaos_runner.labels import LabelWriter, SealError
from correlator.graph import ServiceGraph
from detector.alerts import Alert, iso, write_jsonl
from harness.cli import main
from harness.dataset import FreezeError, build, freeze, set_hash, verify_lock
from harness.score import score
from tests.synth import T0, make_chunks, write_recording

FAULTS = [f"F{n:02d}" for n in range(1, 15)]


def make_run(tmp_path: Path, *, aborted: int = 0, late: int = 0):
    """A fake dataset run: 14 faults x 5, 10 fault-free windows, 5 benign releases,
    all inside a 4-hour recording (overlapping in time, which is fine here)."""
    labels_dir = tmp_path / "labels" / "run-x"
    writer = LabelWriter(labels_dir)
    writer.write({"kind": "run-start", "run": "run-x", "seed": 7})
    start = T0 + 3600
    n = 0
    for fault in FAULTS:
        for rep in range(5):
            s = start + n * 60
            rec = {"kind": "fault", "fault": fault, "target": "catalog", "start": iso(s), "end": iso(s + 300),
                   "root_cause": {"component": "catalog", "category": "bad-release"}, "correct_action": "rollback"}
            if n < aborted:
                rec["aborted"] = True
            if late and n >= 70 - late:  # ends too close to the end of the recording
                rec["start"], rec["end"] = iso(T0 + 4 * 3600 - 600), iso(T0 + 4 * 3600 - 300)
            writer.write(rec)
            n += 1
    for k in range(10):
        writer.write({"kind": "fault-free", "start": iso(start + k * 300), "end": iso(start + k * 300 + 1800)})
    for k in range(5):
        writer.write({"kind": "benign-release", "target": "gateway", "start": iso(start + k * 600),
                      "end": iso(start + k * 600 + 120)})
    writer.write({"kind": "run-end", "run": "run-x"})
    seals = tmp_path / "repo" / "eval" / "seals"
    seals.mkdir(parents=True)
    seal = seals / "run-x.json"
    seal.write_text(json.dumps({"run": "run-x", "records": writer.count, "head_sha256": writer.head}))
    recording = write_recording(tmp_path / "rec", make_chunks(4))
    return writer.path, seal, recording


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    return make_run(tmp_path_factory.mktemp("run"))


def test_freeze_splits_by_fault_and_publishes_only_hashes(run, tmp_path):
    labels, seal, recording = run
    lock_path = tmp_path / "repo" / "eval" / "dataset" / "run-x.lock.json"
    lock = freeze(labels, seal, recording, lock_path, split_path=tmp_path / "sealed" / "split.json")
    assert lock["split"]["tuning"] == {"incidents": 28, "fault_free_windows": 4, "benign_releases": 2}
    assert lock["split"]["test"] == {"incidents": 42, "fault_free_windows": 6, "benign_releases": 3}
    assert all(v == {"tuning": 2, "test": 3} for v in lock["split"]["by_fault"].values())
    public = lock_path.read_text()
    assert "catalog" not in public and "rollback" not in public  # no ground truth in Git
    sealed = json.loads((tmp_path / "sealed" / "split.json").read_text())
    assert set_hash(sealed["test"]) == lock["test_set_sha256"]
    assert not {r["seq"] for r in sealed["test"]} & {r["seq"] for r in sealed["tuning"]}
    with pytest.raises(FreezeError, match="not frozen again"):
        freeze(labels, seal, recording, lock_path, split_path=tmp_path / "sealed" / "split.json")


def test_the_split_cannot_be_chosen_it_follows_from_the_labels(run):
    labels, seal, recording = run
    a, _ = build(labels, seal, recording)
    b, _ = build(labels, seal, recording)
    assert a["test_set_sha256"] == b["test_set_sha256"]


def test_unusable_incidents_are_set_aside_with_a_reason(tmp_path):
    labels, seal, recording = make_run(tmp_path, aborted=2, late=3)
    lock, sealed = build(labels, seal, recording)
    assert lock["excluded"] == {"aborted": 2, "telemetry gap": 3}
    assert lock["split"]["tuning"]["incidents"] + lock["split"]["test"]["incidents"] == 65
    assert len(sealed["excluded"]) == 5


def test_a_changed_label_breaks_the_seal(tmp_path):
    labels, seal, recording = make_run(tmp_path)
    lines = labels.read_text().splitlines()
    lines[5] = lines[5].replace("catalog", "gateway")
    labels.write_text("\n".join(lines) + "\n")
    with pytest.raises(SealError):
        build(labels, seal, recording)


def test_verify_catches_a_swapped_incident_and_a_changed_recording(tmp_path):
    labels, seal, recording = make_run(tmp_path)
    lock_path = seal.parent.parent / "dataset" / "run-x.lock.json"
    split_path = tmp_path / "sealed" / "split.json"
    freeze(labels, seal, recording, lock_path, split_path=split_path)
    assert verify_lock(lock_path, labels, recording, split_path) == []

    sealed = json.loads(split_path.read_text())
    sealed["test"][0], sealed["tuning"][0] = sealed["tuning"][0], sealed["test"][0]
    split_path.write_text(json.dumps(sealed))
    assert set(verify_lock(lock_path, labels, recording, split_path)) == {
        "tuning set: does not match its hash", "test set: does not match its hash"}

    next(iter(sorted((recording / "chunks").glob("*.gz")))).write_bytes(b"x")
    assert "recording: its chunks changed since the freeze" in verify_lock(lock_path, labels, recording, split_path)


def test_score_measures_recall_mttd_noise_and_false_alarms():
    items = [
        {"kind": "fault", "fault": "F02", "start": iso(T0), "end": iso(T0 + 600)},
        {"kind": "fault", "fault": "F09", "start": iso(T0 + 3600), "end": iso(T0 + 4000)},
        {"kind": "fault-free", "start": iso(T0 + 7200), "end": iso(T0 + 7200 + 3600)},
    ]
    alerts = [
        # F02: burn-rate after 90 s, anomaly after 60 s and a correlated catalog alert; static never.
        Alert("burn-rate", "ProductsAvailabilityBudgetBurn", "gateway", T0 + 90, T0 + 700),
        Alert("anomaly", "AnomalyMAD", "catalog", T0 + 60, T0 + 650),
        Alert("anomaly", "AnomalyMAD", "gateway", T0 + 75, T0 + 650),
        # A static alert that was already firing before F09 started does not detect it.
        Alert("static", "StaticNodeNotReady", "node", T0 + 3500, T0 + 3700),
        # One anomaly in the fault-free hour: a false alarm.
        Alert("anomaly", "AnomalyMAD", "redis", T0 + 8000, T0 + 8100),
    ]
    r = score(alerts, items, ServiceGraph.load())
    assert r["B0"]["recall"] == 0 and r["B0"]["mttd_s"] is None
    assert r["B1"]["recall"] == 0.5 and r["B1"]["mttd_s"] == 90 and r["B1"]["false_alarms_per_day"] == 0
    b2 = r["B2"]
    assert b2["recall"] == 0.5 and b2["mttd_s"] == 60
    assert b2["alerts_per_incident"] == 3 and b2["incidents_per_incident"] == 1
    assert b2["noise_reduction"] == pytest.approx(2 / 3)
    assert b2["false_alarms_per_day"] == pytest.approx(24)
    assert b2["per_fault"] == {"F02": {"detected": 1, "of": 1, "mttd_s": 60}, "F09": {"detected": 0, "of": 1, "mttd_s": None}}


def test_the_test_set_is_not_scored_before_week_10(run, tmp_path, capsys):
    labels, seal, recording = run
    split_path = tmp_path / "split.json"
    freeze(labels, seal, recording, tmp_path / "lock.json", split_path=split_path)
    write_jsonl(tmp_path / "alerts.jsonl", [])
    args = ["score", "--alerts", str(tmp_path / "alerts.jsonl"), "--split", str(split_path)]
    assert main(args + ["--set", "test"]) == 2
    assert "week 10" in capsys.readouterr().err
    assert main(args) == 0
    assert "Detection on the tuning set" in capsys.readouterr().out
