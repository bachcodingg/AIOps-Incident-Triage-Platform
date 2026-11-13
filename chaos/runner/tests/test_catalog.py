import json

import pytest
import yaml

from chaos_runner.catalog import (
    RUNBOOKS_DIR,
    RUNTIME_VARS,
    Catalog,
    CatalogError,
    parse_duration,
    render,
)


@pytest.fixture(scope="module")
def catalog():
    return Catalog.load()


def every_injection(catalog):
    for fault in catalog.faults.values():
        for target in fault.targets:
            for intensity in fault.intensities_for(target):
                yield fault, target, intensity


def test_the_catalog_has_the_fourteen_planned_faults(catalog):
    assert list(catalog.faults) == [f"F{n:02d}" for n in range(1, 15)]


def test_every_fault_has_a_runbook(catalog):
    for fault in catalog.faults.values():
        assert (RUNBOOKS_DIR / fault.runbook).is_file(), f"{fault.id}: docs/runbooks/{fault.runbook} is missing"


@pytest.mark.parametrize("text, seconds", [("90s", 90), ("10m", 600), ("1m30s", 90), ("1h", 3600), (45, 45)])
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "10", "ten minutes", "5x"])
def test_parse_duration_rejects_nonsense(text):
    with pytest.raises(CatalogError):
        parse_duration(text)


def test_render_is_strict():
    assert render("${a}-${b}", {"a": 1, "b": "x"}) == "1-x"
    with pytest.raises(CatalogError, match="missing|not set"):
        render("${a}-${nope}", {"a": 1})


def test_every_experiment_renders_to_labelled_chaos_objects(catalog):
    kinds = set()
    for fault, target, intensity in every_injection(catalog):
        if not fault.experiment:
            continue
        v = catalog.variables(fault, target, intensity, fault.duration[0])
        v.update(name="i001-x", **RUNTIME_VARS.get(fault.injector, {}))
        docs = [d for d in yaml.safe_load_all(render(fault.experiment_text(), v)) if d]
        assert docs, fault.id
        for d in docs:
            assert d["metadata"]["name"] == "i001-x"
            assert d["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "chaos-runner"
            kinds.add(d["kind"])
            if d["apiVersion"].startswith("chaos-mesh.org"):
                assert d["spec"]["duration"] == f"{fault.duration[0]}s"
    assert {"StressChaos", "NetworkChaos", "PodChaos", "Job", "ConfigMap"} <= kinds


def test_symptoms_and_labels_render_completely(catalog):
    for fault, target, intensity in every_injection(catalog):
        v = catalog.variables(fault, target, intensity, 600)
        assert "${" not in render(fault.symptom, v)
        cause = json.loads(render(json.dumps(fault.root_cause), v))
        assert cause["component"] and "${" not in cause["component"]


def test_link_faults_shape_the_destinations_replies_to_the_source(catalog):
    fault = catalog.fault("F07")
    v = catalog.variables(fault, "search-api->redis", "100ms", 600)
    v["name"] = "x"
    chaos = yaml.safe_load(render(fault.experiment_text(), v))
    assert chaos["spec"]["selector"]["labelSelectors"] == {"app.kubernetes.io/name": "redis"}
    assert chaos["spec"]["target"]["selector"]["labelSelectors"] == {"app.kubernetes.io/name": "search-api"}
    assert chaos["spec"]["direction"] == "to"
    assert render(json.dumps(fault.root_cause), v) == json.dumps({"component": "search-api->redis", "category": "network"})


def test_bad_releases_target_released_services_and_bake_in_a_flag(catalog):
    for fault in catalog.faults.values():
        if fault.injector in {"image-release", "config-release"}:
            for name, t in fault.targets.items():
                assert catalog.components[name].kind == "rollout"
                assert t.env


def test_validation_catches_mistakes(tmp_path, catalog):
    raw = yaml.safe_load(catalog.path.read_text(encoding="utf-8"))

    def broken(change):
        data = json.loads(json.dumps(raw))
        change(data["faults"][0])
        path = tmp_path / "catalog.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        with pytest.raises(CatalogError):
            Catalog.load(path)

    broken(lambda f: f.update(injector="telepathy"))
    broken(lambda f: f.update(correct_action="panic"))
    broken(lambda f: f.update(symptom="up{job=\"${nobody}\"}"))
    broken(lambda f: f["root_cause"].update(category="bad-luck"))
    broken(lambda f: f.pop("runbook"))
    broken(lambda f: f.update(duration=["10m", "5m"]))
