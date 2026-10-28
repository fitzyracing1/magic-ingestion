import json
import os
import time

import pytest

from magic_ingestion import core, cli, LIT, DARK


@pytest.fixture
def cfg(tmp_path):
    c = core.Config(workdir=tmp_path.resolve())
    c.min_free_bytes = 0
    (tmp_path / "sysroot").mkdir()
    c.sys_root = str(tmp_path / "sysroot")  # isolate from the real host
    return c


# 1. COAX
def test_coax_quiet_when_nothing_needed(cfg):
    assert core.coax(cfg) == []


def test_coax_disk_low(cfg):
    cfg.min_free_bytes = 10**30
    needs = core.coax(cfg)
    assert any(n["kind"] == "disk_low" and n["family"] == "memory_storage" for n in needs)


def test_coax_stale_file(cfg):
    f = cfg.workdir / "old.txt"
    f.write_text("x")
    old = time.time() - 3 * 3600
    os.utime(f, (old, old))
    cfg.stale_files = ["old.txt"]
    cfg.stale_hours = 1
    assert [(n["family"], n["kind"]) for n in core.coax(cfg)] == [("data", "stale_input")]


def test_coax_queue_pending(cfg):
    (cfg.workdir / "q.txt").write_text("a\nb\n")
    cfg.queue_file = "q.txt"
    needs = core.coax(cfg)
    assert needs[0]["kind"] == "tasks_pending" and "2 pending" in needs[0]["text"]


def test_coax_ci_dark_and_lit(cfg):
    import sys
    cfg.ci_command = [sys.executable, "-c", "raise SystemExit(1)"]
    assert any("dark" in n["text"] for n in core.coax(cfg))
    cfg.ci_command = [sys.executable, "-c", "pass"]
    assert core.coax(cfg) == []


def test_broken_probe_becomes_need(cfg):
    def boom(c):
        raise RuntimeError("x")
    assert core.coax(cfg, {"boom": boom})[0]["source"] == "boom"


# 2. INGEST
def test_ingest_writes_jsonl(cfg):
    core.ingest(cfg, [{"source": "disk", "text": "need space"}])
    lines = cfg.needs_path.read_text().splitlines()
    rec = json.loads(lines[0])
    assert rec["source"] == "disk" and rec["text"] == "need space" and "ts" in rec


def test_human_need_cli(cfg):
    assert cli.main(["--workdir", str(cfg.workdir), "need", "water the plants; rm -rf /"]) == 0
    needs = core.pending_needs(cfg)
    assert needs[0]["source"] == "human"
    assert needs[0]["text"] == "water the plants; rm -rf /"


# 3. TRIANGLE
def test_tree_is_ternary(cfg):
    core.validate_tree(core.load_tree(cfg.tree_path))
    with pytest.raises(ValueError):
        core.validate_tree({"name": "r", "children": [{"task": "self_test"}] * 2})


def test_walk_rotates_through_all_nine_leaves():
    tree = core.load_tree(core.DEFAULT_TREE)
    paths = {tuple(core.walk(tree, i)) for i in range(9)}
    assert len(paths) == 9
    assert core.walk(tree, 0) == [0, 0]
    assert core.walk(tree, 1) == [1, 0]
    assert core.walk(tree, 3) == [0, 1]


def test_walk_seedable():
    tree = core.load_tree(core.DEFAULT_TREE)
    assert core.walk(tree, 0, seed=4) == core.walk(tree, 4)


def test_triangle_runs_when_no_needs(cfg):
    (cfg.temp_dir).mkdir()
    (cfg.temp_dir / "junk").write_text("x")
    res = core.once(cfg)
    assert res[0]["kind"] == "task" and res[0]["name"] == "tidy_temp"
    assert not any(cfg.temp_dir.iterdir())
    res2 = core.once(cfg)
    assert res2[0]["cycle"] == 1


def test_needs_take_priority_over_triangle(cfg):
    core.add_human_need(cfg, "hello")
    res = core.once(cfg)
    assert res[0]["kind"] == "need" and res[0]["light"] == "lit"
    assert core.pending_needs(cfg) == []
    assert core.once(cfg)[0]["kind"] == "task"


def test_safe_path_rejects_escape(cfg):
    with pytest.raises(ValueError):
        core._safe_path(cfg, "../../etc/passwd")


# 4. SET
def test_set_lit_and_dark(cfg):
    a = core.set_result(cfg, "task", "x", True, "ok")
    b = core.set_result(cfg, "task", "y", False, "bad")
    assert (a["code"], a["light"]) == (LIT, "lit") == (0, "lit")
    assert (b["code"], b["light"]) == (DARK, "dark") == (1, "dark")
    assert len(cfg.results_path.read_text().splitlines()) == 2


def test_failing_task_recorded_dark(cfg, monkeypatch):
    def fail(c):
        raise RuntimeError("nope")
    monkeypatch.setitem(core.TASKS, "tidy_temp", fail)
    res = core.once(cfg)
    assert res[0]["light"] == "dark" and res[0]["code"] == 1


def test_run_loop_and_status(cfg):
    sleeps = []
    assert core.run(cfg, interval=5, max_cycles=3, sleep=sleeps.append) == 3
    assert sleeps == [5, 5]
    s = core.status(cfg)
    assert s["results"] == 3 and s["lit"] == 3 and s["dark"] == 0 and s["cycle"] == 3


def test_cli_once_and_status(cfg, capsys):
    assert cli.main(["--workdir", str(cfg.workdir), "once"]) == 0
    assert "lit" in capsys.readouterr().out
    assert cli.main(["--workdir", str(cfg.workdir), "status"]) == 0
    assert '"lit": 1' in capsys.readouterr().out
