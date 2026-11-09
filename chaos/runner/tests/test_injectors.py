import pytest

from chaos_runner.catalog import Catalog
from chaos_runner.injectors import (
    CHAOS_NAMESPACE,
    FILL_TABLE,
    ConfigRelease,
    ImageRelease,
    Injection,
    Load,
    NodeStop,
    SqlFill,
    ChaosMesh,
    cleanup,
    injector_for,
)
from tests.fakes import FakeCluster


@pytest.fixture(scope="module")
def catalog():
    return Catalog.load()


def injection(catalog, fault_id, target, intensity=None, duration=600):
    fault = catalog.fault(fault_id)
    v = catalog.variables(fault, target, intensity, duration)
    v["name"] = f"i001-{fault_id.lower()}"
    env = {k: val.replace("${intensity}", intensity or "") for k, val in fault.targets[target].env.items()}
    return Injection(v["name"], target, intensity, duration, v, fault, env)


def test_image_release_bakes_the_flag_into_a_new_tag_and_reverts(catalog):
    c = FakeCluster()
    inj = injection(catalog, "F02", "catalog", "0.05")
    release = ImageRelease(c)

    details = release.start(inj)
    assert c.calls[0][0] == "build_release_image"
    _, service, tag, env = c.calls[0]
    assert (service, env) == ("catalog", {"FAULT_ERROR_RATE": "0.05"})
    assert len(tag) == 7 and details["tag"] == tag
    assert c.params["catalog"] == {"image.tag": tag}  # the history shows a tag, not the flag
    assert c.root_held and details["promoted"]

    release.stop(inj, details)
    assert "catalog" not in c.params and not c.root_held
    assert c.rollout("catalog")["stableRS"] == "stable0"
    assert ("remove_image", details["image"]) in c.calls


def test_config_release_is_an_env_change(catalog):
    c = FakeCluster()
    inj = injection(catalog, "F01", "search-api")
    details = ConfigRelease(c).start(inj)
    assert c.params["search-api"] == {"env.CACHE_TTL_SECONDS": "60s"}
    assert details["promoted"] is None  # a crash loop never becomes stable
    ConfigRelease(c).stop(inj, details)
    assert not c.root_held and "search-api" not in c.params


def test_reverting_an_aborted_release_does_not_promote_again(catalog):
    c = FakeCluster()
    inj = injection(catalog, "F01", "gateway")
    release = ConfigRelease(c)
    details = release.start(inj)
    # The analysis aborted it: the stable pods never changed.
    c.hashes["gateway"]["stableRS"] = "stable0"
    c.hashes["gateway"]["previous"] = "stable0"
    promotions = c.names().count("promote_full")
    release.stop(inj, details)
    assert c.names().count("promote_full") == promotions
    assert not c.root_held


def test_release_stop_releases_root_even_after_a_failed_start(catalog):
    c = FakeCluster()
    c.fail_on = "promote_full"
    inj = injection(catalog, "F03", "gateway", "250")
    release = ImageRelease(c)
    with pytest.raises(Exception):
        release.start(inj)
    c.fail_on = None
    release.stop(inj, {})
    assert not c.root_held


def test_chaos_mesh_creates_the_experiment_in_the_chaos_namespace(catalog):
    c = FakeCluster()
    inj = injection(catalog, "F05", "catalog", "4")
    details = ChaosMesh(c).start(inj)
    assert ("apply", CHAOS_NAMESPACE) in c.calls
    obj = c.applied[0]
    assert obj["kind"] == "StressChaos" and obj["spec"]["stressors"]["cpu"]["workers"] == 4
    assert obj["spec"]["selector"]["namespaces"] == ["shopsearch"]
    ChaosMesh(c).stop(inj, details)
    assert ("delete", "stresschaos", "i001-f05", CHAOS_NAMESPACE) in c.calls


def test_coredns_failure_targets_kube_system(catalog):
    c = FakeCluster()
    ChaosMesh(c).start(injection(catalog, "F09", "coredns"))
    spec = c.applied[0]["spec"]
    assert spec["action"] == "pod-failure"
    assert spec["selector"] == {"namespaces": ["kube-system"], "labelSelectors": {"k8s-app": "kube-dns"}}


def test_load_spreads_the_spike_over_clients_under_the_rate_limit(catalog):
    c = FakeCluster()
    c.gateway_rps = 6.0
    details = Load(c).start(injection(catalog, "F06", "gateway", "5"))
    assert details["extra_rps"] == 24.0 and details["pods"] == 3
    job = next(d for d in c.applied if d["kind"] == "Job")
    assert job["spec"]["parallelism"] == 3
    script = next(d for d in c.applied if d["kind"] == "ConfigMap")["data"]["script.js"]
    assert "rate: 480," in script  # 8 requests a second per pod, under 600 a minute


def test_sql_fill_grows_a_table_then_drops_it(catalog):
    c = FakeCluster()
    inj = injection(catalog, "F13", "postgres", "10", duration=600)
    fill = SqlFill(c)
    details = fill.start(inj)
    loop = next(call[1] for call in c.calls if call[0] == "psql_background")
    assert "FOR i IN 1..120 LOOP" in loop and f"INSERT INTO {FILL_TABLE}" in loop
    assert details["rows_per_step"] * 12 * 1100 == pytest.approx(10 * 1024 * 1024, rel=0.01)
    fill.stop(inj, details)
    assert c.calls[-1] == ("psql", f"DROP TABLE IF EXISTS {FILL_TABLE}")


def test_node_stop_and_start(catalog):
    c = FakeCluster()
    inj = injection(catalog, "F14", "worker")
    NodeStop(c).start(inj)
    assert not c.node_ready("worker")
    NodeStop(c).stop(inj, {})
    assert c.node_ready("worker")


def test_every_injector_in_the_catalog_exists(catalog):
    for fault in catalog.faults.values():
        injector_for(fault.injector, FakeCluster())


def test_cleanup_releases_everything(catalog):
    c = FakeCluster()
    c.hold_root()
    c.set_parameters("gateway", {"image.tag": "abc1234"})
    c.stop_node("worker")
    cleanup(c, catalog.rollouts())
    assert not c.root_held and not c.params and c.node_ready("worker")
