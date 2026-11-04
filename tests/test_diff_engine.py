"""Tests for the reviewable diff engine — the core Helix differentiator."""

from __future__ import annotations

from pathlib import Path

import pytest

from helix.diff_engine import DiffEngine, DiffError, apply_hunks_to_text, build_hunks


@pytest.fixture()
def engine(tmp_path: Path) -> DiffEngine:
    ws = tmp_path / "ws"
    ws.mkdir()
    return DiffEngine(workspace=ws, store_dir=tmp_path / "store")


def test_new_file_diff(engine: DiffEngine) -> None:
    ds = engine.propose_file("hello.txt", "line1\nline2\n", reason="create file")
    assert ds.files[0].is_new_file
    assert ds.total_added == 2
    assert "+line1" in ds.unified()


def test_edit_produces_hunks_and_applies(engine: DiffEngine) -> None:
    (engine.workspace / "a.py").write_text("def add(a, b):\n    return a - b\n")
    ds = engine.propose_edit("a.py", "return a - b", "return a + b", reason="fix bug")
    assert ds.total_added == 1 and ds.total_removed == 1
    assert not (engine.workspace / "a.py").read_text().count("a + b")  # not applied yet
    engine.apply(ds.id)
    assert "return a + b" in (engine.workspace / "a.py").read_text()


def test_hunk_level_partial_accept(engine: DiffEngine) -> None:
    # Changes are far enough apart that difflib emits two separate hunks.
    original = [f"keep{i}" for i in range(20)]
    original[2] = "change2"
    original[15] = "change15"
    (engine.workspace / "m.txt").write_text("\n".join(original) + "\n")

    updated = list(original)
    updated[2] = "NEW2"
    updated[15] = "NEW15"
    ds = engine.propose_file("m.txt", "\n".join(updated) + "\n")
    assert len(ds.files[0].hunks) == 2

    first_hunk = ds.files[0].hunks[0].id
    engine.apply(ds.id, {"m.txt": [first_hunk]})
    result = (engine.workspace / "m.txt").read_text()
    assert "NEW2" in result
    assert "change15" in result and "NEW15" not in result  # second hunk rejected


def test_stale_diff_is_refused(engine: DiffEngine) -> None:
    (engine.workspace / "s.txt").write_text("original\n")
    ds = engine.propose_file("s.txt", "changed\n")
    (engine.workspace / "s.txt").write_text("someone else edited this\n")
    with pytest.raises(DiffError, match="changed on disk"):
        engine.apply(ds.id)
    # force overrides
    engine.apply(ds.id, force=True)
    assert (engine.workspace / "s.txt").read_text() == "changed\n"


def test_rollback_restores_previous_content(engine: DiffEngine) -> None:
    (engine.workspace / "r.txt").write_text("before\n")
    ds = engine.propose_file("r.txt", "after\n")
    engine.apply(ds.id)
    assert (engine.workspace / "r.txt").read_text() == "after\n"
    engine.rollback(ds.id)
    assert (engine.workspace / "r.txt").read_text() == "before\n"


def test_rollback_deletes_new_file(engine: DiffEngine) -> None:
    ds = engine.propose_file("brand_new.txt", "hi\n")
    engine.apply(ds.id)
    assert (engine.workspace / "brand_new.txt").exists()
    engine.rollback(ds.id)
    assert not (engine.workspace / "brand_new.txt").exists()


def test_path_escape_is_rejected(engine: DiffEngine) -> None:
    with pytest.raises(DiffError, match="escapes workspace"):
        engine.propose_file("../evil.txt", "x")


def test_edit_requires_unique_match(engine: DiffEngine) -> None:
    (engine.workspace / "u.txt").write_text("x\nx\n")
    with pytest.raises(DiffError, match="not unique"):
        engine.propose_edit("u.txt", "x", "y")
    ds = engine.propose_edit("u.txt", "x", "y", replace_all=True)
    engine.apply(ds.id)
    assert (engine.workspace / "u.txt").read_text() == "y\ny\n"


def test_persistence_across_engine_instances(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    e1 = DiffEngine(workspace=ws, store_dir=tmp_path / "store")
    ds = e1.propose_file("p.txt", "persisted\n")
    e2 = DiffEngine(workspace=ws, store_dir=tmp_path / "store")
    loaded = e2.get(ds.id)
    assert loaded.files[0].new_content == "persisted\n"


def test_multi_file_atomic_set(engine: DiffEngine) -> None:
    ds = engine.propose_multi(
        [
            {"path": "one.txt", "content": "1\n"},
            {"path": "two.txt", "content": "2\n"},
        ],
        title="two files",
    )
    assert len(ds.files) == 2
    engine.apply(ds.id)
    assert (engine.workspace / "one.txt").read_text() == "1\n"
    assert (engine.workspace / "two.txt").read_text() == "2\n"


def test_build_hunks_and_apply_roundtrip() -> None:
    old = "\n".join(f"line{i}" for i in range(20))
    new = old.replace("line5", "CHANGED")
    hunks = build_hunks(old, new)
    assert len(hunks) == 1
    result = apply_hunks_to_text(old, hunks, {h.id for h in hunks})
    assert result.startswith(new)


def test_preview_does_not_write(engine: DiffEngine) -> None:
    (engine.workspace / "pv.txt").write_text("orig\n")
    ds = engine.propose_file("pv.txt", "new\n")
    data = engine.preview(ds.id)
    assert data["would_write"] == ["pv.txt"]
    assert (engine.workspace / "pv.txt").read_text() == "orig\n"
