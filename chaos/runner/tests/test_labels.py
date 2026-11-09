import json

import pytest

from chaos_runner.labels import GENESIS, LabelWriter, SealError, check_sealed_location, read, verify


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    return root


def write_three(tmp_path, repo):
    w = LabelWriter(tmp_path / "labels" / "run-1", repo_root=repo)
    for n in range(3):
        w.write({"kind": "fault", "fault": f"F0{n + 1}", "target": "catalog"})
    return w


def test_chain_verifies_and_head_pins_it(tmp_path, repo):
    w = write_three(tmp_path, repo)
    count, head = verify(w.path, expected_head=w.head)
    assert (count, head) == (3, w.head)
    assert head != GENESIS
    assert [r["seq"] for r in read(w.path)] == [1, 2, 3]


def test_writer_continues_an_existing_file(tmp_path, repo):
    w = write_three(tmp_path, repo)
    again = LabelWriter(w.run_dir, repo_root=repo)
    assert (again.count, again.head) == (3, w.head)
    again.write({"kind": "run-end"})
    assert verify(w.path)[0] == 4


@pytest.mark.parametrize("tamper", ["edit", "drop", "swap"])
def test_any_change_breaks_the_chain(tmp_path, repo, tamper):
    w = write_three(tmp_path, repo)
    lines = w.path.read_text(encoding="utf-8").splitlines()
    if tamper == "edit":
        record = json.loads(lines[1])
        record["fault"] = "F14"
        lines[1] = json.dumps(record, sort_keys=True, separators=(",", ":"))
    elif tamper == "drop":
        del lines[1]
    else:
        lines[0], lines[1] = lines[1], lines[0]
    w.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(SealError):
        verify(w.path)


def test_rewriting_the_whole_file_changes_the_head(tmp_path, repo):
    w = write_three(tmp_path, repo)
    published = w.head
    w.path.unlink()
    forged = LabelWriter(w.run_dir, repo_root=repo)
    for fault in ("F01", "F02", "F14"):
        forged.write({"kind": "fault", "fault": fault, "target": "catalog"})
    with pytest.raises(SealError, match="head"):
        verify(forged.path, expected_head=published)


def test_labels_may_not_live_in_the_repository(tmp_path, repo):
    with pytest.raises(SealError, match="inside the repository"):
        LabelWriter(repo / "eval" / "labels", repo_root=repo)
    with pytest.raises(SealError):
        check_sealed_location(repo, repo)
    check_sealed_location(tmp_path / "elsewhere", repo)
