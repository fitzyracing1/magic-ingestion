import gzip
import json
import urllib.request

import pytest

from magic_ingestion import core, cli, internet


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("real network used in tests")
    monkeypatch.setattr(urllib.request, "urlopen", boom)


def gz_events():
    evs = [
        {"id": "1", "type": "PushEvent", "created_at": "2012-03-10T15:00:00Z", "payload": {}},
        {"id": "2", "type": "IssuesEvent", "created_at": "2012-03-10T15:01:00Z",
         "payload": {"action": "opened", "issue": {"title": "Old bug", "html_url": "https://github.com/a/b/issues/1"}}},
        {"id": "3", "type": "IssuesEvent", "created_at": "2012-03-10T15:02:00Z",
         "payload": {"action": "closed", "issue": {"title": "x"}}},
    ]
    return gzip.compress(("\n".join(json.dumps(e) for e in evs) + "\nnot json\n").encode())


ROUTES = {
    "api.github.com/search/issues": {"items": [{"id": 11, "title": "Fix typo", "html_url": "https://github.com/x/y/issues/11"}]},
    "api.github.com/users/fitzyracing1/repos": [{"name": "lit-repo"}, {"name": "dark-repo"}],
    "repos/fitzyracing1/lit-repo/actions/runs": {"workflow_runs": [{"id": 1, "status": "completed", "conclusion": "success"}]},
    "repos/fitzyracing1/dark-repo/actions/runs": {"workflow_runs": [{"id": 2, "status": "completed", "conclusion": "failure", "html_url": "https://github.com/r/2"}]},
    "pypi.org/pypi/requests/json": {"info": {"version": "2.0"}},
    "registry.npmjs.org/left-pad/latest": {"version": "1.3.0"},
    "web.archive.org/cdx": [["timestamp", "original", "statuscode"], ["19981202000000", "http://example.com/", "200"]],
    "softwareheritage.org/api/1/origin": [{"date": "2015-06-01T00:00:00Z"}],
}


class FakeGet:
    def __init__(self, fail=()):
        self.urls, self.headers, self.fail = [], [], fail

    def __call__(self, url, headers, timeout):
        self.urls.append(url)
        self.headers.append(headers)
        assert timeout > 0
        if any(f in url for f in self.fail):
            raise TimeoutError("simulated timeout")
        if "data.gharchive.org" in url:
            return gz_events()
        for k, v in ROUTES.items():
            if k in url:
                return json.dumps(v).encode()
        raise AssertionError(url)


@pytest.fixture
def cfg(tmp_path):
    c = core.Config(workdir=tmp_path.resolve())
    c.min_free_bytes = 0
    c.owner = "fitzyracing1"
    c.pypi_packages = ["requests"]
    c.npm_packages = ["left-pad"]
    c.wayback_urls = ["example.com"]
    c.gharchive_hour = "2012-03-10-15"
    c.swh_origins = ["https://github.com/torvalds/linux"]
    return c


def fetcher(get, **kw):
    return internet.Fetcher(getter=get, min_interval=0, **kw)


def test_live_sources(cfg):
    get = FakeGet()
    res = internet.gather(cfg, fetcher(get), "live")
    srcs = sorted(c["source"] for c in res["candidates"])
    assert srcs == ["github-ci", "github-gfi", "npm", "pypi"]
    ci = [c for c in res["candidates"] if c["source"] == "github-ci"][0]
    assert "dark-repo" in ci["text"] and "dark" in ci["text"]
    assert all(c["branch"] == "live" for c in res["candidates"])
    assert not any("archive" in u for u in get.urls)


def test_history_sources_back_to_the_90s(cfg):
    res = internet.gather(cfg, fetcher(FakeGet()), "history")
    by = {c["source"]: c for c in res["candidates"]}
    assert by["wayback"]["year"] == 1998 and "from 1998" in by["wayback"]["text"]
    assert by["gharchive"]["text"] == "old issue from 2012: Old bug"
    assert by["swh"]["year"] == 2015
    assert len([c for c in res["candidates"] if c["source"] == "gharchive"]) == 1


def test_only_get_requests_and_https(cfg):
    get = FakeGet()
    internet.gather(cfg, fetcher(get), "all")
    assert all(u.startswith("https://") for u in get.urls)
    with pytest.raises(ValueError):
        internet.urllib_get("http://example.com", {}, 1)


def test_github_token_only_sent_to_github(cfg, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t0k")
    get = FakeGet()
    internet.gather(cfg, fetcher(get), "all")
    for u, h in zip(get.urls, get.headers):
        assert ("Authorization" in h) == u.startswith("https://api.github.com/")


def test_failing_source_is_skipped(cfg):
    res = internet.gather(cfg, fetcher(FakeGet(fail=("pypi.org",))), "live")
    assert any("pypi" in e and "TimeoutError" in e for e in res["errors"])
    assert any(c["source"] == "github-gfi" for c in res["candidates"])


def test_rate_limit_sleeps():
    t = [0.0]
    slept = []
    f = internet.Fetcher(getter=lambda u, h, to: b"{}", min_interval=2.0,
                         clock=lambda: t[0], sleep=slept.append)
    f.json("https://x/1")
    t[0] = 0.5
    f.json("https://x/2")
    assert slept == [1.5]


def test_cache_dedupe_and_ttl(cfg):
    get = FakeGet()
    clock = [1000.0]
    a = internet.refresh(cfg, fetcher(get), "live", now=lambda: clock[0])
    n_calls = len(get.urls)
    b = internet.refresh(cfg, fetcher(get), "live", now=lambda: clock[0] + 10)
    assert len(get.urls) == n_calls  # cache still fresh
    clock[0] += cfg.cache_ttl + 1
    c = internet.refresh(cfg, fetcher(get), "live", now=lambda: clock[0])
    assert len(get.urls) > n_calls
    ids = [x["id"] for x in c["candidates"]]
    assert len(ids) == len(set(ids)) == len(a["candidates"]) == len(b["candidates"])
    assert cfg.candidates_path.exists()


def test_ternary_tree_shape(cfg):
    items = [{"id": str(i), "text": f"t{i}", "branch": "live"} for i in range(7)]
    builtin = core.load_tree(cfg.tree_path)
    tree = internet.internet_tree(items, builtin)
    core.validate_tree(tree)
    assert [k.get("name") for k in tree["children"]] == ["live", "history", "root"]
    leaves = set()
    for i in range(81):
        node = core.node_at(tree, core.walk(tree, i))
        if "candidate" in node:
            leaves.add(node["candidate"]["id"])
    assert leaves == {str(i) for i in range(7)}


def test_once_records_internet_task_readonly(cfg):
    cfg.source = "all"
    get = FakeGet()
    kinds = set()
    for _ in range(9):
        for r in core.once(cfg, fetcher=fetcher(get)):
            assert r["light"] == "lit"
            kinds.add(r["name"].split(":")[0])
    assert "internet" in kinds
    recs = [json.loads(l) for l in cfg.internet_tasks_path.read_text().splitlines()]
    assert recs and all("text" in r for r in recs)


def test_offline_falls_back_to_builtin(cfg):
    cfg.source = "all"
    get = FakeGet(fail=("https://",))
    res = core.once(cfg, fetcher=fetcher(get))
    assert res[0]["light"] == "lit" and res[0]["name"] in core.TASKS


def test_fetched_text_is_never_executed(cfg, monkeypatch):
    import subprocess
    ROUTES_BAD = {"items": [{"id": 9, "title": "$(rm -rf /); `reboot`", "html_url": ""}]}
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    cfg.source = "live"
    cfg.owner = None
    cfg.pypi_packages = cfg.npm_packages = []
    f = fetcher(lambda u, h, t: json.dumps(ROUTES_BAD).encode())
    core.once(cfg, fetcher=f)
    assert calls == []


def test_cli_fetch(cfg, monkeypatch, capsys):
    get = FakeGet()
    monkeypatch.setattr(internet, "urllib_get", get)
    real = internet.Fetcher.__init__
    monkeypatch.setattr(internet.Fetcher, "__init__",
                        lambda self, getter=None, **kw: real(self, getter=get, min_interval=0))
    (cfg.workdir / "magic.json").write_text(json.dumps({"wayback_urls": ["example.com"]}))
    assert cli.main(["--workdir", str(cfg.workdir), "fetch", "--history"]) == 0
    assert "from 1998" in capsys.readouterr().out
    assert cli.main(["--workdir", str(cfg.workdir), "run", "--once", "--source", "history"]) == 0


def test_wayback_date_range(cfg):
    cfg.wayback_from, cfg.wayback_to, cfg.wayback_limit = "1996", "1999", 2
    get = FakeGet()
    internet.gather(cfg, fetcher(get), "history")
    u = [x for x in get.urls if "cdx" in x][0]
    assert "from=1996" in u and "to=1999" in u and "limit=2" in u
    assert "collapse=timestamp%3A4" in u


def test_wayback_range_from_magic_json(tmp_path):
    (tmp_path / "magic.json").write_text(json.dumps({"wayback_from": "1997", "wayback_to": "1998"}))
    c = core.Config.load(tmp_path)
    assert (c.wayback_from, c.wayback_to) == ("1997", "1998")
