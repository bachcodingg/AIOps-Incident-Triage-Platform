from collections import Counter, defaultdict

import pytest

from chaos_runner.catalog import Catalog
from chaos_runner.schedule import BENIGN, FAULT, FAULT_FREE, Item, plan, summary


@pytest.fixture(scope="module")
def catalog():
    return Catalog.load()


def test_the_default_plan_is_the_planned_dataset(catalog):
    items = plan(catalog, seed=1)
    kinds = Counter(i.kind for i in items)
    assert kinds == {FAULT: 70, FAULT_FREE: 20, BENIGN: 15}
    assert Counter(i.fault for i in items if i.kind == FAULT) == {f: 5 for f in catalog.faults}
    assert [i.id for i in items] == [f"i{n:03d}" for n in range(1, len(items) + 1)]
    assert "70 fault injections" in summary(items)


def test_same_seed_same_plan_other_seed_other_order(catalog):
    assert plan(catalog, seed=42) == plan(catalog, seed=42)
    assert [i.fault for i in plan(catalog, seed=42)] != [i.fault for i in plan(catalog, seed=43)]


def test_targets_and_intensities_are_covered_not_left_to_chance(catalog):
    for seed in range(20):
        uses = defaultdict(list)
        for i in plan(catalog, seed=seed, fault_free=0, benign=0):
            uses[(i.fault, i.target)].append(i.intensity)
        for fault in catalog.faults.values():
            used = {t for (f, t) in uses if f == fault.id}
            assert used == set(fault.targets), (seed, fault.id)
            for t in fault.targets:
                levels = fault.intensities_for(t)
                assert len(set(uses[(fault.id, t)])) == min(len(uses[(fault.id, t)]), len(levels)), (seed, fault.id, t)


def test_durations_and_gaps_stay_in_range(catalog):
    lo_gap, hi_gap = (m * 60 for m in catalog.schedule["gap_minutes"])
    for i in plan(catalog, seed=5):
        assert lo_gap - 30 <= i.gap_s <= hi_gap + 30
        if i.kind == FAULT:
            lo, hi = catalog.fault(i.fault).duration
            assert lo - 15 <= i.duration_s <= hi + 15
            assert i.duration_s % 30 == 0


def test_a_subset_of_faults(catalog):
    items = plan(catalog, seed=3, faults=["F05", "f10"], repeats=2, fault_free=1, benign=0)
    assert Counter(i.fault for i in items if i.kind == FAULT) == {"F05": 2, "F10": 2}
    assert sum(i.kind == FAULT_FREE for i in items) == 1


def test_items_round_trip(catalog):
    for i in plan(catalog, seed=9)[:5]:
        assert Item.from_dict(i.to_dict()) == i
