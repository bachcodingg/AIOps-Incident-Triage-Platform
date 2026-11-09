from datetime import timedelta

import pytest

from chaos_runner.catalog import Catalog
from chaos_runner.labels import LabelWriter, read
from chaos_runner.runner import Interrupted, Runner
from chaos_runner.schedule import BENIGN, FAULT, FAULT_FREE, Item
from tests.fakes import FakeClock, FakeCluster


@pytest.fixture(scope="module")
def catalog():
    return Catalog.load()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def cluster(clock):
    return FakeCluster(clock)


def runner(cluster, catalog, clock):
    return Runner(cluster, catalog, clock=clock, sleep=clock.sleep, log=lambda _: None, poll_s=15)


def fault_item(fault="F05", target="catalog", intensity="4", duration=600, gap=60):
    return Item(id="i001", kind=FAULT, duration_s=duration, gap_s=gap, fault=fault, target=target, intensity=intensity)


def symptom_between(clock, start_after, end_after):
    t0 = clock()
    return lambda query, now: t0 + timedelta(seconds=start_after) <= now < t0 + timedelta(seconds=end_after)


def test_dry_run_sees_the_symptom_and_stops_early(cluster, catalog, clock):
    cluster.symptom = symptom_between(clock, 60, 400)
    seen = []
    label = runner(cluster, catalog, clock).inject(
        fault_item(), check_symptom=True, symptom_timeout_s=300, stop_after_symptom_s=90, on_symptom=seen.append)

    assert label["symptom_before"] is False
    assert label["symptom_seen"] is not None and seen == [label]
    assert label["root_cause"] == {"component": "catalog", "category": "resource"}
    assert label["correct_action"] == "scale_out"
    assert label["baseline_healthy"] is True
    duration = (clock() - clock().replace(hour=20, minute=0, second=0)).total_seconds()
    assert duration < 600  # ended 90 s after the symptom, not after the full 10 minutes
    assert label["recovered"] is not None
    assert ("delete", "stresschaos", "i001-f05", "chaos") in cluster.calls


def test_a_symptom_that_never_shows_is_recorded(cluster, catalog, clock):
    label = runner(cluster, catalog, clock).inject(fault_item(duration=300), check_symptom=True, symptom_timeout_s=120)
    assert label["symptom_seen"] is None
    assert label["end"] is not None


def test_a_failed_injection_still_cleans_up(cluster, catalog, clock):
    cluster.fail_on = "apply"
    label = runner(cluster, catalog, clock).inject(fault_item())
    assert "apply failed" in label["error"]
    assert ("delete", "stresschaos", "i001-f05", "chaos") not in cluster.calls  # nothing was created
    assert label["end"] is not None


def test_redis_down_triggers_a_reindex_afterwards(cluster, catalog, clock):
    runner(cluster, catalog, clock).inject(fault_item("F10", "redis", None, 360))
    assert ("restart_rollout", "indexer") in cluster.calls


def test_recovery_waits_for_health(cluster, catalog, clock):
    t0 = clock()
    cluster.healthy = lambda now: now > t0 + timedelta(seconds=900)
    label = runner(cluster, catalog, clock).inject(fault_item(duration=300))
    assert label["baseline_healthy"] is False
    recovered = label["recovered"]
    assert recovered is not None and recovered > label["end"]


def test_a_run_writes_a_sealed_chain_of_labels(tmp_path, cluster, catalog, clock):
    items = [
        fault_item(gap=120),
        Item(id="i002", kind=FAULT_FREE, duration_s=1800, gap_s=60),
        Item(id="i003", kind=BENIGN, duration_s=300, gap_s=60, target="gateway"),
    ]
    writer = LabelWriter(tmp_path / "run", repo_root=tmp_path / "repo")
    head = runner(cluster, catalog, clock).run(items, writer, "run-x", seed=7)

    records = read(writer.path)
    assert [r["kind"] for r in records] == ["run-start", FAULT, FAULT_FREE, BENIGN, "run-end"]
    assert records[0]["seed"] == 7 and len(records[0]["plan"]) == 3
    assert head == writer.head
    benign = records[3]
    build = next(c for c in cluster.calls if c[0] == "build_release_image")
    assert list(build[3]) == ["BUILD_ID"]  # same path as a bad release, harmless variable
    assert benign["details"]["tag"] == build[2]
    assert not cluster.root_held


def test_an_interrupted_run_still_records_the_fault_in_progress(tmp_path, cluster, catalog, clock):
    calls = {"n": 0}

    def sleep(seconds):
        calls["n"] += 1
        if calls["n"] == 3:
            raise Interrupted()
        clock.sleep(seconds)

    r = Runner(cluster, catalog, clock=clock, sleep=sleep, log=lambda _: None)
    writer = LabelWriter(tmp_path / "run", repo_root=tmp_path / "repo")
    with pytest.raises(Interrupted):
        r.run([fault_item()], writer, "run-x", seed=1)
    records = read(writer.path)
    assert [r["kind"] for r in records] == ["run-start", FAULT, "run-end"]
    assert records[1]["aborted"] is True and records[2]["interrupted"] is True
    assert ("delete", "stresschaos", "i001-f05", "chaos") in cluster.calls
