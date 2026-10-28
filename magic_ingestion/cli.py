import argparse
import json
import os
from pathlib import Path

from . import core


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="magic", description="Magic ingestion: the computer types up its own needs.")
    p.add_argument("--workdir", default=os.environ.get("MAGIC_WORKDIR", ".magic"))
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="loop forever: coax, ingest, triangle, set")
    r.add_argument("--interval", type=float, default=60.0)
    r.add_argument("--max-cycles", type=int, default=None)
    r.add_argument("--once", action="store_true")
    r.add_argument("--source", choices=["builtin", "live", "history", "all"], default=None,
                   help="where TRIANGLE gets default tasks (default: magic.json or builtin)")
    fe = sub.add_parser("fetch", help="fetch internet candidate tasks into the cache (read-only)")
    g = fe.add_mutually_exclusive_group()
    g.add_argument("--live", action="store_true")
    g.add_argument("--history", action="store_true")
    sub.add_parser("once", help="run one cycle")
    n = sub.add_parser("need", help="type a need into the queue")
    n.add_argument("text")
    sub.add_parser("status", help="show queue and lit/dark counts")
    a = p.parse_args(argv)
    cfg = core.Config.load(Path(a.workdir))
    cfg.workdir.mkdir(parents=True, exist_ok=True)
    if getattr(a, "source", None):
        cfg.source = a.source
    if a.cmd == "fetch":
        from . import internet
        modes = ["live"] if a.live else ["history"] if a.history else ["live", "history"]
        for m in modes:
            cache = internet.refresh(cfg, mode=m, force=True)
        for c in cache["candidates"]:
            print(f"{c['branch']}\t{c['source']}\t{c['text']}")
        for e in cache.get("errors", []):
            print(f"dark\t{e}")
        return core.LIT
    if a.cmd == "once" or (a.cmd == "run" and a.once):
        res = core.once(cfg)
        for x in res:
            print(f"{x['light']}\t{x['kind']}\t{x['name']}\t{x['detail']}")
        return core.DARK if any(x["code"] for x in res) else core.LIT
    if a.cmd == "run":
        core.run(cfg, a.interval, a.max_cycles)
        return 0
    if a.cmd == "need":
        rec = core.add_human_need(cfg, a.text)
        print(f"queued: {rec['text']}")
        return 0
    print(json.dumps(core.status(cfg), indent=2))
    return 0
