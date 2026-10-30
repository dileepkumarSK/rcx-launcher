# RCX Env Launcher

A one-click launcher for every client/environment's **Kibana**, **UI** and **NiFi**
(plus Kibana (App), NiFi-registry and Grafana) consoles. Each link button **connects
that environment's VPN** (via NetworkManager/`nmcli`) and then **opens the URL in Chrome**.

Links are auto-extracted from the internal `reactorcx/rcx-devops` wiki, so they stay
accurate and can be refreshed any time.

## First-time setup (and sharing this tool)

Everything is driven by local config files — **no code editing needed** to use a copy
on another machine. Requirements: Linux with `google-chrome`, Python 3, `nmcli` with your
client VPNs already configured, and `git`.

1. **Get the code and start it:**
   ```bash
   cd ~/rcx-launcher
   ./start.sh            # starts the local server + opens the Chrome app window
   ```
2. **Open Settings** (top-nav chip). Each section shows its current (non-sensitive)
   values read-only; press **Edit** on a section to change it, then **Save** (or
   **Cancel**). Fill in **your own** credentials:
   - **GitHub** — press Edit and paste a personal access token (github.com/settings/tokens,
     *classic*, `repo` scope). This is used to sync the wiki links and list the repos. *Or*
     skip it if you already have the `gh` CLI logged in — the tool falls back to that.
   - **Jenkins** — your Jenkins user + **API token** (generate at
     `https://jenkins.devops.lmvi.net/me/configure` → *API Token*) + the VPN name to use
     (`DEVOPS`). Use the API token, **not your password** — see *Builds*
     below. Needed only for the Jenkins view.
   - **Sources (advanced)** — only change these to point the tool at a *different* org or
     Jenkins. Defaults target `reactorcx` / `devops.lmvi.net`.
3. Press **Sync** to pull the links and repos with your credentials.

Credentials live only on your machine: `github.json` and `jenkins.json` (both `chmod 600`),
and non-secret config in `settings.json`. **Don't share those files** — each person adds
their own in Settings. Settings only ever **displays non-sensitive fields** (username,
Jenkins URL/user/VPN, sources); tokens are shown as `•••••••• saved` and never sent back to
the page. `config.json`, `favorites.json`, and the caches are regenerated locally.

## Add it to your desktop / app menu

So you can launch it with one click (and pin it to the dock) instead of running
`start.sh` from a terminal:

```bash
cd ~/rcx-launcher
./install-desktop.sh
```

That generates a desktop entry pointing at **this** copy of the repo (wherever it
lives) and installs it to `~/.local/share/applications/`. Then:

1. Open your app menu / "Activities" and search **"RCX Env Launcher"**.
2. **Right-click** its icon → **Add to Favorites** / **Pin to Dash** to keep it on the dock.
3. Click it any time — it starts the local server (if needed) and opens the app window.

Notes:
- If it doesn't show up right away, log out and back in (or run
  `update-desktop-database ~/.local/share/applications`).
- Re-run `./install-desktop.sh` after you **move the folder** — it rewrites the paths.
- Remove it with `./install-desktop.sh --uninstall`.
- No `sudo` needed; it installs for your user only. Requires a desktop environment
  (GNOME/KDE/XFCE/etc.). Over plain SSH with no display, just use `./start.sh`.

**Manual alternative** (if you'd rather not run the script): copy
`rcx-launcher.desktop` into `~/.local/share/applications/`, then edit its `Exec=`
and `Icon=` lines to the absolute path of your `start.sh` and `web/icon.svg`.

## Daily use

Launch **"RCX Env Launcher"** from the app menu (or pin it to the dock). A Chrome
app window opens on the **client picker**:

- **Choose a client** → its environments appear, ordered **Prod → Dev → Test → Staging → …**.
- Click a link button → the right VPN connects, then the page opens in your normal Chrome.
- Use the client chips in the top bar to switch clients; **← Clients** (or `Esc`) goes back.
  Each client is bookmarkable via the URL hash (e.g. `…/#SEI`).
- The top bar shows **active VPN chips**; click the `×` to disconnect one. Connected
  environments are highlighted (green) on both the picker and the client view.
- Type in the **filter box** (or press `/`) to narrow clients, or envs/links within a client.
- **Sync** re-pulls the links (and the repo list) from GitHub.

Extra views live alongside the clients, each a top-nav chip + a card on the picker,
bookmarkable by hash:

- **Prod** (`#@prod`) — every client's Prod environment on one page.
- **Repos** (`#@repos`) — every repo in the GitHub org as a button; click one to open
  it in Chrome. These are code repos on github.com, so **no VPN** is used. The list is
  pulled live on **Sync** (via your GitHub token, or `gh`; archived repos excluded).
  Repos are **grouped into buckets by client** (a repo whose name contains a client name,
  e.g. `rcx-sei-proxy` → SEI, `rcx-7gt-proxy` → 7GT); the rest fall under **Others**.
  Click the **★ star** on any repo to favorite it — favorites collect in a **★ Favorites**
  bucket at the top and are saved in `favorites.json`.
- **Builds** (`#@jenkins`) — trigger or troubleshoot a Jenkins build; see below.
- **Settings** (`#@settings`) — your GitHub + Jenkins logins, the **theme** (match
  system / light / dark), the **Glass** display mode, which **browser** opens the links
  (your system default by default, or force Chrome), and the data sources. Credential and
  source sections are **read-only until you press Edit** (then Save / Cancel); tokens are
  never shown.

Environments with no matching VPN can't be launched, so they're kept out of the
launchable list — but the client page now shows a **"N environments hidden — no VPN
matched"** disclosure at the bottom (expand it to see which, with a tip on how to fix the
VPN mapping) instead of them vanishing silently.

## Run from the terminal

```bash
~/rcx-launcher/start.sh          # starts the server (if needed) + opens the app window
```

Server only, no window:

```bash
python3 ~/rcx-launcher/server.py            # http://127.0.0.1:8766
```

## Refresh the links

```bash
python3 ~/rcx-launcher/sync_wiki.py          # clones/pulls the wiki, rewrites config.json
```

Uses your existing `git`/`gh` credentials to read the private wiki. Re-run any time
(or press **Sync** in the UI).

## Builds (Jenkins)

The **Builds** view lets you **trigger a build** and **see why one failed** without
opening Jenkins. To diagnose, either pick a **repo + branch** and leave Build as **latest**
(or type a build number), or paste a build/branch URL. The server connects the **DEVOPS**
VPN, pulls the build via Jenkins' API (`/wfapi/describe` for the failed stage +
`/consoleText`), extracts the likely failure reason, and shows it with the full console
log underneath.

- **▶ Build now** — pick a **repo + branch**, then click this to **trigger a build right
  from the launcher** (no Jenkins tab needed). The server connects the VPN, starts the
  build (parameterized jobs run with their default parameters), then **polls and streams
  the console log** into the pane — Queued → Building (live log tail) → the final result
  with the failure reason + full log. Leave the view open while it runs.
- **Open in Jenkins** — pick a repo (and branch), then click this to open that job's page
  in Chrome (VPN connected first) so you can trigger a build yourself with whatever options
  you want (e.g. *Build with Parameters*). With a branch selected it opens the branch job
  (where *Build Now* lives); otherwise it opens the multibranch project.

- **Login:** the first time, enter your Jenkins user + **API token** in the view; it's
  saved to `jenkins.json` (chmod 600, not shared). Generate a token at
  `https://jenkins.devops.lmvi.net/me/configure` → *API Token*. **Prefer the API token over
  your password:** a password login can trip Jenkins' CSRF protection and make **Build now**
  fail with *access denied* (HTTP 403). The launcher shares a session cookie between the
  crumb request and the build POST so a valid crumb is accepted; if you still get 403, the
  message will say whether it's a crumb issue (switch to an API token) or a genuine
  permissions issue (your user needs *Job/Build* on that job).
- The repo list (`/api/jenkins/jobs`) is crawled from Jenkins and cached in
  `.jenkins_jobs_cache.json`; use **↻ refresh repos** after new jobs/branches appear.
- Only hosts under `devops.lmvi.net` are ever fetched.

## Fixing / overriding a VPN mapping

`sync_wiki.py` maps each environment to a NetworkManager VPN connection using the
**client name as the anchor**: it finds the VPNs whose name starts with the client's.
If a client has **one** VPN, that VPN is used for every one of its environments (whatever
the tier is spelled); if it has **several**, each environment is routed to the matching
tier so a prod/perf env doesn't open on the lower-env VPN. **Each person names their VPNs
themselves, so matching is name-based and machine-specific** — if a colleague's VPN name
doesn't match, that environment silently disappears for them (it won't show up).

When a client has multiple VPNs, the tier is recognized from several common spellings
(case/spacing don't matter):

| Tier   | VPN-name tokens accepted            |
|--------|-------------------------------------|
| LOWER  | `LOWER`, `LLE`, `LWR`, `LOW`         |
| PERF   | `PERF`, `PRF`                        |
| PERF2  | `PERF2`, `PRF2`                      |
| PROD   | `PROD`, `PRD`                        |

So `TA LOWER`, `TA LLE`, and `TA-LWR` all map to TA's lower environments. If your VPN uses
a spelling not listed here, either rename the connection or add an override below.

> **Sharing note:** `config.json` is generated per machine from *that machine's* VPN names,
> and it's git-ignored. A colleague must (1) `git pull` the latest code, then (2) press
> **Sync** (or run `sync_wiki.py`) on their own machine for their VPN names to be
> picked up.

### Seeing what matched (read from the VPN side)

The client **name** in a VPN can differ per machine too (e.g. `7GT Lower` vs
`SevenGT Lower`), so an environment can still disappear silently. To see exactly what
your machine's VPN names are and how the tool reads them — no wiki/credentials needed:

```bash
python3 sync_wiki.py --vpns
```

And every full sync now **prints a VPN mapping report** at the end: which VPNs went
**unused**, and which `Client/TIER` combos found **no VPN** — each with a ready-to-paste
`vpn_map.json` line, e.g.:

```
7GT/LOWER: no match
    maybe one of: SevenGT Lower
    to force it, add to vpn_map.json:  "7GT:LOWER": "SevenGT Lower"
```

So instead of an environment just vanishing, the report tells you which of *your* VPN
connections to map it to. Copy that line into `vpn_map.json` and re-run.

To force a specific mapping, add entries to `vpn_map.json` keyed by `"<Client>:<TIER>"`:

```json
{
  "SEI:PERF2": "SEI PERF2",
  "Demo:LOWER": "DEMO",
  "SomeClient:LOWER": ""
}
```

- A connection **name** forces that VPN.
- An empty string `""` means "no VPN" (the environment is dropped).
- Connection names must match `nmcli connection show` exactly (spaces, case, `&`).

Then re-run `sync_wiki.py`.

## How it fits together

| File | Purpose |
|------|---------|
| `sync_wiki.py` | Clones/pulls the wiki, parses URL tables, writes `config.json`. |
| `config.json` | Generated data the UI reads (clients → environments → links). |
| `vpn_map.json` | Optional manual VPN-name overrides. |
| `server.py` | Localhost-only stdlib HTTP server: serves the UI + API (`/api/open`, `/api/vpn/status`, `/api/vpn/disconnect`, `/api/sync`). Runs `nmcli` / Chrome with no shell (no injection). |
| `web/` | The single-page UI (`index.html`, `style.css`, `app.js`). |
| `start.sh` | Starts the server and opens the Chrome app window. |
| `install-desktop.sh` | Installs the app-menu/desktop launcher for this copy (`--uninstall` removes it). |
| `rcx-launcher.desktop` | Reference template for the app-menu entry (the installer generates the real one). |

## Notes

- Nothing is exposed off `127.0.0.1`; there's no auth because it's single-user/local.
- **Theme & Glass** — **Settings → Theme** switches between **Match system** (follows your
  OS light/dark and updates live), **Light**, and **Dark**. **Settings → Display → Glass**
  is a separate, optional **frosted-glass** mode that layers on top of *either* theme
  (Light + Glass or Dark + Glass): translucent, blurred panels over a soft gradient. Both
  choices are saved in the browser (localStorage) and applied before the page paints, so
  there's no flash.
- Link pages open in your **system default browser** by default (via `xdg-open`). You can
  switch to **Google Chrome / Chromium** in **Settings → Browser** — that forces Chrome so
  pages land in your logged-in Chrome profile (where you're already signed in to Kibana/NiFi).
  Each mode falls back to the other if its opener isn't installed. The launcher **window**
  itself still uses Chrome's app-mode (separate profile) so it stays its own app.
- VPNs are brought **up** on demand; other VPNs are left connected (no auto-disconnect).
  If an `nmcli` connection needs a password it isn't storing, NetworkManager will prompt;
  if it can't connect non-interactively the button reports the `nmcli` error.
- You log in to Kibana/NiFi yourself as usual — no credentials are stored by this tool.
