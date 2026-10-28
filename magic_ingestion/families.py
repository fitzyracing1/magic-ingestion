"""One COAX probe per need family. Linux-friendly, read-only, skip gracefully.

All system paths are read under cfg.sys_root (default "/") so tests can mock them.
Secret files are only stat()'d for permissions; their contents are never read.
"""
from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import time
from pathlib import Path
from typing import List, Optional, Tuple

NEEDS_MAP = json.loads(Path(__file__).with_name("needs-map.json").read_text(encoding="utf-8"))
KIND_TO_FAMILY = {k: fam for fam, v in NEEDS_MAP.items() for k in v["kinds"]}
Need = Tuple[str, str]  # (kind, text)


def _root(cfg, rel: str) -> Path:
    return Path(cfg.sys_root) / rel.lstrip("/")


def _read(p: Path) -> Optional[str]:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _lines(cfg, rel) -> List[str]:
    if not rel:
        return []
    t = _read(cfg.workdir / rel)
    return [l for l in (t or "").splitlines() if l.strip()]


# ---------------------------------------------------------------- Power
def probe_power(cfg) -> List[Need]:
    out = []
    ps = _root(cfg, "sys/class/power_supply")
    if ps.is_dir():
        for bat in sorted(ps.glob("BAT*")):
            cap, status = _read(bat / "capacity"), (_read(bat / "status") or "").strip()
            if cap and cap.strip().isdigit() and int(cap) < cfg.battery_min and status == "Discharging":
                out.append(("battery_low", f"battery {bat.name} at {int(cap)}% and discharging"))
    tz = _root(cfg, "sys/class/thermal")
    if tz.is_dir():
        for z in sorted(tz.glob("thermal_zone*")):
            t = (_read(z / "temp") or "").strip()
            if t.lstrip("-").isdigit() and int(t) / 1000 >= cfg.thermal_max_c:
                out.append(("thermal_hot", f"{z.name} at {int(t) / 1000:.0f}C (>= {cfg.thermal_max_c}C)"))
    return out


# ---------------------------------------------------------------- Compute
def probe_compute(cfg) -> List[Need]:
    out = []
    la = _read(_root(cfg, "proc/loadavg"))
    if la:
        load1 = float(la.split()[0])
        cpus = os.cpu_count() or 1
        if load1 > cpus * cfg.load_factor:
            out.append(("cpu_load_high", f"load {load1:.2f} > {cpus} cpus x {cfg.load_factor}"))
    if cfg.results_path.exists():
        last = cfg.results_path.stat().st_mtime
        if time.time() - last > cfg.tick_late_seconds:
            out.append(("tick_late", f"loop tick late: last result {int(time.time() - last)}s ago"))
    return out


# ---------------------------------------------------------------- Memory & storage
def probe_memory_storage(cfg) -> List[Need]:
    import shutil
    out = []
    mi = _read(_root(cfg, "proc/meminfo"))
    if mi:
        kv = {l.split(":")[0]: int(l.split()[1]) for l in mi.splitlines() if len(l.split()) >= 2 and l.split()[1].isdigit()}
        if kv.get("MemTotal") and "MemAvailable" in kv:
            pct = 100 * kv["MemAvailable"] / kv["MemTotal"]
            if pct < cfg.ram_min_pct:
                out.append(("ram_low", f"RAM available {pct:.1f}% (< {cfg.ram_min_pct}%)"))
    free = shutil.disk_usage(cfg.workdir).free
    if free < cfg.min_free_bytes:
        out.append(("disk_low", f"disk space low: {free} bytes free (< {cfg.min_free_bytes})"))
    for b in cfg.backup_paths:
        p = Path(b) if os.path.isabs(b) else cfg.workdir / b
        if not p.exists():
            out.append(("backup_stale", f"backup missing: {b}"))
        elif time.time() - p.stat().st_mtime > cfg.backup_hours * 3600:
            out.append(("backup_stale", f"backup {b} older than {cfg.backup_hours}h"))
    return out


# ---------------------------------------------------------------- Network
def probe_network(cfg) -> List[Need]:
    out = []
    if cfg.dns_host:
        try:
            socket.getaddrinfo(cfg.dns_host, 443)
        except OSError as e:
            out.append(("dns_fail", f"DNS lookup failed for {cfg.dns_host}: {e}"))
    if cfg.net_check:
        host, port = cfg.net_check
        try:
            socket.create_connection((host, int(port)), timeout=5).close()
        except OSError as e:
            out.append(("offline", f"cannot reach {host}:{port}: {e}"))
    ts = _root(cfg, "run/systemd/timesync")
    if ts.is_dir() and not (ts / "synchronized").exists():
        out.append(("time_unsynced", "system clock not synchronized (systemd-timesyncd)"))
    return out


# ---------------------------------------------------------------- Data
def probe_data(cfg) -> List[Need]:
    out = []
    for name in cfg.stale_files:
        p = cfg.workdir / name
        if p.exists() and time.time() - p.stat().st_mtime > cfg.stale_hours * 3600:
            out.append(("stale_input", f"stale file: {name} older than {cfg.stale_hours}h"))
    c = cfg.candidates_path
    if cfg.source != "builtin" and c.exists() and time.time() - c.stat().st_mtime > cfg.cache_ttl * 24:
        out.append(("stale_input", "internet candidate cache is stale"))
    if cfg.archive_dir:
        d = cfg.workdir / cfg.archive_dir
        if d.is_dir():
            n = sum(1 for _ in d.iterdir())
            if n > cfg.archive_max:
                out.append(("archive_bloat", f"{cfg.archive_dir} has {n} archives (> {cfg.archive_max})"))
    return out


# ---------------------------------------------------------------- Software
def _updates_available(cfg) -> Tuple[int, int]:
    t = _read(_root(cfg, "var/lib/update-notifier/updates-available")) or ""
    total = sec = 0
    for line in t.splitlines():
        w = line.split()
        if w and w[0].isdigit():
            if "security" in line:
                sec = int(w[0])
            elif "update" in line:
                total = int(w[0])
    return total, sec


def probe_software(cfg) -> List[Need]:
    out = []
    total, _ = _updates_available(cfg)
    if total:
        out.append(("updates_pending", f"{total} OS updates can be applied"))
    for req in cfg.requirements_files:
        unpinned = [l.split("#")[0].strip() for l in _lines(cfg, req)
                    if not l.lstrip().startswith(("#", "-")) and "==" not in l]
        unpinned = [u for u in unpinned if u]
        if unpinned:
            out.append(("dependency_unpinned", f"{req}: unpinned {', '.join(unpinned[:5])}"))
    if cfg.ci_command:
        argv = shlex.split(cfg.ci_command) if isinstance(cfg.ci_command, str) else cfg.ci_command
        try:
            r = subprocess.run(argv, capture_output=True, timeout=60)  # argv list, no shell
            if r.returncode != 0:
                out.append(("ci_dark", f"CI light is dark (exit {r.returncode})"))
        except (OSError, subprocess.TimeoutExpired) as e:
            out.append(("ci_dark", f"CI light is dark ({type(e).__name__})"))
    return out


# ---------------------------------------------------------------- Security
def probe_security(cfg) -> List[Need]:
    out = []
    _, sec = _updates_available(cfg)
    if sec:
        out.append(("patches_pending", f"{sec} security updates pending"))
    if _root(cfg, "var/run/reboot-required").exists():
        out.append(("patches_pending", "reboot required to finish applying patches"))
    for s in cfg.secret_paths:  # stat only: contents are never opened
        p = Path(s) if os.path.isabs(s) else cfg.workdir / s
        try:
            mode = p.stat().st_mode & 0o777
        except OSError:
            continue
        if mode & 0o077:
            out.append(("secret_permissions", f"{s} is mode {oct(mode)}; group/other can access"))
    return out


# ---------------------------------------------------------------- Humans & purpose
def probe_humans(cfg) -> List[Need]:
    out = []
    for rel, kind, label in ((cfg.queue_file, "tasks_pending", "pending task"),
                             (cfg.feedback_file, "feedback_waiting", "feedback item"),
                             (cfg.approvals_file, "approval_waiting", "approval")):
        n = len(_lines(cfg, rel))
        if n:
            out.append((kind, f"{rel}: {n} {label}(s) waiting"))
    if cfg.proposals_path.exists():
        waiting = sum(1 for l in cfg.proposals_path.read_text().splitlines()
                      if '"awaiting approval"' in l)
        if waiting and cfg.remind_proposals:
            out.append(("approval_waiting", f"{waiting} proposed fix(es) awaiting approval"))
    return out


FAMILY_PROBES = {
    "power": probe_power,
    "compute": probe_compute,
    "memory_storage": probe_memory_storage,
    "network": probe_network,
    "data": probe_data,
    "software": probe_software,
    "security": probe_security,
    "humans": probe_humans,
}


SOURCE_FAMILY = {"github-ci": "software", "github-gfi": "humans", "pypi": "software", "npm": "software",
                 "wayback": "data", "gharchive": "humans", "swh": "data"}


def classify(text: str, default: str = "data") -> str:
    """Pick a family for free text by keyword (first family with most hits)."""
    import re
    t = text.lower()
    best, score = default, 0
    for fam, v in NEEDS_MAP.items():
        s = sum(1 for k in v["keywords"] if re.search(r"\b" + re.escape(k.strip()), t))
        if s > score:
            best, score = fam, s
    return best


def family_for_candidate(c: dict) -> str:
    if c.get("source") == "github-ci":
        return "software"
    return classify(c.get("text", ""), SOURCE_FAMILY.get(c.get("source"), "data"))
