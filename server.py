#!/usr/bin/env python3
"""Local backend for the RCX Env Launcher.

Serves the web UI and a small JSON API that can bring a NetworkManager VPN up
and open a URL in Chrome. Binds to localhost only; single user, no auth.

Run:  python3 server.py [--port 8766]
"""

import base64
import http.cookiejar
import json
import os
import re
import shutil
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
    for k in ("github_org", "wiki_url", "jenkins_host_suffix"):
        if data.get(k):
            cfg[k] = str(data[k]).strip()
    if data.get("browser") in ("chrome", "default"):
        cfg["browser"] = data["browser"]
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
    """Walk the folder tree, collecting multibranch projects (= repos)."""
    repos = []

    def walk(url, path, depth):
        if depth > 5:
            return
        try:
            data = jenkins_api(cfg, url, "jobs[name,url,_class]")
        except Exception:
            return
        for j in data.get("jobs", []):
            cls = j.get("_class", "").split(".")[-1]
            name, jurl = j.get("name"), j.get("url")
            if any(m in cls for m in _MULTI):
                repos.append({"repo": name, "folder": "/".join(path), "url": jurl})
            elif any(f in cls for f in _FOLDER):
                walk(jurl, path + [name], depth + 1)
            elif "WorkflowJob" in cls or "FreeStyle" in cls:
                repos.append({"repo": name, "folder": "/".join(path),
                              "url": jurl, "standalone": True})

    walk(cfg["base"], [], 0)
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


def jenkins_diagnose(cfg, build_url):
    info = jenkins_api(cfg, build_url,
                       "number,result,building,fullDisplayName,url,timestamp,duration")
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
        "stages": stages,
        "failedStages": failed,
        "reason": extract_failure(log),
        "log": log,
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

    info = jenkins_api(cfg, build_url,
                       "number,result,building,fullDisplayName,url")
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
        else:
            self._serve_static(path)

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
            cfg = save_settings(self._read_json())
            self._send_json({"ok": True, "settings": cfg})
        elif path == "/api/github/auth":
            data = self._read_json()
            if not data.get("token"):
                self._send_json({"ok": False, "error": "token required"}, 400)
                return
            g = save_github(data)
            self._send_json({"ok": True, "configured": bool(g["token"]), "user": g["user"]})
        else:
            self.send_error(404, "Not found")

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
