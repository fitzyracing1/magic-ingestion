"""SOLVE: map each need kind to a solution.

auto    -> safe, reversible, workdir-only actions run now.
propose -> exact steps written to proposals.jsonl, status "awaiting approval". Never executed.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

SOLUTIONS = json.loads(Path(__file__).with_name("solutions.json").read_text(encoding="utf-8"))


def _rotate_logs(cfg) -> str:
    log = cfg.log_path
    if not log.exists() or log.stat().st_size < cfg.log_rotate_bytes:
        return "log under rotate size"
    rotated = cfg.workdir / "magic.log.1"
    if rotated.exists():
        rotated.replace(cfg.workdir / "magic.log.2")
    log.replace(rotated)
    return "rotated magic.log -> magic.log.1"


def _refresh_cache(cfg) -> str:
    c = cfg.candidates_path
    if not c.exists():
        return "no cache to refresh"
    c.replace(cfg.workdir / "candidates.json.bak")  # reversible: kept as .bak
    return "marked candidate cache for refresh (old copy in candidates.json.bak)"


def _acknowledge(cfg, need) -> str:
    with cfg.log_path.open("a", encoding="utf-8") as f:
        f.write(f"{time.time():.0f} need [{need.get('family')}/{need.get('kind')}] {need['text']}\n")
    return "acknowledged"


def auto_actions(cfg, need):
    from . import core
    return {
        "tidy_temp": lambda: core.task_tidy_temp(cfg),
        "self_test": lambda: core.task_self_test(cfg),
        "rotate_logs": lambda: _rotate_logs(cfg),
        "refresh_cache": lambda: _refresh_cache(cfg),
        "acknowledge": lambda: _acknowledge(cfg, need),
    }


def propose(cfg, need: dict, steps, extra=None) -> dict:
    from . import core
    rec = {"ts": time.time(), "family": need.get("family"), "kind": need.get("kind"),
           "need": need["text"], "steps": list(steps), "status": "awaiting approval"}
    if extra:
        rec.update(extra)
    core._append_jsonl(cfg.proposals_path, rec)
    return rec


def solve(cfg, need: dict):
    """Return (ok, mode, detail). ok=False means dark."""
    kind = need.get("kind") or "human_need"
    sol = SOLUTIONS.get(kind)
    if sol is None:
        propose(cfg, need, ["No known solution: review manually"])
        return True, "propose", f"no mapped solution for {kind}; proposed manual review"
    if sol["mode"] == "propose":
        extra = {"url": need["url"]} if need.get("url") else None
        propose(cfg, need, sol["steps"], extra)
        return True, "propose", f"proposed fix for {kind} (awaiting approval)"
    actions = auto_actions(cfg, need)
    details = [actions[a]() for a in sol["actions"]]
    return True, "auto", "; ".join(details)
