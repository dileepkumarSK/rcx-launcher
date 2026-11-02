"use strict";

const TYPE_LABEL = {
  "kibana": "Kibana",
  "kibana-compq": "Kibana (App)",
  "ui": "UI",
  "nifi": "NiFi",
  "nifi-registry": "Registry",
  "grafana": "Grafana",
};

// Environment display priority: Prod, Dev, Test, Staging, then the rest.
const ENV_ORDER = [
  "prod", "dev", "test", "staging", "int", "internal",
  "qa", "sandbox", "uat", "puat", "perf", "perf2",
];

const PROD_KEY = "@prod"; // sentinel "client" = the all-prod view
const REPOS_KEY = "@repos"; // sentinel "client" = the GitHub repos view
const JENKINS_KEY = "@jenkins"; // sentinel "client" = the Jenkins troubleshooter
const KAFKA_KEY = "@kafka"; // sentinel "client" = the Kafka certs view
const MONGO_KEY = "@mongo"; // sentinel "client" = the Mongo certs view
const SETTINGS_KEY = "@settings"; // sentinel "client" = the Settings view

let CONFIG = { clients: [], repos: [] };
let activeVpns = new Set();
let favorites = new Set(); // favorite repo names
let jkRepoMap = null;       // repo display label -> job url
let jkBranchMap = {};       // branch name -> job url (for the selected repo)
let jkPoll = null;          // timer id for the active build-status poll
let certApps = {};          // which desktop clients are installed (offsetexplorer/compass)
let selected = null; // client name, PROD_KEY, REPOS_KEY, JENKINS_KEY, or null for the picker

function repoList() { return CONFIG.repos || []; }

const FAV_BUCKET = "★ Favorites";
const OTHER_BUCKET = "Others";

// Map a repo to a client name by matching a client name as a token in the repo
// name (e.g. "rcx-sei-proxy" -> SEI, "rcx-7gt-proxy" -> 7GT). null = no match.
function repoClient(repo) {
  const tokens = repo.name.toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
  for (const c of CONFIG.clients) {
    const t = c.name.toLowerCase();
    if (tokens.includes(t)) return c.name;
  }
  return null;
}

function isProdEnv(env) { return env.name.toLowerCase().startsWith("prod"); }
function prodEntries() {
  // [{client, env}] for every client's Prod environment.
  const out = [];
  for (const c of CONFIG.clients) {
    for (const e of c.environments) if (isProdEnv(e)) out.push({ client: c, env: e });
  }
  return out.sort((a, b) => a.client.name.localeCompare(b.client.name));
}

const $ = (sel, el = document) => el.querySelector(sel);
const el = (tag, cls, txt) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
};

async function api(path, opts) {
  const r = await fetch(path, opts);
  return r.json();
}

function toast(msg, kind = "info", sub = "") {
  const t = el("div", "toast " + kind);
  t.append(el("div", null, msg));
  if (sub) t.append(el("div", "sub", sub));
  $("#toasts").append(t);
  setTimeout(() => { t.style.opacity = "0"; t.style.transition = "opacity .3s"; }, kind === "err" ? 5200 : 2600);
  setTimeout(() => t.remove(), kind === "err" ? 5600 : 3000);
}

function envRank(name) {
  const n = name.toLowerCase();
  const exact = ENV_ORDER.indexOf(n);
  if (exact !== -1) return exact;
  for (let i = 0; i < ENV_ORDER.length; i++) {
    if (n.startsWith(ENV_ORDER[i])) return i + 0.5;
  }
  return 999;
}

function sortedEnvs(client) {
  return [...client.environments].sort(
    (a, b) => (envRank(a.name) - envRank(b.name)) || a.name.localeCompare(b.name)
  );
}

function getClient(name) {
  return CONFIG.clients.find(c => c.name === name);
}

/* ---- top nav (client chips) ---- */
// A special-view nav chip: a colored dot (in the view's accent) + a noun label,
// uniform across Prod / Repos / Builds / Settings so they read as one family.
function navChip(cls, key, label) {
  const c = el("a", "navchip " + cls + (selected === key ? " active" : ""));
  c.append(el("span", "navdot"), document.createTextNode(label));
  c.href = "#";
  c.addEventListener("click", (e) => { e.preventDefault(); location.hash = key; });
  return c;
}

function renderNav() {
  const nav = $("#nav");
  nav.innerHTML = "";
  if (prodEntries().length) nav.append(navChip("prod", PROD_KEY, "Prod"));
  if (repoList().length) nav.append(navChip("repos", REPOS_KEY, "Repos"));
  nav.append(navChip("jenkins", JENKINS_KEY, "Builds"));
  nav.append(navChip("kafka", KAFKA_KEY, "Kafka"));
  nav.append(navChip("mongo", MONGO_KEY, "Mongo"));
  nav.append(navChip("settings", SETTINGS_KEY, "Settings"));
  for (const client of CONFIG.clients) {
    const chip = el("a", "navchip" + (client.name === selected ? " active" : ""), client.name);
    chip.href = "#";
    chip.addEventListener("click", (e) => { e.preventDefault(); selectClient(client.name); });
    nav.append(chip);
  }
}

/* ---- views ---- */
function render() {
  renderNav();
  if (selected === PROD_KEY) renderProdView();
  else if (selected === REPOS_KEY) renderReposView();
  else if (selected === JENKINS_KEY) renderJenkinsView();
  else if (selected === KAFKA_KEY) renderCertsView("kafka");
  else if (selected === MONGO_KEY) renderCertsView("mongo");
  else if (selected === SETTINGS_KEY) renderSettingsView();
  else if (selected && getClient(selected)) renderClientView(selected);
  else renderPicker();
  applyFilter($("#filter").value);
}

function renderPicker() {
  selected = null;
  renderNav();
  const main = $("#main");
  main.innerHTML = "";
  $("#filter").placeholder = "Filter clients…  (/ to focus)";

  if (!CONFIG.clients.length) {
    main.append(el("div", "empty", "No environments. Run Sync to pull links from the wiki."));
    return;
  }

  main.append(el("h1", "page-title", "Choose a client"));
  const grid = el("div", "pickgrid");

  const prod = prodEntries();
  if (prod.length) {
    const pcard = el("button", "clientcard prodcard");
    pcard.dataset.text = "all prod production " + prod.map(p => p.client.name).join(" ").toLowerCase();
    pcard.append(el("div", "cc-name", "All Prod"));
    pcard.append(el("div", "cc-count", prod.length + " production environments"));
    const chips = el("div", "cc-envs");
    for (const p of prod) {
      const t = el("span", "cc-env", p.client.name);
      t.dataset.vpn = p.env.vpn || "";
      if (p.env.vpn && activeVpns.has(p.env.vpn)) t.classList.add("on");
      chips.append(t);
    }
    pcard.append(chips);
    pcard.addEventListener("click", () => { location.hash = PROD_KEY; });
    grid.append(pcard);
  }

  const repos = repoList();
  if (repos.length) {
    const rcard = el("button", "clientcard repocard");
    rcard.dataset.text = "repos github code repositories " +
      repos.map(r => r.name).join(" ").toLowerCase();
    rcard.append(el("div", "cc-name", "Repos"));
    rcard.append(el("div", "cc-count", repos.length + " GitHub repositories"));
    rcard.append(el("div", "cc-sub", "reactorcx org · opens in Chrome, no VPN"));
    rcard.addEventListener("click", () => { location.hash = REPOS_KEY; });
    grid.append(rcard);
  }

  const jcard = el("button", "clientcard jenkinscard");
  jcard.dataset.text = "jenkins build failure troubleshoot ci";
  jcard.append(el("div", "cc-name", "Builds"));
  jcard.append(el("div", "cc-count", "troubleshoot a build"));
  jcard.append(el("div", "cc-sub", "paste a build URL, or pick repo + branch → get the failure reason"));
  jcard.addEventListener("click", () => { location.hash = JENKINS_KEY; });
  grid.append(jcard);

  const kcard = el("button", "clientcard kafkacard");
  kcard.dataset.text = "kafka certs offset explorer truststore keystore jks cluster";
  kcard.append(el("div", "cc-name", "Kafka"));
  kcard.append(el("div", "cc-count", "cert bundles → Offset Explorer"));
  kcard.append(el("div", "cc-sub", "pick a bundle → copy the cluster + keystore/truststore settings"));
  kcard.addEventListener("click", () => { location.hash = KAFKA_KEY; });
  grid.append(kcard);

  const mcard = el("button", "clientcard mongocard");
  mcard.dataset.text = "mongo mongodb certs compass pem connection string";
  mcard.append(el("div", "cc-name", "Mongo"));
  mcard.append(el("div", "cc-count", "cert bundles → Compass"));
  mcard.append(el("div", "cc-sub", "pick a bundle → copy a mongodb:// URI or make a Compass import"));
  mcard.addEventListener("click", () => { location.hash = MONGO_KEY; });
  grid.append(mcard);

  const scard = el("button", "clientcard settingscard");
  scard.dataset.text = "settings credentials github jenkins token config";
  scard.append(el("div", "cc-name", "Settings"));
  scard.append(el("div", "cc-count", "credentials & sources"));
  scard.append(el("div", "cc-sub", "your GitHub + Jenkins logins; make this work on a shared copy"));
  scard.addEventListener("click", () => { location.hash = SETTINGS_KEY; });
  grid.append(scard);

  for (const client of CONFIG.clients) {
    const card = el("button", "clientcard");
    card.dataset.text = (client.name + " " +
      client.environments.map(e => e.name + " " + e.vpn).join(" ")).toLowerCase();
    card.append(el("div", "cc-name", client.name));
    const n = client.environments.length;
    card.append(el("div", "cc-count", n + (n === 1 ? " environment" : " environments")));
    const chips = el("div", "cc-envs");
    for (const env of sortedEnvs(client)) {
      const t = el("span", "cc-env", env.name);
      t.dataset.vpn = env.vpn || "";
      if (env.vpn && activeVpns.has(env.vpn)) t.classList.add("on");
      chips.append(t);
    }
    card.append(chips);
    card.addEventListener("click", () => selectClient(client.name));
    grid.append(card);
  }
  main.append(grid);
}

function renderClientView(name) {
  const client = getClient(name);
  const main = $("#main");
  main.innerHTML = "";
  $("#filter").placeholder = "Filter " + name + " envs & links…  (/ to focus)";

  const head = el("div", "view-head");
  const back = el("button", "backbtn", "← Clients");
  back.addEventListener("click", showPicker);
  head.append(back);
  head.append(el("h1", "page-title", name));
  const n = client.environments.length;
  head.append(el("span", "count", n + (n === 1 ? " env" : " envs")));
  main.append(head);

  const grid = el("div", "envgrid");
  for (const env of sortedEnvs(client)) grid.append(renderEnv(client, env));
  main.append(grid);
  if (client.hidden && client.hidden.length) main.append(renderHiddenNote(client.hidden));
  refreshConnected();
}

// Environments the sync found but couldn't match to a VPN on this machine. Shown
// so a missing env is explained (and fixable) instead of silently disappearing.
function renderHiddenNote(hidden) {
  const box = el("div", "hidden-note");
  const n = hidden.length;
  const summary = el("summary", "hidden-sum",
    n + (n === 1 ? " environment hidden" : " environments hidden") + " — no VPN matched");
  const det = el("details");
  det.append(summary);
  const list = el("div", "hidden-list");
  for (const h of hidden) {
    const row = el("div", "hidden-row");
    row.append(el("span", "hidden-name", h.name));
    row.append(el("span", "hidden-why", (h.reason || "no VPN matched") +
      (h.tier ? " · tier " + h.tier : "")));
    list.append(row);
  }
  det.append(list);
  const tip = el("div", "hidden-tip");
  tip.textContent = "These wiki environments have no matching NetworkManager VPN here. " +
    "Run  python3 sync_wiki.py --vpns  to see your VPN names, or add an override in " +
    "vpn_map.json, then press Sync.";
  det.append(tip);
  box.append(det);
  return box;
}

function renderProdView() {
  const main = $("#main");
  main.innerHTML = "";
  $("#filter").placeholder = "Filter prod clients & links…  (/ to focus)";

  const head = el("div", "view-head");
  const back = el("button", "backbtn", "← Clients");
  back.addEventListener("click", showPicker);
  head.append(back);
  head.append(el("h1", "page-title", "All Prod"));
  const entries = prodEntries();
  head.append(el("span", "count", entries.length + " clients"));
  main.append(head);

  const grid = el("div", "envgrid");
  for (const { client, env } of entries) grid.append(renderEnv(client, env, client.name));
  main.append(grid);
  refreshConnected();
}

function renderReposView() {
  const main = $("#main");
  main.innerHTML = "";
  $("#filter").placeholder = "Filter repos…  (/ to focus)";

  const head = el("div", "view-head");
  const back = el("button", "backbtn", "← Clients");
  back.addEventListener("click", showPicker);
  head.append(back);
  head.append(el("h1", "page-title", "Repos"));
  const repos = repoList();
  head.append(el("span", "count", repos.length + " repositories · reactorcx"));
  main.append(head);

  // Sort every repo into a bucket: Favorites first, else its client, else Others.
  const buckets = new Map();
  const put = (name, repo) => {
    if (!buckets.has(name)) buckets.set(name, []);
    buckets.get(name).push(repo);
  };
  for (const repo of repos) {
    if (favorites.has(repo.name)) put(FAV_BUCKET, repo);
    else put(repoClient(repo) || OTHER_BUCKET, repo);
  }

  const order = [FAV_BUCKET, ...CONFIG.clients.map(c => c.name), OTHER_BUCKET];
  for (const name of order) {
    const items = buckets.get(name);
    if (!items || !items.length) continue;
    items.sort((a, b) => a.name.localeCompare(b.name));

    const sec = el("section", "repo-bucket" + (name === FAV_BUCKET ? " fav" : ""));
    sec.dataset.bucket = name;
    const bh = el("div", "bucket-head");
    bh.append(el("h2", null, name));
    bh.append(el("span", "count", String(items.length)));
    sec.append(bh);

    const grid = el("div", "repogrid");
    for (const repo of items) grid.append(renderRepo(repo));
    sec.append(grid);
    main.append(sec);
  }
}

function renderRepo(repo) {
  const fav = favorites.has(repo.name);
  const card = el("div", "repoitem");
  card.dataset.text = (repo.name + " " + (repo.description || "")).toLowerCase();

  const star = el("button", "repoitem-star" + (fav ? " on" : ""), fav ? "★" : "☆");
  star.title = fav ? "Remove from favorites" : "Add to favorites";
  star.addEventListener("click", (e) => { e.stopPropagation(); toggleFav(repo.name); });

  const open = el("button", "repoitem-open");
  open.title = repo.url;
  open.append(el("span", "repo-name", repo.name));
  if (repo.description) open.append(el("span", "repo-desc", repo.description));
  open.addEventListener("click", () => openRepo(open, repo));

  card.append(star, open);
  return card;
}

async function toggleFav(name) {
  if (favorites.has(name)) favorites.delete(name);
  else favorites.add(name);
  renderReposView();            // re-bucket so the repo moves in/out of Favorites
  applyFilter($("#filter").value);
  try {
    await api("/api/favorites", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repos: [...favorites] }),
    });
  } catch { /* favorites are best-effort; UI already updated */ }
}

async function openRepo(btn, repo) {
  btn.classList.add("busy");
  try {
    const res = await api("/api/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ vpn: null, url: repo.url }),
    });
    if (res.ok) toast("Opened " + repo.name, "ok");
    else toast("Could not open " + repo.name, "err", res.error || "");
  } catch (e) {
    toast("Request failed", "err", String(e));
  } finally {
    btn.classList.remove("busy");
  }
}

/* ---- Jenkins troubleshooter ---- */
function labelRow(labelText, inputEl) {
  const row = el("label", "jk-row");
  row.append(el("span", "jk-label", labelText));
  row.append(inputEl);
  return row;
}

async function renderJenkinsView() {
  const main = $("#main");
  main.innerHTML = "";
  $("#filter").value = "";
  $("#filter").placeholder = "Builds — use the form below";

  const head = el("div", "view-head");
  const back = el("button", "backbtn", "← Clients");
  back.addEventListener("click", showPicker);
  head.append(back);
  head.append(el("h1", "page-title", "Builds"));
  head.append(el("span", "count", "build a branch and watch the log, or diagnose a failure"));
  main.append(head);

  const authLine = el("div", "jk-auth");
  const panel = el("div", "jk-panel");
  const out = el("div", "jk-out");
  out.id = "jk-out";
  main.append(authLine, panel, out);

  renderJkAuth(authLine, panel);
}

async function renderJkAuth(authLine, panel) {
  let cfg = { configured: false };
  try { cfg = await api("/api/jenkins/config"); } catch { /* offline */ }
  authLine.innerHTML = "";
  if (cfg.configured) {
    authLine.append(el("span", "jk-ok", "✓ signed in as " + cfg.user + " · " + cfg.base));
    const change = el("button", "jk-link", "change login");
    change.addEventListener("click", () => showJkAuthForm(authLine, panel, cfg));
    authLine.append(change);
    buildJkPanel(panel, cfg);
  } else {
    authLine.append(el("span", "jk-warn", "⚠ Jenkins login not set"));
    showJkAuthForm(authLine, panel, cfg);
  }
}

function showJkAuthForm(authLine, panel, cfg) {
  panel.innerHTML = "";
  const form = el("div", "jk-authform");
  const base = el("input", "jk-input");
  base.value = cfg.base || "https://jenkins.devops.lmvi.net";
  const user = el("input", "jk-input");
  user.value = cfg.user || "";
  const token = el("input", "jk-input");
  token.type = "password";
  token.placeholder = "API token or password";
  form.append(labelRow("Jenkins URL", base), labelRow("User", user), labelRow("Token", token));

  const save = el("button", "btn-primary", "Save login");
  save.addEventListener("click", async () => {
    if (!user.value.trim() || !token.value.trim()) { toast("Enter user and token", "info"); return; }
    save.disabled = true;
    try {
      const r = await api("/api/jenkins/auth", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ base: base.value.trim(), user: user.value.trim(), token: token.value }),
      });
      if (r.ok) { toast("Jenkins login saved", "ok"); renderJkAuth(authLine, panel); }
      else toast("Save failed", "err", r.error || "");
    } catch (e) { toast("Save failed", "err", String(e)); }
    finally { save.disabled = false; }
  });
  form.append(save);
  const hint = el("div", "jk-hint");
  hint.textContent = "Tip: generate a token at " + (cfg.base || "<jenkins>") + "/me/configure → API Token.";
  form.append(hint);
  panel.append(form);
}

function buildJkPanel(panel, cfg) {
  stopJkPoll();
  panel.innerHTML = "";

  // --- pick repo + branch + build ---
  const repoInput = el("input", "jk-input");
  repoInput.setAttribute("list", "jk-repolist");
  repoInput.placeholder = jkRepoMap ? "type a repo…" : "loading repos…";
  const repoDL = el("datalist"); repoDL.id = "jk-repolist";

  const branchInput = el("input", "jk-input");
  branchInput.setAttribute("list", "jk-branchlist");
  branchInput.placeholder = "select a repo first";
  branchInput.disabled = true;
  const branchDL = el("datalist"); branchDL.id = "jk-branchlist";

  const buildInput = el("input", "jk-input jk-num");
  buildInput.type = "text";
  buildInput.placeholder = "latest";

  const diagnose = el("button", "btn-primary", "Diagnose");

  const trigger = el("button", "btn-build", "▶ Build now");
  trigger.title = "Trigger a build of the selected branch and stream the log here";
  trigger.addEventListener("click", () => {
    const branchUrl = jkBranchMap[branchInput.value];
    if (!branchUrl) { toast("Pick a repo and branch to build", "info"); return; }
    triggerBuild(trigger, branchUrl, branchInput.value, repoInput.value);
  });

  const openJob = el("button", "btn-ghost", "Open in Jenkins ↗");
  openJob.title = "Open the job page in Jenkins so you can build it yourself";
  openJob.addEventListener("click", () => {
    const url = jkBranchMap[branchInput.value] || (jkRepoMap && jkRepoMap[repoInput.value]);
    if (!url) { toast("Pick a repo (and branch) first", "info"); return; }
    openJenkinsJob(url, cfg.vpn);
  });

  const refresh = el("button", "jk-link", "↻ refresh repos");
  refresh.addEventListener("click", () => loadJkRepos(repoInput, repoDL, true));

  repoInput.addEventListener("change", () => {
    const url = jkRepoMap && jkRepoMap[repoInput.value];
    jkBranchMap = {};
    branchDL.innerHTML = "";
    branchInput.value = "";
    if (!url) { branchInput.disabled = true; branchInput.placeholder = "select a repo first"; return; }
    branchInput.disabled = true;
    branchInput.placeholder = "loading branches…";
    loadJkBranches(url, branchInput, branchDL);
  });

  diagnose.addEventListener("click", () => {
    const branchUrl = jkBranchMap[branchInput.value];
    if (!branchUrl) { toast("Pick a repo and branch", "info"); return; }
    runDiagnose(diagnose, { branchUrl, build: buildInput.value.trim() || "latest" });
  });

  const form = el("div", "jk-form");
  form.append(labelRow("Repo", repoInput), labelRow("Branch", branchInput),
    labelRow("Build #", buildInput), trigger, diagnose, openJob, refresh);
  form.append(repoDL, branchDL);
  panel.append(form);

  // --- or paste a URL ---
  const urlInput = el("input", "jk-input jk-urlinput");
  urlInput.placeholder = "…or paste a build / branch URL and press Enter";
  const urlGo = el("button", "btn-ghost", "Diagnose URL");
  const doUrl = () => {
    if (!urlInput.value.trim()) return;
    runDiagnose(urlGo, { url: urlInput.value.trim(), build: "latest" });
  };
  urlInput.addEventListener("keydown", (e) => { if (e.key === "Enter") doUrl(); });
  urlGo.addEventListener("click", doUrl);
  const urlRow = el("div", "jk-urlrow");
  urlRow.append(urlInput, urlGo);
  panel.append(urlRow);

  loadJkRepos(repoInput, repoDL, false);
}

async function loadJkRepos(repoInput, repoDL, refresh) {
  repoInput.placeholder = refresh ? "refreshing repos…" : "loading repos…";
  try {
    const r = await api("/api/jenkins/jobs" + (refresh ? "?refresh=1" : ""));
    if (!r.ok) { toast("Could not load repos", "err", r.error || ""); return; }
    jkRepoMap = {};
    repoDL.innerHTML = "";
    for (const repo of r.repos) {
      const label = repo.folder ? repo.repo + " · " + repo.folder : repo.repo;
      jkRepoMap[label] = repo.url;
      const o = el("option"); o.value = label; repoDL.append(o);
    }
    repoInput.placeholder = "type a repo… (" + r.repos.length + ")";
    if (refresh) toast("Repo list refreshed (" + r.repos.length + ")", "ok");
  } catch (e) {
    toast("Could not load repos", "err", String(e));
    repoInput.placeholder = "failed to load repos";
  }
}

async function loadJkBranches(repoUrl, branchInput, branchDL) {
  try {
    const r = await api("/api/jenkins/branches?url=" + encodeURIComponent(repoUrl));
    if (!r.ok) { toast("Could not load branches", "err", r.error || ""); return; }
    jkBranchMap = {};
    branchDL.innerHTML = "";
    for (const b of r.branches) {
      jkBranchMap[b.name] = b.url;
      const o = el("option"); o.value = b.name; branchDL.append(o);
    }
    branchInput.disabled = false;
    branchInput.placeholder = "type a branch… (" + r.branches.length + ")";
  } catch (e) {
    toast("Could not load branches", "err", String(e));
    branchInput.placeholder = "failed to load branches";
  }
}

function stopJkPoll() {
  if (jkPoll) { clearTimeout(jkPoll); jkPoll = null; }
}

async function runDiagnose(btn, body) {
  stopJkPoll();
  const out = $("#jk-out");
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = "Diagnosing…";
  out.innerHTML = "";
  out.append(el("div", "jk-sub", "Connecting VPN and fetching the build…"));
  try {
    const res = await api("/api/jenkins/diagnose", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    renderJkResult(out, res);
  } catch (e) {
    out.innerHTML = "";
    out.append(el("div", "jk-err", "✗ Request failed: " + String(e)));
  } finally {
    btn.disabled = false; btn.textContent = label;
  }
}

function fmtDur(ms) {
  if (ms == null || ms < 0) return "—";
  const s = Math.round(ms / 1000);
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60), r = s % 60;
  if (m < 60) return r ? m + "m " + r + "s" : m + "m";
  const h = Math.floor(m / 60);
  return h + "h " + (m % 60) + "m";
}
function fmtAgo(ts) {
  if (!ts) return "";
  const s = Math.round((Date.now() - ts) / 1000);
  if (s < 60) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return m + " min ago";
  const h = Math.floor(m / 60);
  if (h < 24) return h + "h ago";
  return Math.floor(h / 24) + "d ago";
}
function fmtWhen(ts) {
  if (!ts) return "";
  try {
    return new Date(ts).toLocaleString([], {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
  } catch (e) { return ""; }
}

function renderJkResult(out, res) {
  out.innerHTML = "";
  if (!res.ok) {
    out.append(el("div", "jk-err", "✗ " + (res.error || "could not diagnose")));
    if (res.url) {
      const s = el("div", "jk-sub"); s.textContent = res.url; out.append(s);
    }
    return;
  }
  const result = res.building ? "BUILDING" : (res.result || "UNKNOWN");
  const cls = result === "SUCCESS" ? "ok"
    : (["FAILURE", "UNSTABLE", "ABORTED"].includes(result) ? "bad" : "warn");

  const rhead = el("div", "jk-rhead");
  rhead.append(el("span", "jk-badge " + cls, result));
  rhead.append(el("span", "jk-name", res.name || res.requestUrl || ""));
  const open = el("button", "jk-link", "open in Jenkins ↗");
  open.addEventListener("click", () => openRaw(res.url || res.requestUrl));
  rhead.append(open);
  out.append(rhead);

  // Build-details card — number / result / duration / when / trigger, plus the
  // job's last successful build, so you see the outcome at a glance, not just logs.
  const card = el("div", "jk-info jk-info-" + cls);
  const addRow = (label, value, vcls) => {
    if (value == null || value === "") return;
    const row = el("div", "jk-irow");
    row.append(el("span", "jk-ilabel", label));
    const v = el("span", "jk-ival" + (vcls ? " " + vcls : ""));
    if (value instanceof Node) v.append(value); else v.textContent = value;
    row.append(v);
    card.append(row);
  };
  if (res.number != null) addRow("Build", "#" + res.number);
  addRow("Result", result, cls === "ok" ? "jk-ok" : (cls === "bad" ? "jk-bad" : "jk-warn"));
  if (res.version) {
    const src = res.versionSource && !["parameter", "image"].includes(res.versionSource)
      ? "  (" + res.versionSource + ")" : "";
    addRow("Version", res.version + src, "jk-ok");
  }
  if (res.image) addRow("Image", res.image, "jk-mono");
  const scm = res.scm || {};
  if (scm.branch || scm.commit) {
    addRow("Commit", (scm.branch || "") + (scm.commit ? (scm.branch ? " @ " : "") + scm.commit : ""));
  }
  if (!res.building && res.duration) addRow("Duration", fmtDur(res.duration));
  if (res.building && res.estimatedDuration) addRow("Est. time", "~" + fmtDur(res.estimatedDuration));
  if (res.timestamp) {
    const end = res.building ? res.timestamp : res.timestamp + (res.duration || 0);
    const when = fmtWhen(end);
    addRow(res.building ? "Started" : "Finished", fmtAgo(end) + (when ? " · " + when : ""));
  }
  if (res.cause) addRow("Trigger", res.cause);
  const ls = (res.job || {}).lastSuccess;
  if (ls && ls.number != null) {
    const same = ls.number === res.number;
    const span = el("span");
    span.textContent = "#" + ls.number +
      (ls.timestamp ? " · " + fmtAgo(ls.timestamp) : "") +
      (ls.duration ? " · " + fmtDur(ls.duration) : "") + (same ? " (this build)" : "");
    if (ls.url && !same) {
      const o = el("button", "jk-link", " open ↗");
      o.addEventListener("click", () => openRaw(ls.url));
      span.append(o);
    }
    addRow("Last success", span, "jk-ok");
  }
  if (card.childNodes.length) out.append(card);

  if (res.params && res.params.length) {
    const det = el("details", "jk-params");
    det.append(el("summary", null, "Parameters (" + res.params.length + ")"));
    const pg = el("div", "jk-pgrid");
    for (const p of res.params) {
      pg.append(el("span", "jk-pname", p.name));
      pg.append(el("span", "jk-pval", p.value || "—"));
    }
    det.append(pg);
    out.append(det);
  }

  if (res.building) out.append(el("div", "jk-sub", "Build is still running — this is the output so far."));

  if (res.failedStages && res.failedStages.length) {
    const fs = el("div", "jk-stages");
    fs.append(el("span", "jk-stages-h", "Failed stage: "));
    fs.append(el("b", null, res.failedStages[0]));
    if (res.failedStages.length > 1) fs.append(el("span", "jk-sub2", " (+" + (res.failedStages.length - 1) + " more)"));
    out.append(fs);
  }

  if (result === "SUCCESS" && !(res.reason && res.reason.length)) {
    out.append(el("div", "jk-good", "✓ Build succeeded — nothing to troubleshoot."));
  }

  let reason = res.reason;
  if (res.building && (!reason || !reason.length) && res.log) {
    reason = res.log.split("\n").filter((l) => l.length).slice(-40);
  }
  if (reason && reason.length) {
    const box = el("div", "jk-reason");
    const h = res.building ? "Console (live, last lines)"
      : (result === "SUCCESS" ? "Console tail" : "Likely failure reason");
    box.append(el("div", "jk-reason-h", h));
    box.append(el("pre", "jk-pre", reason.join("\n")));
    out.append(box);
  }

  const log = res.log || "";
  const det = el("details", "jk-logwrap");
  det.append(el("summary", null, "Full console log (" + log.split("\n").length + " lines)"));
  det.append(el("pre", "jk-pre jk-logpre", log));
  out.append(det);
}

// Kick off a build of the selected branch, then poll until it finishes,
// streaming the console log into the result pane.
async function triggerBuild(btn, branchUrl, branchName, repoLabel) {
  stopJkPoll();
  const out = $("#jk-out");
  const title = (repoLabel || "") + (branchName ? " · " + branchName : "");
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = "Starting…";
  out.innerHTML = "";
  out.append(el("div", "jk-sub", "Connecting VPN and starting a build of " + title + "…"));
  try {
    const res = await api("/api/jenkins/trigger", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ branchUrl }),
    });
    if (!res.ok) {
      out.innerHTML = "";
      out.append(el("div", "jk-err", "✗ " + (res.error || "could not start the build")));
      return;
    }
    renderJkQueued(out, title, res.parameterized
      ? "Build queued with the job's default parameters — waiting for an executor…"
      : "Build queued — waiting for an executor…");
    pollBuild({ queue: res.queue }, out, title);
  } catch (e) {
    out.innerHTML = "";
    out.append(el("div", "jk-err", "✗ Request failed: " + String(e)));
  } finally {
    btn.disabled = false; btn.textContent = label;
  }
}

function renderJkQueued(out, title, why) {
  out.innerHTML = "";
  const head = el("div", "jk-rhead");
  head.append(el("span", "jk-badge warn", "QUEUED"));
  head.append(el("span", "jk-name", title));
  out.append(head);
  out.append(el("div", "jk-sub", why || "waiting for an executor…"));
  out.append(el("div", "jk-sub2", "Updates automatically — keep this view open."));
}

function pollBuild(ref, out, title) {
  let buildUrl = ref.build || null;
  const tick = async () => {
    try {
      const qs = buildUrl
        ? "build=" + encodeURIComponent(buildUrl)
        : "queue=" + encodeURIComponent(ref.queue);
      const res = await api("/api/jenkins/build-status?" + qs);
      if (!res.ok) {
        out.innerHTML = "";
        out.append(el("div", "jk-err", "✗ " + (res.error || "lost track of the build")));
        return;
      }
      if (res.state === "cancelled") {
        out.innerHTML = "";
        out.append(el("div", "jk-err", "✗ Build was cancelled: " + (res.why || "")));
        return;
      }
      if (res.state === "queued") {
        renderJkQueued(out, title, res.why);
        jkPoll = setTimeout(tick, 3000);
        return;
      }
      buildUrl = res.buildUrl || buildUrl;
      if (!res.name) res.name = title + (res.number ? " #" + res.number : "");
      renderJkResult(out, res);
      if (res.state === "building") { jkPoll = setTimeout(tick, 3000); return; }
      // finished
      toast("Build " + (res.result || "finished") + " — " + title, res.result === "SUCCESS" ? "ok" : "info");
    } catch (e) {
      out.innerHTML = "";
      out.append(el("div", "jk-err", "✗ Request failed: " + String(e)));
    }
  };
  jkPoll = setTimeout(tick, 1500);
}

async function openRaw(url) {
  if (!url) return;
  try {
    const res = await api("/api/open", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ vpn: null, url }),
    });
    if (res.ok) toast("Opened in Jenkins", "ok");
    else toast("Could not open", "err", res.error || "");
  } catch (e) { toast("Request failed", "err", String(e)); }
}

// Open a Jenkins job/branch page in Chrome (connecting its VPN first) so the
// user can trigger a build there with whatever options they want.
async function openJenkinsJob(url, vpn) {
  try {
    const res = await api("/api/open", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ vpn: vpn || null, url }),
    });
    if (res.ok) toast("Opened job in Jenkins — build it there", "ok");
    else toast("Could not open", "err", res.error || "");
  } catch (e) { toast("Request failed", "err", String(e)); }
}

/* ---- Kafka / Mongo cert bundles ---- */
async function renderCertsView(kind) {
  const label = kind === "kafka" ? "Kafka" : "Mongo";
  const main = $("#main");
  main.innerHTML = "";
  $("#filter").value = "";
  $("#filter").placeholder = "Filter " + label + " bundles…  (/ to focus)";

  const head = el("div", "view-head");
  const back = el("button", "backbtn", "← Clients");
  back.addEventListener("click", showPicker);
  head.append(back);
  head.append(el("h1", "page-title", label));
  const count = el("span", "count", "cert bundles");
  head.append(count);
  const openApp = el("button", "btn-ghost", kind === "kafka" ? "Open Offset Explorer" : "Open Compass");
  openApp.style.marginLeft = "auto";
  openApp.addEventListener("click", () => launchApp(kind === "kafka" ? "offsetexplorer" : "compass", null, openApp));
  head.append(openApp);
  main.append(head);

  const body = el("div");
  body.append(el("div", "jk-sub", "Scanning for " + label + " cert bundles…"));
  main.append(body);

  let data;
  try { data = await api("/api/certs?kind=" + kind); }
  catch (e) { body.innerHTML = ""; body.append(el("div", "jk-err", "✗ " + String(e))); return; }
  body.innerHTML = "";
  if (!data.ok) { body.append(el("div", "jk-err", "✗ " + (data.error || "failed"))); return; }
  certApps = data.apps || {};
  const appInstalled = kind === "kafka" ? certApps.offsetexplorer : certApps.compass;
  if (!appInstalled) openApp.remove();

  const root = el("div", "certs-root");
  root.append(document.createTextNode(data.searchHome ? "searching: " : "folder: "));
  root.append(el("code", null, data.root));
  root.append(document.createTextNode("  ·  change in Settings → Certs"));
  body.append(root);

  if (!data.bundles.length) {
    const e = el("div", "empty");
    e.textContent = data.exists
      ? "No " + label + " cert bundles found under " + data.root
      : "Folder not found: " + data.root + " — set it in Settings → Certs";
    body.append(e);
    count.textContent = "0 bundles";
    return;
  }
  count.textContent = data.bundles.length + (data.bundles.length === 1 ? " bundle" : " bundles");

  const byClient = {};
  for (const b of data.bundles) (byClient[b.client] = byClient[b.client] || []).push(b);
  for (const client of Object.keys(byClient).sort((a, c) => a.toLowerCase().localeCompare(c.toLowerCase()))) {
    const sec = el("div", "certs-group");
    sec.append(el("div", "certs-client", client));
    const grid = el("div", "certs-grid");
    for (const b of byClient[client]) grid.append(renderCertBundle(kind, b));
    sec.append(grid);
    body.append(sec);
  }
}

function renderCertBundle(kind, b) {
  const card = el("div", "certcard");
  card.dataset.text = (b.client + " " + b.env + " " + b.name + " " + b.files.join(" ")).toLowerCase();
  const head = el("div", "certcard-head");
  head.append(el("div", "certcard-name", b.env === b.client ? b.name : b.env));
  head.append(el("span", "certcard-files",
    b.files.length + " file" + (b.files.length === 1 ? "" : "s") + (b.hasDetails ? " · details" : " · no details")));
  card.append(head);

  const actions = el("div", "certcard-actions");
  const setup = el("button", "btn-primary", kind === "kafka" ? "Cluster settings" : "Connection");
  const openBtn = el("button", "btn-ghost", "Open folder");
  openBtn.addEventListener("click", () => certOpen(kind, b.path, openBtn));
  actions.append(setup, openBtn);
  card.append(actions);

  const panel = el("div", "certpanel hidden");
  card.append(panel);
  let loaded = false;
  setup.addEventListener("click", async () => {
    const hidden = panel.classList.toggle("hidden");
    setup.textContent = hidden ? (kind === "kafka" ? "Cluster settings" : "Connection") : "Hide";
    if (!hidden && !loaded) { loaded = true; await loadCertPanel(kind, b, panel); }
  });
  return card;
}

async function loadCertPanel(kind, b, panel) {
  panel.innerHTML = "";
  panel.append(el("div", "jk-sub", "Reading connection details…"));
  let res;
  try { res = await api("/api/certs/connect?kind=" + kind + "&path=" + encodeURIComponent(b.path)); }
  catch (e) { panel.innerHTML = ""; panel.append(el("div", "jk-err", "✗ " + String(e))); return; }
  panel.innerHTML = "";
  if (!res.ok) { panel.append(el("div", "jk-err", "✗ " + (res.error || "failed"))); return; }
  if (kind === "kafka") renderKafkaPanel(panel, res, b);
  else renderMongoPanel(panel, res, b);
}

// one label / value row with optional Copy and reveal-for-secrets
function certRow(parent, label, value, opts) {
  opts = opts || {};
  const row = el("div", "cert-kv");
  row.append(el("span", "cert-k", label));
  const v = el("span", "cert-v" + (opts.mono ? " jk-mono" : ""));
  const shown = () => (opts.secret && v.dataset.hidden === "1") ? "••••••••" : (value || "—");
  v.textContent = shown();
  if (opts.secret) { v.dataset.hidden = "1"; v.textContent = "••••••••"; }
  row.append(v);
  if (value && opts.secret) {
    const r = el("button", "cert-mini", "reveal");
    r.addEventListener("click", () => {
      const hide = v.dataset.hidden !== "1";
      v.dataset.hidden = hide ? "1" : "0";
      v.textContent = hide ? "••••••••" : value;
      r.textContent = hide ? "reveal" : "hide";
    });
    row.append(r);
  }
  if (value && opts.copy !== false) {
    const c = el("button", "cert-mini", "Copy");
    c.addEventListener("click", () => copyText(opts.copyValue || value, c));
    row.append(c);
  }
  parent.append(row);
  return row;
}

function renderKafkaPanel(panel, res, b) {
  certRow(panel, "Cluster", res.cluster);
  certRow(panel, "Bootstrap", res.bootstrap, { mono: true });
  certRow(panel, "Security", res.security || "SSL", { copy: false });
  certRow(panel, "Truststore", res.truststore.file, { mono: true, copyValue: res.truststore.path });
  certRow(panel, "  ↳ password", res.truststore.password, { secret: true });
  certRow(panel, "Keystore", res.keystore.file, { mono: true, copyValue: res.keystore.path });
  certRow(panel, "  ↳ password", res.keystore.password, { secret: true });

  const all = [
    "Cluster name: " + res.cluster,
    "Bootstrap servers: " + res.bootstrap,
    "Security: SSL",
    "Truststore: " + res.truststore.path,
    "Truststore password: " + res.truststore.password,
    "Keystore: " + res.keystore.path,
    "Keystore password: " + res.keystore.password,
  ].join("\n");
  const row = el("div", "certcard-actions");
  if (certApps.offsetexplorerCli) {
    const add = el("button", "btn-primary", "Save to Offset Explorer");
    add.addEventListener("click", () => addToOffset(b.path, add));
    row.append(add);
  }
  const copyAll = el("button", certApps.offsetexplorerCli ? "btn-ghost" : "btn-primary", "Copy all");
  copyAll.addEventListener("click", () => copyText(all, copyAll));
  row.append(copyAll);
  if (certApps.offsetexplorer) {
    const open = el("button", "btn-ghost", "Open Offset Explorer");
    open.addEventListener("click", () => launchApp("offsetexplorer", null, open));
    row.append(open);
  }
  panel.append(row);
  panel.append(el("div", "cert-hint", certApps.offsetexplorerCli
    ? "“Save to Offset Explorer” adds this as an SSL cluster via its command-line tool. "
      + "If Offset Explorer is open it's closed and reopened automatically (it only loads connections at startup)."
    : "In Offset Explorer → Add Cluster: paste the Bootstrap servers, set Security to SSL, "
      + "and point the truststore/keystore at the files above with their passwords."));
}

function renderMongoPanel(panel, res, b) {
  certRow(panel, "Name", res.name);
  certRow(panel, "Host", (res.host || "—") + (res.port ? ":" + res.port : ""), { mono: true });
  if (res.user) certRow(panel, "User", res.user);
  if (res.authMech) certRow(panel, "Auth", res.authMech, { copy: false });
  if (res.databases && res.databases.length) certRow(panel, "Databases", res.databases.join(", "), { copy: false });
  if (res.ca.file) certRow(panel, "CA file", res.ca.file, { mono: true, copyValue: res.ca.path });
  if (res.clientPem.file) certRow(panel, "Client PEM", res.clientPem.file, { mono: true, copyValue: res.clientPem.path });

  const uriWrap = el("div", "cert-uri");
  uriWrap.append(el("div", "cert-k", "Connection string"));
  const ta = el("textarea", "cert-uri-box");
  ta.readOnly = true; ta.value = res.uri; ta.rows = 3;
  uriWrap.append(ta);
  panel.append(uriWrap);

  const row = el("div", "certcard-actions");
  if (certApps.compass) {
    const openc = el("button", "btn-primary", "Open in Compass");
    openc.addEventListener("click", () => launchApp("compass", b.path, openc));
    row.append(openc);
  }
  const copyUri = el("button", certApps.compass ? "btn-ghost" : "btn-primary", "Copy URI");
  copyUri.addEventListener("click", () => copyText(res.uri, copyUri));
  const compass = el("button", "btn-ghost", "Make Compass import file");
  compass.addEventListener("click", () => makeCompass(b.path, compass));
  row.append(copyUri, compass);
  panel.append(row);
  panel.append(el("div", "cert-hint", certApps.compass
    ? "“Open in Compass” launches Compass with this connection ready to connect. Or copy the "
      + "URI into Compass → New Connection."
    : "Paste the URI into Compass → New Connection. Or make the import file, then in "
      + "Compass: Connections (sidebar ⋯) → Import connections → pick that file."));
}

async function certOpen(kind, path, btn) {
  const label = btn.textContent;
  btn.disabled = true;
  try {
    const res = await api("/api/certs/open", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, path }),
    });
    if (res.ok) toast("Opened folder", "ok");
    else toast("Could not open", "err", res.error || "");
  } catch (e) { toast("Request failed", "err", String(e)); }
  finally { btn.disabled = false; btn.textContent = label; }
}

async function addToOffset(path, btn) {
  const label = btn.textContent;
  btn.disabled = true;
  // Closing + reopening OE takes a few seconds, so say so while it runs.
  btn.textContent = "Saving…";
  try {
    const res = await api("/api/certs/kafka-add", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    if (res.ok) {
      const name = res.cluster || "";
      const head = res.duplicate ? "Already in Offset Explorer: " + name
                                 : "Saved to Offset Explorer: " + name;
      const sub = res.closed
        ? (res.reopened ? "closed Offset Explorer, saved, and reopened it — it's in the cluster list"
                        : "saved — reopen Offset Explorer to see it")
        : "open Offset Explorer to see it";
      toast(head, "ok", sub);
    } else {
      toast("Not saved", "err", res.error || "Offset Explorer did not store the cluster");
    }
  } catch (e) { toast("Request failed", "err", String(e)); }
  finally { btn.disabled = false; btn.textContent = label; }
}

async function launchApp(app, path, btn) {
  const label = btn.textContent;
  btn.disabled = true;
  try {
    const res = await api("/api/certs/launch", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ app, path }),
    });
    if (res.ok) toast("Opening " + (app === "compass" ? "Compass" : "Offset Explorer") + "…", "ok");
    else toast("Could not open", "err", res.error || "");
  } catch (e) { toast("Request failed", "err", String(e)); }
  finally { btn.disabled = false; btn.textContent = label; }
}

async function makeCompass(path, btn) {
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = "Writing…";
  try {
    const res = await api("/api/certs/compass", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    if (res.ok) toast("Wrote " + res.file, "ok", "Compass → Import connections → pick this file");
    else toast("Could not write import file", "err", res.error || "");
  } catch (e) { toast("Request failed", "err", String(e)); }
  finally { btn.disabled = false; btn.textContent = label; }
}

function copyText(text, btn) {
  const done = () => { const t = btn.textContent; btn.textContent = "Copied"; setTimeout(() => (btn.textContent = t), 1200); };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
  } else { fallbackCopy(text, done); }
}
function fallbackCopy(text, done) {
  const ta = document.createElement("textarea");
  ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.append(ta); ta.select();
  try { document.execCommand("copy"); done(); } catch (e) { toast("Copy failed — select and copy manually", "err"); }
  ta.remove();
}

/* ---- Settings (credentials + dynamic sources) ---- */
async function renderSettingsView() {
  const main = $("#main");
  main.innerHTML = "";
  $("#filter").value = "";
  $("#filter").placeholder = "Settings";

  const head = el("div", "view-head");
  const back = el("button", "backbtn", "← Clients");
  back.addEventListener("click", showPicker);
  head.append(back);
  head.append(el("h1", "page-title", "Settings"));
  head.append(el("span", "count", "your logins & where the data comes from"));
  main.append(head);

  const body = el("div", "set-body");
  body.append(el("div", "jk-sub", "Loading…"));
  main.append(body);

  let data = {};
  try { data = await api("/api/settings"); } catch { /* offline */ }
  body.innerHTML = "";
  body.append(buildGithubSection(data));
  body.append(buildJenkinsSection(data));
  body.append(buildThemeSection());
  body.append(buildDisplaySection());
  body.append(buildBrowserSection(data));
  body.append(buildCertsSection(data));
  body.append(buildSourcesSection(data));
}

function buildThemeSection() {
  const current = (window.__themeChoice ? window.__themeChoice() : "auto");
  const sec = setSection("Theme", "How the launcher itself looks.");
  const form = el("div", "set-form");
  const choices = [
    ["auto", "Match system (recommended)",
      "Follows your OS light/dark setting, and switches live when it changes."],
    ["light", "Light", "Always use the light theme."],
    ["dark", "Dark", "Always use the dark theme."],
  ];
  for (const [val, title, desc] of choices) {
    const row = el("label", "set-radio");
    const radio = el("input"); radio.type = "radio"; radio.name = "themepref"; radio.value = val;
    if (val === current) radio.checked = true;
    radio.addEventListener("change", () => {
      if (window.__applyTheme) window.__applyTheme(val);
      toast("Theme set to " + title.replace(" (recommended)", ""), "ok");
    });
    const txt = el("div", "set-radio-txt");
    txt.append(el("div", "set-radio-title", title), el("div", "set-radio-desc", desc));
    row.append(radio, txt);
    form.append(row);
  }
  sec.append(form);
  return sec;
}

function buildDisplaySection() {
  const sec = setSection("Display",
    "An optional frosted-glass look that layers over whichever theme you pick.");
  const form = el("div", "set-form");
  const row = el("label", "set-radio");
  const cb = el("input"); cb.type = "checkbox"; cb.className = "set-check";
  cb.checked = !!(window.__glassOn && window.__glassOn());
  cb.addEventListener("change", () => {
    if (window.__applyGlass) window.__applyGlass(cb.checked);
    toast(cb.checked ? "Glass mode on" : "Glass mode off", "ok");
  });
  const txt = el("div", "set-radio-txt");
  txt.append(el("div", "set-radio-title", "Glass (frosted panels)"),
    el("div", "set-radio-desc",
      "Translucent, blurred panels over a soft gradient — works on top of light or dark."));
  row.append(cb, txt);
  form.append(row);
  sec.append(form);
  return sec;
}

function buildBrowserSection(data) {
  const s = (data.settings || {});
  const current = s.browser === "chrome" ? "chrome" : "default";
  const sec = setSection("Browser",
    "Which browser opens the Kibana / UI / NiFi links when you click them.");

  const form = el("div", "set-form");
  const choices = [
    ["default", "System default browser (recommended)",
      "Opens in whatever browser your system is set to (Firefox, Edge, Chrome, …) via xdg-open."],
    ["chrome", "Google Chrome / Chromium",
      "Forces Chrome/Chromium — opens in your logged-in Chrome profile so you stay signed in to Kibana/NiFi."],
  ];
  for (const [val, title, desc] of choices) {
    const row = el("label", "set-radio");
    const radio = el("input"); radio.type = "radio"; radio.name = "browserpref"; radio.value = val;
    if (val === current) radio.checked = true;
    radio.addEventListener("change", async () => {
      try {
        const r = await api("/api/settings", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ browser: val }),
        });
        if (r.ok) toast("Links now open in " + (val === "default" ? "your system default browser" : "Chrome"), "ok");
        else toast("Save failed", "err", r.error || "");
      } catch (e) { toast("Save failed", "err", String(e)); }
    });
    const txt = el("div", "set-radio-txt");
    txt.append(el("div", "set-radio-title", title), el("div", "set-radio-desc", desc));
    row.append(radio, txt);
    form.append(row);
  }
  sec.append(form);
  return sec;
}

function setSection(title, subtitle) {
  const sec = el("section", "set-section");
  sec.append(el("h2", "set-h", title));
  if (subtitle) sec.append(el("div", "set-sub", subtitle));
  return sec;
}

// Read-only view (non-sensitive values) + an Edit button that reveals `form`
// (which holds the editable inputs + Save). Cancel restores the view. A
// successful Save re-renders the whole Settings view, which lands back here.
function editToggle(sec, viewRows, form) {
  const view = el("div", "set-view");
  for (const [label, value, dim] of viewRows) {
    const row = el("div", "set-vrow");
    row.append(el("div", "set-vlabel", label));
    row.append(el("div", "set-vval" + (dim ? " set-dim" : ""), value));
    view.append(row);
  }
  const editBtn = el("button", "btn-ghost", "Edit");
  const editRow = el("div", "set-editrow"); editRow.append(editBtn);
  view.append(editRow);

  const cancel = el("button", "btn-ghost", "Cancel");
  form.append(cancel);
  form.classList.add("hidden");

  editBtn.addEventListener("click", () => {
    view.classList.add("hidden"); form.classList.remove("hidden");
    const first = form.querySelector("input"); if (first) first.focus();
  });
  cancel.addEventListener("click", () => {
    form.classList.add("hidden"); view.classList.remove("hidden");
  });
  sec.append(view, form);
}

function buildGithubSection(data) {
  const gh = (data.github || {});
  const sec = setSection("GitHub",
    "Used to sync the wiki links and list repos. A copy on another machine needs its own token.");

  const status = el("div", "set-status");
  if (gh.configured) status.append(el("span", "jk-ok", "✓ token saved" + (gh.user ? " for " + gh.user : "")));
  else if (data.ghCli) status.append(el("span", "jk-ok", "✓ using the gh CLI login (no token needed)"));
  else status.append(el("span", "jk-warn", "⚠ no GitHub token and no gh CLI — the Repos list and wiki sync won't work"));
  sec.append(status);

  const user = el("input", "jk-input"); user.value = gh.user || ""; user.placeholder = "GitHub username (optional)";
  const token = el("input", "jk-input"); token.type = "password"; token.placeholder = "Personal access token (repo scope)";
  const save = el("button", "btn-primary", "Save GitHub token");
  save.addEventListener("click", async () => {
    if (!token.value.trim()) { toast("Paste a token", "info"); return; }
    save.disabled = true;
    try {
      const r = await api("/api/github/auth", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user: user.value.trim(), token: token.value }),
      });
      if (r.ok) { toast("GitHub token saved — press Sync to re-pull", "ok"); renderSettingsView(); }
      else toast("Save failed", "err", r.error || "");
    } catch (e) { toast("Save failed", "err", String(e)); }
    finally { save.disabled = false; }
  });
  const form = el("div", "set-form");
  const hint = el("div", "jk-hint");
  hint.textContent = "Create one at github.com/settings/tokens (classic, with 'repo' scope). Leave blank to keep using the gh CLI login.";
  form.append(labelRow("Username", user), labelRow("Token", token), hint, save);

  editToggle(sec, [
    ["Username", gh.user || "—"],
    ["Token", gh.configured ? "•••••••• saved" : "not set", true],
  ], form);
  return sec;
}

function buildJenkinsSection(data) {
  const jk = (data.jenkins || {});
  const sec = setSection("Jenkins",
    "Used by the Jenkins troubleshooter to read build logs. Stored locally (chmod 600).");

  const status = el("div", "set-status");
  if (jk.configured) status.append(el("span", "jk-ok", "✓ signed in as " + jk.user + " · " + jk.base));
  else status.append(el("span", "jk-warn", "⚠ not set"));
  sec.append(status);

  const base = el("input", "jk-input"); base.value = jk.base || "https://jenkins.devops.lmvi.net";
  const user = el("input", "jk-input"); user.value = jk.user || "";
  const token = el("input", "jk-input"); token.type = "password"; token.placeholder = "API token or password";
  const vpn = el("input", "jk-input"); vpn.value = jk.vpn || "DEVOPS";
  const save = el("button", "btn-primary", "Save Jenkins login");
  save.addEventListener("click", async () => {
    if (!user.value.trim() || !token.value.trim()) { toast("Enter user and token", "info"); return; }
    save.disabled = true;
    try {
      const r = await api("/api/jenkins/auth", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ base: base.value.trim(), user: user.value.trim(), token: token.value, vpn: vpn.value.trim() }),
      });
      if (r.ok) { toast("Jenkins login saved", "ok"); renderSettingsView(); }
      else toast("Save failed", "err", r.error || "");
    } catch (e) { toast("Save failed", "err", String(e)); }
    finally { save.disabled = false; }
  });
  const form = el("div", "set-form");
  const hint = el("div", "jk-hint");
  hint.textContent = "VPN is the NetworkManager connection name to bring up before fetching (e.g. DEVOPS). Use an API token (not your password) — /me/configure → API Token.";
  form.append(labelRow("Jenkins URL", base), labelRow("User", user),
    labelRow("Token", token), labelRow("VPN", vpn), hint, save);

  editToggle(sec, [
    ["Jenkins URL", jk.base || "https://jenkins.devops.lmvi.net"],
    ["User", jk.user || "—"],
    ["VPN", jk.vpn || "DEVOPS"],
    ["Token", jk.configured ? "•••••••• saved" : "not set", true],
  ], form);
  return sec;
}

function buildSourcesSection(data) {
  const s = (data.settings || {});
  const sec = setSection("Sources (advanced)",
    "Where the data comes from. Change these only to point this tool at a different org / Jenkins.");

  const org = el("input", "jk-input"); org.value = s.github_org || "";
  const wiki = el("input", "jk-input jk-urlinput"); wiki.value = s.wiki_url || "";
  const suffix = el("input", "jk-input"); suffix.value = s.jenkins_host_suffix || "";
  const save = el("button", "btn-primary", "Save sources");
  save.addEventListener("click", async () => {
    save.disabled = true;
    try {
      const r = await api("/api/settings", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ github_org: org.value.trim(), wiki_url: wiki.value.trim(), jenkins_host_suffix: suffix.value.trim() }),
      });
      if (r.ok) { toast("Sources saved — press Sync to re-pull", "ok"); renderSettingsView(); }
      else toast("Save failed", "err", r.error || "");
    } catch (e) { toast("Save failed", "err", String(e)); }
    finally { save.disabled = false; }
  });
  const form = el("div", "set-form");
  const hint = el("div", "jk-hint");
  hint.textContent = "Jenkins host suffix is a safety allowlist — the server only fetches build logs from hosts ending in this domain.";
  form.append(labelRow("GitHub org", org), labelRow("Wiki repo URL", wiki),
    labelRow("Jenkins host suffix", suffix), hint, save);

  editToggle(sec, [
    ["GitHub org", s.github_org || "—"],
    ["Wiki repo URL", s.wiki_url || "—"],
    ["Jenkins host suffix", s.jenkins_host_suffix || "—"],
  ], form);
  return sec;
}

function buildCertsSection(data) {
  const s = (data.settings || {});
  const sec = setSection("Certs (Kafka / Mongo)",
    "Local folders of downloaded cert bundles the Kafka and Mongo sections scan.");

  const kafka = el("input", "jk-input jk-urlinput"); kafka.value = s.kafka_certs_dir || "";
  const mongo = el("input", "jk-input jk-urlinput"); mongo.value = s.mongo_certs_dir || "";
  const searchHome = el("input"); searchHome.type = "checkbox"; searchHome.checked = !!s.certs_search_home;
  const save = el("button", "btn-primary", "Save cert folders");
  save.addEventListener("click", async () => {
    save.disabled = true;
    try {
      const r = await api("/api/settings", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          kafka_certs_dir: kafka.value.trim(), mongo_certs_dir: mongo.value.trim(),
          certs_search_home: searchHome.checked,
        }),
      });
      if (r.ok) { toast("Cert folders saved", "ok"); renderSettingsView(); }
      else toast("Save failed", "err", r.error || "");
    } catch (e) { toast("Save failed", "err", String(e)); }
    finally { save.disabled = false; }
  });
  const form = el("div", "set-form");
  const searchRow = el("label", "set-radio");
  searchRow.append(searchHome);
  const stxt = el("div", "set-radio-txt");
  stxt.append(el("div", "set-radio-title", "Search my whole home folder"),
    el("div", "set-radio-desc", "Scan all of ~ for cert bundles instead of only the two folders above (slower)."));
  searchRow.append(stxt);
  const hint = el("div", "jk-hint");
  hint.textContent = "Point these at where you downloaded the Kafka and Mongo cert bundles (e.g. ~/SOFT/kafka, ~/SOFT/mongo).";
  form.append(labelRow("Kafka certs folder", kafka), labelRow("Mongo certs folder", mongo),
    searchRow, hint, save);

  editToggle(sec, [
    ["Kafka certs folder", s.kafka_certs_dir || "—"],
    ["Mongo certs folder", s.mongo_certs_dir || "—"],
    ["Search whole home", s.certs_search_home ? "yes" : "no"],
  ], form);
  return sec;
}

function renderEnv(client, env, titleOverride) {
  const card = el("div", "envcard");
  card.dataset.vpn = env.vpn || "";
  card.dataset.text = (client.name + " " + env.name + " " + env.vpn + " " +
    env.links.map(l => l.label).join(" ")).toLowerCase();

  const head = el("div", "env-head");
  head.append(el("span", "env-conn"));
  head.append(el("span", "env-name", titleOverride || env.name));
  if (titleOverride) head.append(el("span", "env-sub", env.name));
  if (env.vpn) head.append(el("span", "vpn-tag", "⛨ " + env.vpn));
  card.append(head);

  const links = el("div", "links");
  for (const link of env.links) links.append(renderLink(env, link));
  card.append(links);
  return card;
}

function renderLink(env, link) {
  const b = el("button", "linkbtn t-" + link.type);
  b.append(el("span", "swatch"));
  // compq links carry cryptic wiki names (app-es, Kibana-compq-index…); they all
  // point at the app Elasticsearch/Kibana, so show one clear label.
  const label = link.type === "kibana-compq" ? "Kibana (App)" : link.label;
  b.append(el("span", "lbl", label));
  b.append(el("span", "tp", TYPE_LABEL[link.type] || link.type));
  b.title = link.url;
  b.addEventListener("click", () => openLink(b, env, link));
  return b;
}

/* ---- navigation (hash-driven, so clients are bookmarkable) ---- */
function selectClient(name) { location.hash = encodeURIComponent(name); }
function showPicker() { location.hash = ""; }

function syncFromHash() {
  const want = decodeURIComponent(location.hash.replace(/^#/, ""));
  let next = null;
  if (want === PROD_KEY) next = PROD_KEY;
  else if (want === REPOS_KEY) next = REPOS_KEY;
  else if (want === JENKINS_KEY) next = JENKINS_KEY;
  else if (want === KAFKA_KEY) next = KAFKA_KEY;
  else if (want === MONGO_KEY) next = MONGO_KEY;
  else if (want === SETTINGS_KEY) next = SETTINGS_KEY;
  else if (CONFIG.clients.some(c => c.name === want)) next = want;
  if (next !== selected) { selected = next; $("#filter").value = ""; }
  render();
  window.scrollTo({ top: 0 });
}

/* ---- actions ---- */
async function openLink(btn, env, link) {
  btn.classList.add("busy");
  const needVpn = env.vpn && !activeVpns.has(env.vpn);
  if (needVpn) toast("Connecting VPN " + env.vpn + "…", "info");
  try {
    const res = await api("/api/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ vpn: env.vpn || null, url: link.url }),
    });
    if (res.ok) {
      const connected = env.vpn && res.vpnMessage !== "already connected";
      toast("Opened " + link.label, "ok", env.vpn ? (connected ? "VPN " + env.vpn + " connected" : env.vpn) : "");
      btn.classList.remove("opened"); void btn.offsetWidth; btn.classList.add("opened");
      setTimeout(() => btn.classList.remove("opened"), 1200);
      if (env.vpn) { activeVpns.add(env.vpn); refreshConnected(); }
      pollStatus();
    } else {
      toast("Could not open " + link.label, "err", res.error || "");
    }
  } catch (e) {
    toast("Request failed", "err", String(e));
  } finally {
    btn.classList.remove("busy");
  }
}

async function disconnect(vpn) {
  toast("Disconnecting " + vpn + "…", "info");
  const res = await api("/api/vpn/disconnect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ vpn }),
  });
  if (res.ok) { activeVpns.delete(vpn); toast(vpn + " disconnected", "ok"); }
  else toast("Disconnect failed", "err", res.error || "");
  pollStatus();
}

/* ---- status ---- */
async function pollStatus() {
  try {
    const res = await api("/api/vpn/status");
    activeVpns = new Set(res.active || []);
  } catch { /* ignore */ }
  renderStatus();
  refreshConnected();
}

function renderStatus() {
  const box = $("#status");
  box.innerHTML = "";
  for (const vpn of activeVpns) {
    const chip = el("span", "vchip");
    chip.append(el("span", "dot"));
    chip.append(el("span", null, vpn));
    const x = el("button", null, "×");
    x.title = "Disconnect " + vpn;
    x.addEventListener("click", () => disconnect(vpn));
    chip.append(x);
    box.append(chip);
  }
}

function refreshConnected() {
  document.querySelectorAll(".envcard").forEach(card => {
    const v = card.dataset.vpn;
    card.classList.toggle("connected", !!v && activeVpns.has(v));
  });
  // reflect live state on picker env chips too
  document.querySelectorAll(".cc-env").forEach(chip => {
    const v = chip.dataset.vpn;
    chip.classList.toggle("on", !!v && activeVpns.has(v));
  });
}

/* ---- filter ---- */
function applyFilter(q) {
  q = q.trim().toLowerCase();
  if (selected === REPOS_KEY) {
    document.querySelectorAll(".repoitem").forEach(card => {
      card.classList.toggle("hidden", !!q && !card.dataset.text.includes(q));
    });
    document.querySelectorAll(".repo-bucket").forEach(sec => {
      const anyVisible = [...sec.querySelectorAll(".repoitem")]
        .some(c => !c.classList.contains("hidden"));
      sec.classList.toggle("hidden", !anyVisible);
    });
  } else if (selected) {
    document.querySelectorAll(".envcard").forEach(card => {
      card.classList.toggle("hidden", !!q && !card.dataset.text.includes(q));
    });
  } else {
    document.querySelectorAll(".clientcard").forEach(card => {
      card.classList.toggle("hidden", !!q && !card.dataset.text.includes(q));
    });
  }
}

/* ---- sync ---- */
async function doSync(btn) {
  btn.disabled = true;
  btn.textContent = "Syncing…";
  try {
    const res = await api("/api/sync", { method: "POST" });
    if (res.ok) { toast("Synced links & reloaded", "ok"); await loadConfig(); }
    else toast("Sync failed", "err", (res.output || res.error || "").slice(-200));
  } catch (e) {
    toast("Sync request failed", "err", String(e));
  } finally {
    btn.disabled = false; btn.textContent = "Sync";
  }
}

/* ---- boot ---- */
async function loadFavorites() {
  try {
    const r = await api("/api/favorites");
    favorites = new Set(r.repos || []);
  } catch { /* ignore — favorites just start empty */ }
}

async function loadConfig() {
  await loadFavorites();
  CONFIG = await api("/api/config");
  syncFromHash();
}

function init() {
  $("#filter").addEventListener("input", e => applyFilter(e.target.value));
  $("#sync").addEventListener("click", e => doSync(e.currentTarget));
  window.addEventListener("hashchange", syncFromHash);
  document.addEventListener("keydown", e => {
    if (e.key === "/" && document.activeElement !== $("#filter")) {
      e.preventDefault(); $("#filter").focus();
    }
    if (e.key === "Escape") {
      if ($("#filter").value) { $("#filter").value = ""; applyFilter(""); }
      else if (selected) showPicker();
    }
  });
  loadConfig();
  pollStatus();
  setInterval(pollStatus, 6000);
}

document.addEventListener("DOMContentLoaded", init);
