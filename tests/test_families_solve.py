import json
import os
import socket
import time

import pytest

from magic_ingestion import core, families, solve


@pytest.fixture
def cfg(tmp_path):
    c = core.Config(workdir=(tmp_path / "wd").resolve())
    c.workdir.mkdir()
    c.min_free_bytes = 0
    root = tmp_path / "root"
    root.mkdir()
    c.sys_root = str(root)
    return c


def put(cfg, rel, text):
    p = os.path.join(cfg.sys_root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)


def kinds(needs):
    return {(n["family"], n["kind"]) for n in needs}


def test_needs_map_has_eight_families_and_every_kind_has_a_solution():
    assert set(families.NEEDS_MAP) == {"power", "compute", "memory_storage", "network",
                                       "data", "software", "security", "humans"}
    for fam, v in families.NEEDS_MAP.items():
        for k in v["kinds"]:
            assert k in solve.SOLUTIONS, k
    assert set(families.FAMILY_PROBES) == set(families.NEEDS_MAP)


def test_quiet_host_has_no_needs(cfg):
    assert core.coax(cfg) == []


def test_power_probe(cfg):
    put(cfg, "sys/class/power_supply/BAT0/capacity", "7\n")
    put(cfg, "sys/class/power_supply/BAT0/status", "Discharging\n")
    put(cfg, "sys/class/thermal/thermal_zone0/temp", "91000\n")
    assert kinds(core.coax(cfg)) == {("power", "battery_low"), ("power", "thermal_hot")}


def test_compute_probe(cfg):
    put(cfg, "proc/loadavg", f"{(os.cpu_count() or 1) * 10}.0 1 1 1/1 1\n")
    cfg.results_path.write_text("{}\n")
    old = time.time() - 10 * 3600
    os.utime(cfg.results_path, (old, old))
    assert kinds(core.coax(cfg)) == {("compute", "cpu_load_high"), ("compute", "tick_late")}


def test_memory_storage_probe(cfg):
    put(cfg, "proc/meminfo", "MemTotal: 1000 kB\nMemAvailable: 10 kB\n")
    cfg.min_free_bytes = 10**30
    cfg.backup_paths = ["nope.bak"]
    assert kinds(core.coax(cfg)) == {("memory_storage", k) for k in ("ram_low", "disk_low", "backup_stale")}


def test_network_probe(cfg, monkeypatch):
    def bad(*a, **k):
        raise OSError("mock failure")
    monkeypatch.setattr(socket, "getaddrinfo", bad)
    monkeypatch.setattr(socket, "create_connection", bad)
    cfg.dns_host, cfg.net_check = "example.invalid", ["example.invalid", 443]
    os.makedirs(os.path.join(cfg.sys_root, "run/systemd/timesync"))
    assert kinds(core.coax(cfg)) == {("network", k) for k in ("dns_fail", "offline", "time_unsynced")}


def test_data_probe(cfg):
    (cfg.workdir / "arch").mkdir()
    for i in range(3):
        (cfg.workdir / "arch" / f"{i}.tar").write_text("x")
    cfg.archive_dir, cfg.archive_max = "arch", 2
    assert kinds(core.coax(cfg)) == {("data", "archive_bloat")}


def test_software_and_security_probes(cfg):
    put(cfg, "var/lib/update-notifier/updates-available",
        "\n12 updates can be applied immediately.\n3 of these updates are standard security updates.\n")
    put(cfg, "var/run/reboot-required", "")
    (cfg.workdir / "requirements.txt").write_text("requests\nflask==3.0\n# c\n")
    cfg.requirements_files = ["requirements.txt"]
    sec = cfg.workdir / "token.env"
    sec.write_text("SECRET")
    os.chmod(sec, 0o644)
    cfg.secret_paths = ["token.env"]
    got = kinds(core.coax(cfg))
    assert {("software", "updates_pending"), ("software", "dependency_unpinned"),
            ("security", "patches_pending"), ("security", "secret_permissions")} <= got


def test_secret_contents_never_read(cfg, monkeypatch):
    sec = cfg.workdir / "id_key"
    sec.write_text("PRIVATE")
    os.chmod(sec, 0o640)
    cfg.secret_paths = [str(sec)]
    import pathlib
    real = pathlib.Path.read_text

    def guard(self, *a, **k):
        assert self.name != "id_key", "secret contents read"
        return real(self, *a, **k)
    monkeypatch.setattr(pathlib.Path, "read_text", guard)
    assert ("security", "secret_permissions") in kinds(core.coax(cfg))


def test_humans_probe(cfg):
    for f in ("q.txt", "fb.txt", "ok.txt"):
        (cfg.workdir / f).write_text("one\n")
    cfg.queue_file, cfg.feedback_file, cfg.approvals_file = "q.txt", "fb.txt", "ok.txt"
    assert kinds(core.coax(cfg)) == {("humans", k) for k in ("tasks_pending", "feedback_waiting", "approval_waiting")}


def test_probe_skips_gracefully_when_unavailable(cfg):
    cfg.sys_root = "/definitely/not/here"
    assert core.coax(cfg) == []


# ---------------------------------------------------------------- SOLVE
def test_auto_solution_runs_disk_low(cfg):
    cfg.temp_dir.mkdir()
    (cfg.temp_dir / "junk").write_text("x")
    cfg.log_path.write_text("x" * 20)
    cfg.log_rotate_bytes = 10
    core.ingest(cfg, [{"source": "memory_storage", "family": "memory_storage", "kind": "disk_low", "text": "low"}])
    res = core.once(cfg, probes={})
    assert res[0]["solution"] == "auto" and res[0]["light"] == "lit"
    assert not any(cfg.temp_dir.iterdir())
    assert (cfg.workdir / "magic.log.1").exists()
    assert not cfg.proposals_path.exists()


def test_refresh_cache_is_reversible(cfg):
    cfg.candidates_path.write_text("{}")
    core.ingest(cfg, [{"source": "data", "family": "data", "kind": "stale_input", "text": "stale"}])
    core.once(cfg, probes={})
    assert (cfg.workdir / "candidates.json.bak").exists() and not cfg.candidates_path.exists()


def test_risky_solution_is_proposed_not_run(cfg, monkeypatch):
    import subprocess
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    core.ingest(cfg, [{"source": "software", "family": "software", "kind": "updates_pending", "text": "12 updates"}])
    res = core.once(cfg, probes={})
    assert res[0]["solution"] == "propose" and res[0]["light"] == "lit"
    p = [json.loads(l) for l in cfg.proposals_path.read_text().splitlines()]
    assert p[0]["status"] == "awaiting approval" and p[0]["steps"]
    assert calls == []


def test_every_propose_solution_has_steps_and_auto_actions_exist(cfg):
    acts = solve.auto_actions(cfg, {"text": "x"})
    for k, s in solve.SOLUTIONS.items():
        if s["mode"] == "auto":
            assert all(a in acts for a in s["actions"]), k
        else:
            assert s["mode"] == "propose" and s["steps"], k


def test_failing_auto_solution_is_dark(cfg, monkeypatch):
    def boom(c):
        raise RuntimeError("tidy failed")
    monkeypatch.setattr(core, "task_tidy_temp", boom)
    core.ingest(cfg, [{"source": "x", "family": "memory_storage", "kind": "disk_low", "text": "low"}])
    res = core.once(cfg, probes={})
    assert res[0]["light"] == "dark" and res[0]["code"] == 1


def test_dark_ci_candidate_proposes_fork_plan_only(cfg, monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("executed"))
    cand = {"id": "github-ci:fitzyracing1/x:1", "branch": "live", "source": "github-ci",
            "text": "CI light is dark on fitzyracing1/x", "url": "https://github.com/r/1"}
    core.record_candidate(cfg, cand)
    p = json.loads(cfg.proposals_path.read_text().splitlines()[0])
    assert p["kind"] == "ci_dark" and p["status"] == "awaiting approval"
    assert any("fork" in s for s in p["steps"]) and any("owner's OK" in s for s in p["steps"])
    rec = json.loads(cfg.internet_tasks_path.read_text())
    assert rec["family"] == "software"


def test_candidates_are_family_tagged():
    assert families.family_for_candidate({"source": "wayback", "text": "increasing modem speed to 28.8 Kbps"}) == "network"
    assert families.family_for_candidate({"source": "wayback", "text": "Y2K Interactive Teleconference"}) == "security"
    assert families.family_for_candidate({"source": "wayback", "text": "review archived page x"}) == "data"
    assert families.family_for_candidate({"source": "github-gfi", "text": "read good first issue: x"}) == "humans"
