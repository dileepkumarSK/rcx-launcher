#!/usr/bin/env python3
"""Sync the rcx-devops wiki into config.json for the RCX Env Launcher.

Clones (or pulls) the GitHub wiki, parses the per-environment URL tables, keeps
only the link types we care about (Kibana / Kibana-compq / NiFi / NiFi-registry /
Grafana), assigns each environment its NetworkManager VPN name, and writes
config.json next to this script.

Re-runnable: safe to run any time to refresh the links.
Requires: git with access to the wiki (uses the machine's existing gh/git creds),
and nmcli (to learn the real VPN connection names).
"""

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

WIKI_URL = "https://github.com/reactorcx/rcx-devops.wiki.git"
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.expanduser("~/.cache/rcx-launcher")
# Allow pointing at an already-cloned wiki (used during development/testing).
WIKI_DIR = os.environ.get("RCX_WIKI_DIR", os.path.join(CACHE_DIR, "wiki"))
CONFIG_PATH = os.path.join(HERE, "config.json")
VPN_MAP_PATH = os.path.join(HERE, "vpn_map.json")
SETTINGS_PATH = os.path.join(HERE, "settings.json")
GITHUB_PATH = os.path.join(HERE, "github.json")

# The link types we surface, in display order.
LINK_ORDER = {"kibana": 0, "kibana-compq": 1, "ui": 2, "nifi": 3, "nifi-registry": 4, "grafana": 5}

# Prod page section id -> (client display name). From All-Prod-URLs.md anchors.
PROD_ID_TO_CLIENT = {
    "seiprod": "SEI",
    "wuprod": "WU",
    "mgmprod2": "MGM",
    "spwyprod": "SPWY",
    "bpprod": "BP",
    "7gt-prod1": "7GT",
    "taprod": "TA",
    "gapprod": "GAP",
}

# Rank for ordering environments within a client.
TIER_RANK = {"LOWER": 0, "PERF": 1, "PERF2": 2, "PROD": 3}

# Tokens a VPN connection name may use for each tier. VPN names are chosen by each
# person in NetworkManager, so the same lower env is "SEI LOWER" on one machine and
# "SEI LLE" (lower-level environment) on another — list every spelling we've seen so
# a colleague's naming still matches. Matched case/separator-insensitively (see norm).
TIER_TOKENS = {
    "LOWER": ("LOWER", "LLE", "LWR", "LOW"),
    "PERF": ("PERF", "PRF"),
    "PERF2": ("PERF2", "PRF2"),
    "PROD": ("PROD", "PRD"),
}

# Backends whose rows belong to a different client than their name implies.
# e.g. RCX-SEIInt (Sandbox/Staging, org ids 7in/sgthi) are actually 7GT.
BACKEND_CLIENT_OVERRIDE = {
    "RCX-SEIInt": ("7GT", "LOWER"),
}


GITHUB_ORG = "reactorcx"

# Defaults — overridable (without editing code) via settings.json, so a shared
# copy of this tool can point at a different org/wiki.
DEFAULT_SETTINGS = {
    "github_org": GITHUB_ORG,
    "wiki_url": WIKI_URL,
    "jenkins_host_suffix": "devops.lmvi.net",
}


def load_settings():
    cfg = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            cfg.update({k: v for k, v in json.load(f).items() if v})
    except (FileNotFoundError, ValueError):
        pass
    return cfg


def load_github():
    try:
        with open(GITHUB_PATH, encoding="utf-8") as f:
            g = json.load(f)
    except (FileNotFoundError, ValueError):
        g = {}
    return {"user": (g.get("user") or "").strip(), "token": (g.get("token") or "").strip()}


def git_auth_args():
    """Extra `git -c` args that authenticate with the stored GitHub token, if any."""
    g = load_github()
    if not g["token"]:
        return []
    user = g["user"] or "x-access-token"
    basic = base64.b64encode(f"{user}:{g['token']}".encode()).decode()
    return ["-c", "http.extraheader=Authorization: Basic " + basic]


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _repos_via_api(org, gh):
    """List org repos through the GitHub REST API using the stored token.

    Works without the `gh` CLI, so a shared copy only needs a token. Returns the
    repo list, or None on failure (so the caller keeps the previous list)."""
    repos, page = [], 1
    while page <= 20:
        url = (f"https://api.github.com/orgs/{org}/repos"
               f"?per_page=100&type=all&sort=full_name&page={page}")
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + gh["token"],
            "Accept": "application/vnd.github+json",
            "User-Agent": "rcx-launcher",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                batch = json.load(resp)
        except Exception as exc:
            print(f"WARNING: GitHub API repo list failed ({exc}); keeping existing repos.")
            return None
        if not batch:
            break
        for repo in batch:
            if repo.get("archived"):
                continue
            repos.append({
                "name": repo.get("name", ""),
                "description": (repo.get("description") or "").strip(),
                "url": repo.get("html_url", ""),
                "pushedAt": repo.get("pushed_at", ""),
            })
        if len(batch) < 100:
            break
        page += 1
    repos.sort(key=lambda x: x["name"].lower())
    print(f"Fetched {len(repos)} GitHub repos from {org} (API).")
    return repos


def _repos_via_gh(org):
    """List org repos through the `gh` CLI (reuses the machine's gh auth)."""
    if not shutil.which("gh"):
        print("WARNING: no GitHub token set and `gh` not found; skipping repo list.")
        return None
    r = run(["gh", "repo", "list", org, "--limit", "500",
             "--json", "name,description,url,isArchived,pushedAt"])
    if r.returncode != 0:
        print("WARNING: `gh repo list` failed; keeping existing repos.\n  "
              + (r.stderr or r.stdout).strip())
        return None
    try:
        data = json.loads(r.stdout)
    except Exception as exc:
        print(f"WARNING: could not parse gh output ({exc}); keeping existing repos.")
        return None
    repos = [{
        "name": repo.get("name", ""),
        "description": (repo.get("description") or "").strip(),
        "url": repo.get("url", ""),
        "pushedAt": repo.get("pushedAt", ""),
    } for repo in data if not repo.get("isArchived")]
    repos.sort(key=lambda x: x["name"].lower())
    print(f"Fetched {len(repos)} GitHub repos from {org} (gh).")
    return repos


def fetch_repos():
    """List the org's non-archived GitHub repos. Prefers the stored token (via
    the API — no `gh` needed), falls back to the `gh` CLI. Returns None on
    failure so a transient hiccup won't wipe the Repos view."""
    gh = load_github()
    if gh["token"]:
        return _repos_via_api(GITHUB_ORG, gh)
    return _repos_via_gh(GITHUB_ORG)


def ensure_wiki():
    """Clone the wiki if missing, otherwise pull latest."""
    if os.environ.get("RCX_WIKI_DIR"):
        if not os.path.isdir(WIKI_DIR):
            sys.exit(f"RCX_WIKI_DIR set but not found: {WIKI_DIR}")
        return
    os.makedirs(CACHE_DIR, exist_ok=True)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    auth = git_auth_args()
    if os.path.isdir(os.path.join(WIKI_DIR, ".git")):
        print(f"Updating wiki in {WIKI_DIR} ...")
        r = run(["git"] + auth + ["-C", WIKI_DIR, "pull", "--ff-only"], env=env)
        if r.returncode != 0:
            print("  (pull failed, continuing with cached copy)\n  " + r.stderr.strip())
    else:
        print(f"Cloning wiki into {WIKI_DIR} ...")
        r = run(["git"] + auth + ["clone", "--depth", "1", WIKI_URL, WIKI_DIR], env=env)
        if r.returncode != 0:
            sys.exit("git clone failed:\n" + r.stderr)


def list_vpns():
    """Return the list of NetworkManager VPN connection names."""
    r = run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"])
    vpns = []
    for line in r.stdout.splitlines():
        # NAME may contain spaces; TYPE is the final colon-delimited field.
        if ":" not in line:
            continue
        name, _, ctype = line.rpartition(":")
        if ctype == "vpn":
            vpns.append(name)
    return vpns


def norm(s):
    return re.sub(r"[^A-Z0-9]", "", s.upper())


# Tier priority for reading a tier out of a VPN name (most specific first, so
# "SEI PERF2" reads as PERF2 not PERF, and a bare "... LLE" reads as LOWER).
_TIER_READ_ORDER = ("PERF2", "PROD", "PERF", "LOWER")


def tier_in_vpn(name):
    """Best-effort: which tier does this VPN connection name look like? or None."""
    n = norm(name)
    for tier in _TIER_READ_ORDER:
        if any(tok in n for tok in TIER_TOKENS[tier]):
            return tier
    return None


def is_ui(name, url):
    """True for web UI / portal rows (main app UI, FeedX UI, membercare portal…)."""
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    if "github.com" in host:  # repo links, not an env UI
        return False
    n = name.strip().lower()
    UI_NAMES = {
        "ui", "ui-sso", "fxui", "fx-ui-sso", "fxui-sso",
        "mcp", "mcp-sso", "aimcp", "aimcp-sso", "mssp", "mssp-sso",
        "crud-ui-sso", "rc-ui-sso",
    }
    if n in UI_NAMES:
        return True
    if re.search(r"(^|[-_])ui(-sso)?$", n):       # anything ending in …-ui / …-ui-sso
        return True
    if "membercare" in n or "portal" in n or "self-service" in n or "selfservice" in n:
        return True
    # URL fallback: rcx-ui.* / rcx-fxui.* / rcx-mcp.* / *.ui.lmvi.net
    if re.search(r"^(rcx-)?(ui|fxui|mcp|aimcp|mssp)(-sso)?\.", host) or ".ui." in host:
        return True
    return False


def classify(name, url):
    """Return the link type for a (service name, url) row, or None to ignore."""
    n = name.lower()
    u = url.lower()
    if "grafana" in n or "grafana" in u:
        return "grafana"
    if "nifi" in n or "nifi" in u:
        if "registr" in n or "registr" in u:
            return "nifi-registry"
        return "nifi"
    if is_ui(name, url):
        return "ui"
    # Kibana / OpenSearch dashboards
    is_kibana = (
        "kibana" in n or "kibana" in u
        or "opensearch" in n or "open serach" in n  # sic (typo in wiki)
        or "_dashboard" in u or "/kibana" in u
    )
    if is_kibana:
        if "kibana-app" in n or "kibana-app" in u or "compq" in n or "comp-index" in n or "comp index" in n or "app-es" in n or n.strip() == "app-es":
            return "kibana-compq"
        return "kibana"
    return None


def is_retired_nifi(name, url):
    """True for old/retired NiFi clusters we should skip (keep current ones)."""
    n = name.lower()
    if "old" in n or "retired" in n or "deprecated" in n:
        return True
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    if "nifi-1" in host:  # the retired 'rcx-nifi-1' naming (not 'rcx-nifi1:8001')
        return True
    return False


def clean_label(name):
    # Drop trailing notes after a comma ("Kibana-compq-index, NOTE: ...").
    label = name.split(",")[0].strip()
    label = re.sub(r"\s+", " ", label)
    return label or "link"


def find_url(cells):
    for c in cells:
        m = re.search(r"https?://[^\s|)]+", c)
        if m:
            return m.group(0).rstrip("/|")
    return None


def parse_table_rows(text):
    """Yield (name, url) from every markdown table row `| name | url |`."""
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 2:
            continue
        name = cells[0]
        if not name or name.lower() in ("servicename", "service name", "username", "backend"):
            continue
        if set(name) <= set("-: "):  # separator row
            continue
        url = find_url(cells[1:])
        if url:
            yield name, url


def grafana_freetext(text):
    """Grafana URLs that appear outside tables (e.g. 'For Grafana ... https://...')."""
    for m in re.finditer(r"https?://grafana[^\s|)]+", text):
        yield "Grafana", m.group(0).rstrip("/|")


def extract_links(text):
    """Return an ordered, de-duplicated list of link dicts from a page/section."""
    links = []
    seen = set()

    def add(name, url):
        t = classify(name, url)
        if not t:
            return
        if t in ("nifi", "nifi-registry") and is_retired_nifi(name, url):
            return  # skip old/retired NiFi clusters
        key = url.rstrip("/")
        if key in seen:
            return
        seen.add(key)
        links.append({"label": clean_label(name), "type": t, "url": url})

    for name, url in parse_table_rows(text):
        add(name, url)
    for name, url in grafana_freetext(text):
        add(name, url)

    links.sort(key=lambda l: LINK_ORDER.get(l["type"], 99))
    return links


# ---- Common-Access-Sheet: client + env -> wiki page ----

def parse_access_sheet(path):
    """Yield (client_display, env_label, page_name, tier_token) for each lower env."""
    rows = []
    with open(path, encoding="utf-8") as f:
        text = f.read()
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|RCX-") and not s.startswith("| RCX-"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 2:
            continue
        backend = cells[0]
        m = re.search(r"\[([^\]]+)\]\(([^)]+)\)", cells[1])
        if not m:
            continue
        env_label = m.group(1).strip()
        page = urllib.parse.unquote(m.group(2).rsplit("/", 1)[-1])
        client, tier = client_and_tier(backend, env_label)
        rows.append((client, env_label, page, tier))
    return rows


def client_and_tier(backend, env_label):
    if backend in BACKEND_CLIENT_OVERRIDE:
        client, tier = BACKEND_CLIENT_OVERRIDE[backend]
        if "perf" in env_label.lower():
            tier = "PERF"
        return client, tier
    base = backend.replace("RCX-", "").strip()
    tier = "LOWER"
    if base.endswith("Perf2"):
        base, tier = base[:-5], "PERF2"
    elif base.endswith("Perf"):
        base, tier = base[:-4], "PERF"
    elif base.endswith("Int"):
        base = base[:-3]  # SEIInt -> SEI, still a lower-tier VPN
    # Env label can also signal perf.
    if "perf" in env_label.lower() and tier == "LOWER":
        tier = "PERF"
    return base, tier


def read_page(page):
    for candidate in (page, page.replace(" ", "-"), page.replace("-", "_")):
        p = os.path.join(WIKI_DIR, candidate + ".md")
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                return f.read()
    return None


# ---- All-Prod-URLs: split into per-client sections ----

def parse_prod(path):
    """Yield (client_display, 'Prod', links) for each prod client section."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    # Sections begin with a header line containing <a id="xxx">.
    parts = re.split(r'^.*<a id="([^"]+)"[^>]*>.*$', text, flags=re.MULTILINE)
    # re.split with one capture group -> [pre, id1, body1, id2, body2, ...]
    out = []
    for i in range(1, len(parts) - 1, 2):
        sect_id = parts[i].strip().lower()
        body = parts[i + 1]
        client = PROD_ID_TO_CLIENT.get(sect_id)
        if not client:
            continue
        out.append((client, "Prod", extract_links(body)))
    return out


# ---- VPN assignment ----

def assign_vpn(client, tier, vpns, vpn_map):
    """Pick the nmcli VPN for a (client, tier). The client name is the anchor:

    - one VPN for the client  -> use it for every env, whatever its tier spelling
      (so a colleague's "TA" / "TA-LLE" style naming never drops an env);
    - several VPNs for the client -> still route each env to its own tier, so a
      prod/perf env doesn't open on the lower-env VPN (different tunnel).
    """
    override = vpn_map.get(f"{client}:{tier}")
    if override is not None:
        return override or None  # "" in map means intentionally no VPN
    cnorm = norm(client)
    owned = [v for v in vpns if norm(v).startswith(cnorm)]
    if not owned:
        return None
    if len(owned) == 1:
        return owned[0]  # client name alone decides it

    # Client has several VPNs -> match the tier within them.
    tokens = TIER_TOKENS.get(tier, (norm(tier),))
    tiered = [v for v in owned if any(t in norm(v) for t in tokens)]
    if not tiered:
        tiered = [v for v in owned if tier_in_vpn(v) == tier]
    if tiered:
        return sorted(tiered, key=len)[0]
    # Requested tier isn't spelled out on any of this client's VPNs. For a lower
    # env, fall back to a VPN that reads as lower (or carries no tier); never route
    # a prod/perf env onto another tunnel -> leave it unmatched (report flags it).
    if tier == "LOWER":
        plain = [v for v in owned if tier_in_vpn(v) in (None, "LOWER")]
        if plain:
            return sorted(plain, key=len)[0]
    return None


def tier_of_env(env_name, source_tier):
    if source_tier:
        return source_tier
    e = env_name.lower()
    if "perf" in e:
        return "PERF"
    if "prod" in e:
        return "PROD"
    return "LOWER"


def _common_prefix_len(a, b):
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def vpn_report(clients, vpns, vpn_map):
    """Read the mapping from the VPN side and print what matched / what didn't.

    Each person names their nmcli VPN connections themselves, so a wiki client or
    tier can silently fail to match a colleague's naming. This surfaces both sides:
    which VPNs went unused, and which client/tier combos found no VPN -- with a
    ready-to-paste vpn_map.json line to fix each one.
    """
    # What sync wanted, and what it resolved to.
    used = {}        # vpn name -> list of "Client/TIER" that use it
    dropped = []     # (client, tier) that resolved to no VPN
    for client in sorted(clients, key=str.upper):
        for env in clients[client].values():
            tier = env["tier"]
            if env["vpn"]:
                used.setdefault(env["vpn"], []).append(f"{client}/{tier}")
            else:
                if (client, tier) not in dropped:
                    dropped.append((client, tier))

    print("\n--- VPN mapping report (read from nmcli) ---")
    print(f"{len(vpns)} VPN connection(s) on this machine.")

    unused = [v for v in vpns if v not in used]
    if unused:
        print(f"\n{len(unused)} VPN(s) not matched to any environment:")
        for v in sorted(unused, key=str.upper):
            t = tier_in_vpn(v)
            print(f"  • {v}" + (f"   (looks like tier {t})" if t else ""))

    if dropped:
        print(f"\n{len(dropped)} client/tier(s) found NO VPN (their environments are dropped):")
        for client, tier in sorted(dropped):
            cnorm = norm(client)
            toks = TIER_TOKENS.get(tier, (norm(tier),))
            # Rank unused VPNs as likely matches: same tier first, then closest
            # client-name prefix. This is what a colleague would pick as the fix.
            def score(v):
                vn = norm(v)
                same_tier = tier_in_vpn(v) == tier
                return (same_tier, _common_prefix_len(vn, cnorm))
            guesses = [v for v in sorted(unused, key=score, reverse=True)
                       if score(v)[0] or score(v)[1] > 0][:3]
            print(f"  • {client}/{tier}: no match")
            if guesses:
                best = guesses[0]
                print(f"      maybe one of: {', '.join(guesses)}")
                print(f'      to force it, add to vpn_map.json:  "{client}:{tier}": "{best}"')
            else:
                print(f"      (no VPN on this machine looks like {client} {tier})")
    else:
        print("\nEvery wanted client/tier matched a VPN. ✓")
    print("--- end VPN report ---")


def main():
    global WIKI_URL, GITHUB_ORG
    settings = load_settings()
    WIKI_URL = settings["wiki_url"]
    GITHUB_ORG = settings["github_org"]
    ensure_wiki()
    vpns = list_vpns()
    print(f"Found {len(vpns)} VPN connections.")
    vpn_map = {}
    if os.path.isfile(VPN_MAP_PATH):
        with open(VPN_MAP_PATH, encoding="utf-8") as f:
            vpn_map = json.load(f)

    # client display name -> {env_name: {vpn, links, tier}}
    clients = {}

    def add_env(client, env_name, tier, links):
        if not links:
            return
        vpn = assign_vpn(client, tier, vpns, vpn_map)
        # Envs with no VPN are dropped later; add '<client>:<tier>' to
        # vpn_map.json to force a VPN if one should apply.
        envs = clients.setdefault(client, {})
        # Merge if the env already exists (dedupe by url).
        entry = envs.setdefault(env_name, {"vpn": vpn, "tier": tier, "links": [], "_urls": set()})
        if entry["vpn"] is None:
            entry["vpn"] = vpn
        for l in links:
            if l["url"].rstrip("/") not in entry["_urls"]:
                entry["_urls"].add(l["url"].rstrip("/"))
                entry["links"].append(l)

    # Prod
    prod_path = os.path.join(WIKI_DIR, "All-Prod-URLs.md")
    if os.path.isfile(prod_path):
        for client, env_name, links in parse_prod(prod_path):
            add_env(client, env_name, "PROD", links)
    else:
        print("WARNING: All-Prod-URLs.md not found")

    # Lower envs via the access sheet
    sheet_path = os.path.join(WIKI_DIR, "Common-Access-Sheet.md")
    if os.path.isfile(sheet_path):
        for client, env_label, page, tier in parse_access_sheet(sheet_path):
            text = read_page(page)
            if text is None:
                print(f"  [missing page] {client}/{env_label}: {page}.md")
                continue
            # Keep the 2nd perf cluster as its own env so it maps to its own VPN.
            if tier == "PERF2" and not env_label.rstrip().endswith("2"):
                env_label = env_label + "2"
            add_env(client, env_label, tier_of_env(env_label, tier), extract_links(text))
    else:
        print("WARNING: Common-Access-Sheet.md not found")

    # Build output, sorted. Environments without a matching VPN can't be launched,
    # so they're kept out of the launchable list -- but recorded under "hidden" so
    # the UI can show "N hidden (no VPN matched)" instead of them vanishing silently.
    out_clients = []
    dropped = 0
    for client in sorted(clients, key=str.upper):
        envs = clients[client]
        env_list = []
        hidden = []
        for env_name in sorted(envs, key=lambda e: (TIER_RANK.get(envs[e]["tier"], 1), e.lower())):
            e = envs[env_name]
            if not e["vpn"]:
                dropped += 1
                hidden.append({"name": env_name, "tier": e["tier"], "reason": "no VPN matched"})
                continue
            env_list.append({"name": env_name, "vpn": e["vpn"], "links": e["links"]})
        if env_list:
            co = {"name": client, "environments": env_list}
            if hidden:
                co["hidden"] = hidden
            out_clients.append(co)

    repos = fetch_repos()
    if repos is None:  # fetch failed — preserve whatever the last config had
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                repos = json.load(f).get("repos", [])
        except Exception:
            repos = []

    config = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "clients": out_clients,
        "repos": repos,
    }
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    n_env = sum(len(c["environments"]) for c in out_clients)
    n_link = sum(len(e["links"]) for c in out_clients for e in c["environments"])
    print(f"\nWrote {CONFIG_PATH}")
    print(f"  {len(out_clients)} clients, {n_env} environments, {n_link} links "
          f"({dropped} env(s) dropped for having no VPN), {len(repos)} repos.")

    vpn_report(clients, vpns, vpn_map)


def vpns_only():
    """Read-only: just show the VPN names on this machine and how they parse.

    No wiki/credentials needed -- handy for a colleague to see what naming the
    tool sees, and to copy exact connection names into vpn_map.json.
    """
    vpns = list_vpns()
    print(f"{len(vpns)} VPN connection(s) from nmcli:\n")
    for v in sorted(vpns, key=str.upper):
        t = tier_in_vpn(v)
        print(f"  {v:<28} -> tier {t or '?'}")
    print("\nThe tool matches a VPN when its name starts with the client name and")
    print("contains a tier token (LOWER/LLE/LWR/LOW, PERF/PRF, PERF2, PROD/PRD).")
    print("If a name doesn't fit that, add an override in vpn_map.json, e.g.")
    print('  "7GT:LOWER": "SevenGT Lower"')


if __name__ == "__main__":
    if "--vpns" in sys.argv[1:]:
        vpns_only()
    else:
        main()
