#!/usr/bin/env python3
"""Build the rcx (rcf) environment list for the launcher from REAL sources, so it
works for everyone — not only people who happen to have the ~/.bashrc rcx aliases.

Two authoritative inputs, no bashrc:
  * GitHub wiki  -> Common-Access-Sheet.md "Backend | Tenant | Org id" table
                    gives every environment's RCX_BACKEND + RCX_TENANT.
  * AWS SSO      -> aws_accounts.json (pulled by the launcher) gives the real
                    AWS_PROFILE per client.

Each wiki row (backend, tenant) is matched to its AWS profile and written to
aws_envs.json as {name, client, profile, region, backend, tenant}, grouped
client-wise. rcf_dir / venv_activate / sso_session meta is preserved from the
existing aws_envs.json (or defaults) so launching rcx keeps working.

Re-runnable. Reads the wiki clone that sync_wiki.py maintains
(~/.cache/rcx-launcher/wiki, override with RCX_WIKI_DIR); run sync_wiki.py first
if it is missing. Reads ONLY the public backend/tenant table — no secrets.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.expanduser("~/.cache/rcx-launcher")
WIKI_DIR = os.environ.get("RCX_WIKI_DIR", os.path.join(CACHE_DIR, "wiki"))
ACCESS_SHEET = os.path.join(WIKI_DIR, "Common-Access-Sheet.md")
AWS_ENVS_PATH = os.path.join(HERE, "aws_envs.json")
AWS_ACCOUNTS_PATH = os.path.join(HERE, "aws_accounts.json")

# Wiki backend prefix -> AWS profile (client). The clean matches are by name;
# the handful that differ were confirmed with the owner:
#   RCX-SEIInt  lives in the 7gt DEV account
#   RCX-SEI / Dev7 / Demo / POC / Upgrade  all live in the SEI (RCX Development) account
BACKEND_PROFILE = {
    "RCX-Dev7": "sei",
    "RCX-SEI": "sei",
    "RCX-SEIPerf": "sei",
    "RCX-SEIPerf2": "sei",
    "RCX-POC": "sei",
    "RCX-Demo": "sei",
    "RCX-Upgrade": "sei",
    "RCX-SEIInt": "7gt",
    "RCX-WU": "wu",
    "RCX-WUPerf": "wu",
    "RCX-WUPerf-DR": "wu",
    "RCX-MGM": "mgm",
    "RCX-MGMPerf": "mgm",
    "RCX-SPWY": "spwy",
    "RCX-SPWYPerf": "spwy",
    "RCX-BP": "bp",
    "RCX-TA": "ta",
    "RCX-TAPerf": "ta",
    "RCX-GAP": "gap",
    "RCX-BCLC": "bclc",
    "RCX-SWA": "swa",
    "RCX-ATT": "att",
    "RCX-FB": "fontainebleau",
    "RCX-PCK": "peacock",
    "RCX-MI": "marriott",
    "RCX-MIPerf": "marriott",
    "RCX-Perf1": "internal-perf",
}

# Resource region per backend (AWS_REGION). SSO lives in us-west-2 for every
# profile, but a few clients' resources run elsewhere (from the known aliases).
REGION_BY_BACKEND = {
    "RCX-BP": "us-east-2",
    "RCX-TA": "us-east-2",
    "RCX-TAPerf": "us-east-2",
    "RCX-WU": "us-east-1",
    "RCX-WUPerf": "us-east-1",
    "RCX-WUPerf-DR": "us-east-1",
    "RCX-Dev7": "ap-southeast-1",
}
DEFAULT_REGION = "us-west-2"

DEFAULT_META = {
    "rcf_dir": "",
    "venv_activate": "venv/bin/activate",
    "sso_session": "login",
    "start_url": "",
}

# A markdown table row:  |RCX-Foo | [Tenant](url) | org |
ROW_RE = re.compile(r"^\|\s*(RCX-[A-Za-z0-9\-]+)\s*\|\s*\[([^\]]+)\]")


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def parse_access_sheet(path):
    """-> list of (backend, tenant) in document order."""
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = ROW_RE.match(line.strip())
            if m:
                rows.append((m.group(1).strip(), m.group(2).strip()))
    return rows


def load_meta():
    meta = dict(DEFAULT_META)
    try:
        with open(AWS_ENVS_PATH, encoding="utf-8") as f:
            old = json.load(f)
        for k in DEFAULT_META:
            if old.get(k):
                meta[k] = old[k]
    except (FileNotFoundError, ValueError):
        pass
    return meta


def known_profiles():
    try:
        with open(AWS_ACCOUNTS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        return set()
    return {p.get("profile") for p in data.get("profiles", [])}


def build_envs(rows, profiles):
    envs, unmatched, seen = [], [], set()
    for backend, tenant in rows:
        profile = BACKEND_PROFILE.get(backend)
        region = REGION_BY_BACKEND.get(backend, DEFAULT_REGION)
        # client = the profile we group under; fall back to the backend slug.
        client = profile or slug(backend.replace("RCX-", ""))
        name = "%s-%s" % (client, slug(tenant))
        if name in seen:
            name = "%s-%s-%s" % (client, slug(tenant), slug(backend.replace("RCX-", "")))
        seen.add(name)
        entry = {"name": name, "client": client,
                 "profile": profile or "", "region": region,
                 "backend": backend, "tenant": tenant}
        if not profile:
            entry["needsProfile"] = True
            unmatched.append(entry)
        elif profiles and profile not in profiles:
            # mapped to a profile that has not been pulled yet — keep it, but flag
            entry["profileMissing"] = True
        envs.append(entry)
    return envs, unmatched


def main():
    if not os.path.exists(ACCESS_SHEET):
        sys.stderr.write(
            "Common-Access-Sheet.md not found at %s\nRun sync_wiki.py first to "
            "clone/refresh the wiki.\n" % ACCESS_SHEET)
        return 2
    rows = parse_access_sheet(ACCESS_SHEET)
    profiles = known_profiles()
    envs, unmatched = build_envs(rows, profiles)
    meta = load_meta()

    # group client-wise (stable: first-seen client order), tenants within a client
    order, by_client = [], {}
    for e in envs:
        by_client.setdefault(e["client"], []).append(e)
        if e["client"] not in order:
            order.append(e["client"])
    grouped = [e for c in order for e in by_client[c]]

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "wiki+aws",
        "rcf_dir": meta["rcf_dir"],
        "venv_activate": meta["venv_activate"],
        "sso_session": meta["sso_session"],
        "start_url": meta["start_url"],
        "clients": order,
        "envs": grouped,
    }
    with open(AWS_ENVS_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
        f.write("\n")

    print("Wrote %d environments across %d clients -> %s"
          % (len(grouped), len(order), AWS_ENVS_PATH))
    print("Clients:", ", ".join(order))
    if unmatched:
        print("\n%d environment(s) have no AWS profile yet (shown without Open):"
              % len(unmatched))
        for e in unmatched:
            print("  %-14s %s" % (e["backend"], e["tenant"]))
    missing = [e for e in grouped if e.get("profileMissing")]
    if missing:
        print("\n%d env(s) map to a profile not yet pulled (press Refresh in @aws): %s"
              % (len(missing), ", ".join(sorted({e["profile"] for e in missing}))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
