"""Internet candidate tasks for the TRIANGLE step.

Everything here is READ-ONLY: we only issue HTTP GETs to public APIs.
Nothing is ever posted, commented, or contacted, and fetched text is
stored as data and never executed.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import time
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional

USER_AGENT = "magic-ingestion/0.2 (+https://github.com/fitzyracing1/magic-ingestion)"

# A getter takes (url, headers, timeout) and returns raw bytes. Tests inject a fake.
Getter = Callable[[str, Dict[str, str], float], bytes]


def urllib_get(url: str, headers: Dict[str, str], timeout: float) -> bytes:
    if not url.startswith("https://"):
        raise ValueError("only https URLs are fetched")
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (https only)
        return r.read(5_000_000)  # cap size


class Fetcher:
    """GET-only client with a timeout and a simple per-call rate limit."""

    def __init__(self, getter: Getter = urllib_get, timeout: float = 10.0,
                 min_interval: float = 1.0, clock=time.monotonic, sleep=time.sleep):
        self.getter, self.timeout, self.min_interval = getter, timeout, min_interval
        self.clock, self.sleep = clock, sleep
        self._last: Optional[float] = None
        self.calls = 0

    def _wait(self) -> None:
        if self._last is not None:
            gap = self.clock() - self._last
            if gap < self.min_interval:
                self.sleep(self.min_interval - gap)
        self._last = self.clock()

    def get(self, url: str, accept: str = "application/json") -> bytes:
        self._wait()
        self.calls += 1
        headers = {"User-Agent": USER_AGENT, "Accept": accept}
        token = os.environ.get("GITHUB_TOKEN")
        if token and url.startswith("https://api.github.com/"):
            headers["Authorization"] = f"Bearer {token}"
        return self.getter(url, headers, self.timeout)

    def json(self, url: str):
        return json.loads(self.get(url).decode("utf-8"))


def _cand(branch: str, source: str, key: str, text: str, url: str = "", year=None) -> dict:
    c = {"id": f"{source}:{key}", "branch": branch, "source": source,
         "text": str(text)[:300], "url": url}
    if year:
        c["year"] = int(year)
    return c


# ---------------------------------------------------------------- live
def gh_good_first_issues(f: Fetcher, limit: int = 5) -> List[dict]:
    q = urllib.parse.quote('label:"good first issue" state:open is:issue')
    data = f.json(f"https://api.github.com/search/issues?q={q}&sort=created&per_page={limit}")
    return [_cand("live", "github-gfi", str(i["id"]),
                  f"read good first issue: {i.get('title', '')}", i.get("html_url", ""))
            for i in data.get("items", [])[:limit]]


def gh_dark_ci(f: Fetcher, owner: str, max_repos: int = 10) -> List[dict]:
    o = urllib.parse.quote(owner)
    repos = f.json(f"https://api.github.com/users/{o}/repos?sort=pushed&per_page={max_repos}")
    out = []
    for r in repos[:max_repos]:
        name = r["name"]
        runs = f.json(f"https://api.github.com/repos/{o}/{urllib.parse.quote(name)}/actions/runs?per_page=1")
        wr = (runs.get("workflow_runs") or [None])[0]
        if wr and wr.get("status") == "completed" and wr.get("conclusion") != "success":
            out.append(_cand("live", "github-ci", f"{owner}/{name}:{wr.get('id')}",
                             f"CI light is dark on {owner}/{name} ({wr.get('conclusion')}): review the run",
                             wr.get("html_url", "")))
    return out


def pypi_meta(f: Fetcher, pkg: str) -> List[dict]:
    d = f.json(f"https://pypi.org/pypi/{urllib.parse.quote(pkg)}/json")
    v = d.get("info", {}).get("version", "?")
    return [_cand("live", "pypi", f"{pkg}@{v}", f"review PyPI {pkg} latest release {v}",
                  f"https://pypi.org/project/{pkg}/")]


def npm_meta(f: Fetcher, pkg: str) -> List[dict]:
    d = f.json(f"https://registry.npmjs.org/{urllib.parse.quote(pkg, safe='@')}/latest")
    v = d.get("version", "?")
    return [_cand("live", "npm", f"{pkg}@{v}", f"review npm {pkg} latest release {v}",
                  f"https://www.npmjs.com/package/{pkg}")]


# ---------------------------------------------------------------- history
def wayback(f: Fetcher, url: str, limit: int = 3, since: str = "1996",
            until: Optional[str] = None, collapse: Optional[str] = None) -> List[dict]:
    params = {"url": url, "output": "json", "from": since, "limit": limit,
              "fl": "timestamp,original,statuscode", "filter": "statuscode:200"}
    if until:
        params["to"] = until
    if collapse:
        params["collapse"] = collapse  # e.g. "timestamp:4" = one snapshot per year
    q = urllib.parse.urlencode(params)
    rows = f.json(f"https://web.archive.org/cdx/search/cdx?{q}")
    out = []
    for row in rows[1:]:  # first row is the header
        ts, orig = row[0], row[1]
        out.append(_cand("history", "wayback", f"{orig}@{ts}",
                         f"review archived page {orig} from {ts[:4]}",
                         f"https://web.archive.org/web/{ts}/{orig}", year=ts[:4]))
    return out


def gharchive(f: Fetcher, hour: str, limit: int = 5) -> List[dict]:
    """hour like '2012-03-10-15'. Streams the gzip and keeps opened IssuesEvents."""
    raw = f.get(f"https://data.gharchive.org/{hour}.json.gz", accept="application/gzip")
    out = []
    with gzip.open(io.BytesIO(raw), "rt", encoding="utf-8", errors="replace") as g:
        for line in g:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") != "IssuesEvent":
                continue
            p = ev.get("payload", {})
            if p.get("action") != "opened":
                continue
            iss = p.get("issue", {})
            title = iss.get("title", "") if isinstance(iss, dict) else ""
            url = iss.get("html_url", "") if isinstance(iss, dict) else ""
            year = (ev.get("created_at") or hour)[:4]
            out.append(_cand("history", "gharchive", str(ev.get("id")),
                             f"old issue from {year}: {title}", url, year=year))
            if len(out) >= limit:
                break
    return out


def software_heritage(f: Fetcher, origin: str) -> List[dict]:
    o = urllib.parse.quote(origin, safe="")
    visits = f.json(f"https://archive.softwareheritage.org/api/1/origin/{o}/visits/?per_page=1")
    out = []
    for v in visits[:1]:
        date = v.get("date", "")
        out.append(_cand("history", "swh", f"{origin}@{date}",
                         f"review archived origin {origin} (visited {date[:10]})",
                         f"https://archive.softwareheritage.org/browse/origin/?origin_url={o}",
                         year=date[:4] if date[:4].isdigit() else None))
    return out


# ---------------------------------------------------------------- gather
def gather(cfg, f: Fetcher, mode: str = "all") -> Dict[str, object]:
    """Return {'candidates': [...], 'errors': [...]}. A failing source is skipped."""
    jobs = []
    if mode in ("all", "live"):
        jobs.append(("github-gfi", lambda: gh_good_first_issues(f)))
        if cfg.owner:
            jobs.append(("github-ci", lambda: gh_dark_ci(f, cfg.owner)))
        for p in cfg.pypi_packages:
            jobs.append((f"pypi:{p}", lambda p=p: pypi_meta(f, p)))
        for p in cfg.npm_packages:
            jobs.append((f"npm:{p}", lambda p=p: npm_meta(f, p)))
    if mode in ("all", "history"):
        for u in cfg.wayback_urls:
            jobs.append((f"wayback:{u}", lambda u=u: wayback(f, u, cfg.wayback_limit, cfg.wayback_from, cfg.wayback_to,
                                                  cfg.wayback_collapse)))
        if cfg.gharchive_hour:
            jobs.append(("gharchive", lambda: gharchive(f, cfg.gharchive_hour)))
        for o in cfg.swh_origins:
            jobs.append((f"swh:{o}", lambda o=o: software_heritage(f, o)))
    cands, errors, seen = [], [], set()
    for name, job in jobs:
        try:
            for c in job():
                if c["id"] not in seen:
                    seen.add(c["id"])
                    cands.append(c)
        except Exception as e:  # timeouts, HTTP errors, bad JSON
            errors.append(f"{name}: {type(e).__name__}: {e}")
    return {"candidates": cands, "errors": errors}


def load_cache(path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def refresh(cfg, f: Optional[Fetcher] = None, mode: str = "all", force: bool = False,
            now=time.time) -> dict:
    """Fetch into the workdir cache unless the cache is still fresh. Merges + dedupes."""
    cache = load_cache(cfg.candidates_path) or {"fetched": {}, "candidates": []}
    last = cache.get("fetched", {}).get(mode)
    fresh = last is not None and now() - last < cfg.cache_ttl
    if fresh and not force:
        return cache
    f = f or Fetcher(timeout=cfg.timeout, min_interval=cfg.rate_limit)
    got = gather(cfg, f, mode)
    by_id = {c["id"]: c for c in cache["candidates"]}
    for c in got["candidates"]:
        by_id.setdefault(c["id"], c)
    cache["candidates"] = list(by_id.values())
    cache["fetched"][mode] = now()
    cache["errors"] = got["errors"]
    cfg.candidates_path.write_text(json.dumps(cache, indent=1, sort_keys=True), encoding="utf-8")
    return cache


# ---------------------------------------------------------------- ternary tree
def ternary(items: List[dict], filler: dict) -> dict:
    """Arrange candidate leaves into a tree with exactly 3 children per node."""
    if not items:
        return filler
    if len(items) == 1:
        return {"candidate": items[0]}
    return {"name": "group", "children": [ternary(items[i::3], filler) for i in range(3)]}


def internet_tree(candidates: List[dict], builtin: dict, mode: str = "all") -> dict:
    """Root has 3 branches: live / history / built-in. Empty branches fall back to built-in."""
    live = [c for c in candidates if c["branch"] == "live"] if mode in ("all", "live") else []
    hist = [c for c in candidates if c["branch"] == "history"] if mode in ("all", "history") else []
    return {"name": "internet", "children": [
        {"name": "live", **_wrap(ternary(live, builtin))},
        {"name": "history", **_wrap(ternary(hist, builtin))},
        builtin,
    ]}


def _wrap(node: dict) -> dict:
    return {k: v for k, v in node.items() if k != "name"}
