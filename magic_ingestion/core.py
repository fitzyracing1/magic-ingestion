"""The four-step loop: COAX -> INGEST -> TRIANGLE -> SET."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import LIT, DARK  # noqa: F401

DEFAULT_TREE = Path(__file__).with_name("tree.json")


def now() -> float:
    return time.time()


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def _read_jsonl(path: Path) -> List[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


@dataclass
class Config:
    workdir: Path
    min_free_bytes: int = 100 * 1024 * 1024
    stale_hours: float = 24.0
    stale_files: List[str] = field(default_factory=list)
    queue_file: Optional[str] = None  # an external queue to watch (relative to workdir)
    ci_command: Optional[List[str]] = None  # argv list; nonzero exit = dark CI light
    seed: int = 0
    tree_path: Path = DEFAULT_TREE

    @property
    def needs_path(self) -> Path:
        return self.workdir / "needs.jsonl"

    @property
    def results_path(self) -> Path:
        return self.workdir / "results.jsonl"

    @property
    def state_path(self) -> Path:
        return self.workdir / "state.json"

    @property
    def log_path(self) -> Path:
        return self.workdir / "magic.log"

    @property
    def temp_dir(self) -> Path:
        return self.workdir / "tmp"

    @classmethod
    def load(cls, workdir: Path) -> "Config":
        workdir = Path(workdir).resolve()
        cfg = cls(workdir=workdir)
        f = workdir / "magic.json"
        if f.exists():
            data = json.loads(f.read_text(encoding="utf-8"))
            for k in ("min_free_bytes", "stale_hours", "stale_files", "queue_file", "seed"):
                if k in data:
                    setattr(cfg, k, data[k])
            if "ci_command" in data:
                c = data["ci_command"]
                cfg.ci_command = shlex.split(c) if isinstance(c, str) else list(c)
            if "tree" in data:
                cfg.tree_path = (workdir / data["tree"]).resolve()
        return cfg


# ---------------------------------------------------------------- 1. COAX
Probe = Callable[[Config], Optional[str]]


def probe_disk(cfg: Config) -> Optional[str]:
    free = shutil.disk_usage(cfg.workdir).free
    if free < cfg.min_free_bytes:
        return f"disk space low: {free} bytes free (< {cfg.min_free_bytes})"
    return None


def probe_stale(cfg: Config) -> Optional[str]:
    limit = cfg.stale_hours * 3600
    for name in cfg.stale_files:
        p = _safe_path(cfg, name)
        if p.exists() and now() - p.stat().st_mtime > limit:
            return f"stale file: {name} older than {cfg.stale_hours}h"
    return None


def probe_queue(cfg: Config) -> Optional[str]:
    if not cfg.queue_file:
        return None
    p = _safe_path(cfg, cfg.queue_file)
    if not p.exists():
        return None
    pending = [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    if pending:
        return f"queue {cfg.queue_file} has {len(pending)} pending item(s)"
    return None


def probe_ci(cfg: Config) -> Optional[str]:
    if not cfg.ci_command:
        return None
    try:
        r = subprocess.run(cfg.ci_command, capture_output=True, timeout=60)  # argv, no shell
        if r.returncode != 0:
            return f"CI light is dark (exit {r.returncode})"
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"CI light is dark ({type(e).__name__})"
    return None


PROBES: Dict[str, Probe] = {
    "disk": probe_disk,
    "stale": probe_stale,
    "queue": probe_queue,
    "ci": probe_ci,
}


def coax(cfg: Config, probes: Optional[Dict[str, Probe]] = None) -> List[dict]:
    """Ask every probe 'what do you need?'. Return proposed needs."""
    needs = []
    for name, probe in (probes or PROBES).items():
        try:
            text = probe(cfg)
        except Exception as e:  # a broken probe is itself a need
            text = f"probe {name} failed: {e}"
        if text:
            needs.append({"source": name, "text": text})
    return needs


# ---------------------------------------------------------------- 2. INGEST
def ingest(cfg: Config, needs: List[dict]) -> List[dict]:
    """The computer types its needs into the queue as plain-text lines."""
    written = []
    for n in needs:
        rec = {"ts": now(), "source": n["source"], "text": str(n["text"]), "status": "pending"}
        _append_jsonl(cfg.needs_path, rec)
        written.append(rec)
    return written


def add_human_need(cfg: Config, text: str) -> dict:
    return ingest(cfg, [{"source": "human", "text": text}])[0]


def pending_needs(cfg: Config) -> List[dict]:
    done = {r.get("need_ts") for r in _read_jsonl(cfg.results_path) if r.get("need_ts")}
    return [n for n in _read_jsonl(cfg.needs_path) if n["ts"] not in done]


# ---------------------------------------------------------------- 3. TRIANGLE
def load_tree(path: Path) -> dict:
    tree = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_tree(tree)
    return tree


def validate_tree(node: dict) -> None:
    if "task" in node:
        if node["task"] not in TASKS:
            raise ValueError(f"unknown task {node['task']!r}")
        return
    kids = node.get("children")
    if not isinstance(kids, list) or len(kids) != 3:
        raise ValueError(f"node {node.get('name')!r} must have exactly 3 children (ternary)")
    for k in kids:
        validate_tree(k)


def walk(tree: dict, cycle: int, seed: int = 0) -> List[int]:
    """Deterministic rotation: the path is cycle+seed written in base 3,
    least-significant digit chooses at the root, so every leaf is visited."""
    n = cycle + seed
    path, node = [], tree
    while "task" not in node:
        branch = n % 3
        n //= 3
        path.append(branch)
        node = node["children"][branch]
    return path


def leaf_at(tree: dict, path: List[int]) -> str:
    node = tree
    for b in path:
        node = node["children"][b]
    return node["task"]


# ---- safe built-in tasks (only ever touch cfg.workdir)
def _safe_path(cfg: Config, rel: str) -> Path:
    p = (cfg.workdir / rel).resolve()
    if p != cfg.workdir and cfg.workdir not in p.parents:
        raise ValueError(f"path escapes workdir: {rel}")
    return p


def task_tidy_temp(cfg: Config) -> str:
    cfg.temp_dir.mkdir(parents=True, exist_ok=True)
    removed = 0
    for p in cfg.temp_dir.iterdir():
        if p.is_symlink() or p.is_file():
            p.unlink()
        else:
            shutil.rmtree(p)
        removed += 1
    return f"tidied {removed} item(s) from tmp"


def task_summarize_log(cfg: Config) -> str:
    results = _read_jsonl(cfg.results_path)
    lit = sum(1 for r in results if r.get("light") == "lit")
    dark = sum(1 for r in results if r.get("light") == "dark")
    summary = f"{len(results)} results: {lit} lit, {dark} dark"
    with cfg.log_path.open("a", encoding="utf-8") as f:
        f.write(f"{now():.0f} summary {summary}\n")
    return summary


def task_self_test(cfg: Config) -> str:
    validate_tree(load_tree(cfg.tree_path))
    probe = cfg.workdir / ".selftest"
    probe.write_text("ok", encoding="utf-8")
    ok = probe.read_text(encoding="utf-8") == "ok"
    probe.unlink()
    if not ok:
        raise RuntimeError("workdir read/write failed")
    return "self-test passed"


TASKS: Dict[str, Callable[[Config], str]] = {
    "tidy_temp": task_tidy_temp,
    "summarize_log": task_summarize_log,
    "self_test": task_self_test,
}


def handle_need(cfg: Config, need: dict) -> str:
    """Address a need. The need text is data only: logged, never executed."""
    with cfg.log_path.open("a", encoding="utf-8") as f:
        f.write(f"{now():.0f} need [{need['source']}] {need['text']}\n")
    return f"acknowledged need from {need['source']}: {need['text']}"


# ---------------------------------------------------------------- 4. SET
def set_result(cfg: Config, kind: str, name: str, ok: bool, detail: str, **extra) -> dict:
    code = LIT if ok else DARK
    rec = {"ts": now(), "kind": kind, "name": name, "code": code,
           "light": "lit" if code == LIT else "dark", "detail": detail}
    rec.update(extra)
    _append_jsonl(cfg.results_path, rec)
    return rec


# ---------------------------------------------------------------- loop
def _load_cycle(cfg: Config) -> int:
    if cfg.state_path.exists():
        return int(json.loads(cfg.state_path.read_text()).get("cycle", 0))
    return 0


def _save_cycle(cfg: Config, cycle: int) -> None:
    cfg.state_path.write_text(json.dumps({"cycle": cycle}))


def once(cfg: Config, probes: Optional[Dict[str, Probe]] = None) -> List[dict]:
    cfg.workdir.mkdir(parents=True, exist_ok=True)
    ingest(cfg, coax(cfg, probes))                    # 1 + 2
    results = []
    needs = pending_needs(cfg)
    if needs:
        for n in needs:
            try:
                ok, detail = True, handle_need(cfg, n)
            except Exception as e:
                ok, detail = False, str(e)
            results.append(set_result(cfg, "need", n["source"], ok, detail, need_ts=n["ts"]))
        return results
    cycle = _load_cycle(cfg)                          # 3
    tree = load_tree(cfg.tree_path)
    path = walk(tree, cycle, cfg.seed)
    task = leaf_at(tree, path)
    try:
        ok, detail = True, TASKS[task](cfg)
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {e}"
    _save_cycle(cfg, cycle + 1)
    results.append(set_result(cfg, "task", task, ok, detail, path=path, cycle=cycle))  # 4
    return results


def run(cfg: Config, interval: float = 60.0, max_cycles: Optional[int] = None,
        sleep=time.sleep) -> int:
    n = 0
    while max_cycles is None or n < max_cycles:
        once(cfg)
        n += 1
        if max_cycles is None or n < max_cycles:
            sleep(interval)
    return n


def status(cfg: Config) -> dict:
    results = _read_jsonl(cfg.results_path)
    return {
        "workdir": str(cfg.workdir),
        "pending_needs": len(pending_needs(cfg)),
        "results": len(results),
        "lit": sum(1 for r in results if r.get("light") == "lit"),
        "dark": sum(1 for r in results if r.get("light") == "dark"),
        "last": results[-1] if results else None,
        "cycle": _load_cycle(cfg),
    }
