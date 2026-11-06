#!/usr/bin/env python3
"""Local backend for the RCX Env Launcher.

Serves the web UI and a small JSON API that can bring a NetworkManager VPN up
and open a URL in Chrome. Binds to localhost only; single user, no auth.

Run:  python3 server.py [--port 8766]
"""

import base64
import glob
import http.cookiejar
import json
import os
import re
import shutil
import signal
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, urlencode

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")
CONFIG_PATH = os.path.join(HERE, "config.json")
FAVORITES_PATH = os.path.join(HERE, "favorites.json")
JENKINS_PATH = os.path.join(HERE, "jenkins.json")
JENKINS_JOBS_CACHE = os.path.join(HERE, ".jenkins_jobs_cache.json")
SETTINGS_PATH = os.path.join(HERE, "settings.json")
GITHUB_PATH = os.path.join(HERE, "github.json")
SYNC_SCRIPT = os.path.join(HERE, "sync_wiki.py")
DEFAULT_PORT = 8766

# Dynamic configuration (overridable via settings.json, no code edits needed).
DEFAULT_SETTINGS = {
    "github_org": "reactorcx",
    "wiki_url": "https://github.com/reactorcx/rcx-devops.wiki.git",
    "jenkins_host_suffix": "devops.lmvi.net",  # only fetch builds from this domain
    "browser": "default",  # "default" = system default browser (xdg-open); "chrome" = logged-in Chrome/Chromium
    "kafka_certs_dir": "~/SOFT/kafka",   # local folder of downloaded Kafka cert bundles
    "mongo_certs_dir": "~/SOFT/mongo",   # local folder of downloaded Mongo cert bundles
    "certs_search_home": False,          # scan the whole home dir instead of the folders above
}


def load_settings():
    cfg = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            cfg.update({k: v for k, v in json.load(f).items() if v})
    except (FileNotFoundError, ValueError):
        pass
    return cfg


def save_settings(data):
    cfg = load_settings()
    for k in ("github_org", "wiki_url", "jenkins_host_suffix",
              "kafka_certs_dir", "mongo_certs_dir"):
        if data.get(k):
            cfg[k] = str(data[k]).strip()
    if data.get("browser") in ("chrome", "default"):
        cfg["browser"] = data["browser"]
    if "certs_search_home" in data:
        cfg["certs_search_home"] = bool(data["certs_search_home"])
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return cfg


def load_github():
    try:
        with open(GITHUB_PATH, encoding="utf-8") as f:
            g = json.load(f)
    except (FileNotFoundError, ValueError):
        g = {}
    return {"user": (g.get("user") or "").strip(), "token": (g.get("token") or "").strip()}


def save_github(data):
    g = {"user": str(data.get("user", "")).strip(), "token": str(data.get("token", "")).strip()}
    with open(GITHUB_PATH, "w", encoding="utf-8") as f:
        json.dump(g, f, indent=2)
    try:
        os.chmod(GITHUB_PATH, 0o600)
    except OSError:
        pass
    return g

CHROME = (
    shutil.which("google-chrome")
    or shutil.which("google-chrome-stable")
    or shutil.which("chromium-browser")
    or shutil.which("chromium")
)

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}

_lock = threading.Lock()  # serialise nmcli/chrome actions


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"clients": []}


def load_favorites():
    """Favorite repo names (a plain list of strings)."""
    try:
        with open(FAVORITES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        repos = data.get("repos", []) if isinstance(data, dict) else data
        return [str(x) for x in repos if isinstance(x, str)]
    except (FileNotFoundError, ValueError):
        return []


def save_favorites(repos):
    names = [str(x) for x in repos if isinstance(x, str)]
    with open(FAVORITES_PATH, "w", encoding="utf-8") as f:
        json.dump({"repos": names}, f, indent=2)
    return names


# ---------------------------------------------------------------------------
# Jenkins troubleshooter
# ---------------------------------------------------------------------------

def load_jenkins():
    try:
        with open(JENKINS_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except (FileNotFoundError, ValueError):
        cfg = {}
    cfg.setdefault("base", "")
    cfg.setdefault("user", "")
    cfg.setdefault("token", "")
    cfg.setdefault("vpn", "DEVOPS")
    return cfg


def jenkins_configured(cfg=None):
    cfg = cfg or load_jenkins()
    return bool(cfg.get("base") and cfg.get("user") and cfg.get("token"))


def save_jenkins(data):
    cfg = load_jenkins()
    for k in ("base", "user", "token", "vpn"):
        if data.get(k) is not None:
            cfg[k] = str(data[k]).strip()
    with open(JENKINS_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    try:
        os.chmod(JENKINS_PATH, 0o600)
    except OSError:
        pass
    return cfg


def _jenkins_request(cfg, full_url, method="GET", data=None, headers=None, cookiejar=None):
    """Make an authenticated Jenkins request. Returns (status, headers, body_bytes).
    Verifies TLS, falling back to unverified only on a cert error (internal host
    over VPN). Only hosts under the configured suffix are ever contacted.

    Pass a shared `cookiejar` across a crumb GET + a build POST so Jenkins sees them
    as one session — its CSRF crumb is tied to the session cookie, so a POST without
    it is rejected with 403 even when the token is valid."""
    host = urlparse(full_url).hostname or ""
    suffix = load_settings()["jenkins_host_suffix"]
    if not host.endswith(suffix):
        raise ValueError(f"refusing non-Jenkins host: {host or '(none)'}")
    token = base64.b64encode(f"{cfg['user']}:{cfg['token']}".encode()).decode()
    last = None
    for unverified in (False, True):
        req = urllib.request.Request(full_url, data=data, method=method)
        req.add_header("Authorization", "Basic " + token)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        ctx = ssl._create_unverified_context() if unverified else None
        handlers = [urllib.request.HTTPSHandler(context=ctx)]
        if cookiejar is not None:
            handlers.append(urllib.request.HTTPCookieProcessor(cookiejar))
        opener = urllib.request.build_opener(*handlers)
        try:
            with opener.open(req, timeout=30) as r:
                return r.status, dict(r.headers), r.read()
        except ssl.SSLError as exc:
            last = exc
            continue
    raise last


def _jenkins_open(cfg, full_url):
    """GET a Jenkins URL with basic auth. Returns raw bytes."""
    return _jenkins_request(cfg, full_url)[2]


def jenkins_api(cfg, obj_url, tree=None):
    u = obj_url.rstrip("/") + "/api/json"
    if tree:
        u += "?tree=" + urllib.request.quote(tree, safe="[],*")
    return json.loads(_jenkins_open(cfg, u).decode("utf-8", "replace"))


def jenkins_text(cfg, full_url):
    return _jenkins_open(cfg, full_url).decode("utf-8", "replace")


def ensure_vpn(cfg):
    """Bring the configured Jenkins VPN up. Returns (ok, message)."""
    vpn = cfg.get("vpn")
    if not vpn:
        return True, "no vpn"
    with _lock:
        return vpn_up(vpn)


_MULTI = ("MultiBranch", "MultiBranchDefaults")
_FOLDER = ("Folder", "OrganizationFolder")


def jenkins_crawl_repos(cfg):
    """Walk the folder tree, collecting multibranch projects (= repos). Each folder
    level is listed in parallel, so a full refresh is seconds rather than minutes
    over the VPN (one sequential call per folder used to take ~1–2 min)."""
    from concurrent.futures import ThreadPoolExecutor

    repos = []

    def children(url):
        try:
            return jenkins_api(cfg, url, "jobs[name,url,_class]").get("jobs", [])
        except Exception:
            return []

    frontier = [(cfg["base"], [])]            # (folder url, path-so-far)
    depth = 0
    with ThreadPoolExecutor(max_workers=10) as ex:
        while frontier and depth <= 6:
            fetched = list(ex.map(lambda it: (it[1], children(it[0])), frontier))
            nxt = []
            for path, jobs in fetched:
                for j in jobs:
                    cls = j.get("_class", "").split(".")[-1]
                    name, jurl = j.get("name"), j.get("url")
                    if any(m in cls for m in _MULTI):
                        repos.append({"repo": name, "folder": "/".join(path), "url": jurl})
                    elif any(f in cls for f in _FOLDER):
                        nxt.append((jurl, path + [name]))
                    elif "WorkflowJob" in cls or "FreeStyle" in cls:
                        repos.append({"repo": name, "folder": "/".join(path),
                                      "url": jurl, "standalone": True})
            frontier = nxt
            depth += 1
    repos.sort(key=lambda r: (r["folder"].lower(), r["repo"].lower()))
    return repos


def jenkins_jobs(cfg, refresh=False):
    if not refresh:
        try:
            with open(JENKINS_JOBS_CACHE, encoding="utf-8") as f:
                return json.load(f).get("repos", [])
        except (FileNotFoundError, ValueError):
            pass
    repos = jenkins_crawl_repos(cfg)
    try:
        with open(JENKINS_JOBS_CACHE, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "repos": repos}, f)
    except OSError:
        pass
    return repos


def _branch_sort_key(b):
    m = re.match(r"PR-(\d+)$", b["name"])
    if m:
        return (1, -int(m.group(1)), "")
    return (0, 0, b["name"].lower())


def jenkins_branches(cfg, repo_url):
    data = jenkins_api(cfg, repo_url, "jobs[name,url,color]")
    out = [{"name": b.get("name"), "url": b.get("url")} for b in data.get("jobs", [])]
    out.sort(key=_branch_sort_key)
    return out


# Lines in a console log that usually point at the real failure.
_FAIL_PATTERNS = [re.compile(p, re.I) for p in [
    r"\bBUILD FAILURE\b", r"\bBUILD FAILED\b", r"^\s*\[ERROR\]", r"^ERROR[: ]",
    r"\berror:", r"Exception(?:[: ]|$)", r"Caused by:", r"\bFAILED\b", r"npm ERR!",
    r"AssertionError", r"Tests? run:.*Failures?: [1-9]", r"Tests? run:.*Errors?: [1-9]",
    r"> Task .* FAILED", r"make.*\bError\b", r"Finished: (FAILURE|UNSTABLE)",
    r"compilation error", r"cannot find symbol", r"Traceback \(most recent call last\)",
    r"OOMKilled|OutOfMemory", r"No such file or directory", r"Permission denied",
    r"script returned exit code [1-9]",
]]


def extract_failure(log):
    """Pull the lines most likely to explain a failure out of a console log."""
    if not log:
        return []
    lines = log.splitlines()
    hits = [i for i, l in enumerate(lines) if any(p.search(l) for p in _FAIL_PATTERNS)]
    if not hits:
        return lines[-15:]
    seen, picked = set(), []
    for i in hits:
        for j in range(max(0, i - 1), min(len(lines), i + 3)):
            if j not in seen:
                seen.add(j)
                picked.append(j)
    picked.sort()
    if len(picked) > 70:          # keep the last ones — final errors matter most
        picked = picked[-70:]
    return [lines[j] for j in picked]


def resolve_build_url(target, build):
    """Turn a branch/job/build URL + a build selector into a concrete build URL."""
    t = target.strip().rstrip("/")
    m = re.search(r"/(\d+)$", t)
    build = (build or "").strip()
    if build and build.lower() not in ("latest", "last", ""):
        base = t[:m.start()] if m else t
        return base.rstrip("/") + "/" + str(int(build))
    if re.search(r"/(lastBuild|lastCompletedBuild|lastFailedBuild|\d+)$", t):
        return t                   # already points at a specific/last build
    return t + "/lastBuild"


def _first_cause(info):
    """Human 'who/what started this build' from the build's actions, or ''."""
    for a in info.get("actions", []) or []:
        for c in (a.get("causes") or []):
            if c.get("shortDescription"):
                return c["shortDescription"]
    return ""


def _build_params(info):
    """Build parameters as [{name, value}] (from the ParametersAction)."""
    out = []
    for a in info.get("actions", []) or []:
        for p in (a.get("parameters") or []):
            name = p.get("name")
            if name is None:
                continue
            val = p.get("value")
            if isinstance(val, bool):
                val = "true" if val else "false"
            out.append({"name": name, "value": "" if val is None else str(val)})
    return out


def _build_scm(info):
    """Git branch + short commit for the build, if the git plugin recorded it."""
    for a in info.get("actions", []) or []:
        rev = a.get("lastBuiltRevision")
        if rev:
            sha = rev.get("SHA1") or ""
            branches = rev.get("branch") or []
            branch = branches[0].get("name") if branches else ""
            return {"commit": sha[:10] if sha else "", "branch": branch or ""}
    return {}


# Parameter names (lowercased) that commonly carry the release/image version.
_VERSION_KEYS = ("version", "release_version", "build_version", "app_version",
                 "image_tag", "docker_tag", "tag", "release", "artifact_version")


def _derive_version(params, description):
    """Best-effort build version: a version-ish parameter, else a version in the
    build description (teams often set it there), else ''."""
    pm = {p["name"].lower(): p["value"] for p in params}
    for k in _VERSION_KEYS:
        if pm.get(k):
            return pm[k]
    for p in params:
        nl = p["name"].lower()
        if ("version" in nl or "tag" in nl) and p["value"]:
            return p["value"]
    if description:
        m = re.search(r"\bv?\d+\.\d+[\w.\-+]*", description)
        if m:
            return m.group(0)
    return ""


def _version_from_log(log):
    """Scan the console for a published artifact/image version. Teams publish to
    Nexus or AWS ECR at the end of a build, and the version/tag shows up there.
    Returns (version, source) or ('', '')."""
    if not log:
        return "", ""
    patterns = [
        # AWS ECR push: the tag is printed as "1.4.2: digest: sha256:..."
        (r"(?m)^(v?\d[\w.\-]*): digest: sha256:", "ECR image"),
        # AWS ECR image ref: 1234.dkr.ecr.us-east-1.amazonaws.com/repo:TAG
        (r"\.dkr\.ecr\.[\w-]+\.amazonaws\.com/[\w./-]+:(v?\d[\w.\-]*)", "ECR image"),
        (r"(?:docker push|Pushing|Pushed|tagging|tagged)\s+[\w./-]+:(v?\d[\w.\-]*)", "image tag"),
        # Nexus / Maven artifact upload: .../artifact-1.2.3.jar, Uploaded .../1.2.3/...
        (r"[Uu]pload(?:ed|ing)?\b[^\n]*?/(\d[\w.\-]*)/[\w.\-]+\.(?:jar|war|zip|tgz|pom)", "Nexus artifact"),
        (r"(?:nexus|repository)[^\n]*?(\d+\.\d+\.\d[\w.\-]*)", "Nexus"),
        (r"Building (?:jar|war):[^\n]*?-(\d+\.\d+[\w.\-]*)\.(?:jar|war)", "artifact"),
        (r"(?:^|\s)(?:version|VERSION|releaseVersion)\s*[=:]\s*v?(\d+\.\d+[\w.\-]*)", "log"),
    ]
    for rx, src in patterns:
        m = re.search(rx, log, re.MULTILINE)
        if m:
            return m.group(1), src
    return "", ""


def _image_from_log(log):
    """The published container image ref (repo:tag) from the log, repo shortened to
    its last path segment: 'host/ns/rcx-api-gateway:TAG' -> 'rcx-api-gateway:TAG'.
    The tag may carry an arch/branch prefix (arm64-feature-...-2.0.0-8), so it is
    NOT required to start with a digit -- only to contain one (skips :latest)."""
    if not log:
        return ""
    tag = r"[A-Za-z0-9][\w.\-]*\d[\w.\-]*"          # a tag that contains a digit
    rx_ref = re.compile(r"(?:Successfully tagged|docker\s+push|docker\s+tag[^\n]*?\s)\s*"
                        r"([A-Za-z0-9][\w./\-]*:" + tag + r")\b", re.I)
    rx_ecr = re.compile(r"([\w.\-]*\.amazonaws\.com/[\w./\-]+:" + tag + r")\b")
    rx_digest = re.compile(r"^(" + tag + r"): digest: sha256:")
    rx_repo = re.compile(r"repository\s*\[([^\]]+)\]")

    def short(ref):
        repo, _, t = ref.rpartition(":")
        return repo.rstrip("/").split("/")[-1] + ":" + t

    lines = log.splitlines()
    for line in lines:
        m = rx_ref.search(line) or rx_ecr.search(line)
        if m:
            return short(m.group(1))
    last_repo = ""
    for line in lines:
        rm = rx_repo.search(line)
        if rm:
            last_repo = rm.group(1).rstrip("/").split("/")[-1]
        dm = rx_digest.search(line.strip())
        if dm:
            return (last_repo + ":" + dm.group(1)) if last_repo else dm.group(1)
    return ""


def _build_extras(info, log=None):
    """The 'full build info' extras: parameters, git commit/branch, description, the
    published image ref, and the release version (from a parameter, the description,
    the image tag, or the log)."""
    params = _build_params(info)
    desc = info.get("description")
    version = _derive_version(params, desc)
    source = "parameter" if version else ""
    image = _image_from_log(log)
    if not version and image:
        tag = image.split(":", 1)[1]
        m = re.search(r"\d+\.\d+[\w.\-]*", tag)      # pull a clean version out of the tag
        version, source = (m.group(0) if m else tag), "image"
    if not version:
        version, source = _version_from_log(log)
    return {
        "params": params,
        "scm": _build_scm(info),
        "description": desc,
        "image": image,
        "version": version,
        "versionSource": source,
    }


def jenkins_job_tail(cfg, build_url):
    """Last-successful / last build of the job that owns build_url (for context)."""
    m = re.search(r"^(.*?)/(\d+)/?$", build_url.rstrip("/"))
    job_url = (m.group(1) + "/") if m else (build_url.rstrip("/") + "/")
    try:
        data = jenkins_api(
            cfg, job_url,
            "lastSuccessfulBuild[number,timestamp,duration,url],"
            "lastBuild[number,timestamp,duration,url,result,building]")
    except Exception:
        return {}
    return {"url": job_url,
            "lastSuccess": data.get("lastSuccessfulBuild"),
            "lastBuild": data.get("lastBuild")}


def jenkins_diagnose(cfg, build_url):
    info = jenkins_api(
        cfg, build_url,
        "number,result,building,fullDisplayName,displayName,description,url,timestamp,"
        "duration,estimatedDuration,"
        "actions[causes[shortDescription],parameters[name,value],"
        "lastBuiltRevision[SHA1,branch[name]]]")
    stages = []
    try:
        wf = json.loads(jenkins_text(cfg, build_url.rstrip("/") + "/wfapi/describe"))
        stages = [{"name": s.get("name"), "status": s.get("status")}
                  for s in wf.get("stages", [])]
    except Exception:
        pass
    try:
        log = jenkins_text(cfg, build_url.rstrip("/") + "/consoleText")
    except Exception as exc:
        log = f"(could not fetch console log: {exc})"
    failed = [s["name"] for s in stages
              if s["status"] in ("FAILED", "UNSTABLE", "ABORTED")]
    return {
        "ok": True,
        "result": info.get("result"),
        "building": info.get("building"),
        "name": info.get("fullDisplayName"),
        "number": info.get("number"),
        "url": info.get("url") or build_url,
        "timestamp": info.get("timestamp"),
        "duration": info.get("duration"),
        "estimatedDuration": info.get("estimatedDuration"),
        "cause": _first_cause(info),
        "job": jenkins_job_tail(cfg, build_url),
        "stages": stages,
        "failedStages": failed,
        "reason": extract_failure(log),
        "log": log,
        **_build_extras(info, log),
    }


def jenkins_crumb(cfg, cookiejar=None):
    """CSRF crumb header for POSTs, or {} if the issuer is disabled/unreachable.
    Share `cookiejar` with the following POST so the crumb's session cookie rides along."""
    try:
        _, _, body = _jenkins_request(
            cfg, cfg["base"].rstrip("/") + "/crumbIssuer/api/json", cookiejar=cookiejar)
        d = json.loads(body.decode("utf-8", "replace"))
        return {d["crumbRequestField"]: d["crumb"]}
    except Exception:
        return {}


def jenkins_job_params(cfg, job_url):
    """(buildable, {param: default}) for a job. Empty dict = not parameterized."""
    data = jenkins_api(
        cfg, job_url,
        "buildable,property[parameterDefinitions[name,defaultParameterValue[value]]]")
    params = {}
    for prop in data.get("property", []):
        for pd in prop.get("parameterDefinitions", []) or []:
            name = pd.get("name")
            if name is None:
                continue
            dv = (pd.get("defaultParameterValue") or {}).get("value")
            if isinstance(dv, bool):
                dv = "true" if dv else "false"
            params[name] = "" if dv is None else str(dv)
    return data.get("buildable", True), params


def jenkins_trigger(cfg, job_url):
    """Kick off a build of a branch/job. Returns {"queue": <queue item url>}.
    Parameterized jobs are built with their default parameter values."""
    buildable, params = jenkins_job_params(cfg, job_url)
    if not buildable:
        raise ValueError("this job can't be built directly — pick a branch, not the repo")
    # One cookie session for the crumb GET + the build POST (Jenkins ties the
    # CSRF crumb to the session cookie; a POST without it gets 403).
    jar = http.cookiejar.CookieJar()
    headers = jenkins_crumb(cfg, cookiejar=jar)
    base = job_url.rstrip("/")
    if params:
        data = urlencode(params).encode()
        url = base + "/buildWithParameters"
    else:
        data = b""
        url = base + "/build"
    _, resp_headers, _ = _jenkins_request(
        cfg, url, method="POST", data=data, headers=headers, cookiejar=jar)
    loc = (resp_headers.get("Location") or "").rstrip("/")
    return {"queue": loc, "parameterized": bool(params)}


def jenkins_build_status(cfg, queue_url=None, build_url=None):
    """Poll a queued/running build. While queued, returns the queue reason; once a
    build exists, returns its state + console log (full diagnosis when finished)."""
    if not build_url and queue_url:
        q = jenkins_api(cfg, queue_url)
        if q.get("cancelled"):
            return {"ok": True, "state": "cancelled",
                    "why": q.get("why") or "build was cancelled in the queue"}
        ex = q.get("executable")
        if not ex or not ex.get("url"):
            return {"ok": True, "state": "queued",
                    "why": q.get("why") or "waiting for an executor…"}
        build_url = ex.get("url")
    if not build_url:
        return {"ok": False, "error": "no queue item or build url"}

    info = jenkins_api(
        cfg, build_url,
        "number,result,building,fullDisplayName,displayName,description,url,timestamp,"
        "duration,estimatedDuration,"
        "actions[causes[shortDescription],parameters[name,value],"
        "lastBuiltRevision[SHA1,branch[name]]]")
    building = info.get("building")
    out = {
        "ok": True,
        "state": "building" if building else "done",
        "building": building,
        "result": info.get("result"),
        "number": info.get("number"),
        "name": info.get("fullDisplayName"),
        "url": info.get("url") or build_url,
        "buildUrl": build_url,
        "timestamp": info.get("timestamp"),
        "duration": info.get("duration"),
        "estimatedDuration": info.get("estimatedDuration"),
        "cause": _first_cause(info),
        "job": jenkins_job_tail(cfg, build_url),
    }
    if building:
        try:
            out["log"] = jenkins_text(cfg, build_url.rstrip("/") + "/consoleText")
        except Exception:
            out["log"] = ""
    else:
        diag = jenkins_diagnose(cfg, build_url)
        out.update({"stages": diag["stages"], "failedStages": diag["failedStages"],
                    "reason": diag["reason"], "log": diag["log"]})
    out.update(_build_extras(info, out.get("log")))
    return out


def known_vpn_names():
    """VPN names we accept in /api/open: those in config.json plus live nmcli VPNs."""
    names = set()
    cfg = load_config()
    for c in cfg.get("clients", []):
        for e in c.get("environments", []):
            if e.get("vpn"):
                names.add(e["vpn"])
    try:
        r = subprocess.run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"],
                           capture_output=True, text=True, timeout=10)
        for line in r.stdout.splitlines():
            name, _, ctype = line.rpartition(":")
            if ctype == "vpn":
                names.add(name)
    except Exception:
        pass
    return names


def active_vpns():
    try:
        r = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,TYPE,STATE", "connection", "show", "--active"],
            capture_output=True, text=True, timeout=10)
    except Exception as exc:
        return {"active": [], "error": str(exc)}
    out = []
    for line in r.stdout.splitlines():
        fields = line.split(":")
        if len(fields) >= 2 and fields[-2] == "vpn":
            out.append(":".join(fields[:-2]))
    return {"active": out}


def vpn_up(name):
    """Bring a VPN up (if not already active). Returns (ok, message)."""
    if name in active_vpns().get("active", []):
        return True, "already connected"
    try:
        r = subprocess.run(["nmcli", "connection", "up", name],
                           capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return False, "timed out connecting VPN"
    except Exception as exc:
        return False, str(exc)
    if r.returncode == 0:
        return True, "connected"
    return False, (r.stderr or r.stdout or "nmcli failed").strip()


def vpn_down(name):
    try:
        r = subprocess.run(["nmcli", "connection", "down", name],
                           capture_output=True, text=True, timeout=30)
    except Exception as exc:
        return False, str(exc)
    return (r.returncode == 0), (r.stderr or r.stdout or "").strip()


def _spawn(argv):
    try:
        subprocess.Popen(argv,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except FileNotFoundError:
        return False, f"{argv[0]} not found on this machine"
    except Exception as exc:
        return False, str(exc)
    return True, "opened"


def open_url(url):
    """Open a URL in the configured browser. 'default' (the default) uses the
    system default browser via xdg-open; 'chrome' uses the detected Chrome/Chromium
    so pages land in your already-logged-in profile. Each mode falls back to the
    other if its opener isn't available."""
    pref = load_settings().get("browser", "default")
    if pref == "chrome":
        if CHROME:
            return _spawn([CHROME, url])
        # Chrome not installed → fall back to the system default
    if shutil.which("xdg-open"):
        return _spawn(["xdg-open", url])
    if CHROME:                            # no xdg-open → fall back to Chrome
        return _spawn([CHROME, url])
    return False, "no browser available (install a browser, or xdg-open)"


# ---------------------------------------------------------------------------
# Kafka / Mongo cert bundles: scan a local folder of downloaded cert bundles and
# turn each into copy-ready connection settings. Secrets (keystore passwords, DB
# passwords) live only in the user's local files; they're read on demand and sent
# to the localhost UI, never stored in config or committed.
# ---------------------------------------------------------------------------

_CERT_EXTS = {"kafka": (".jks",),
              "mongo": (".pem", ".crt", ".key", ".p12", ".jks")}


def _certs_root(kind):
    s = load_settings()
    key = "kafka_certs_dir" if kind == "kafka" else "mongo_certs_dir"
    default = DEFAULT_SETTINGS[key]
    return os.path.realpath(os.path.expanduser(s.get(key) or default))


def _certs_roots(kind):
    """Directories we're allowed to scan/read for this kind."""
    if load_settings().get("certs_search_home"):
        return [os.path.realpath(os.path.expanduser("~"))]
    return [_certs_root(kind)]


def _cert_path_ok(kind, path):
    """True if `path` sits inside an allowed cert root (guards open/read/write)."""
    rp = os.path.realpath(path)
    for a in _certs_roots(kind):
        if rp == a or rp.startswith(a + os.sep):
            return True
    return False


def _is_details(fn):
    l = fn.lower()
    return l.endswith(".txt") and ("connection" in l or "detail" in l)


def _client_env(dirpath, root):
    rel = os.path.relpath(dirpath, root)
    parts = [p.strip() for p in rel.split(os.sep) if p and p != "."]
    if not parts:
        parts = [os.path.basename(dirpath)]
    client = parts[0]
    env = " / ".join(parts[1:]) if len(parts) > 1 else parts[0]
    return client, env


def scan_cert_bundles(kind):
    """Each directory that holds a relevant cert file is one bundle (metadata only)."""
    exts = _CERT_EXTS[kind]
    out = {}
    for root in _certs_roots(kind):
        if not os.path.isdir(root):
            continue
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            certs = sorted(f for f in files if f.lower().endswith(exts))
            if not certs:
                continue
            client, env = _client_env(dirpath, root)
            out[dirpath] = {
                "kind": kind, "path": dirpath, "client": client, "env": env,
                "name": os.path.basename(dirpath) or client,
                "files": certs,
                "hasDetails": any(_is_details(f) for f in files),
            }
    return sorted(out.values(), key=lambda b: (b["client"].lower(), b["env"].lower()))


def _read_details(path):
    try:
        f = next((x for x in os.listdir(path) if _is_details(x)), None)
    except OSError:
        return ""
    if not f:
        return ""
    try:
        with open(os.path.join(path, f), encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _grab(txt, pat):
    m = re.search(r"(?im)(?:" + pat + r")\s*[:=]\s*(\S+)", txt)
    return m.group(1).strip() if m else ""


def _pick(files, *needles, ext=None):
    for f in files:
        l = f.lower()
        if ext and not l.endswith(ext):
            continue
        if all(n in l for n in needles):
            return f
    return ""


def kafka_connect(path):
    files = os.listdir(path)
    txt = _read_details(path)
    truststore = _pick(files, "truststore", ext=".jks")
    keystore = (_pick(files, "keystore", ext=".jks")
                or _pick(files, "clientstore", ext=".jks"))
    return {
        "kind": "kafka",
        "cluster": _grab(txt, r"cluster\s*name") or os.path.basename(path),
        "bootstrap": _grab(txt, r"bootstrap"),
        "zookeeper": _grab(txt, r"zookeeper\s*host"),
        "security": "SSL",
        "truststore": {"file": truststore,
                       "path": os.path.join(path, truststore) if truststore else "",
                       "password": _grab(txt, r"truststore\s*password")},
        "keystore": {"file": keystore,
                     "path": os.path.join(path, keystore) if keystore else "",
                     "password": _grab(txt, r"keystore\s*password")},
    }


def mongo_connect(path):
    import urllib.parse as up
    files = os.listdir(path)
    txt = _read_details(path)
    ca = _pick(files, "ca", ext=".pem") or _pick(files, "ca", ext=".crt")
    client_pem = (_pick(files, "client", ext=".pem")
                  or next((f for f in files if f.lower().endswith(".pem") and f != ca), ""))
    host = _grab(txt, r"address|host")
    port = _grab(txt, r"port") or "27017"
    user = _grab(txt, r"user\s*name|username|user")
    pw = _grab(txt, r"password")
    mech = _grab(txt, r"auth\s*mech\w*") or "SCRAM-SHA-1"
    passphrase = _grab(txt, r"passphrase")
    dbs = re.findall(r"(?im)^\s*database\s*[:=]\s*(\S+)", txt)
    ca_path = os.path.join(path, ca) if ca else ""
    pem_path = os.path.join(path, client_pem) if client_pem else ""
    cred = (up.quote(user, safe="") + ":" + up.quote(pw, safe="") + "@") if user else ""
    params = ["tls=true", "directConnection=true"]
    if ca_path:
        params.append("tlsCAFile=" + up.quote(ca_path, safe="/"))
    if pem_path:
        params.append("tlsCertificateKeyFile=" + up.quote(pem_path, safe="/"))
    if passphrase:
        params.append("tlsCertificateKeyFilePassword=" + up.quote(passphrase, safe=""))
    if mech:
        params.append("authMechanism=" + up.quote(mech, safe=""))
    hostport = (host or "localhost") + ":" + port
    uri = "mongodb://" + cred + hostport + "/?" + "&".join(params)
    return {
        "kind": "mongo",
        "name": _grab(txt, r"name") or os.path.basename(path),
        "host": host, "port": port, "user": user,
        "authMech": mech, "databases": sorted(set(dbs)),
        "ca": {"file": ca, "path": ca_path},
        "clientPem": {"file": client_pem, "path": pem_path},
        "uri": uri,
    }


def write_compass_import(path):
    """Write a Compass 'Import Connections' JSON for a mongo bundle; return its path.
    The file carries the connection string (with credentials), so it's chmod 600."""
    import uuid
    info = mongo_connect(path)
    doc = {
        "type": "Compass Connections",
        "version": 1,
        "connections": [{
            "id": str(uuid.uuid4()),
            "favorite": {"name": info["name"]},
            "connectionOptions": {"connectionString": info["uri"]},
        }],
    }
    out = os.path.join(path, "compass-connection.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    try:
        os.chmod(out, 0o600)
    except OSError:
        pass
    return out


# ---- launching the desktop clients ----

def _first_exe(*cands):
    for c in cands:
        if not c:
            continue
        c = os.path.expanduser(c)
        matches = sorted(glob.glob(c)) if "*" in c else [c]
        for m in matches:
            if os.path.isfile(m) and os.access(m, os.X_OK):
                return m
    return ""


def offsetexplorer_gui():
    return _first_exe(shutil.which("offset-explorer") or "",
                      shutil.which("offsetexplorer") or "",
                      "~/offsetexplorer*/offsetexplorer",
                      "~/offset-explorer*/offset-explorer",
                      "/opt/offsetexplorer*/offsetexplorer",
                      "/opt/offset-explorer*/offset-explorer",
                      "/snap/offset-explorer/current/offsetexplorer")


def offsetexplorer_cli():
    return _first_exe("~/offsetexplorer*/commandline",
                      "~/offset-explorer*/commandline",
                      "/opt/offsetexplorer*/commandline",
                      "/opt/offset-explorer*/commandline",
                      "/snap/offset-explorer/current/commandline")


def compass_bin():
    return (shutil.which("mongodb-compass") or shutil.which("mongodb-compass-stable")
            or shutil.which("mongodb-compass-beta") or "")


def offsetexplorer_running():
    """True if the Offset Explorer GUI is open (it reloads connections only at
    startup, so a save made while it's running shows up after a restart)."""
    try:
        r = subprocess.run(["pgrep", "-f", "MainApp_offsetexplorer"],
                           capture_output=True, text=True, timeout=5)
        return bool(r.stdout.strip())
    except Exception:
        return False


def _offsetexplorer_pids():
    try:
        r = subprocess.run(["pgrep", "-f", "MainApp_offsetexplorer"],
                           capture_output=True, text=True, timeout=5)
        return [int(p) for p in r.stdout.split() if p.strip().isdigit()]
    except Exception:
        return []


def stop_offsetexplorer(timeout=20):
    """Close the running Offset Explorer GUI and wait for it to exit, so the
    connections it holds in memory are flushed to connections.xml before we add
    to that file (OE rewrites the file on close, so a save made while it's open
    would otherwise be lost). SIGTERM first for a clean shutdown, SIGKILL only
    if it refuses to go. Returns True once no OE process remains."""
    pids = _offsetexplorer_pids()
    if not pids:
        return True
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not offsetexplorer_running():
            time.sleep(0.5)          # let the file write settle
            return True
        time.sleep(0.4)
    for pid in _offsetexplorer_pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    time.sleep(0.6)
    return not offsetexplorer_running()


def offsetexplorer_connections_file():
    """Locate Offset Explorer's connections.xml. The data-dir name tracks the
    install version (offsetexplorer4 -> ~/.offsetexplorer4, offsetexplorer5 ->
    ~/.offsetexplorer5, older Kafka Tool -> ~/.kafkatool2) and differs from
    machine to machine, so derive it from the installed app rather than hardcode
    a version — otherwise the CLI writes one file and we verify another, and a
    good save reads back as 'Not saved'."""
    existing = []
    for pat in ("~/.offsetexplorer*", "~/.kafkatool*"):
        for d in glob.glob(os.path.expanduser(pat)):
            f = os.path.join(d, "connections.xml")
            if os.path.isfile(f):
                existing.append(f)
    derived = ""
    exe = offsetexplorer_cli() or offsetexplorer_gui()
    if exe:
        base = os.path.basename(os.path.dirname(exe))   # e.g. offsetexplorer4
        if base:
            derived = os.path.expanduser("~/." + base + "/connections.xml")
    # The install-derived dir is where this machine's CLI writes; trust it if the
    # file is already there. Otherwise fall back to whatever data file exists
    # (newest), and finally to the derived path (the CLI creates it on first save).
    if derived and os.path.isfile(derived):
        return derived
    if existing:
        existing.sort(key=os.path.getmtime, reverse=True)
        return existing[0]
    return derived or os.path.expanduser("~/.offsetexplorer4/connections.xml")


def cluster_in_connections(cluster):
    """Return (found, file_checked): whether a connection with this name is in
    Offset Explorer's saved file. Reads only connection names, never secrets."""
    conn = offsetexplorer_connections_file()
    try:
        with open(conn, "r", encoding="utf-8", errors="ignore") as f:
            data = f.read()
    except OSError:
        return False, conn
    names = set(re.findall(r'name="([^"]*)"', data))
    return cluster in names, conn


def certs_apps():
    return {
        "offsetexplorer": bool(offsetexplorer_gui()),
        "offsetexplorerCli": bool(offsetexplorer_cli()),
        "compass": bool(compass_bin()),
    }


def pick_directory(title="Choose a folder"):
    """Open a native folder picker on the user's desktop and return the chosen
    absolute path, so cert folders can be picked instead of typed. Returns None
    if no picker tool is installed, "" if the user cancelled."""
    tool = shutil.which("zenity") or shutil.which("qarma")
    if tool:
        argv = [tool, "--file-selection", "--directory", "--title", title]
    elif shutil.which("kdialog"):
        argv = [shutil.which("kdialog"), "--getexistingdirectory",
                os.path.expanduser("~")]
    else:
        return None
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    except Exception:
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


class Handler(BaseHTTPRequestHandler):
    server_version = "RCXLauncher/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # --- helpers ---
    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def _serve_static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        rel = path.lstrip("/")
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(WEB_DIR) or not os.path.isfile(full):
            self.send_error(404, "Not found")
            return
        ext = os.path.splitext(full)[1]
        ctype = _CONTENT_TYPES.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # --- routes ---
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/config":
            self._send_json(load_config())
        elif path == "/api/vpn/status":
            self._send_json(active_vpns())
        elif path == "/api/favorites":
            self._send_json({"repos": load_favorites()})
        elif path == "/api/jenkins/config":
            cfg = load_jenkins()
            self._send_json({"configured": jenkins_configured(cfg),
                             "base": cfg["base"], "user": cfg["user"], "vpn": cfg["vpn"]})
        elif path == "/api/jenkins/jobs":
            self._handle_jenkins_jobs()
        elif path == "/api/jenkins/branches":
            self._handle_jenkins_branches()
        elif path == "/api/jenkins/build-status":
            self._handle_jenkins_build_status()
        elif path == "/api/settings":
            s = load_settings()
            g = load_github()
            jk = load_jenkins()
            self._send_json({
                "settings": s,
                "github": {"configured": bool(g["token"]), "user": g["user"]},
                "ghCli": bool(shutil.which("gh")),
                "jenkins": {"configured": jenkins_configured(jk),
                            "base": jk["base"], "user": jk["user"], "vpn": jk["vpn"]},
            })
        elif path == "/api/certs":
            self._handle_certs_list()
        elif path == "/api/certs/connect":
            self._handle_certs_connect()
        else:
            self._serve_static(path)

    def _handle_certs_list(self):
        q = parse_qs(urlparse(self.path).query)
        kind = q.get("kind", [""])[0]
        if kind not in ("kafka", "mongo"):
            self._send_json({"ok": False, "error": "kind must be kafka or mongo"}, 400)
            return
        root = _certs_root(kind)
        self._send_json({"ok": True, "kind": kind, "root": root,
                         "searchHome": bool(load_settings().get("certs_search_home")),
                         "exists": os.path.isdir(root),
                         "apps": certs_apps(),
                         "bundles": scan_cert_bundles(kind)})

    def _handle_certs_connect(self):
        q = parse_qs(urlparse(self.path).query)
        kind = q.get("kind", [""])[0]
        path = q.get("path", [""])[0]
        if kind not in ("kafka", "mongo"):
            self._send_json({"ok": False, "error": "bad kind"}, 400)
            return
        if not path or not _cert_path_ok(kind, path) or not os.path.isdir(path):
            self._send_json({"ok": False, "error": "unknown cert folder"}, 400)
            return
        info = kafka_connect(path) if kind == "kafka" else mongo_connect(path)
        self._send_json({"ok": True, **info})

    def _handle_jenkins_jobs(self):
        cfg = load_jenkins()
        if not jenkins_configured(cfg):
            self._send_json({"ok": False, "error": "Jenkins login not set"}, 400)
            return
        refresh = parse_qs(urlparse(self.path).query).get("refresh", ["0"])[0] == "1"
        ok, msg = ensure_vpn(cfg)
        if not ok:
            self._send_json({"ok": False, "error": f"VPN {cfg['vpn']}: {msg}"}, 502)
            return
        try:
            repos = jenkins_jobs(cfg, refresh=refresh)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 502)
            return
        self._send_json({"ok": True, "repos": repos})

    def _handle_jenkins_branches(self):
        cfg = load_jenkins()
        if not jenkins_configured(cfg):
            self._send_json({"ok": False, "error": "Jenkins login not set"}, 400)
            return
        repo_url = parse_qs(urlparse(self.path).query).get("url", [""])[0]
        if not repo_url:
            self._send_json({"ok": False, "error": "missing repo url"}, 400)
            return
        ok, msg = ensure_vpn(cfg)
        if not ok:
            self._send_json({"ok": False, "error": f"VPN {cfg['vpn']}: {msg}"}, 502)
            return
        try:
            branches = jenkins_branches(cfg, repo_url)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 502)
            return
        self._send_json({"ok": True, "branches": branches})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/open":
            self._handle_open()
        elif path == "/api/vpn/disconnect":
            self._handle_disconnect()
        elif path == "/api/sync":
            self._handle_sync()
        elif path == "/api/favorites":
            self._handle_favorites()
        elif path == "/api/jenkins/auth":
            self._handle_jenkins_auth()
        elif path == "/api/jenkins/diagnose":
            self._handle_jenkins_diagnose()
        elif path == "/api/jenkins/trigger":
            self._handle_jenkins_trigger()
        elif path == "/api/settings":
            try:
                cfg = save_settings(self._read_json())
            except Exception as exc:
                # e.g. settings.json not writable — say so instead of returning
                # an empty response that just looks like "it didn't save".
                self._send_json({"ok": False,
                                 "error": "could not write %s: %s" % (SETTINGS_PATH, exc)}, 500)
                return
            self._send_json({"ok": True, "settings": cfg})
        elif path == "/api/github/auth":
            data = self._read_json()
            if not data.get("token"):
                self._send_json({"ok": False, "error": "token required"}, 400)
                return
            g = save_github(data)
            self._send_json({"ok": True, "configured": bool(g["token"]), "user": g["user"]})
        elif path == "/api/certs/open":
            self._handle_certs_open()
        elif path == "/api/certs/compass":
            self._handle_certs_compass()
        elif path == "/api/certs/launch":
            self._handle_certs_launch()
        elif path == "/api/certs/kafka-add":
            self._handle_kafka_add()
        elif path == "/api/pick-folder":
            self._handle_pick_folder()
        else:
            self.send_error(404, "Not found")

    def _handle_pick_folder(self):
        title = (self._read_json().get("title") or "Choose a folder")[:120]
        path = pick_directory(title)
        if path is None:
            self._send_json({"ok": False, "unavailable": True,
                             "error": "no folder picker found (install zenity) — type the path instead"})
            return
        if not path:
            self._send_json({"ok": False, "cancelled": True})
            return
        self._send_json({"ok": True, "path": path, "isdir": os.path.isdir(path)})

    def _handle_certs_launch(self):
        data = self._read_json()
        app = data.get("app")
        if app == "offsetexplorer":
            exe = offsetexplorer_gui()
            if not exe:
                self._send_json({"ok": False, "error": "Offset Explorer not found"}, 404)
                return
            ok, msg = _spawn([exe])
            self._send_json({"ok": ok, "error": None if ok else msg})
            return
        if app == "compass":
            exe = compass_bin()
            if not exe:
                self._send_json({"ok": False, "error": "MongoDB Compass not found"}, 404)
                return
            argv = [exe]
            path = (data.get("path") or "").strip()
            if path and _cert_path_ok("mongo", path) and os.path.isdir(path):
                argv.append(mongo_connect(path)["uri"])
            ok, msg = _spawn(argv)
            self._send_json({"ok": ok, "error": None if ok else msg})
            return
        self._send_json({"ok": False, "error": "unknown app"}, 400)

    def _handle_kafka_add(self):
        data = self._read_json()
        path = (data.get("path") or "").strip()
        if not path or not _cert_path_ok("kafka", path) or not os.path.isdir(path):
            self._send_json({"ok": False, "error": "unknown cert folder"}, 400)
            return
        cli = offsetexplorer_cli()
        if not cli:
            self._send_json({"ok": False,
                             "error": "Offset Explorer command-line tool not found"}, 404)
            return
        info = kafka_connect(path)
        if not info["bootstrap"]:
            self._send_json({"ok": False,
                             "error": "no bootstrap server in the connection details"}, 400)
            return
        # The official CLI adds + saves the cluster into Offset Explorer (it handles
        # its own password storage), so we don't touch connections.xml. Args are a
        # list (no shell), so passwords never hit a shell history.
        args = [cli, "-addServerConnection", "-name", info["cluster"],
                "-bootstrapServers", info["bootstrap"], "-security", "ssl",
                "-testConnection", "false"]
        ts, ks = info["truststore"], info["keystore"]
        if ts["path"]:
            args += ["-truststoreLocation", ts["path"]]
            if ts["password"]:
                args += ["-truststorePassword", ts["password"]]
        if ks["path"]:
            args += ["-keystoreLocation", ks["path"]]
            if ks["password"]:
                args += ["-keystorePassword", ks["password"],
                         "-keystorePrivateKey", ks["password"]]
        # Offset Explorer rewrites connections.xml when it closes, so a save made
        # while it's open is wiped. If it's running, close it (flushing its own
        # connections to disk first), add via the CLI, then relaunch it so the new
        # cluster is loaded. The CLI exits 0 even on failure and prints the reason
        # to stdout, so success is judged by whether the cluster actually landed in
        # the saved file (we read only connection names there, never the secrets).
        cluster = info["cluster"]
        was_running = offsetexplorer_running()
        closed = stop_offsetexplorer() if was_running else False
        if was_running and not closed:
            self._send_json({
                "ok": False, "cluster": cluster, "duplicate": False,
                "wasRunning": True, "closed": False, "reopened": False,
                "error": "could not close Offset Explorer to save; close it manually and try again",
            })
            return
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=120)
            out = ((r.stdout or "") + (r.stderr or "")).strip()
        except Exception as exc:
            out = ""
            r = None
            err = str(exc)
        else:
            err = None
        low = out.lower()
        dup = "already exist" in low
        # The authoritative check: is the cluster now in the saved connections file?
        saved, connfile = cluster_in_connections(cluster)
        ok = err is None and (saved or dup)
        reopened = False
        if closed:                       # put OE back exactly as we found it
            rok, _ = _spawn([offsetexplorer_gui()])
            reopened = rok
        if ok:
            err_msg = None
        elif err:
            err_msg = err
        elif out:
            # CLI ran but the cluster didn't land in the file we checked — name the
            # file so a mismatched Offset Explorer data dir is obvious on any machine.
            err_msg = out[-300:] + "  [checked " + connfile + "]"
        else:
            err_msg = "cluster was not written to " + connfile
        self._send_json({
            "ok": ok, "cluster": cluster, "duplicate": dup,
            "wasRunning": was_running, "closed": closed, "reopened": reopened,
            "connectionsFile": connfile,
            "output": out[-500:],
            "error": err_msg,
        })

    def _handle_certs_open(self):
        data = self._read_json()
        kind = data.get("kind")
        path = (data.get("path") or "").strip()
        if kind not in ("kafka", "mongo") or not path or not _cert_path_ok(kind, path):
            self._send_json({"ok": False, "error": "unknown cert folder"}, 400)
            return
        if not os.path.isdir(path):
            self._send_json({"ok": False, "error": "folder not found"}, 404)
            return
        if not shutil.which("xdg-open"):
            self._send_json({"ok": False, "error": "xdg-open not available"}, 500)
            return
        ok, msg = _spawn(["xdg-open", path])
        self._send_json({"ok": ok, "error": None if ok else msg})

    def _handle_certs_compass(self):
        data = self._read_json()
        path = (data.get("path") or "").strip()
        if not path or not _cert_path_ok("mongo", path) or not os.path.isdir(path):
            self._send_json({"ok": False, "error": "unknown cert folder"}, 400)
            return
        try:
            out = write_compass_import(path)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)
            return
        self._send_json({"ok": True, "file": out})

    def _handle_jenkins_auth(self):
        data = self._read_json()
        if not (data.get("user") and data.get("token")):
            self._send_json({"ok": False, "error": "user and token required"}, 400)
            return
        cfg = save_jenkins(data)
        self._send_json({"ok": True, "configured": jenkins_configured(cfg),
                         "base": cfg["base"], "user": cfg["user"]})

    def _handle_jenkins_diagnose(self):
        cfg = load_jenkins()
        if not jenkins_configured(cfg):
            self._send_json({"ok": False, "error": "Jenkins login not set"}, 400)
            return
        data = self._read_json()
        target = (data.get("url") or data.get("branchUrl") or "").strip()
        if not target:
            self._send_json({"ok": False, "error": "no build or branch URL given"}, 400)
            return
        try:
            build_url = resolve_build_url(target, str(data.get("build") or ""))
        except (ValueError, TypeError) as exc:
            self._send_json({"ok": False, "error": f"bad build selector: {exc}"}, 400)
            return
        ok, msg = ensure_vpn(cfg)
        if not ok:
            self._send_json({"ok": False, "error": f"VPN {cfg['vpn']}: {msg}",
                             "url": build_url}, 502)
            return
        try:
            result = jenkins_diagnose(cfg, build_url)
        except urllib.error.HTTPError as exc:
            msg = {401: "authentication failed — check Jenkins user/token",
                   403: "access denied for this user",
                   404: "build not found — check repo / branch / number"}.get(
                       exc.code, f"Jenkins returned HTTP {exc.code}")
            self._send_json({"ok": False, "error": msg, "url": build_url})
            return
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc), "url": build_url})
            return
        result["requestUrl"] = build_url
        self._send_json(result)

    @staticmethod
    def _jenkins_http_msg(code):
        return {401: "authentication failed — check Jenkins user/token",
                403: "access denied for this user",
                404: "job not found — check repo / branch",
                409: "job can't be built right now (disabled or queue conflict)"}.get(
                    code, f"Jenkins returned HTTP {code}")

    def _handle_jenkins_trigger(self):
        cfg = load_jenkins()
        if not jenkins_configured(cfg):
            self._send_json({"ok": False, "error": "Jenkins login not set"}, 400)
            return
        data = self._read_json()
        job_url = (data.get("branchUrl") or data.get("url") or "").strip()
        if not job_url:
            self._send_json({"ok": False, "error": "pick a repo and branch to build"}, 400)
            return
        ok, msg = ensure_vpn(cfg)
        if not ok:
            self._send_json({"ok": False, "error": f"VPN {cfg['vpn']}: {msg}"}, 502)
            return
        try:
            res = jenkins_trigger(cfg, job_url)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
            return
        except urllib.error.HTTPError as exc:
            self._send_json({"ok": False, "error": self._jenkins_trigger_error(exc)}, 502)
            return
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 502)
            return
        self._send_json({"ok": True, **res})

    @staticmethod
    def _jenkins_trigger_error(exc):
        """Explain a failed build POST — tell a CSRF-crumb 403 from a real permission one."""
        body = reason = ""
        try:
            body = (exc.read() or b"").decode("utf-8", "replace")
        except Exception:
            pass
        try:
            reason = exc.headers.get("X-Error") or ""
        except Exception:
            pass
        blob = (reason + " " + body).lower()
        if exc.code == 403 and "crumb" in blob:
            return ("Jenkins rejected the CSRF crumb. Generate an API token at "
                    "/me/configure and use it (not your password) in Settings → Jenkins.")
        if exc.code == 403:
            return ("access denied — your Jenkins user can't start this build "
                    "(needs Job/Build permission)" + (f": {reason}" if reason else ""))
        msg = Handler._jenkins_http_msg(exc.code)
        return msg + (f" ({reason})" if reason else "")

    def _handle_jenkins_build_status(self):
        cfg = load_jenkins()
        if not jenkins_configured(cfg):
            self._send_json({"ok": False, "error": "Jenkins login not set"}, 400)
            return
        q = parse_qs(urlparse(self.path).query)
        queue_url = q.get("queue", [""])[0]
        build_url = q.get("build", [""])[0]
        if not (queue_url or build_url):
            self._send_json({"ok": False, "error": "missing queue or build url"}, 400)
            return
        ok, msg = ensure_vpn(cfg)
        if not ok:
            self._send_json({"ok": False, "error": f"VPN {cfg['vpn']}: {msg}"}, 502)
            return
        try:
            res = jenkins_build_status(cfg, queue_url or None, build_url or None)
        except urllib.error.HTTPError as exc:
            self._send_json({"ok": False, "error": self._jenkins_http_msg(exc.code)}, 502)
            return
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 502)
            return
        self._send_json(res)

    def _handle_favorites(self):
        data = self._read_json()
        repos = data.get("repos")
        if not isinstance(repos, list):
            self._send_json({"ok": False, "error": "repos must be a list"}, 400)
            return
        with _lock:
            saved = save_favorites(repos)
        self._send_json({"ok": True, "repos": saved})

    def _handle_open(self):
        data = self._read_json()
        url = (data.get("url") or "").strip()
        vpn = data.get("vpn")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            self._send_json({"ok": False, "error": "invalid url"}, 400)
            return
        if vpn is not None and vpn not in known_vpn_names():
            self._send_json({"ok": False, "error": f"unknown VPN: {vpn}"}, 400)
            return
        with _lock:
            vpn_connected = None
            vpn_msg = ""
            if vpn:
                ok, vpn_msg = vpn_up(vpn)
                vpn_connected = ok
                if not ok:
                    self._send_json({"ok": False, "vpnConnected": False,
                                     "error": f"VPN '{vpn}': {vpn_msg}"}, 502)
                    return
            ok, msg = open_url(url)
        self._send_json({"ok": ok, "vpnConnected": vpn_connected,
                         "vpnMessage": vpn_msg, "error": None if ok else msg})

    def _handle_disconnect(self):
        data = self._read_json()
        vpn = data.get("vpn")
        if not vpn or vpn not in known_vpn_names():
            self._send_json({"ok": False, "error": "unknown VPN"}, 400)
            return
        with _lock:
            ok, msg = vpn_down(vpn)
        self._send_json({"ok": ok, "error": None if ok else msg})

    def _handle_sync(self):
        with _lock:
            try:
                r = subprocess.run([sys.executable, SYNC_SCRIPT],
                                   capture_output=True, text=True, timeout=180)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, 500)
                return
        self._send_json({"ok": r.returncode == 0,
                         "output": (r.stdout + r.stderr)[-4000:]})


def main():
    port = DEFAULT_PORT
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"RCX Env Launcher serving at http://127.0.0.1:{port}  (Chrome: {CHROME})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        httpd.shutdown()


if __name__ == "__main__":
    main()
