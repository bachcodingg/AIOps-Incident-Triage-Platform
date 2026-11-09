import pytest

from indexer.faults import Faults


def test_off_by_default():
    faults = Faults.from_env({})
    faults.on_poll()
    assert faults.retained == []


@pytest.mark.parametrize("raw", ["-1", "lots"])
def test_rejects_bad_values(raw):
    with pytest.raises(ValueError, match="FAULT_LEAK_KB_PER_POLL"):
        Faults.from_env({"FAULT_LEAK_KB_PER_POLL": raw})


def test_keeps_memory_after_every_poll():
    faults = Faults.from_env({"FAULT_LEAK_KB_PER_POLL": "512"})
    for _ in range(4):
        faults.on_poll()
    assert sum(map(len, faults.retained)) == 4 * 512 * 1024
