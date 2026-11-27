"""Phase 4: drift since verification, and tracing Must acceptance criteria to tests."""
from __future__ import annotations

import re
from pathlib import Path

from idea_to_scaffold.backlog import load_backlog
from idea_to_scaffold.cli import main
from idea_to_scaffold.package import Package
from idea_to_scaffold.trace import criteria

from .test_landing import git_identity, py, repo  # noqa: F401 - fixtures
from .test_parallel import git, run_json

STORE = "src/streak/store.py"


def verified(root: Path, ident: str) -> str:
    return next(i.get("Verified") for i in load_backlog(Package.load(root)) if i.id == ident)


def done_on_main(repo: Path, ident: str, path: str, content: str) -> None:  # noqa: F811
    (repo / path).write_text(content)
    assert main(["done", ident, str(repo)]) == 0
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", f"feat: {ident}")


# --- the phase gate: a change to verified code shows up as drift, and re-verifying clears it --------------------

def test_drift_names_the_items_and_requirements_whose_code_changed_after_verification(repo: Path, capsys):  # noqa: F811
    done_on_main(repo, "T-2.1", STORE, "def add(): ...\n")
    assert re.fullmatch(r"\d{4}-\d\d-\d\d, base [0-9a-f]{10}, tree [0-9a-f]{12}", verified(repo, "T-2.1"))
    code, out = run_json(capsys, "drift", str(repo), "--check")
    assert code == 0 and out["drifted"] == [] and out["clean"] == ["T-2.1"]
    assert out["no_touches"] == ["T-1.1"]

    (repo / STORE).write_text("def add(): return None\n")
    git(repo, "commit", "-qam", "refactor the store")
    code, out = run_json(capsys, "drift", str(repo), "--check")
    assert code == 8
    [entry] = out["drifted"]
    assert entry["id"] == "T-2.1" and entry["files"] == [STORE] and not entry["uncommitted"]
    assert [c.split(" ", 1)[1] for c in entry["commits"]] == ["refactor the store"]
    assert [r["id"] for r in out["requirements"]] == ["FR-001", "FR-002", "NFR-002"]

    code, out = run_json(capsys, "verify", "T-2.1", str(repo))
    assert code == 0 and out["status"] == "done" and out["verified"] != out["previous_verified"]
    _, out = run_json(capsys, "drift", str(repo))
    assert out["clean"] == ["T-2.1"]  # the files match the hash the re-verify recorded, committed or not
    git(repo, "commit", "-qam", "chore: re-verify T-2.1")
    code, out = run_json(capsys, "drift", str(repo), "--check")
    assert code == 0 and out["clean"] == ["T-2.1"]


def test_uncommitted_edits_count_as_drift(repo: Path, capsys):  # noqa: F811
    done_on_main(repo, "T-2.1", STORE, "def add(): ...\n")
    (repo / STORE).write_text("changed\n")
    _, out = run_json(capsys, "drift", str(repo))
    assert out["drifted"][0]["uncommitted"] and out["drifted"][0]["commits"] == []


def test_a_landed_item_is_verified_at_its_merge(repo: Path, capsys):  # noqa: F811
    _, a = run_json(capsys, "start", "T-2.1", str(repo), "--worktree")
    worktree = Path(a["claim"]["worktree"])
    done_on_main(worktree, "T-2.1", STORE, "def add(): ...\n")
    (repo / "README.md").write_text("main moved on\n")  # unrelated work on main meanwhile
    git(repo, "commit", "-qam", "docs")
    assert run_json(capsys, "land", "--dir", str(repo))[0] == 0
    _, out = run_json(capsys, "drift", str(repo))
    assert out["clean"] == ["T-2.1"] and out["drifted"] == []


def test_verify_needs_a_done_item_and_passing_checks(repo: Path, capsys):  # noqa: F811
    assert run_json(capsys, "verify", "T-2.1", str(repo))[1]["error"]["code"] == "bad_status"


# --- tracing Must acceptance criteria to tests ----------------------------------------------------------------

def test_validate_trace_lists_must_criteria_that_no_test_names(repo: Path, capsys):  # noqa: F811
    (repo / "tests/test_store.py").write_text(
        "def test_fr_001_ac2_rejects_a_duplicate():\n    assert True\n\n"
        "def test_check_in():  # FR-003\n    assert True\n")
    code, out = run_json(capsys, "validate", str(repo), "--trace")
    assert code == 1 and out["valid"] and out["trace"]["checked"]
    uncovered = {(c["id"], c["ac"]) for c in out["trace"]["uncovered"]}
    assert ("FR-001", 1) in uncovered and ("FR-001", 2) not in uncovered
    assert {(c["id"], c["ac"]) for c in out["trace"]["by_requirement"]} == {("FR-003", 1), ("FR-003", 2)}
    fr001 = next(r for r in out["trace"]["requirements"] if r["id"] == "FR-001")
    assert fr001["criteria"][1]["tests"] == [{"file": "tests/test_store.py", "line": 1}]

    must = {r["id"] for r in out["trace"]["requirements"]}
    (repo / "tests/test_all.py").write_text("".join(f"def test_{m.lower().replace('-', '_')}():\n    pass\n"
                                                    for m in sorted(must)))
    code, out = run_json(capsys, "validate", str(repo), "--trace")
    assert code == 0 and out["trace"]["uncovered"] == []
    assert main(["validate", str(repo)]) == 0  # without --trace, coverage never fails validate


def test_criteria_are_numbered_by_position_or_by_their_own_label():
    section = ("**Priority:** Must\n\n**Acceptance criteria**\n- first\n  continued\n- **AC5:** labelled\n"
               "- third\n\nSome prose after the list.\n- not a criterion\n")
    assert criteria(section) == [(1, "first"), (5, "labelled"), (3, "third")]
    assert criteria("**Priority:** Must\n\nNo criteria here.\n") == []


def test_sub_bullets_are_details_not_criteria():  # T1
    section = "**Acceptance criteria**\n- adds a habit\n  - including its name\n- lists habits\n"
    assert criteria(section) == [(1, "adds a habit"), (2, "lists habits")]


def test_criteria_as_a_field_list_item():  # T2
    section = "- **Priority:** Must\n- **Acceptance criteria:**\n  - one\n  - two\n- **Out of scope:** export\n"
    assert criteria(section) == [(1, "one"), (2, "two")]


def test_a_later_list_is_not_read_as_the_criteria():  # T3
    section = "**Acceptance criteria**\nSee the design doc.\n\n**Out of scope**\n- exporting\n- syncing\n"
    assert criteria(section) == []


def test_labels_and_positions_never_collide():
    assert criteria("**Acceptance criteria**\n- AC2: b\n- c\n") == [(2, "b"), (3, "c")]


def test_reference_spellings():  # T4
    from idea_to_scaffold.trace import REF_RE
    found = [(m.group(1), m.group(2)) for m in REF_RE.finditer("FR-001, AC2 / FR-002 (AC3) / fr0012ab / fr_003_ac1")]
    assert found == [("001", "2"), ("002", "3"), ("003", "1")]
