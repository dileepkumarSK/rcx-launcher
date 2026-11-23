#!/usr/bin/env python3
"""Seed the launcher's own AWS environment list (aws_envs.json) from the rcx env
recipes found in ~/.bashrc.

This is a ONE-TIME / refresh SEED step only: the bashrc aliases are used purely as a
reference for what each environment needs (AWS profile + region + RCX backend/tenant
+ the rcf project dir). At runtime the launcher reads ONLY aws_envs.json and builds
the `rcx` login/run command itself — it never shells out to the aliases.

We read ONLY the alias name and its AWS_*/RCX_* exports. Every other line in .bashrc
(which may hold unrelated secrets) is ignored and never copied anywhere.
"""
import datetime
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "aws_envs.json")
BASHRC = os.path.expanduser("~/.bashrc")
AWS_CONFIG = os.path.expanduser("~/.aws/config")


def _expand(path, work_dir):
    return path.replace("$WORK_DIR", work_dir).replace("${WORK_DIR}", work_dir)


def _sso_session_and_url():
    """The [sso-session NAME] + its start URL from ~/.aws/config (default 'login')."""
    name, url = "login", ""
    try:
        with open(AWS_CONFIG, encoding="utf-8") as f:
            txt = f.read()
        m = re.search(r"(?m)^\[sso-session\s+([^\]]+)\]", txt)
        if m:
            name = m.group(1).strip()
            block = txt[m.end():]
            block = block[:block.find("\n[") if "\n[" in block else len(block)]
            u = re.search(r"(?m)^\s*sso_start_url\s*=\s*(\S+)", block)
            if u:
                url = u.group(1).strip()
    except OSError:
        pass
    return name, url


def parse_bashrc():
    with open(BASHRC, encoding="utf-8", errors="replace") as f:
        text = f.read()

    wm = re.search(r"(?m)^\s*export\s+WORK_DIR=(\S+)", text)
    work_dir = wm.group(1) if wm else os.path.expanduser("~")

    # rcf project dir from:  alias rcf='source <dir>/venv/bin/activate'
    rcf_dir = ""
    rm = re.search(r"(?m)^\s*alias\s+rcf=(['\"])\s*source\s+(\S+?)/venv/bin/activate\1", text)
    if rm:
        rcf_dir = _expand(rm.group(2), work_dir)

    envs = []
    for ln in text.splitlines():
        m = re.match(r"\s*alias\s+([A-Za-z0-9_.-]+)=(['\"])(.*)\2\s*$", ln)
        if not m:
            continue
        name, body = m.group(1), m.group(3)
        # only rcx-launching env aliases (skip rcf, -vpn, docker helpers, etc.)
        if "AWS_PROFILE=" not in body or not re.search(r"(?:^|&|\s)rcx\b", body):
            continue

        def grab(key):
            mm = re.search(key + r"=([^\s&]+)", body)
            return mm.group(1).strip() if mm else ""

        profile = grab("AWS_PROFILE")
        if not profile:
            continue
        envs.append({
            "name": name,
            "client": name.split("-")[0],
            "profile": profile,
            "region": grab("AWS_REGION"),
            "backend": grab("RCX_BACKEND"),
            "tenant": grab("RCX_TENANT"),
        })

    # de-dupe by name (keep first), then sort by client then name
    seen, uniq = set(), []
    for e in envs:
        if e["name"] in seen:
            continue
        seen.add(e["name"])
        uniq.append(e)
    uniq.sort(key=lambda e: (e["client"].lower(), e["name"].lower()))
    return work_dir, rcf_dir, uniq


def main():
    work_dir, rcf_dir, envs = parse_bashrc()
    sess, url = _sso_session_and_url()
    data = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "rcf_dir": rcf_dir,
        "venv_activate": "venv/bin/activate",   # relative to rcf_dir
        "sso_session": sess,
        "start_url": url,
        "envs": envs,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print("wrote %s" % OUT)
    print("  rcf_dir     : %s" % (rcf_dir or "(not found)"))
    print("  sso_session : %s   start_url: %s" % (sess, url or "(none)"))
    print("  %d environments:" % len(envs))
    for e in envs:
        print("    %-12s profile=%-10s region=%-12s backend=%-16s tenant=%s"
              % (e["name"], e["profile"], e["region"], e["backend"], e["tenant"]))


if __name__ == "__main__":
    main()
