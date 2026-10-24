"use strict";
const ST = ["backlog", "todo", "in_progress", "review", "blocked", "done", "declined"];
const LBL = {backlog: "Backlog", todo: "To do", in_progress: "In progress", review: "Review", blocked: "Blocked", done: "Done", declined: "Declined"};
const HINT = {backlog: "Ideas and unscheduled work", todo: "Ready to pick up", in_progress: "Nothing in flight", review: "Nothing awaiting review", blocked: "No blockers", done: "Finished work lands here", declined: "Owner declined — reason on card"};
const CHANNELS = [];
const SUG_LB = ["bug", "feature", "chore", "docs", "gate", "next", "blocked-external"];
const LB_DISP = {nudge: "Nudge"};
const lbShow = l => LB_DISP[l] || l;
const BASE = (location.pathname.match(/^(\/taskboard(?=\/|$))/) || ["", ""])[1];
// short event keys -> full task/sprint/board fields
const KM = {
  task: {cs: "cost", tk: "tokens", b: "board_id", s: "sprint_id", t: "title", d: "description", st: "status", pr: "priority", as: "assignee", k: "kind", u: "updated", bl: "blocker", bb: "blocked_by", lb: "labels", ch: "channel", w: "watch", sh: "short_url", vid: "video_id", at: "publish_at", pu: "pr_url", sha: "sha", cm: "commits", dp: "depends", pt: "points", cb: "claimed_by", cu: "claim_until", v: "version", dn: "dod_done", ty: "type", pa: "parent", tg: "target", cr: "current"},
  sprint: {b: "board_id", n: "name", g: "goal", from: "start", to: "end", st: "state"},
  board: {n: "name", d: "description", tm: "team"},
};
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const csv = v => (Array.isArray(v) ? v : String(v || "").split(",")).map(x => String(x).trim()).filter(Boolean);
const initials = n => n.trim().split(/[\s._-]+/).slice(0, 2).map(w => w[0] || "").join("").toUpperCase();
const hue = n => { let h = 0; for (const c of n) h = (h * 31 + c.charCodeAt(0)) % 360; return h; };
let tok = new URLSearchParams(location.search).get("token") || localStorage.tb_token;
if (tok === null || tok === undefined) tok = prompt("Taskboard token (blank if none)") || "";
localStorage.tb_token = tok;
const picked = new Set();
let boards = [], B = null, es = null, dragId = null, toastT = null, autoSprint = true, view = localStorage.tb_view || "board", feed = [], dashT = null;

function setTheme(t) { document.documentElement.dataset.theme = t; localStorage.tb_theme = t; }
document.documentElement.dataset.theme = localStorage.tb_theme || (matchMedia("(prefers-color-scheme:dark)").matches ? "dark" : "light");

function toast(m, label, fn) {
  let t = $("toast");
  if (!t) { t = document.createElement("div"); t.id = "toast"; t.className = "toast"; t.setAttribute("role", "status"); document.body.appendChild(t); }
  t.textContent = m; t.hidden = false;
  if (label) { const b = document.createElement("button"); b.type = "button"; b.className = "toastbtn"; b.textContent = label; b.onclick = () => { t.hidden = true; fn(); }; t.append(" ", b); }
  clearTimeout(toastT); toastT = setTimeout(() => { t.hidden = true; }, label ? 8000 : 4000);
}

const sleep = ms => new Promise(r => setTimeout(r, ms));
async function api(m, p, b) {
  const sep = p.includes("?") ? "&" : "?";
  for (let i = 0; ; i++) {
    let r, j;
    try {
      r = await fetch(BASE + "/api" + p + sep + "full=1", {
        method: m, headers: {Authorization: "Bearer " + tok, "Content-Type": "application/json"},
        body: b ? JSON.stringify(b) : undefined,
      });
      j = await r.json();
    } catch (_) {                                   // dropped connection or a non-JSON error page from a proxy: reads are safe to retry
      if (m === "GET" && i < 3) { await sleep(300 * 3 ** i); continue; }
      toast("Could not reach the server — check your connection"); throw {e: "net"};
    }
    if (!r.ok) {
      if (m === "GET" && r.status >= 500 && i < 3) { await sleep(300 * 3 ** i); continue; }
      if (j.e === "auth") { localStorage.removeItem("tb_token"); toast("Auth failed — reload and enter a valid token"); }
      else toast("Error: " + j.e + (j.f ? " (" + j.f + ")" : ""));
      throw j;
    }
    return j;
  }
}

const TEAM_LABELS = {};
const teamLabel = t => TEAM_LABELS[t] || ("All " + t);
let BS = {};
const scope = () => { const v = $("board").value || "all"; return v === "all" ? {k: "all"} : v.startsWith("team:") ? {k: "team", team: v.slice(5)} : {k: "b", id: +v.replace(/^b:/, "")}; };

async function loadBoards(sel) {
  boards = await api("GET", "/boards");
  const s = $("board"), teams = [...new Set(boards.map(b => b.team).filter(Boolean))].sort();
  s.innerHTML = boards.length ? `<option value="all">All boards (${boards.length})</option>` +
    (teams.length ? `<optgroup label="By type">${teams.map(t => `<option value="team:${esc(t)}">${esc(teamLabel(t))} (${boards.filter(b => b.team === t).length})</option>`).join("")}</optgroup>` : "") +
    `<optgroup label="Boards">${boards.map(b => `<option value="b:${b.id}">${esc(b.name)}</option>`).join("")}</optgroup>` : "<option value=''>(no boards)</option>";
  let want = sel ? (typeof sel === "number" ? "b:" + sel : sel) : (localStorage.tb_scope || "all");
  if (!/^(all|team:|b:)/.test(want) || ![...s.options].some(o => o.value === want)) want = "all";
  if (boards.length) { s.value = want; await loadBoard(); }
  else { B = null; render(); }
}

let loadSeq = 0;
async function loadBoard() {
  const my = ++loadSeq, sc = scope();
  try { localStorage.tb_scope = $("board").value; } catch (_) {}
  if (sc.k === "b") { const one = await api("GET", "/boards/" + sc.id); if (my !== loadSeq) return; B = one; BS = {[B.id]: B}; }
  else {
    const list = boards.filter(b => sc.k === "all" || b.team === sc.team);
    const snaps = [];
    for (let i = 0; i < list.length; i += 4) snaps.push(...await Promise.all(list.slice(i, i + 4).map(b => api("GET", "/boards/" + b.id))));   // a few at a time
    if (my !== loadSeq) return;
    BS = {}; snaps.forEach(x => { BS[x.id] = x; });
    B = {id: 0, multi: true, name: sc.k === "all" ? "All boards" : teamLabel(sc.team), scopeTeam: sc.k === "team" ? sc.team : "", method: "multi", sprints: [], cfg: "", dod: "",
         tasks: snaps.flatMap(x => x.tasks), boardIds: snaps.map(x => x.id)};
  }
  const cur = $("sprint").value, sp = $("sprint");
  sp.innerHTML = '<option value="">All tasks</option><option value="none">No sprint</option>' +
    B.sprints.map(s => `<option value="${s.id}">${esc(s.name)} · ${s.state}</option>`).join("");
  const act = B.sprints.filter(x => x.state === "active").pop();
  sp.value = autoSprint ? (act ? String(act.id) : "") : ([...sp.options].some(o => o.value === cur) ? cur : "");
  const as = $("asg"), ac = as.value;
  as.innerHTML = '<option value="">Everyone</option><option value="none">Unassigned</option>' +
    [...new Set(B.tasks.map(t => t.assignee).filter(Boolean))].sort().map(a => `<option value="${esc(a)}">${esc(a)}</option>`).join("");
  as.value = [...as.options].some(o => o.value === ac) ? ac : "";
  const chs = $("chan"), cc = chs.value;
  const used = [...new Set(B.tasks.map(t => t.channel).filter(Boolean))];
  chs.innerHTML = '<option value="">All areas</option><option value="none">No area</option>' +
    [...new Set([...CHANNELS, ...used])].map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
  chs.value = [...chs.options].some(o => o.value === cc) ? cc : "";
  render(); connect();
}

const query = () => $("search").value.trim().toLowerCase();
let preset = localStorage.tb_preset || "";
function applyPreset(key, persist) {
  if (key === preset && persist !== false) key = ""; // toggle off
  preset = key || "";
  if (persist !== false) localStorage.tb_preset = preset;
  document.querySelectorAll("#presets .preset").forEach(b => b.classList.toggle("on", b.dataset.preset === preset));
  render();
}
function hiddenReasons(t) {
  const r = [], sp = $("sprint").value, q = query(), ch = $("chan").value.toLowerCase(), ag = $("asg").value, p = preset;
  if (sp !== "" && !(sp === "none" ? !t.sprint_id : t.sprint_id == sp)) { const x = B.sprints.find(y => y.id == t.sprint_id); r.push(t.sprint_id ? "it is in " + (x ? x.name : "another sprint") : "it has no sprint"); }
  if (ag !== "" && !(ag === "none" ? !t.assignee : t.assignee === ag)) r.push("the assignee filter");
  if (ch !== "" && !(ch === "none" ? !t.channel : (t.channel || "").toLowerCase() === ch)) r.push("the area filter");
  if (q && !(t.title + " " + (t.channel || "") + " " + (t.assignee || "") + " " + csv(t.labels).join(" ") + " #" + t.id).toLowerCase().includes(q)) r.push("the search box");
  if (p) r.push(`the "${p}" filter`);
  return r;
}
function showTask(t) {
  $("search").value = ""; $("asg").value = ""; $("chan").value = ""; autoSprint = false;
  $("sprint").value = t.sprint_id ? String(t.sprint_id) : "";
  applyPreset("");
  const c = document.querySelector(`.card[data-id="${t.id}"]`);
  if (c) { c.scrollIntoView({block: "center"}); c.classList.add("flash"); setTimeout(() => c.classList.remove("flash"), 2200); }
}
async function ensureVisible(id) {
  for (let i = 0; i < 14 && !(B && B.tasks.some(t => t.id == id)); i++) await new Promise(r => setTimeout(r, 150));     // wait for the live update to arrive
  if (!B || view !== "board") return;
  const t = B.tasks.find(x => x.id == id);
  if (!t || visible().some(x => x.id == id)) return;
  const why = hiddenReasons(t);
  toast(`#${id} is saved but hidden by your view${why.length ? ": " + why.join(", ") : ""}.`, "Show it", () => showTask(t));
}

function visible() {
  const sp = $("sprint").value, q = query(), ch = $("chan").value.toLowerCase(), p = preset, ag = $("asg").value;
  return B.tasks.filter(t => (ag === "" || (ag === "none" ? !t.assignee : t.assignee === ag)) && (sp === "" || (sp === "none" ? !t.sprint_id : t.sprint_id == sp)) &&
    (ch === "" || (ch === "none" ? !t.channel : (t.channel || "").toLowerCase() === ch)) &&
    (!q || (t.title + " " + (t.channel || "") + " " + (t.assignee || "") + " " + csv(t.labels).join(" ") + " #" + t.id).toLowerCase().includes(q)) &&
    (p === "" || (p === "open" && t.status !== "done" && t.status !== "declined") ||
      (p === "blocked" && t.status === "blocked") ||
      (p === "gate" && csv(t.labels).map(l => l.toLowerCase()).includes("gate")) ||
      (p === "owner" && /owner/i.test(t.assignee || "")) ||
      (p === "nudged" && (!!t.nudge || csv(t.labels).map(l => l.toLowerCase()).includes("nudge")))));
}


function isOwnerCard(t) { return false; }

const REASONS = ["Out of scope", "Duplicate", "Needs more information", "Not now - revisit later", "Won't do"];
function declineTask(id) {
  const t = B.tasks.find(x => x.id == id); if (!t) return;
  const prev = ["todo", "in_progress", "backlog", "review"].includes(t.status) ? t.status : "todo";
  dlg(`<h3>Decline #${id}</h3><p class="muted hint">${esc(t.title)}${t.assignee ? " · " + esc(t.assignee) : ""}. The reason is saved on the task and the assignee is notified.</p>
    <label>Reason (required)<textarea name="reason" required maxlength="500" placeholder="Be concrete: why, and what would change your mind"></textarea></label>
    <div class="reasons">${REASONS.map(r => `<button type="button" class="chipbtn" data-reason="${esc(r)}">${esc(r)}</button>`).join("")}</div>
    <label>Also notify (agent names, comma separated — optional)<input name="to" placeholder="e.g. the requester"></label>`,
    async d => { await api("POST", `/tasks/${id}/decline`, {r: d.reason, to: csv(d.to)}); toast(`#${id} declined`, "Undo", () => api("POST", `/tasks/${id}/reopen`, {to: prev})); },
    "", "Decline task");
  $("f").onclick = e => { const b = e.target.closest("[data-reason]"); if (b) { const ta = $("f").reason; ta.value = b.dataset.reason; dirty = true; ta.focus(); } };
}

function reopenTask(id) {
  const t = B.tasks.find(x => x.id == id); if (!t) return;
  dlg(`<h3>Reopen #${id}</h3><p class="muted hint">${esc(t.title)} · now ${esc(LBL[t.status])}. Clears the ${t.status === "declined" ? "decline reason" : t.status === "blocked" ? "blocker" : "done state"}.</p>
    <label>Put it back as<select name="to">${opt(["todo", "in_progress", "backlog", "review"], "todo")}</select></label>
    <label>Note (optional)<input name="note" maxlength="500" placeholder="Why is it back?"></label>
    <label>Also notify (agent names, comma separated — optional)<input name="notify"></label>`,
    async d => { await api("POST", `/tasks/${id}/reopen`, {to: d.to, note: d.note, notify: csv(d.notify)}); toast(`#${id} reopened as ${LBL[d.to]}`); },
    "", "Reopen task");
}

function card(t) {
  const lbs = csv(t.labels), ag = t.kind === "agent", bb = csv(t.blocked_by), own = isOwnerCard(t);
  const links = [];
  if (t.pr_url) links.push(`<a class="chip link" href="${esc(t.pr_url)}" target="_blank" rel="noopener" title="PR">PR</a>`);
  const meta = [];
  if (t.sha) meta.push(`<span class="chip" title="SHA / deployed">${esc(t.sha.length > 12 ? t.sha.slice(0, 12) + "…" : t.sha)}</span>`);
  const od = csv(t.depends).filter(i => { const x = B && B.tasks.find(y => y.id == i); return x && x.status !== "done" && x.status !== "declined"; });
  if (od.length) meta.push(`<span class="chip dep" title="Waiting on open dependencies">⛓ ${od.map(i => "#" + i).join(" ")}</span>`);
  if (B && B.multi && BS[t.board_id]) meta.unshift(`<span class="chip bd" style="--h:${hue(BS[t.board_id].name.toLowerCase())}" title="Board">${esc(BS[t.board_id].name)}</span>`);
  if (t.type && t.type !== "task") meta.push(`<span class="chip ty ${esc(t.type)}">${esc(t.type)}</span>`);
  if (t.parent) { const ep = B && B.tasks.find(y => y.id == t.parent); meta.push(`<span class="chip epic" title="Epic">epic: ${ep ? esc(ep.title.slice(0, 18)) : "#" + t.parent}</span>`); }
  if (t.type === "epic" && B) { const ks = B.tasks.filter(y => y.parent == t.id); if (ks.length) meta.push(`<span class="chip" title="Epic progress">${ks.filter(y => y.status === "done").length}/${ks.length} done</span>`); }
  if (meth().sla && t.status !== "done" && t.status !== "declined" && t.created) {
    const sla = ({1: 4, 2: 24, 3: 72, 4: 168, ...(bcfg().sla_hours || {})})[t.priority] || 72, left = sla - (Date.now() - Date.parse(t.created.replace(" ", "T") + "Z")) / 3600000;
    meta.push(`<span class="chip sla ${left < 0 ? "bad" : left < sla * 0.25 ? "warn" : "ok"}" title="Response target ${sla}h">${left < 0 ? "SLA breached " + hrs(-left) : "SLA " + hrs(left) + " left"}</span>`);
  }
  if (t.points) meta.push(`<span class="chip" title="Points">${t.points}pt</span>`);
  if (t.cost) meta.push(`<span class="chip cost" title="Agent spend on this task">${money(t.cost)}</span>`);
  if (t.claimed_by && (t.claim_until || "") > new Date().toISOString().slice(0, 19).replace("T", " ")) meta.push(`<span class="chip claim" title="Claimed until ${esc(t.claim_until)} UTC">claimed · ${esc(t.claimed_by)}</span>`);
  if (t.nudge || csv(t.labels).map(l => l.toLowerCase()).includes("nudge"))
    meta.push(`<span class="chip nudge" title="Stale — nudged ${esc(t.nudge || "")}">Nudge</span>`);
  const cms = (t.commits || []).map(c => {
    const h = c.h || c.sha || "", u = c.u || c.url || "", m = c.m || c.msg || "", sh = h.slice(0, 7) || "commit";
    const ok = /^https:\/\//.test(u);
    return `<div class="cm">${ok ? `<a class="chip link" href="${esc(u)}" target="_blank" rel="noopener" title="Commit">${esc(sh)}</a>` : `<span class="chip">${esc(sh)}</span>`} <span class="cmm">${esc(m)}</span></div>`;
  }).join("");
  const acts = [];
  const soft = own ? "" : " soft";   // Owner cards always show the buttons; others reveal them on hover/focus
  if (t.status !== "declined" && t.status !== "done")
    acts.push(`<button type="button" class="act decline${soft}" data-decline="${t.id}" title="Decline with a reason">Decline</button>`);
  if (t.status === "declined" || t.status === "blocked" || t.status === "done")
    acts.push(`<button type="button" class="act reopen${soft}" data-reopen="${t.id}" title="Put it back in flight">Reopen</button>`);
  return `<article class="card pr${t.priority} ${t.kind}${t.status === "declined" ? " declined" : ""}" draggable="true" tabindex="0" data-id="${t.id}">
    <input type="checkbox" class="sel" data-sel="${t.id}" ${picked.has(t.id) ? "checked" : ""} aria-label="Select #${t.id}">
    <span class="pdot" title="Priority ${t.priority}"></span>
    <div class="t">${esc(t.title)}</div>
    ${t.description ? `<div class="ds">${esc(t.description)}</div>` : ""}
    ${t.status === "blocked" && (t.blocker || bb.length) ? `<div class="blk"><span>⛔ ${esc(t.blocker)}${bb.length ? ` (waiting on ${bb.map(i => "#" + esc(i)).join(" ")})` : ""}</span></div>` : ""}
    ${t.status === "declined" ? `<div class="blk declined"><span>✖ Declined${t.decline_reason ? ": " + esc(t.decline_reason) : ""}</span></div>` : ""}
    ${lbs.length ? `<div class="labels">${lbs.map(l => `<span class="lbl">${esc(lbShow(l))}</span>`).join("")}</div>` : ""}
    ${links.length || meta.length ? `<div class="m links">${links.join("")}${meta.join("")}</div>` : ""}
    ${cms ? `<div class="cms">${cms}</div>` : ""}
    ${acts.length ? `<div class="acts">${acts.join("")}</div>` : ""}
    <div class="m">${t.channel ? `<span class="chip chan" title="Area" style="--h:${hue(t.channel.toLowerCase())}">${esc(t.channel)}</span>` : ""}${ag ? '<span class="chip agent">agent</span>' : ""}</div>
    <div class="foot">${t.assignee ? `<span class="avatar${ag ? " ag" : ""}" aria-hidden="true">${esc(initials(t.assignee))}</span><span class="who">${esc(t.assignee)}</span>` : '<span class="avatar noone" aria-hidden="true">–</span><span class="who muted">Unassigned</span>'}<span class="id">#${t.id}</span></div>
  </article>`;
}

function colHtml(s, ts) {
  const c = ts.filter(t => t.status === s).sort((x, y) => x.priority - y.priority || x.id - y.id);
  const lim = meth().wip ? (bcfg().wip || {})[s] : 0, over = lim && c.length > lim;
  return `<section class="col" data-s="${s}"><h3>${L(s)}<b class="${over ? "over" : ""}" ${lim ? `title="WIP limit ${lim}"` : ""}>${lim ? c.length + "/" + lim : c.length}</b><button type="button" class="add" data-add="${s}" title="Add task to ${L(s)}" aria-label="Add task to ${L(s)}">+</button></h3><div class="cards">` +
    (qa && qa.status === s ? `<form class="qa" data-qa="${s}"><input name="t" maxlength="200" autocomplete="off" placeholder="Task title — Enter to add, Esc to cancel" value="${esc(qa.text)}"><button type="submit" class="primary mini">Add</button><button type="button" class="mini" data-qacancel>✕</button></form>` : "") +
    (c.map(card).join("") || `<p class="none">${query() ? "No matches" : HINT[s]}</p>`) + "</div></section>";
}

function prioCol(p, ts) {
  const m = meth(), c = ts.filter(t => String(t.priority) === p && t.status !== "done" && t.status !== "declined").sort((x, y) => x.id - y.id);
  return `<section class="col" data-p="${p}"><h3>${esc(m.groups[p])}<b>${c.length}</b><button type="button" class="add" data-addp="${p}" title="Add task" aria-label="Add task">+</button></h3><div class="hint2">${esc(m.group_hints[p])}</div><div class="cards">${c.map(card).join("") || '<p class="none">Drop tasks here</p>'}</div></section>`;
}

const okrPct = t => { const tg = t.target || 0; return tg > 0 ? Math.min(1, (t.current || 0) / tg) : (t.status === "done" ? 1 : 0); };
function okrHtml(ts) {
  const objs = ts.filter(t => t.type === "epic").sort((a, b) => a.id - b.id), kids = id => ts.filter(t => t.parent == id && t.type !== "epic");
  const kr = t => `<div class="kr"><button type="button" class="link krt" data-opent="${t.id}">${esc(t.title)}</button><span class="muted who">${esc(t.assignee || "")}</span><div class="krbar"><div class="bar"><i style="width:${Math.round(okrPct(t) * 100)}%"></i></div></div><span class="krnum"><input class="krcur" data-id="${t.id}" type="number" step="any" min="0" value="${t.current ?? 0}" aria-label="Current value"> / ${t.target ?? "—"}</span><b class="krpct">${Math.round(okrPct(t) * 100)}%</b></div>`;
  const obj = o => { const ks = kids(o.id), p = ks.length ? ks.reduce((a, k) => a + okrPct(k), 0) / ks.length : 0;
    return `<article class="obj"><header><button type="button" class="link objt" data-opent="${o.id}">${esc(o.title)}</button><b class="objpct">${Math.round(p * 100)}%</b></header><div class="bar big"><i style="width:${Math.round(p * 100)}%"></i></div><div class="krs">${ks.map(kr).join("") || '<p class="muted">No key results yet.</p>'}</div><button type="button" class="mini" data-addkr="${o.id}">+ Key result</button></article>`; };
  const loose = ts.filter(t => t.type !== "epic" && !t.parent);
  return `<div class="okr"><div class="okrhead"><button type="button" class="primary mini" data-addobj>+ Objective</button></div>${objs.map(obj).join("") || '<div class="empty"><b>No objectives yet</b><br>Add an objective, then give it measurable key results.</div>'}${loose.length ? `<article class="obj"><header><b>Not linked to an objective</b></header><div class="krs">${loose.map(kr).join("")}</div></article>` : ""}</div>`;
}
function okrDlg(parent) {
  if (!B || B.multi) return;
  dlg(`<h3>${parent ? "New key result" : "New objective"}</h3><label>${parent ? "Key result" : "Objective"}<input name="title" required placeholder="${parent ? "e.g. 10,000 weekly signups" : "e.g. Grow the user base"}"></label>
    ${parent ? `<div class="row2"><label>Target value<input type="number" step="any" min="0" name="target" required></label><label>Starts at<input type="number" step="any" min="0" name="current" value="0"></label></div>` : ""}
    <label>Owner (optional)<input name="assignee"></label>`,
    async d => { await api("POST", "/tasks", {board_id: B.id, title: d.title, assignee: d.assignee || "", kind: "human", channel: lastChannel() || "okr", ...(parent ? {type: "story", parent, target: +d.target, current: +d.current || 0} : {type: "epic"})}); },
    "", parent ? "Add key result" : "Add objective");
}

function sprintBar() {
  const id = $("sprint").value, s = B && B.sprints.find(x => x.id == id);
  const info = $("sprintinfo");
  $("sstart").hidden = !(s && s.state === "planned");
  $("sclose").hidden = !(s && s.state === "active");
  $("es").hidden = !s;
  $("sretro").hidden = !(s && s.state === "closed");
  $("sstart").textContent = "Start " + term().toLowerCase(); $("sclose").textContent = "Complete " + term().toLowerCase(); $("ns").textContent = "+ " + term();
  if (!s) { info.textContent = id === "none" ? "Tasks without a sprint" : "All tasks on this board"; return; }
  const p = s.progress || {};
  info.innerHTML = `<b>${esc(s.name)}</b> · ${esc(s.state)}${s.goal ? " · " + esc(s.goal) : ""}${s.start ? " · " + esc(s.start) + " → " + esc(s.end || "?") : ""}`;
}

function render() {
  $("crumb").textContent = B ? B.name : "";
  const bar = $("sprintbar"), fil = $("filters");
  bar.hidden = !B || view !== "board" || !meth().sprints;
  fil.hidden = !B || view !== "board";
  $("bset").hidden = !B || B.multi || !["board", "overview"].includes(view);
  if (view === "overview") return renderOverview();
  if (view === "agents") return renderAgents();
  if (view === "specs") return renderSpecs();
  if (view === "mywork") return renderMyWork();
  if (view === "admin") return renderAdmin();
  if (!B) { $("cols").innerHTML = '<div class="empty"><b>No boards yet</b><br>Create your first board with “+ Board”.</div>'; $("closed").hidden = true; $("progt").textContent = ""; $("bar").value = 0; return; }
  const ts = visible(), done = ts.filter(t => t.status === "done").length;
  $("bar").value = ts.length ? 100 * done / ts.length : 0;
  $("progt").textContent = `${done}/${ts.length} done`;
  sprintBar();
  const grp = meth().group || "status";
  $("cols").classList.toggle("okrmode", grp === "okr");
  if (grp === "okr") { $("cols").innerHTML = okrHtml(ts); $("closed").hidden = true; }
  else if (grp === "priority") {
    $("cols").innerHTML = ["1", "2", "3", "4"].map(p => prioCol(p, ts)).join("");
    const nc = ts.filter(t => t.status === "done" || t.status === "declined").length;
    $("closed").hidden = false; $("closedn").textContent = nc ? `(${nc})` : ""; $("closedcols").innerHTML = colHtml("done", ts) + colHtml("declined", ts);
  } else {
    $("cols").innerHTML = meth().columns.filter(s => s !== "declined").map(s => colHtml(s, ts)).join("");
    const nd = ts.filter(t => t.status === "declined").length;
    $("closed").hidden = false; $("closedn").textContent = nd ? `(${nd})` : "";
    $("closedcols").innerHTML = colHtml("declined", ts);
  }
  for (const id of [...picked]) if (!B.tasks.some(t => t.id === id)) picked.delete(id);
  document.querySelectorAll(".card").forEach(c => c.classList.toggle("picked", picked.has(+c.dataset.id)));
  bulkBar();
  const qi = document.querySelector(".qa input");
  if (qi && !$("dlg").open) { qi.focus(); qi.setSelectionRange(qi.value.length, qi.value.length); }
}

// ---- specs: shared technical knowledge between agents ----
const SPEC_ST = ["draft", "review", "approved", "deprecated"];
let specSel = +localStorage.tb_spec || 0, specQ = "", specTag = "", specStat = "", specCache = [];
function md(src) {
  const inl = t => esc(t).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<i>$2</i>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const out = []; let list = null, code = null;
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const line of String(src || "").split("\n")) {
    if (code !== null) { if (/^```/.test(line)) { out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`); code = null; } else code.push(line); continue; }
    if (/^```/.test(line)) { closeList(); code = []; continue; }
    let m;
    if ((m = line.match(/^(#{1,4})\s+(.*)/))) { closeList(); out.push(`<h${m[1].length + 1}>${inl(m[2])}</h${m[1].length + 1}>`); }
    else if ((m = line.match(/^\s*[-*]\s+(.*)/))) { if (list !== "ul") { closeList(); out.push("<ul>"); list = "ul"; } out.push(`<li>${inl(m[1])}</li>`); }
    else if ((m = line.match(/^\s*\d+\.\s+(.*)/))) { if (list !== "ol") { closeList(); out.push("<ol>"); list = "ol"; } out.push(`<li>${inl(m[1])}</li>`); }
    else if ((m = line.match(/^>\s?(.*)/))) { closeList(); out.push(`<blockquote>${inl(m[1])}</blockquote>`); }
    else if (!line.trim()) closeList();
    else { closeList(); out.push(`<p>${inl(line)}</p>`); }
  }
  if (code !== null) out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`);
  closeList();
  return out.join("");
}

async function renderSpecs() {
  const el = $("v-specs");
  const qs = new URLSearchParams();
  if (specQ) qs.set("q", specQ);
  if (specTag) qs.set("tag", specTag);
  if (specStat) qs.set("st", specStat);
  const r = await api("GET", "/specs?" + qs);
  if (view !== "specs") return;
  specCache = r.i;
  const all = specQ || specTag || specStat ? (await api("GET", "/specs")).i : r.i;
  const tags = [...new Set(all.flatMap(x => csv(x.tg)))].sort();
  if (!specCache.some(x => x.id === specSel)) specSel = specCache[0] ? specCache[0].id : 0;
  const list = specCache.map(x => `<button type="button" class="spec-item${x.id === specSel ? " on" : ""}" data-spec="${x.id}"><span class="sp-t">${esc(x.t)}</span><span class="sp-m"><span class="stb ${x.st}">${esc(x.st)}</span>${csv(x.tg).slice(0, 3).map(g => `<span class="chip">${esc(g)}</span>`).join("")}<span class="muted">rev ${x.rv} · ${esc(ago(x.u))}</span></span>${x.sn ? `<span class="sp-sn">${esc(x.sn)}</span>` : ""}</button>`).join("") || '<p class="muted pad2">No specs match. Agents publish them with the <code class="k">add_spec</code> tool.</p>';
  el.innerHTML = `<div class="specs"><aside class="spec-side">
      <div class="spec-tools"><input id="sq" type="search" placeholder="Search specs…" value="${esc(specQ)}"><button type="button" class="mini primary" id="newspec">+ New spec</button></div>
      <div class="spec-filters"><select id="sst" aria-label="Status"><option value="">Any status</option>${SPEC_ST.map(x => `<option ${x === specStat ? "selected" : ""}>${x}</option>`).join("")}</select>
      <select id="stag" aria-label="Tag"><option value="">Any tag</option>${tags.map(x => `<option ${x === specTag ? "selected" : ""}>${esc(x)}</option>`).join("")}</select></div>
      <div class="spec-list">${list}</div></aside><article class="spec-main" id="specmain"></article></div>`;
  if (document.activeElement && document.activeElement.id === "sq") document.activeElement.focus();
  showSpec(specSel);
}

async function showSpec(id, rev) {
  const m = $("specmain");
  if (!m) return;
  if (!id) { m.innerHTML = '<div class="empty"><b>No spec selected</b><br>Specs are the shared technical memory of your agents: APIs, decisions, conventions.</div>'; return; }
  specSel = id; localStorage.tb_spec = id;
  document.querySelectorAll(".spec-item").forEach(b => b.classList.toggle("on", +b.dataset.spec === id));
  let x;
  try { x = await api("GET", `/specs/${id}` + (rev ? `/revs/${rev}` : "")); } catch (_) { return; }
  const cur = specCache.find(c => c.id === id), old = rev && cur && rev !== cur.rv;
  const links = csv(x.tk).map(i => `<span class="chip link tk" data-tk="${i}" title="Open task">#${i}</span>`).join("");
  m.innerHTML = `<header class="sp-head"><div><h2>${esc(x.t)}</h2><div class="sp-meta"><span class="stb ${x.st}">${esc(x.st)}</span>${csv(x.tg).map(g => `<span class="chip">${esc(g)}</span>`).join("")}${links}<span class="muted">${x.b ? "board " + x.b : "global"} · rev ${x.rv} · ${esc(x.by || "")} · ${esc(ago(x.u))} ago</span></div></div>
    <div class="sp-acts">${old ? `<button type="button" class="mini primary" data-restore="${rev}">Restore this revision</button><button type="button" class="mini" data-latest>Back to latest</button>` : `<button type="button" class="mini" data-hist>History</button><button type="button" class="mini primary" data-editspec>Edit</button>`}</div></header>
    ${old ? `<p class="notice">Viewing old revision ${rev}${x.note ? " — " + esc(x.note) : ""}.</p>` : ""}<div class="md">${md(x.d) || '<p class="muted">Empty spec.</p>'}</div>`;
  m.dataset.rev = x.rv; m.dataset.id = id;
  m._spec = x;
}

function specDlg(x) {
  const isNew = !x;
  x = x || {st: "draft", d: "", t: "", tg: "", tk: ""};
  dlg(`<h3>${isNew ? "New spec" : "Edit spec #" + x.id}</h3>
    <label>Title<input name="t" required maxlength="200" value="${esc(x.t)}" placeholder="e.g. Auth token format"></label>
    <div class="row2"><label>Status<select name="st">${SPEC_ST.map(v => `<option ${v === x.st ? "selected" : ""}>${v}</option>`).join("")}</select></label>
    <label>Tags (comma)<input name="tg" value="${esc(x.tg)}" placeholder="api, auth, infra"></label></div>
    <div class="row2"><label>Linked tasks (ids — their assignees are notified)<input name="tk" value="${esc(x.tk)}"></label>
    <label>Scope<select name="b"><option value="">Global (all boards)</option>${boards.map(b => `<option value="${b.id}" ${b.id == x.b ? "selected" : ""}>${esc(b.name)}</option>`).join("")}</select></label></div>
    <label>Spec (markdown)<textarea name="d" class="mono" rows="14" placeholder="# Goal&#10;## Interface&#10;## Decisions&#10;## Open questions">${esc(x.d)}</textarea></label>
    <div class="row2"><label>Change note<input name="note" placeholder="What changed?"></label><label>Also notify agents (comma)<input name="to"></label></div>`,
    async d => {
      const body = {t: d.t, st: d.st, tg: d.tg, tk: csv(d.tk).map(Number).filter(Boolean), b: d.b ? +d.b : null, d: d.d, note: d.note, to: csv(d.to)};
      if (isNew) { const r = await api("POST", "/specs", body); specSel = r.id; }
      else { body.base = x.rv; try { await api("PATCH", "/specs/" + x.id, body); } catch (e) { if (e.e === "conflict") toast("Someone edited this spec meanwhile — reload and merge"); throw e; } }
      renderSpecs();
    }, !isNew ? '<button type="button" class="danger sp" id="delspec">Delete</button>' : "", isNew ? "Publish spec" : "Save new revision");
  const dl = $("delspec");
  if (dl) dl.onclick = async () => {
    if (!dl.classList.contains("arm")) { dl.classList.add("arm"); dl.textContent = "Click again to confirm"; setTimeout(() => { dl.classList.remove("arm"); dl.textContent = "Delete"; }, 4000); return; }
    await api("DELETE", "/specs/" + x.id); closeDlg(true); specSel = 0; renderSpecs();
  };
}

async function specHistory(id) {
  const r = await api("GET", `/specs/${id}/revs`);
  const f = $("f");
  f.innerHTML = `<h3>Revision history</h3><div class="revs">${r.i.map(v => `<button type="button" class="rev" data-rev="${v.rv}"><b>rev ${v.rv}</b> · ${esc(v.by || "")} · ${esc(ago(v.u))} ago<span class="muted">${esc(v.note || "")} (${v.len} chars)</span></button>`).join("")}</div><menu><button type="button" id="hclose" class="primary">Close</button></menu>`;
  f.onsubmit = e => e.preventDefault(); f.oninput = f.onchange = null; dirty = false;
  $("hclose").onclick = () => $("dlg").close();
  f.onclick = e => { const b = e.target.closest("[data-rev]"); if (b) { $("dlg").close(); showSpec(id, +b.dataset.rev); } };
  $("dlg").showModal();
}

const spEl = $("v-specs");
spEl.addEventListener("click", async e => {
  const it = e.target.closest("[data-spec]"); if (it) return showSpec(+it.dataset.spec);
  if (e.target.id === "newspec") return specDlg();
  if (e.target.closest("[data-editspec]")) return specDlg($("specmain")._spec);
  if (e.target.closest("[data-hist]")) return specHistory(+$("specmain").dataset.id);
  if (e.target.closest("[data-latest]")) return showSpec(+$("specmain").dataset.id);
  const rs = e.target.closest("[data-restore]");
  if (rs) { const x = $("specmain")._spec; try { await api("PATCH", "/specs/" + x.id, {d: x.d, t: x.t, note: "Restored revision " + rs.dataset.restore}); toast("Revision restored as a new revision"); renderSpecs(); } catch (_) {} return; }
  const tk = e.target.closest("[data-tk]"); if (tk) { setView("board"); $("sprint").value = ""; autoSprint = false; render(); taskDlg(+tk.dataset.tk); }
});
let sqT = null;
spEl.addEventListener("input", e => { if (e.target.id === "sq") { specQ = e.target.value.trim(); clearTimeout(sqT); sqT = setTimeout(renderSpecs, 250); } });
spEl.addEventListener("change", e => { if (e.target.id === "sst") { specStat = e.target.value; renderSpecs(); } if (e.target.id === "stag") { specTag = e.target.value; renderSpecs(); } });

// ---- my work: one place for a person/agent ----
async function renderMyWork() {
  const el = $("v-mywork");
  const names = (await api("GET", "/agents")).i.map(a => a.n);
  let me = localStorage.tb_me || "";
  const head = `<div class="panel mehead"><label>Showing work for<input id="meinp" list="mel" value="${esc(me)}" placeholder="agent or person name"></label><datalist id="mel">${names.map(n => `<option value="${esc(n)}">`).join("")}</datalist><button type="button" class="mini primary" id="mego">Show</button></div>`;
  if (!me) { el.innerHTML = `<div class="pad">${head}<div class="empty"><b>Who are you?</b><br>Enter your assignee name to see your tasks, notifications and what is ready to start.</div></div>`; return; }
  const q = encodeURIComponent(me);
  const [d, ib, rd] = await Promise.all([api("GET", "/dashboard?as=" + q), api("GET", "/inbox?as=" + q + "&all=1&limit=20"), api("GET", `/tasks?as=${q}&ready=1&st=todo`)]);
  if (view !== "mywork") return;
  const work = (d.me && d.me.work) || [];
  const grp = st => work.filter(t => t.st === st);
  const row = t => `<button type="button" class="mrow" data-open="${t.id}" data-b="${t.b}"><span class="id">#${t.id}</span><span class="mt">${esc(t.t)}</span><span class="chip pri">P${t.pr}</span></button>`;
  const sec = (st, title) => grp(st).length ? `<div class="msec"><h3>${title} <b>${grp(st).length}</b></h3>${grp(st).map(row).join("")}</div>` : "";
  const unread = ib.unread;
  el.innerHTML = `<div class="pad">${head}
    <div class="tiles"><div class="tile ${unread ? "warn" : ""}"><div class="n">${unread}</div><div class="l">Unread notifications</div></div><div class="tile"><div class="n">${grp("in_progress").length}</div><div class="l">In progress</div></div><div class="tile"><div class="n">${grp("review").length}</div><div class="l">In review</div></div><div class="tile ${grp("blocked").length ? "bad" : ""}"><div class="n">${grp("blocked").length}</div><div class="l">Blocked</div></div><div class="tile ok"><div class="n">${rd.items.length}</div><div class="l">Ready to start</div></div></div>
    <div class="grid2"><div class="panel"><h2>My open work</h2>${work.length ? sec("in_progress", "In progress") + sec("review", "In review") + sec("todo", "To do") + sec("blocked", "Blocked") + sec("backlog", "Backlog") : '<span class="muted">Nothing assigned to you.</span>'}</div>
    <div><div class="panel"><h2>Ready to start <span class="muted">(no open dependencies)</span></h2>${rd.items.length ? rd.items.slice(0, 10).map(t => row({id: t.id, b: t.board_id, t: t.title, pr: t.priority})).join("") : '<span class="muted">Nothing ready.</span>'}</div>
    <div class="panel"><h2>Notifications</h2>${ib.i.length ? ib.i.slice().reverse().map(n => `<div class="inb ${n.rd ? "rd" : ""}">${esc(n.m)}<small>${esc(n.ty)} · ${esc(n.by || "")} · ${esc(ago(n.ts))}</small></div>`).join("") : '<span class="muted">No notifications.</span>'}${unread ? `<p><button type="button" class="mini" id="meack">Mark all read</button></p>` : ""}</div></div></div></div>`;
}
$("v-mywork").addEventListener("click", async e => {
  if (e.target.id === "mego") { localStorage.tb_me = $("meinp").value.trim(); return renderMyWork(); }
  if (e.target.id === "meack") { await api("POST", "/inbox/ack", {as: localStorage.tb_me}); refreshDash(); return renderMyWork(); }
  const r = e.target.closest("[data-open]"); if (r) openTask(+r.dataset.open, +r.dataset.b);
});
$("v-mywork").addEventListener("keydown", e => { if (e.key === "Enter" && e.target.id === "meinp") $("mego").click(); });

// ---- admin: access, notifications, backups, templates, import/export ----
async function download(path, name) {
  const r = await fetch(BASE + "/api" + path, {headers: {Authorization: "Bearer " + tok}});
  if (!r.ok) return toast("Download failed");
  const a = document.createElement("a"); a.href = URL.createObjectURL(await r.blob()); a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}
async function rawPost(path, text) {
  const r = await fetch(BASE + "/api" + path, {method: "POST", headers: {Authorization: "Bearer " + tok}, body: text});
  return r.json();
}
async function renderAdmin() {
  const el = $("v-admin");
  let tk, se, bk, tp, us, ch;
  try { [tk, se, bk, tp, us, ch] = await Promise.all([api("GET", "/tokens"), api("GET", "/settings"), api("GET", "/admin/backups"), api("GET", "/templates"), api("GET", "/admin/usage"), api("GET", "/chats")]); CHATS = ch.i; }
  catch (_) { el.innerHTML = '<div class="pad"><div class="empty"><b>Admin only</b><br>Your token does not have the admin role.</div></div>'; return; }
  if (view !== "admin") return;
  const last = bk.last;
  el.innerHTML = `<div class="pad"><div class="grid2 admingrid">
    <div class="panel wide"><h2>Access — one token per agent</h2><p class="muted hint">Roles: <b>admin</b> everything · <b>supervisor</b> agent + deletes sprints, gets escalations · <b>agent</b> read/write, no deletes of boards/sprints, no admin · <b>viewer</b> read-only. A token's name is the actor in the audit trail and the default inbox.</p>
      <table class="tb"><tr><th>Name</th><th>Role</th><th>Boards</th><th>Last used</th><th></th></tr>${tk.i.map(t => `<tr><td>${esc(t.n)}</td><td><span class="stb">${esc(t.role)}</span></td><td class="muted">${t.bs ? esc(t.bs.map(i => (boards.find(b => b.id === i) || {}).name || "#" + i).join(", ")) : "all"}</td><td>${t.env ? '<span class="muted">env</span>' : esc(t.u ? ago(t.u) + " ago" : "never")}</td><td class="num">${t.env ? "" : `<span class="rowacts"><button type="button" class="mini" data-tokscope="${t.id}" data-bs="${(t.bs || []).join(",")}" ${t.role === "admin" ? "disabled" : ""}>Boards…</button><button type="button" class="mini" data-deltoken="${t.id}">Revoke</button></span>`}</td></tr>`).join("")}</table>
      <p><button type="button" class="mini primary" id="newtoken">+ Create agent token</button></p></div>
    <div class="panel"><h2>Notifications &amp; supervisor</h2><form id="setf" class="stack">
      <label>Supervisor agent(s) — get escalations (comma separated names)<input name="supervisors" value="${esc(se.supervisors)}" placeholder="e.g. Supervisor"></label>
      <label>Daily digest webhook (Slack/Teams/Discord incoming URL)<input name="digest_url" value="${esc(se.digest_url)}" placeholder="https://hooks.slack.com/…"></label>
      <div class="row2"><label>Digest hour (UTC)<input type="number" name="digest_hour" min="0" max="23" value="${esc(se.digest_hour || 7)}"></label><label>Backups to keep<input type="number" name="backup_keep" min="1" max="365" value="${esc(se.backup_keep || 14)}"></label></div>
      <label>Instant alert webhook (sprint turns red)<input name="alert_url" value="${esc(se.alert_url)}" placeholder="https://…"></label>
      <div class="rowbtn"><button class="primary mini" type="submit">Save settings</button><button type="button" class="mini" id="digestnow">Send digest now</button></div></form></div>
    <div class="panel"><h2>Chat notifications</h2><p class="muted hint">Post to Slack, Discord, Teams or Telegram when something needs a human: a blocked task, a finished one, an escalation, a sprint at risk, a budget warning. Web addresses are secrets and are never shown again.</p>
      ${ch.i.length ? `<table class="tb"><tr><th>Channel</th><th>Where</th><th>Tells you when</th><th>Last</th><th></th></tr>${ch.i.map(c => `<tr><td><b>${esc(c.n)}</b>${c.on ? "" : ' <span class="muted">(paused)</span>'}${c.b ? ` <span class="muted">${esc((boards.find(x => x.id === c.b) || {}).name || "board " + c.b)}</span>` : ""}${c.minpr < 4 ? ` <span class="muted">P${c.minpr}+</span>` : ""}</td><td>${esc(c.kind)} <span class="muted">${esc(c.host)}</span></td><td class="muted">${c.ev.map(e => esc(e)).join(", ")}</td><td>${c.last ? (c.ok ? esc(ago(c.last.slice(0, 19))) + " ago" : '<span class="hl red">' + esc(c.last.slice(20) || "failed") + "</span>") : '<span class="muted">never</span>'}</td><td class="num"><button type="button" class="mini" data-chat="test:${c.id}">Test</button> <button type="button" class="mini" data-chat="toggle:${c.id}">${c.on ? "Pause" : "Resume"}</button> <button type="button" class="mini danger" data-chat="del:${c.id}">Delete</button></td></tr>`).join("")}</table>` : '<p class="muted">No channels yet.</p>'}
      <p><button type="button" class="mini primary" id="newchat">+ Add channel</button></p></div>
    <div class="panel"><h2>Git: pull requests and commits</h2><p class="muted hint">Point a webhook of GitHub, GitLab or Gitea at <code class="k">${esc(location.origin + BASE)}/api/hooks/git</code> (content type JSON, events: pushes, pull requests, reviews, workflow runs). Commits and PRs attach to a task when the message, title or branch names it (<code class="k">#42</code>, <code class="k">task-42-login</code>). A PR opening moves the task to review; merging closes it unless its board's definition of done or an open review says otherwise.</p>
      <div class="kv"><span>Webhook secret</span><b>${se.git_secret ? "set" : '<span class="hl red">not set - webhook is off</span>'}</b></div>
      <p><button type="button" class="mini primary" id="gitsecret">${se.git_secret ? "Create a new secret" : "Create secret"}</button></p></div>
    <div class="panel"><h2>Integrations, wake-ups and budgets</h2><form id="opsf" class="stack">
      <label>Public address of this board <span class="muted">(adds task links to chat messages)</span><input name="base_url" value="${esc(se.base_url)}" placeholder="https://board.example.com"></label>
      <div class="row2"><label>Wake sleeping agents after (minutes, 0 = off)<input type="number" name="wake_after_min" min="0" max="1440" value="${esc(se.wake_after_min || 10)}"></label><label>Close a task when its PR merges<select name="git_autodone"><option value="1" ${se.git_autodone !== "0" ? "selected" : ""}>Yes (if the gate allows)</option><option value="0" ${se.git_autodone === "0" ? "selected" : ""}>No, only comment</option></select></label></div>
      <label>Monthly budget for all boards (USD, blank = none)<input type="number" step="any" min="0" name="budget_usd" value="${esc(se.budget_usd)}"></label>
      <label>Model prices <span class="muted">(JSON, USD per million tokens [input, output], used when an agent reports tokens without a cost)</span><textarea name="prices" rows="2" placeholder='{"my-model": [3, 15]}'>${esc(se.prices)}</textarea></label>
      <div class="rowbtn"><button class="primary mini" type="submit">Save</button></div></form></div>
    <div class="panel"><h2>Backups</h2>${last ? `<p>Last: <b>${esc(last.file)}</b> · ${Math.round(last.size / 1024)} KB · ${last.ok ? '<span class="hl green">verified</span>' : '<span class="hl red">verification failed</span>'}<br><span class="muted">${last.counts.tasks} tasks, ${last.counts.boards} boards, ${last.counts.specs} specs · ${esc(last.ts)} UTC</span></p>` : '<p class="muted">No backup yet — one is made automatically every 24h.</p>'}
      <p class="muted hint">${bk.n} kept in <code class="k">${esc(bk.dir)}</code>. Each backup is integrity-checked right after it is written.</p><p><button type="button" class="mini primary" id="bknow">Back up now</button></p></div>
    <div class="panel"><h2>Import / export</h2><p class="muted hint">Exports include tasks, sprints, docs, specs and comments. Imports always create new copies — nothing is overwritten.</p>
      <div class="rowbtn"><button type="button" class="mini" id="expall">Export everything (JSON)</button><button type="button" class="mini" id="expb">Export this board (JSON)</button><button type="button" class="mini" id="expcsv">Tasks (CSV)</button></div>
      <div class="rowbtn"><label class="mini filebtn">Import JSON<input type="file" id="impjson" accept=".json,application/json" hidden></label><label class="mini filebtn">Import CSV into this board<input type="file" id="impcsv" accept=".csv,text/csv" hidden></label></div>
      <p class="muted hint">CSV header: <code class="k">title,description,status,priority,assignee,kind,channel,labels,points</code></p></div></div>
    <div class="panel"><h2>Board rules — definition of done${B && !B.multi ? " · " + esc(B.name) : ""}</h2><p class="muted hint">One item per line. A task on this board cannot move to Done until every item is ticked and no review is open.</p><form id="dodf" class="stack"><textarea name="dod" rows="4" placeholder="Tests pass&#10;PR linked&#10;Deployed to test">${esc(B && !B.multi ? B.dod || "" : "")}</textarea><div class="rowbtn"><button class="primary mini" type="submit" ${B && !B.multi ? "" : "disabled"}>Save rules</button></div></form></div>
    <div class="panel"><h2>Token usage <span class="muted">since ${esc((us.since || "").slice(0, 16))} UTC</span></h2><p class="muted hint">Approximate tokens (bytes ÷ 4) that each agent has sent and received through the API. Use it to see which calls cost the most; <code class="k">briefing</code>, <code class="k">next</code> and <code class="k">finish</code> replace several calls each.</p>
      ${us.agents.length ? `<table class="tb"><tr><th>Agent</th><th>Calls</th><th>Tokens</th><th>Costliest routes</th></tr>${us.agents.map(a => `<tr><td>${esc(a.n)}</td><td class="num">${a.calls}</td><td class="num">${a.t}</td><td class="muted">${a.routes.slice(0, 3).map(r => esc(r.r) + " " + r.t).join(" · ")}</td></tr>`).join("")}</table>` : '<span class="muted">No calls yet.</span>'}</div>
    <div class="panel"><h2>Templates &amp; recurring tasks</h2><p class="muted hint">Rules: ${esc(tp.help)}. A recurring template creates a task on schedule, unless the previous one is still open.</p>
      ${tp.i.length ? `<table class="tb"><tr><th>Template</th><th>Rule</th><th>Next run</th><th>Assignee</th><th></th></tr>${tp.i.map(t => `<tr><td>${esc(t.n)}${t.on ? "" : ' <span class="muted">(paused)</span>'}</td><td>${esc(t.rc || "manual")}</td><td>${esc(t.nx || "—")}</td><td>${esc(t.as || "—")}</td><td class="num"><button type="button" class="mini" data-usetpl="${t.id}">Use</button> <button type="button" class="mini" data-edittpl="${t.id}">Edit</button> <button type="button" class="mini" data-deltpl="${t.id}">Delete</button></td></tr>`).join("")}</table>` : '<p class="muted">No templates yet.</p>'}
      <div class="rowbtn"><button type="button" class="mini primary" id="newtpl">+ New template</button><button type="button" class="mini" id="runtpl">Run due recurring now</button></div></div></div></div>`;
  el._tpl = tp.i;
}

function tplDlg(t) {
  const isNew = !t; t = t || {pr: 3, k: "agent", on: true};
  dlg(`<h3>${isNew ? "New template" : "Edit template"}</h3>
    <div class="row2"><label>Template name<input name="n" required value="${esc(t.n)}"></label><label>Board<select name="b"><option value="">Any board</option>${boards.map(b => `<option value="${b.id}" ${b.id == t.b ? "selected" : ""}>${esc(b.name)}</option>`).join("")}</select></label></div>
    <label>Task title<input name="t" value="${esc(t.t)}" placeholder="defaults to the template name"></label>
    <label>Description<textarea name="d">${esc(t.d)}</textarea></label>
    <label>Checklist (one item per line)<textarea name="cl" rows="3">${esc(t.cl)}</textarea></label>
    <div class="row2"><label>Assignee<input name="as" value="${esc(t.as)}"></label><label>Area<input name="ch" value="${esc(t.ch)}"></label></div>
    <div class="row2"><label>Priority<select name="pr">${opt([1, 2, 3, 4], t.pr)}</select></label><label>Points<input type="number" name="pt" min="0" value="${t.pt || ""}"></label></div>
    <div class="row2"><label>Repeat<input name="rc" list="rcl" value="${esc(t.rc)}" placeholder="blank = manual only"><datalist id="rcl"><option value="daily"><option value="weekdays"><option value="weekly:mon"><option value="monthly:1"></datalist></label><label>First run<input type="date" name="nx" value="${esc(t.nx)}"></label></div>
    <label class="chk"><input type="checkbox" name="on" ${t.on ? "checked" : ""}> Active</label>`,
    async d => {
      const body = {n: d.n, b: d.b ? +d.b : null, t: d.t, d: d.d, cl: d.cl, as: d.as, ch: d.ch, pr: +d.pr, pt: +d.pt || 0, rc: d.rc.trim(), nx: d.nx, on: d.on === "on"};
      if (isNew) await api("POST", "/templates", body); else await api("PATCH", "/templates/" + t.id, body);
      renderAdmin();
    }, "", isNew ? "Create template" : "Save");
}

$("v-admin").addEventListener("click", async e => {
  const el = $("v-admin"), id = k => e.target.id === k;
  if (await adminExtras(e)) return;
  const dt = e.target.closest("[data-deltoken]"); if (dt) { if (confirm("Revoke this token? Anything using it stops working.")) { await api("DELETE", "/tokens?id=" + dt.dataset.deltoken); renderAdmin(); } return; }
  if (id("newtoken")) return dlg(`<h3>Create agent token</h3><label>Agent name (exactly as used in Assignee)<input name="n" required></label><label>Role<select name="role">${opt(["agent", "supervisor", "viewer", "admin"], "agent")}</select></label>${boardPick([])}`,
    async d => { const bs = boards.filter(b => d["tb_" + b.id]).map(b => b.id); if (bs.length && d.role === "admin") { toast("Admin tokens cannot be limited to boards"); throw {e: "bad"}; } const r = await api("POST", "/tokens", {n: d.n, role: d.role, boards: bs}); closeDlg(true); setTimeout(() => dlg(`<h3>Token for ${esc(r.n)}</h3><p class="muted hint">Shown once. Store it in the agent's secret store now.</p><label>Token<input readonly value="${esc(r.token)}" id="tkv"></label>`, async () => { await navigator.clipboard.writeText(r.token).catch(() => {}); toast("Copied"); renderAdmin(); }, "", "Copy & close"), 50); throw {e: "shown"}; }, "", "Create");
  if (id("bknow")) { try { const r = await api("POST", "/admin/backup"); toast(r.ok ? "Backup created and verified" : "Backup created but verification FAILED"); renderAdmin(); } catch (_) {} return; }
  if (id("digestnow")) { const r = await api("POST", "/admin/digest"); return toast(r.sent ? "Digest sent" : "Not sent: " + (r.why || "nothing to do")); }
  if (id("expall")) return download("/export", "taskboard-export.json");
  if (id("expb")) return B && !B.multi ? download("/export?b=" + B.id, `taskboard-${B.name}.json`) : null;
  if (id("expcsv")) return download("/export.csv" + (B && !B.multi ? "?b=" + B.id : ""), "taskboard-tasks.csv");
  if (id("newtpl")) return tplDlg();
  if (id("runtpl")) { const r = await api("POST", "/templates/run"); toast(`Created ${r.made.length}, skipped ${r.skipped}${r.failed ? ", failed " + r.failed : ""}`); return renderAdmin(); }
  const et = e.target.closest("[data-edittpl]"); if (et) return tplDlg(el._tpl.find(x => x.id == et.dataset.edittpl));
  const ut = e.target.closest("[data-usetpl]"); if (ut) { const r = await api("POST", `/templates/${ut.dataset.usetpl}/use`, {}); return toast(`Task #${r.id} created`, "Open", () => openTask(r.id, r.b)); }
  const dtp = e.target.closest("[data-deltpl]"); if (dtp && confirm("Delete this template?")) { await api("DELETE", "/templates/" + dtp.dataset.deltpl); renderAdmin(); }
});
$("v-admin").addEventListener("submit", async e => {
  if (e.target.id === "dodf" && B && !B.multi) { e.preventDefault(); try { await api("PATCH", "/boards/" + B.id, {dod: new FormData(e.target).get("dod")}); await loadBoard(); toast("Board rules saved"); } catch (_) {} return; }
  if (e.target.id === "opsf") { e.preventDefault(); try { await api("POST", "/settings", Object.fromEntries(new FormData(e.target))); toast("Saved"); } catch (_) {} return; }
  if (e.target.id !== "setf") return;
  e.preventDefault();
  try { await api("POST", "/settings", Object.fromEntries(new FormData(e.target))); toast("Settings saved"); } catch (_) {}
});
$("v-admin").addEventListener("change", async e => {
  const f = e.target.files && e.target.files[0]; if (!f) return;
  const text = await f.text();
  try {
    if (e.target.id === "impjson") { const r = await api("POST", "/import", JSON.parse(text)); toast(`Imported ${r.n.tasks} tasks into ${Object.keys(r.boards).length} new board(s)`); await loadBoards(); }
    if (e.target.id === "impcsv") { if (!B || B.multi) return toast("Pick a single board first"); const r = await rawPost("/import/csv?b=" + B.id, text); toast(`Imported ${r.made} task(s)${r.bad && r.bad.length ? `, ${r.bad.length} row(s) skipped` : ""}`); await loadBoard(); }
  } catch (_) { toast("Import failed — check the file"); }
  e.target.value = "";
});

// ---- views ----
const TITLES = {overview: "Overview", board: "Board", mywork: "My work", specs: "Specs", agents: "Agents", admin: "Admin"};
function setView(v) {
  view = v; localStorage.tb_view = v;
  document.querySelectorAll("#tabs .tab").forEach(b => b.classList.toggle("on", b.dataset.view === v));
  ["overview", "board", "mywork", "specs", "agents", "admin"].forEach(x => { $("v-" + x).hidden = x !== v; });
  $("pagetitle").textContent = TITLES[v];
  $("nt").hidden = false;
  render();
}
const ago = ts => { const m = Math.max(0, Math.round((Date.now() - Date.parse(ts.replace(" ", "T") + "Z")) / 60000)); return m < 1 ? "<1m" : m < 60 ? m + "m" : m < 1440 ? Math.round(m / 60) + "h" : Math.round(m / 1440) + "d"; };
const sum = o => Object.values(o || {}).reduce((a, b) => a + b, 0);

async function refreshDash() {
  try {
    const d = await api("GET", "/dashboard" + (B && !B.multi ? "?b=" + B.id : B && B.scopeTeam ? "?tm=" + encodeURIComponent(B.scopeTeam) : ""));
    const n = (d.agents || []).reduce((a, x) => a + x.inbox, 0);
    $("inboxn").textContent = n; $("inboxn").hidden = !n;
    return d;
  } catch (_) { return null; }
}
const kickDash = () => { clearTimeout(dashT); dashT = setTimeout(() => { if (view === "overview") renderOverview(); else if (view === "agents") renderAgents(); else refreshDash(); }, 400); };

const CFD_ORDER = [["done", "#00805c"], ["review", "#7d58c2"], ["in_progress", "#ba5c33"], ["todo", "#326bae"], ["backlog", "#b2b6b5"]];
function cfdSvg(cfd) {
  const W = 560, H = 170, P = 26, n = Math.max(1, cfd.length - 1), tot = cfd.map(r => CFD_ORDER.reduce((a, [k]) => a + r[k], 0)), max = Math.max(1, ...tot);
  const x = i => P + (W - P * 2) * i / n, y = v => H - P - (H - P * 2) * v / max;
  let base = cfd.map(() => 0), out = "";
  for (const [k, col] of CFD_ORDER) {
    const top = cfd.map((r, i) => base[i] + r[k]);
    out += `<polygon points="${top.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ")} ${base.map((v, i) => `${x(cfd.length - 1 - i).toFixed(1)},${y(base[cfd.length - 1 - i]).toFixed(1)}`).join(" ")}" fill="${col}" fill-opacity=".85"/>`;
    base = top;
  }
  return `<svg class="burn" viewBox="0 0 ${W} ${H}" role="img" aria-label="Cumulative flow diagram">${out}<line class="ax" x1="${P}" y1="${H - P}" x2="${W - P}" y2="${H - P}"/><text x="${P}" y="${H - 8}">${esc(cfd[0].d.slice(5))}</text><text x="${W - P}" y="${H - 8}" text-anchor="end">${esc(cfd[cfd.length - 1].d.slice(5))}</text><text x="${P - 4}" y="${P}" text-anchor="end">${max}</text></svg>`;
}
const hrs = v => v == null ? "—" : v < 48 ? Math.round(v) + "h" : (v / 24).toFixed(1) + "d";
function flowPanel(f) {
  const wipN = f.wip.in_progress + f.wip.review, lastWeek = f.per_week.length ? f.per_week[f.per_week.length - 1].n : 0;
  const mx = Math.max(1, ...f.per_week.map(w => w.n));
  return `<div class="panel sprintpanel"><h2>Flow · last ${f.days} days ${f.over.length ? `<span class="hl red">Over WIP limit: ${f.over.map(esc).join(", ")}</span>` : ""}</h2>
    <div class="tiles" style="margin-bottom:14px"><div class="tile"><div class="n">${wipN}</div><div class="l">In flight (doing + review)</div></div><div class="tile"><div class="n">${lastWeek}</div><div class="l">Finished last 7 days</div></div><div class="tile"><div class="n">${hrs(f.cycle_h.avg)}</div><div class="l">Avg cycle time${f.cycle_h.p85 != null ? " · p85 " + hrs(f.cycle_h.p85) : ""}</div></div><div class="tile ${f.stuck.length ? "warn" : ""}"><div class="n">${f.stuck.length}</div><div class="l">Aging unusually long</div></div></div>
    <div class="grid2 inner"><div><h3>Cumulative flow</h3>${cfdSvg(f.cfd)}<div class="legend">${CFD_ORDER.slice().reverse().map(([k, c]) => `<span style="display:inline-flex;align-items:center;gap:4px;margin-right:8px"><i style="width:10px;height:10px;border:0;background:${c};margin:0;border-radius:2px"></i>${esc(L(k) || k)}</span>`).join("")}</div></div>
    <div><h3>Throughput per week</h3><div class="bars2">${f.per_week.map(w => `<div class="b2"><i style="height:${Math.round(100 * w.n / mx)}%"></i><span>${w.n}</span><small>${esc(w.from.slice(5))}</small></div>`).join("")}</div>
    <h3 style="margin-top:14px">Oldest work in flight</h3>${f.aging.length ? f.aging.slice(0, 5).map(a => `<div class="coord"><div><button type="button" class="link" data-open="${a.id}"><span class="mono">#${a.id}</span></button> ${esc(a.t)}<div class="muted">${esc(a.st.replace("_", " "))} · ${hrs(a.age_h)}${a.as ? " · " + esc(a.as) : ""}</div></div>${a.stuck ? '<span class="badge red">stuck</span>' : ""}</div>`).join("") : '<span class="muted">Nothing in flight.</span>'}</div></div></div>`;
}
function okrPanel(o) {
  return `<div class="panel"><h2>Objectives <span class="hl ${o.overall >= 0.7 ? "green" : o.overall >= 0.4 ? "amber" : "red"}">${Math.round(o.overall * 100)}% overall</span></h2>${o.i.length ? o.i.map(x => `<div class="okrrow"><div><b>${esc(x.t)}</b><div class="muted">${x.krs.length} key result(s)${x.as ? " · " + esc(x.as) : ""}</div></div><div class="bar"><i style="width:${Math.round(x.progress * 100)}%"></i></div><b>${Math.round(x.progress * 100)}%</b></div>`).join("") : '<span class="muted">No objectives yet.</span>'}</div>`;
}
function slaPanel(s) {
  const row = (it, cls) => `<div class="coord"><div><button type="button" class="link" data-open="${it.id}"><span class="mono">#${it.id}</span></button> ${esc(it.t)}<div class="muted">P${it.pr} · open ${hrs(it.age_h)} of ${it.sla_h}h target${it.as ? " · " + esc(it.as) : ""}</div></div><span class="badge ${cls}">${cls === "red" ? "breached" : "at risk"}</span></div>`;
  return `<div class="panel"><h2>Response targets <span class="hl ${s.breached.length ? "red" : s.at_risk.length ? "amber" : "green"}">${s.breached.length} breached · ${s.at_risk.length} at risk · ${s.ok} on track</span></h2>${s.breached.map(i => row(i, "red")).join("")}${s.at_risk.map(i => row(i, "")).join("") || (s.breached.length ? "" : '<span class="muted">Everything is inside its target.</span>')}</div>`;
}
function velPanel(v) {
  const mx = Math.max(1, ...v.i.map(x => Math.max(x.committed, x.done)));
  return `<div class="panel"><h2>Velocity ${v.avg != null ? `<span class="hl green">avg ${v.avg} per closed sprint</span>` : ""}</h2><div class="bars2 wide">${v.i.map(x => `<div class="b2"><div class="pair"><i class="c" style="height:${Math.round(100 * x.committed / mx)}%" title="Committed ${x.committed}"></i><i class="d" style="height:${Math.round(100 * x.done / mx)}%" title="Done ${x.done}"></i></div><span>${x.done}/${x.committed || "–"}</span><small>${esc(x.n.slice(0, 10))}</small></div>`).join("")}</div><div class="legend"><span><i style="width:10px;height:10px;border:0;background:var(--bd);margin:0 4px 0 0;border-radius:2px"></i>committed</span><span><i style="width:10px;height:10px;border:0;background:var(--pri);margin:0 4px 0 12px;border-radius:2px"></i>done</span></div></div>`;
}
const HL = {green: "On track", amber: "Watch", red: "At risk"};
const pc = x => Math.round((x || 0) * 100) + "%";
function burnSvg(burn) {
  const W = 520, H = 150, P = 24, max = Math.max(1, ...burn.map(r => r[1]), ...burn.map(r => r[2] ?? 0)), n = Math.max(1, burn.length - 1);
  const x = i => P + (W - P * 2) * i / n, y = v => H - P - (H - P * 2) * v / max;
  const line = (k, cls) => { const pts = burn.map((r, i) => r[k] == null ? null : `${x(i).toFixed(1)},${y(r[k]).toFixed(1)}`).filter(Boolean); return pts.length > 1 ? `<polyline class="${cls}" points="${pts.join(" ")}" fill="none"/>` : ""; };
  return `<svg class="burn" viewBox="0 0 ${W} ${H}" role="img" aria-label="Burndown chart"><line class="ax" x1="${P}" y1="${H - P}" x2="${W - P}" y2="${H - P}"/><line class="ax" x1="${P}" y1="${P}" x2="${P}" y2="${H - P}"/>${line(1, "ideal")}${line(2, "actual")}<text x="${P}" y="${H - 6}">${esc(burn[0][0].slice(5))}</text><text x="${W - P}" y="${H - 6}" text-anchor="end">${esc(burn[burn.length - 1][0].slice(5))}</text><text x="${P - 4}" y="${P + 4}" text-anchor="end">${max}</text></svg>`;
}
function sprintPanel(sp, ss) {
  if (!sp) return `<div class="panel"><h2>Active sprint</h2><div class="sprintcard"><span class="muted">No active sprint.</span><button type="button" class="mini primary" id="ovnew">Start a new sprint</button></div></div>`;
  if (!ss) return `<div class="panel"><h2>Active sprint</h2><b>${esc(sp.n)}</b></div>`;
  const w = ss.w, d = ss.days;
  const risks = ss.risks.length ? ss.risks.map(r => `<li class="rk ${r.sev}"><span class="sev">${r.sev}</span><span>${esc(r.m)}${r.ids.length ? ` <span class="muted">${r.ids.slice(0, 8).map(i => "#" + i).join(" ")}${r.ids.length > 8 ? " …" : ""}</span>` : ""}</span></li>`).join("") : '<li class="muted">No risks detected.</li>';
  return `<div class="panel sprintpanel"><h2>Active sprint <span class="hl ${ss.health}">${HL[ss.health]}</span></h2>
    <div class="sprinthead"><div><b>${esc(ss.n)}</b>${ss.g ? `<div class="muted">${esc(ss.g)}</div>` : ""}<div class="muted">${esc(ss.from)} → ${esc(ss.to)} · ${d.left >= 0 ? d.left + " day(s) left" : -d.left + " day(s) overdue"}</div></div><button type="button" class="mini" data-sclose="${sp.id}">Complete sprint</button></div>
    <div class="bars"><label>Time used ${pc(ss.pct.time)}<progress max="100" value="${ss.pct.time * 100}"></progress></label><label>Work done ${pc(ss.pct.done)} (${w.done}/${w.scope})<progress class="${ss.pct.done + 0.01 < ss.pct.time ? "late" : ""}" max="100" value="${ss.pct.done * 100}"></progress></label></div>
    <div class="facts"><span><b>${w.open}</b> open</span><span><b>${ss.pace}</b> done/day</span><span>projected finish <b>${esc(ss.proj || "—")}</b></span><span><b>${ss.added}</b> added mid-sprint</span></div>
    <div class="grid2 inner"><div><h3>Burndown</h3>${burnSvg(ss.burn)}<div class="legend"><i class="ideal"></i>ideal <i class="actual"></i>actual</div></div>
    <div><h3>Risks</h3><ul class="risks">${risks}</ul></div></div></div>`;
}

function sprintsPanel(sp) {
  if (!sp || !sp.length) return "";
  return `<div class="panel"><h2>Active sprints <span class="muted">${sp.length}</span></h2><div class="sprintgrid">${sp.map(s => `<article class="sprintmini"><header><b>${esc(s.bn)}</b><span class="hl ${s.health}">${HL[s.health]}</span></header>
    <div class="muted">${esc(s.n)} · ${s.days.left >= 0 ? s.days.left + " day(s) left" : -s.days.left + " day(s) overdue"} · ${s.w.done}/${s.w.scope} done</div>${burnSvg(s.burn)}
    <div class="bars"><label>Time used ${pc(s.pct.time)}<progress max="100" value="${s.pct.time * 100}"></progress></label><label>Work done ${pc(s.pct.done)}<progress class="${s.pct.done + 0.01 < s.pct.time ? "late" : ""}" max="100" value="${s.pct.done * 100}"></progress></label></div>
    ${s.risks.length ? `<div class="muted">${esc(s.risks[0].m)}${s.risks.length > 1 ? " +" + (s.risks.length - 1) + " more" : ""}</div>` : ""}<div><button type="button" class="mini" data-board="${s.b}">Open board</button></div></article>`).join("")}</div></div>`;
}
function okrRollup(l) {
  if (!l || !l.length) return "";
  return `<div class="panel"><h2>Objectives by board</h2><div class="budgets">${l.map(o => `<div class="bud"><span><b>${esc(o.bn)}</b> <span class="muted">${o.n} objective(s)</span></span><div class="bar"><i class="${o.overall < 0.4 ? "bad" : o.overall < 0.7 ? "warn" : ""}" style="width:${Math.round(o.overall * 100)}%"></i></div><b>${pc(o.overall)}</b></div>`).join("")}</div></div>`;
}
function slaRollup(l) {
  if (!l || !l.length) return "";
  const items = l.flatMap(x => x.items).slice(0, 6), br = l.reduce((a, x) => a + x.breached, 0), ar = l.reduce((a, x) => a + x.at_risk, 0);
  return `<div class="panel"><h2>Response targets <span class="hl ${br ? "red" : ar ? "amber" : "green"}">${br} breached · ${ar} at risk</span></h2><table class="tb"><tr><th>Board</th><th>Breached</th><th>At risk</th><th>On track</th></tr>${l.map(x => `<tr><td>${esc(x.bn)}</td><td class="num">${x.breached ? `<b style="color:var(--bad)">${x.breached}</b>` : 0}</td><td class="num">${x.at_risk}</td><td class="num">${x.ok}</td></tr>`).join("")}</table>
    ${items.map(it => `<div class="coord"><div><button type="button" class="link" data-open="${it.id}"><span class="mono">#${it.id}</span></button> ${esc(it.t)}<div class="muted">${esc(it.bn)} · P${it.pr} · open ${hrs(it.age_h)} of ${it.sla_h}h target${it.as ? " · " + esc(it.as) : ""}</div></div><span class="badge red">late</span></div>`).join("")}</div>`;
}
function envPanel(envs) {
  if (!envs || !envs.length) return "";
  return `<div class="panel"><h2>Environments</h2><table class="tb"><tr><th>Env</th><th>Deployed</th><th>By</th><th>When</th></tr>${envs.map(e => `<tr><td><b>${esc(e.n)}</b>${e.frozen ? ` <span class="badge red">Frozen until ${esc(e.frozen.slice(5, 16))} UTC</span>` : ""}</td><td class="num">${esc((e.sha || "").slice(0, 8) || "—")}</td><td>${esc(e.by || "")}</td><td>${e.at ? esc(ago(e.at)) + " ago" : ""}</td></tr>`).join("")}</table></div>`;
}
async function renderOverviewMulti(d) {
  const el = $("v-overview"), t = d.totals || {}, bs = d.boards || [];
  const qs0 = B.scopeTeam ? "?tm=" + encodeURIComponent(B.scopeTeam) : "", soft = p => p.catch(() => null);
  const [cost, an, envr] = await Promise.all([soft(api("GET", "/cost" + qs0)), soft(api("GET", "/overview" + qs0)), soft(api("GET", "/envs"))]);
  const astat = {}; ((an && an.agents && an.agents.i) || []).forEach(x => { astat[x.as] = x; });
  const fl = an && an.flow, flowOn = fl && (fl.done_n || fl.wip.in_progress || fl.wip.review);
  if (view !== "overview") return;
  const nudged = bs.reduce((a, b) => a + (b.nu || []).length, 0);
  const rows = bs.map(b => { const by = b.by || {}, open = sum(by) - (by.done || 0) - (by.declined || 0), sp = b.sp, m = MET[b.mt] || {};
    return `<tr><td><b>${esc(b.n)}</b>${b.tm ? ` <span class="muted">${esc(b.tm)}</span>` : ""}</td><td>${esc(m.name || b.mt)}</td><td class="num">${open}</td><td class="num">${by.in_progress || 0}</td><td class="num">${by.blocked ? `<b style="color:var(--bad)">${by.blocked}</b>` : 0}</td>
      <td>${sp ? `<span class="hl ${sp.hl}">${esc(HL[sp.hl] || sp.hl)}</span> <span class="muted">${esc(sp.n)} · ${sp.p[0]}/${sp.p[1]}</span>` : (b.fl ? `<span class="muted">${b.fl.over.length ? "over WIP: " + esc(b.fl.over.join(", ")) : "flow ok"} · ${b.fl.done14} done in 14d</span>` : '<span class="muted">—</span>')}</td>
      <td class="num"><button type="button" class="mini" data-board="${b.id}">Open</button></td></tr>`; }).join("");
  const agents = (d.agents || []).map(a => `<tr><td>${esc(a.as)}</td><td class="num">${a.by.in_progress || 0}</td><td class="num">${a.by.todo || 0}</td><td class="num">${a.by.review || 0}</td><td class="num">${a.by.blocked || 0}</td><td class="num">${a.inbox ? `<b>${a.inbox}</b>` : 0}</td><td class="num">${(astat[a.as] || {}).done ?? 0}</td><td class="num">${(astat[a.as] || {}).cycle_h != null ? astat[a.as].cycle_h + "h" : "—"}</td><td class="num">${(astat[a.as] || {}).decline_rate != null ? Math.round(astat[a.as].decline_rate * 100) + "%" : "—"}</td></tr>`).join("");
  const att = (d.attention || []).length ? `<div class="panel"><h2>Needs attention</h2><ul class="risks">${d.attention.map(r => `<li class="rk ${r.sev}"><span class="sev">${r.sev}</span><span>${esc(r.m)}${r.ids.length ? ` <span class="muted">${r.ids.slice(0, 8).map(i => "#" + i).join(" ")}</span>` : ""}</span></li>`).join("")}</ul></div>` : "";
  el.innerHTML = `<div class="pad"><p class="muted lead">${esc(B.name)} — ${t.boards || 0} board(s) combined. Pick a single board in the menu to work on it.</p>
    <div class="tiles"><div class="tile"><div class="n">${t.open || 0}</div><div class="l">Open tasks</div></div><div class="tile"><div class="n">${t.in_progress || 0}</div><div class="l">In progress</div></div><div class="tile"><div class="n">${t.review || 0}</div><div class="l">In review</div></div><div class="tile ${t.blocked ? "bad" : ""}"><div class="n">${t.blocked || 0}</div><div class="l">Blocked</div></div><div class="tile ${nudged ? "warn" : ""}"><div class="n">${nudged}</div><div class="l">Stale</div></div><div class="tile ${t.red_sprints ? "bad" : "ok"}"><div class="n">${t.red_sprints || 0}</div><div class="l">Sprints at risk</div></div></div>
    ${att}
    <div class="panel"><h2>Boards</h2><table class="tb"><tr><th>Board</th><th>Method</th><th>Open</th><th>Doing</th><th>Blocked</th><th>Sprint / flow</th><th></th></tr>${rows}</table></div>
    ${an ? sprintsPanel(an.sprints) : ""}
    ${flowOn ? flowPanel(fl) : ""}
    ${an ? okrRollup(an.okr) : ""}
    ${an ? slaRollup(an.sla) : ""}
    ${costPanel(cost)}
    ${envPanel(envr && envr.i)}
    <div class="panel"><h2>Agent workload across these boards</h2>${agents ? `<table class="tb"><tr><th>Agent</th><th>Doing</th><th>To do</th><th>Review</th><th>Blocked</th><th>Inbox</th><th>Done ${an ? an.days : 30}d</th><th>Cycle</th><th>Decl.</th></tr>${agents}</table>` : '<span class="muted">No assigned work.</span>'}</div></div>`;
}

async function renderOverview() {
  const el = $("v-overview");
  const d = await refreshDash();
  if (!d || view !== "overview") return;
  if (B && B.multi) return renderOverviewMulti(d);
  const b = (d.boards || [])[0];
  if (!b) { el.innerHTML = '<div class="empty"><b>No boards yet</b></div>'; return; }
  const by = b.by || {}, open = sum(by) - (by.done || 0) - (by.declined || 0);
  const sp = b.sp;
  const mt = MET[b.mt] || {sprints: true, wip: false};
  let fl = null, vel = null;
  if (mt.wip) { try { fl = await api("GET", `/boards/${b.id}/flow?days=30`); } catch (_) {} }
  if (mt.sprints) { try { vel = await api("GET", `/boards/${b.id}/velocity`); } catch (_) {} }
  let okr = null, sla = null, cost = null;
  try { cost = await api("GET", "/cost?b=" + b.id); } catch (_) {}
  if (mt.group === "okr") { try { okr = await api("GET", `/boards/${b.id}/okr`); } catch (_) {} }
  if (mt.sla) { try { sla = await api("GET", `/boards/${b.id}/sla`); } catch (_) {} }
  if (view !== "overview") return;
  let ss = null;
  if (sp && mt.sprints) { try { ss = await api("GET", `/sprints/${sp.id}/stats`); } catch (_) {} if (view !== "overview") return; }
  let stats = {}, caps = {}, envs = [];
  try { const [sr, ar, er] = await Promise.all([api("GET", "/stats/agents?b=" + b.id), api("GET", "/agents"), api("GET", "/envs")]); sr.i.forEach(x => { stats[x.as] = x; }); ar.i.forEach(x => { if (x.cap) caps[x.n] = x.cap; }); envs = er.i; } catch (_) {}
  if (view !== "overview") return;
  const rows = (d.agents || []).map(a => { const x = stats[a.as] || {}; return `<tr><td>${esc(a.as)}</td><td class="num">${a.by.in_progress || 0}</td><td class="num">${a.by.todo || 0}</td><td class="num">${a.by.review || 0}</td><td class="num">${a.by.blocked || 0}</td><td class="num">${a.inbox ? `<b>${a.inbox}</b>` : "0"}</td><td class="num">${x.done ?? 0}</td><td class="num">${x.cycle_h != null ? x.cycle_h + "h" : "—"}</td><td class="num">${x.decline_rate != null ? Math.round(x.decline_rate * 100) + "%" : "—"}</td><td class="num">${caps[a.as] || "—"}</td></tr>`; }).join("");
  const att = (d.attention || []).length ? `<div class="panel"><h2>Needs attention</h2><ul class="risks">${d.attention.map(r => `<li class="rk ${r.sev}"><span class="sev">${r.sev}</span><span>${esc(r.m)}${r.ids.length ? ` <span class="muted">${r.ids.slice(0, 8).map(i => "#" + i).join(" ")}${r.ids.length > 8 ? " …" : ""}</span>` : ""}</span></li>`).join("")}</ul></div>` : "";
  el.innerHTML = `<div class="pad">
    <div class="tiles">
      <div class="tile"><div class="n">${open}</div><div class="l">Open tasks</div></div>
      <div class="tile"><div class="n">${by.in_progress || 0}</div><div class="l">In progress</div></div>
      <div class="tile"><div class="n">${by.review || 0}</div><div class="l">In review</div></div>
      <div class="tile ${by.blocked ? "bad" : ""}"><div class="n">${by.blocked || 0}</div><div class="l">Blocked</div></div>
      <div class="tile ${(b.nu || []).length ? "warn" : ""}"><div class="n">${(b.nu || []).length}</div><div class="l">Nudged (stale)</div></div>
      <div class="tile ok"><div class="n">${by.done || 0}</div><div class="l">Done</div></div>
    </div>
    ${att}
    ${mt.sprints ? sprintPanel(sp, ss) : ""}
    ${fl ? flowPanel(fl) : ""}
    ${vel && vel.i.length > 1 && !mt.gate && mt.group !== "okr" ? velPanel(vel) : ""}
    ${okr ? okrPanel(okr) : ""}
    ${sla ? slaPanel(sla) : ""}
    ${costPanel(cost)}
    <div class="panel"><h2>Environments</h2>${envs.length ? `<table class="tb"><tr><th>Env</th><th>Deployed</th><th>By</th><th>When</th><th></th></tr>${envs.map(e => `<tr><td><b>${esc(e.n)}</b>${e.frozen ? ` <span class="badge red">Frozen until ${esc(e.frozen.slice(5, 16))} UTC</span>` : ""}</td><td class="num">${esc((e.sha || "—").slice(0, 10))}</td><td>${esc(e.by || "—")}</td><td class="muted">${e.at ? esc(ago(e.at)) + " ago" : "—"}</td><td class="num"><button type="button" class="mini" ${e.frozen ? `data-unfreeze="${esc(e.n)}">Unfreeze` : `data-freeze="${esc(e.n)}">Freeze`}</button></td></tr>`).join("")}</table>` : '<span class="muted">No deploys recorded yet. Agents record them with the deploy_env tool; freezes stop deploys and notify everyone.</span>'}</div>
    <div class="grid2">
      <div class="panel"><h2>Agent workload</h2>${rows ? `<table class="tb"><tr><th>Agent</th><th>Doing</th><th>To do</th><th>Review</th><th>Blocked</th><th>Inbox</th><th>Done 30d</th><th>Cycle</th><th>Decl.</th><th>Cap</th></tr>${rows}</table>` : '<span class="muted">No assigned work yet.</span>'}</div>
      <div class="panel"><h2>Live activity</h2>${feed.length ? `<ul class="feed">${feed.slice(0, 14).map(f => `<li><time>${esc(f.t)}</time><span>${esc(f.m)}</span></li>`).join("")}</ul>` : '<span class="muted">Events appear here as agents and humans work.</span>'}</div>
    </div></div>`;
}

async function renderAgents() {
  const el = $("v-agents");
  const [ag, d, lk, ak, rv, msgs] = await Promise.all([api("GET", "/agents"), refreshDash(), api("GET", "/locks"), api("GET", "/asks"), api("GET", "/reviews"), api("GET", "/messages?limit=12").catch(() => ({i: []}))]);
  let sups = [];
  try { sups = ((await api("GET", "/settings")).supervisors || "").split(",").map(x => x.trim().toLowerCase()).filter(Boolean); } catch (_) {}
  if (view !== "agents") return;
  const load = {}; ((d && d.agents) || []).forEach(a => { load[a.as] = a.by; });
  const cards = ag.i.map(a => {
    const by = load[a.n] || {}, open = sum(by) - (by.done || 0) - (by.declined || 0), cap = a.cap || 0, over = cap && open > cap;
    const sup = sups.includes(a.n.toLowerCase());
    return `<article class="panel agent${sup ? " sup" : ""}"><div class="hd"><span class="avatar lg ${sup ? "" : "ag"}">${esc(initials(a.n))}</span><div><b>${esc(a.n)}</b><span class="muted">${sup ? "Supervisor · gets escalations" : "Agent"}</span></div>${a.inbox ? `<span class="badge teal">${a.inbox} unread</span>` : ""}${a.silent ? '<span class="badge red">Silent</span>' : ""}${over ? '<span class="badge red">Over capacity</span>' : ""}</div>
      ${a.stx ? `<div class="statusline">${esc(a.stx)}${a.stk ? ` <span class="mono">#${a.stk}</span>` : ""}</div>` : ""}
      <div class="kv"><span>Last seen</span><b>${a.seen ? esc(ago(a.seen)) + " ago" : "never"}</b></div>
      <div class="kv"><span>Delivery</span><b>${a.hook ? "Webhook" + (a.hs ? " · signed" : "") : "Polls inbox"}</b></div>
      <div class="kv"><span>Open tasks</span><b>${open}${cap ? " of " + cap : ""}</b></div>
      ${cap ? `<div class="bar"><i class="${over ? "bad" : ""}" style="width:${Math.min(100, Math.round(100 * open / cap))}%"></i></div>` : ""}
      ${a.sk ? `<div class="skills">${a.sk.split(",").map(x => `<span class="chip">${esc(x)}</span>`).join("")}</div>` : ""}
      <div class="btnrow"><button type="button" class="mini" data-inbox="${esc(a.n)}">Open inbox</button>${a.hook ? `<button type="button" class="mini" data-wake="${esc(a.n)}" title="Send a wake-up ping to its webhook">Wake</button>` : ""}<button type="button" class="mini" data-msg="${esc(a.n)}">Message</button><button type="button" class="mini" data-hook="${esc(a.n)}" data-url="${esc(a.hook || "")}" data-cap="${a.cap || ""}" data-skills="${esc(a.sk || "")}">Settings</button></div></article>`;
  }).join("");
  const locks = lk.i.length ? lk.i.map(l => `<div class="coord"><div><b class="mono">${esc(l.name)}</b><div class="muted">held by ${esc(l.owner)} until ${esc(l.until.slice(11, 16))} UTC${l.note ? " — " + esc(l.note) : ""}</div></div><button type="button" class="mini" data-unlock="${esc(l.name)}" data-owner="${esc(l.owner)}">Release</button></div>`).join("") : '<span class="muted">Nothing is locked.</span>';
  const asks = ak.i.length ? ak.i.map(x => `<div class="coord"><div><b>${esc(x.from)} → ${esc(x.to)}</b>${x.tk ? ` <span class="mono">#${x.tk}</span>` : ""}<div>${esc(x.q)}</div><div class="muted">answer by ${esc(x.due.slice(5, 16))} UTC${x.due < new Date().toISOString().slice(0, 19).replace("T", " ") ? ' · <b style="color:var(--bad)">overdue</b>' : ""}</div></div><button type="button" class="mini primary" data-answer="${x.id}" data-q="${esc(x.q)}" data-to="${esc(x.to)}">Answer</button></div>`).join("") : '<span class="muted">No open questions between agents.</span>';
  const revs = rv.i.length ? rv.i.map(x => `<div class="coord"><div><b>${esc(x.to)}</b> reviews <button type="button" class="link" data-open="${x.tk}"><span class="mono">#${x.tk}</span></button><div class="muted">asked by ${esc(x.from)} · ${esc(ago(x.c))} ago${x.note ? " — " + esc(x.note) : ""}</div></div></div>`).join("") : '<span class="muted">No reviews waiting.</span>';
  el.innerHTML = `<div class="pad"><p class="muted lead">An agent is told the moment a task is assigned to it, handed over, commented on or goes stale. It reads this from its inbox, or gets it pushed to a webhook.</p>
    ${cards ? `<div class="agentcards">${cards}</div>` : '<span class="muted">No agents yet — assign a task to someone.</span>'}
    <div class="grid2"><div class="panel"><h2>Locks <span class="muted">${lk.i.length}</span></h2>${locks}</div><div class="panel"><h2>Open questions <span class="muted">${ak.i.length}</span></h2>${asks}</div></div>
    <div class="grid2"><div class="panel"><h2>Reviews waiting <span class="muted">${rv.i.length}</span></h2>${revs}</div><div class="panel" id="inboxview"><h2>Inbox</h2><span class="muted">Pick an agent to see their notifications.</span></div></div>
    <div class="panel"><h2>Messages <span class="muted">latest</span></h2>${msgs.i.length ? msgs.i.slice().reverse().map(m => `<div class="msg"><b>${esc(m.from)}</b> → ${esc(m.to)}${m.tk ? ` <button type="button" class="link" data-open="${m.tk}"><span class="mono">#${m.tk}</span></button>` : ""}<div>${esc(m.c)}</div><small class="muted">${esc(ago(m.ts))} ago</small></div>`).join("") : '<span class="muted">No messages yet. Agents write to each other with the say tool; use Message on an agent card to write to one yourself.</span>'}
      <p><button type="button" class="mini primary" data-msg="">New message</button></p></div>
    <div class="panel"><h2>Register an agent</h2><p class="muted">Add a push webhook, a signing secret, a capacity and skills for an agent. Skills drive automatic routing (assign a task to “auto” with a matching label). Agents also appear here once a task is assigned to them or they send a heartbeat.</p><button type="button" class="mini primary" id="addagent">+ Register agent</button></div></div>`;
}

async function showInbox(name) {
  const r = await api("GET", "/inbox?as=" + encodeURIComponent(name) + "&all=1&limit=30");
  const v = $("inboxview");
  v.innerHTML = `<h2>Inbox · ${esc(name)}</h2>` + (r.i.length ? r.i.slice().reverse().map(n => `<div class="inb ${n.rd ? "rd" : ""}">${esc(n.m)}<small>${esc(n.ty)} · ${esc(n.by || "")} · ${esc(ago(n.ts))}${n.hk ? " · hook " + esc(n.hk) : ""}</small></div>`).join("") : '<span class="muted">Empty.</span>') +
    `<p><button type="button" class="mini" data-ackall="${esc(name)}">Mark all read</button></p>`;
}

function hookDlg(name, url, cap, skills) {
  dlg(`<h3>Agent webhook</h3><label>Agent name (exactly as used in Assignee)<input name="n" required value="${esc(name)}"></label>
    <label>Webhook URL (blank = inbox only)<input name="hook" placeholder="https://…" value="${esc(url)}"></label>
    <label>Signing secret (optional, HMAC-SHA256 in X-Taskboard-Signature)<input name="secret" placeholder="leave empty to keep current"></label>
    <label>Capacity per sprint (points, or tasks if you don't use points; 0 = unlimited)<input type="number" name="capacity" min="0" value="${esc(cap || "")}"></label>
    <label>Skills (comma separated, e.g. frontend, qa) — used for automatic routing<input name="skills" value="${esc(skills || "")}"></label>`,
    async d => { if (!d.secret) delete d.secret; d.capacity = +d.capacity || 0; await api("POST", "/agents", d); renderAgents(); });
}

// ---- SSE (events are tiny deltas: {t,a,id,b,d}) ----
let esDown = false;
addEventListener("pagehide", () => { if (es) es.close(); });      // a reload must not leave a dead stream behind
function connect() {
  if (es) es.close();
  es = new EventSource(`${BASE}/api/events?${B.multi ? "" : "board=" + B.id + "&"}token=${encodeURIComponent(tok)}`);
  es.onopen = () => { $("dot").classList.add("on"); $("who").textContent = "live"; if (esDown) { esDown = false; loadBoard().catch(() => {}); } };   // catch up after a gap
  es.onerror = () => { esDown = true; $("dot").classList.remove("on"); $("who").textContent = "reconnecting…"; };
  es.onmessage = e => onEv(JSON.parse(e.data));
}

function onEv(ev) {
  $("who").textContent = `${ev.a}: ${ev.t}${ev.t.startsWith("task") ? " #" + ev.id : ""}`;
  const [k, a] = ev.t.split(".");
  feed.unshift({t: new Date().toTimeString().slice(0, 5), m: `${ev.a}: ${ev.t}${k === "task" ? " #" + ev.id : ""}${ev.d && ev.d.st ? " → " + ev.d.st : ""}`}); feed.length = Math.min(feed.length, 30);
  kickDash();
  if (a === "commented") return;
  if (k === "spec") { if (view === "specs") renderSpecs(); return; }
  if (k === "task") {
    if (a === "moved" && ev.d && ev.d.b != null && !(B.multi ? B.boardIds.includes(ev.d.b) : B.id == ev.d.b)) {
      const j = B.tasks.findIndex(t => t.id == ev.id); if (j >= 0) B.tasks.splice(j, 1); render(); return;
    }
    if (B.multi && ev.b != null && !B.boardIds.includes(ev.b)) return;
    const i = B.tasks.findIndex(t => t.id == ev.id);
    if (a === "deleted") { if (i >= 0) B.tasks.splice(i, 1); }
    else {
      const t = i >= 0 ? B.tasks[i] : {id: ev.id, description: "", assignee: "", kind: "human", priority: 3, status: "backlog", watch: "", short_url: "", video_id: "", publish_at: "", pr_url: "", sha: ""};
      for (const [sk, v] of Object.entries(ev.d || {})) if (KM.task[sk]) t[KM.task[sk]] = v;
      if (i < 0) B.tasks.push(t);
    }
    render();
  } else if (k === "sprint") loadBoard();
  else if (k === "board") loadBoards($("board").value);
}

// ---- dialogs ----
let MET = {}, TYPES = ["task", "story", "bug", "epic", "spike", "chore"];
api("GET", "/methods").then(r => { r.i.forEach(m => { MET[m.id] = m; }); TYPES = r.types || TYPES; if (B) render(); }).catch(() => {});
const meth = () => (B && B.multi) ? {sprints: false, wip: false, term: "Sprint", columns: ST.filter(x => x !== "declined"), labels: {}} : (B && MET[B.method]) || {sprints: true, wip: false, term: "Sprint", columns: ST.filter(x => x !== "declined"), labels: {}};
const L = x => (meth().labels || {})[x] || LBL[x];
const term = () => meth().term || "Sprint";
const bcfg = () => { try { return JSON.parse((B && B.cfg) || "{}"); } catch (_) { return {}; } };
let dirty = false;
function closeDlg(force) {
  if (!force && dirty && !confirm("Discard your changes?")) return;
  dirty = false; $("dlg").close();
}
function dlg(html, ok, extra, saveLabel) {
  const f = $("f");
  dirty = false;
  f.innerHTML = html + `<menu>${extra || ""}<button type="button" id="dcancel">Cancel</button><button class="primary" id="dsave" value="ok">${saveLabel || "Save"}</button></menu>`;
  $("dcancel").onclick = () => closeDlg();
  f.oninput = f.onchange = () => { dirty = true; };
  f.onsubmit = async e => {
    e.preventDefault();
    const btn = $("dsave");
    if (btn.disabled) return;
    btn.disabled = true;
    try { await ok(Object.fromEntries(new FormData(f))); closeDlg(true); } catch (_) { /* api() toasted */ } finally { btn.disabled = false; }
  };
  f.onkeydown = e => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); f.requestSubmit($("dsave")); } };
  $("dlg").showModal();
  const first = f.querySelector("input,textarea,select");
  if (first) first.focus();
}
$("dlg").addEventListener("cancel", e => { if (dirty && !confirm("Discard your changes?")) e.preventDefault(); else dirty = false; });
$("dlg").addEventListener("mousedown", e => { if (e.target === $("dlg")) closeDlg(); });
const opt = (a, v) => a.map(x => `<option ${x == v ? "selected" : ""} ${typeof x === "string" ? `value="${x}"` : ""}>${(typeof x === "string" && L(x)) || x}</option>`).join("");

const methodCards = sel => Object.values(MET).map(m => `<label class="mcard"><input type="radio" name="method" value="${m.id}" ${m.id === sel ? "checked" : ""}><b>${esc(m.name)}</b><span>${esc(m.about)}</span></label>`).join("");
function newBoard() {
  dlg(`<h3>New board</h3><label>Name<input name="name" required></label><label>Description<input name="description"></label>
    <div><label>Way of working</label><div class="methods">${methodCards("scrum")}</div><p class="muted hint">You can change this later in Board settings; nothing is lost.</p></div>`,
    async d => { const b = await api("POST", "/boards", {name: d.name, description: d.description, method: d.method || "scrum"}); await loadBoards(b.id); }, "", "Create board");
}

function boardSetDlg() {
  if (!B) return;
  const cfg = bcfg(), w = cfg.wip || {};
  dlg(`<h3>Board settings · ${esc(B.name)}</h3>
    <div><label>Way of working</label><div class="methods">${methodCards(B.method || "scrum")}</div></div>
    <fieldset class="fs"><legend>Work in progress limits <span class="muted">(kanban, scrumban — blank = no limit)</span></legend>
      <div class="row3"><label>${esc(L("todo"))}<input type="number" name="w_todo" min="1" value="${w.todo || ""}"></label><label>${esc(L("in_progress"))}<input type="number" name="w_in_progress" min="1" value="${w.in_progress || ""}"></label><label>${esc(L("review"))}<input type="number" name="w_review" min="1" value="${w.review || ""}"></label></div>
      <label>When a limit is hit<select name="wip_mode"><option value="warn" ${cfg.wip_mode !== "block" ? "selected" : ""}>Warn — allow it, show a warning</option><option value="block" ${cfg.wip_mode === "block" ? "selected" : ""}>Block — refuse (admins and supervisors can force)</option></select></label></fieldset>
    <fieldset class="fs"><legend>Cadence <span class="muted">(scrum, scrumban, shape up)</span></legend>
      <div class="row2"><label>Length in days<input type="number" name="sprint_days" min="1" max="120" value="${cfg.sprint_days || meth().sprint_days || 14}"></label><label>Keep ready column at least<input type="number" name="replenish_below" min="1" value="${cfg.replenish_below || ""}" placeholder="scrumban only"></label></div>
      <label class="chk"><input type="checkbox" name="auto_rollover" ${cfg.auto_rollover ? "checked" : ""}> When a ${term().toLowerCase()} ends, close it automatically and start the next</label></fieldset>
    <fieldset class="fs"><legend>Support desk <span class="muted">(response target in hours, by priority)</span></legend><div class="row4">${[1, 2, 3, 4].map(p => `<label>P${p}<input type="number" name="sla${p}" min="1" value="${(cfg.sla_hours || {})[p] || ({1: 4, 2: 24, 3: 72, 4: 168})[p]}"></label>`).join("")}</div></fieldset>
    <fieldset class="fs"><legend>Agent spend budgets <span class="muted">(USD, blank = none; the supervisor is warned at 80% and 100%)</span></legend><div class="row2"><label>Per month, this board<input type="number" step="any" min="0" name="budget_usd" value="${cfg.budget_usd || ""}"></label><label>Per task<input type="number" step="any" min="0" name="task_budget_usd" value="${cfg.task_budget_usd || ""}"></label></div></fieldset>
    <label>Phases <span class="muted">(waterfall: one per line; used when the board has no phases yet)</span><textarea name="phases" rows="2">${esc((cfg.phases || []).join("\n"))}</textarea></label>
    <label>Definition of done <span class="muted">(one item per line)</span><textarea name="dod" rows="3">${esc(B.dod || "")}</textarea></label>`,
    async d => {
      const wip = {}; for (const k of ["todo", "in_progress", "review"]) if (d["w_" + k]) wip[k] = +d["w_" + k];
      await api("PATCH", "/boards/" + B.id, {method: d.method, dod: d.dod, cfg: {wip, wip_mode: d.wip_mode, sprint_days: +d.sprint_days || 0, auto_rollover: d.auto_rollover === "on", replenish_below: +d.replenish_below || 0, sla_hours: {1: +d.sla1 || 0, 2: +d.sla2 || 0, 3: +d.sla3 || 0, 4: +d.sla4 || 0}, phases: d.phases, budget_usd: +d.budget_usd || 0, task_budget_usd: +d.task_budget_usd || 0}});
      await loadBoard(); toast("Board settings saved");
    }, "", "Save settings");
}

function retroDlg(sid) {
  api("GET", `/sprints/${sid}/retro`).then(r => dlg(`<h3>Retrospective · ${esc(r.n)}</h3><p class="muted hint">${r.done} of ${r.total} tasks done${r.committed ? ` · committed ${r.committed}` : ""}. Keep it short: what to keep, what to change, and the actions that follow.</p>
    <label>What went well<textarea name="well">${esc(r.well || "")}</textarea></label>
    <label>What to improve<textarea name="improve">${esc(r.improve || "")}</textarea></label>
    <label>Actions <span class="muted">(one per line — each becomes a backlog task labelled “retro”)</span><textarea name="actions" rows="3">${esc((r.actions || []).map(a => a.t).join("\n"))}</textarea></label>`,
    async d => { await api("POST", `/sprints/${sid}/retro`, {well: d.well, improve: d.improve, actions: d.actions}); toast("Retrospective saved"); await loadBoard(); }, "", "Save retrospective"));
}

function sprintDlg(s, preset) {
  if (!B) return;
  const isNew = !(s && s.id);
  s = s || {state: preset || "planned"};
  dlg(`<h3>${isNew ? "New sprint" : "Edit sprint"}</h3><label>Name <span class="muted">(blank = automatic “Sprint N”)</span><input name="name" value="${esc(s.name)}" placeholder="Sprint ${B.sprints.length + 1}"></label>
    <label>Goal<input name="goal" value="${esc(s.goal)}" placeholder="What should this sprint achieve?"></label>
    <div class="row2"><label>Start<input type="date" name="start" value="${esc(s.start)}"></label><label>End<input type="date" name="end" value="${esc(s.end)}"></label></div>
    <label>State<select name="state">${opt(["planned", "active", "closed"], s.state)}</select></label>
    <p class="muted hint">Dates are optional — an active sprint defaults to 14 days. Starting a sprint completes the current one and carries unfinished tasks over.</p>`,
    async d => {
      for (const k of ["name", "goal", "start", "end"]) if (!d[k]) delete d[k];
      if (!isNew) { await api("PATCH", "/sprints/" + s.id, d); await loadBoard(); return; }
      d.board_id = B.id;
      const sp = await api("POST", "/sprints", d);
      autoSprint = false; await loadBoard(); $("sprint").value = String(sp.id); render();
      toast(`${sp.name} created`, "Undo", async () => { await api("DELETE", "/sprints/" + sp.id); autoSprint = true; loadBoard(); });
    }, "", isNew ? "Create sprint" : "Save");
}

async function quickSprint() {
  if (!B) return;
  const sp = await api("POST", "/sprints", {board_id: B.id});
  autoSprint = false; await loadBoard(); $("sprint").value = String(sp.id); render();
  toast(sp.name + " created — press Start sprint when ready");
}
async function sprintAction(id, act) {
  if (act === "close" && !confirm("Complete this " + term().toLowerCase() + "? Unfinished tasks move to the next planned one (or the backlog).")) return;
  let r;
  try { r = await api("POST", `/sprints/${id}/${act}`, {}); }
  catch (e) { if (e && e.e === "gate") toast("This " + term().toLowerCase() + " still has open work — finish it first (a supervisor can force the close)."); return; }
  toast(act === "start" ? term() + " started" : `${term()} completed — ${r.mv} unfinished task(s) moved`);
  autoSprint = true; await loadBoard();
  if (act === "close") retroDlg(id);
}
function editSprint() {
  const id = $("sprint").value;
  const s = B && B.sprints.find(x => x.id == id);
  if (s) sprintDlg(s);
}

const lastChannel = () => ($("chan").value && $("chan").value !== "none") ? $("chan").value : (localStorage.tb_ch || "");
const undoTask = nt => toast(`Task #${nt.id} created` + (nt.assignee ? ` · ${nt.assignee} notified` : ""), "Undo", async () => { await api("DELETE", "/tasks/" + nt.id); });

// inline quick-add (one input per column; Enter = add, Esc = cancel)
let qa = null;
async function quickAdd(status, title) {
  title = title.trim();
  if (B.multi) { qa = null; render(); return taskDlg(0, status, title); }
  if (!title) return;
  const ch = lastChannel();
  const sp = $("sprint").value;
  const body = {board_id: B.id, title, status, kind: "agent", ...(ch ? {channel: ch} : {})};
  if (sp > 0) body.sprint_id = +sp;
  const nt = await api("POST", "/tasks", body);
  qa = {status, text: ""}; render(); undoTask(nt); ensureVisible(nt.id);
}

function handoffDlg(id) {
  const t = B.tasks.find(x => x.id == id); if (!t) return;
  const people = [...new Set(B.tasks.map(x => x.assignee).filter(Boolean))];
  dlg(`<h3>Hand off #${id}</h3><p class="muted hint">${esc(t.title)}${t.assignee ? " · now " + esc(t.assignee) : ""}. The receiver gets one notification with your summary.</p>
    <label>Hand to<input name="to" required list="hpl" placeholder="agent or person"><datalist id="hpl">${people.map(p => `<option value="${esc(p)}">`).join("")}</datalist></label>
    <label>What was done (required)<textarea name="summary" required maxlength="2000" placeholder="State of the work, decisions made, where the code/spec is"></textarea></label>
    <label>What to do next<textarea name="next" maxlength="1000" placeholder="The next concrete step"></textarea></label>
    <label>Status after handoff<select name="st"><option value="">keep (${LBL[t.status]})</option>${ST.filter(x => x !== "declined").map(x => `<option value="${x}">${LBL[x]}</option>`).join("")}</select></label>`,
    async d => { const body = {to: d.to, summary: d.summary, next: d.next}; if (d.st) body.st = d.st; await api("POST", `/tasks/${id}/handoff`, body); toast(`#${id} handed to ${d.to}`); },
    "", "Hand off");
}

function taskDlg(id, status, title, boardId, pri) {
  if (!B) return;
  if (!id && B.multi && !boardId) return dlg(`<h3>New task</h3><label>Which board?<select name="b">${B.boardIds.map(i => `<option value="${i}">${esc(BS[i].name)}</option>`).join("")}</select></label>`,
    async d => { setTimeout(() => taskDlg(0, status, title, +d.b), 60); }, "", "Continue");
  const sp = $("sprint").value;
  const t = (id && B.tasks.find(x => x.id == id)) ||
    {status: status || "todo", priority: pri || 3, kind: "agent", title: title || "", sprint_id: sp > 0 ? +sp : ""};
  const people = [...new Set(B.tasks.map(x => x.assignee).filter(Boolean))];
  const bid = id ? t.board_id : (boardId || B.id), bd = BS[bid] || B;
  if (!id && !t.sprint_id && $("sprint").value !== "none") {      // viewing "No sprint" keeps it sprintless; otherwise land where the team is working
    const act = (bd.sprints || []).filter(x => x.state === "active").pop(), mm = MET[bd.method];
    if (act && (!mm || mm.sprints)) t.sprint_id = act.id;
  }
  dlg(`<div class="dlghead"><span class="crumb">${id ? `<span class="mono">#${id}</span> · ` : ""}${esc(bd.name)}</span><h3>${id ? esc(t.title) : "New task"}</h3></div>
    ${id ? `<div class="dtabs" role="tablist"><button type="button" class="dtab on" role="tab" data-tab="details">Details</button><button type="button" class="dtab" role="tab" data-tab="activity">Activity</button><button type="button" class="dtab" role="tab" data-tab="comments">Comments</button><button type="button" class="dtab" role="tab" data-tab="files">Files</button><button type="button" class="dtab" role="tab" data-tab="review">Review</button></div>` : ""}
    <div class="tp" data-p="details">
    <label>Title<input name="title" required value="${esc(t.title)}"></label>
    <label>Description<textarea name="description">${esc(t.description)}</textarea></label>
    <div class="row2"><label>Assignee (agent or person — they get notified)<input name="assignee" list="ppl" value="${esc(t.assignee)}"><datalist id="ppl">${people.map(p => `<option value="${esc(p)}">`).join("")}</datalist></label>
    <label>Kind<select name="kind">${opt(["agent", "human"], t.kind)}</select></label></div>
    <div class="row2"><label>Status<select name="status">${opt(ST, t.status)}</select></label>
    <label>Priority (1 = high)<select name="priority">${opt([1, 2, 3, 4], t.priority)}</select></label></div>
    ${id ? `<label>Board<select name="move_board">${boards.map(b => `<option value="${b.id}" ${b.id == bid ? "selected" : ""}>${esc(b.name)}</option>`).join("")}</select><small class="muted hint" id="movehint" hidden></small></label>` : ""}
    <div class="row2"><label>Sprint<select name="sprint_id"><option value="">none</option>${bd.sprints.map(s => `<option value="${s.id}" ${s.id == t.sprint_id ? "selected" : ""}>${esc(s.name)}</option>`).join("")}</select></label>
    <label>Area / project (optional)<input name="channel" list="chl" placeholder="e.g. backend, docs" value="${esc(t.channel || lastChannel())}"><datalist id="chl">${[...new Set([...CHANNELS, ...B.tasks.map(x => x.channel).filter(Boolean)])].map(c => `<option value="${esc(c)}">`).join("")}</datalist></label></div>
    <div class="row2"><label>Type<select name="type">${TYPES.map(x => `<option value="${x}" ${x === (t.type || "task") ? "selected" : ""}>${x}</option>`).join("")}</select></label>
    <label>Epic<select name="parent"><option value="">none</option>${B.tasks.filter(x => x.type === "epic" && x.board_id == bid && x.id != id).map(x => `<option value="${x.id}" ${x.id == t.parent ? "selected" : ""}>#${x.id} ${esc(x.title)}</option>`).join("")}</select></label></div>
    <div class="row2"><label>Depends on (task ids, any board)<input name="depends" value="${esc(csv(t.depends).join(", "))}" placeholder="e.g. 12, 15"><input id="dpq" class="dpq" list="dpl" placeholder="Search a task on any board to add it" autocomplete="off"><datalist id="dpl"></datalist></label>
    <label>Points (estimate)<input type="number" name="points" min="0" max="1000" value="${t.points || ""}"></label></div>
    ${id ? '<div id="linksbox" class="linksbox"></div>' + codeBox(t) : ""}
    ${meth().group === "okr" ? `<div class="row2"><label>Target (key result)<input type="number" step="any" min="0" name="target" value="${t.target ?? ""}"></label><label>Current value<input type="number" step="any" min="0" name="current" value="${t.current ?? ""}"></label></div>` : ""}
    ${(bd.dod || "").trim() ? `<div class="dod"><label>Definition of done — every item before this task can be done</label>${bd.dod.split("\n").filter(x => x.trim()).map((x, i) => `<label class="chk"><input type="checkbox" name="dodi" value="${i}" ${csv(t.dod_done).includes(String(i)) ? "checked" : ""}> ${esc(x)}</label>`).join("")}</div>` : ""}
    <details class="more"><summary>More fields (labels, links, blockers, PR)</summary>
    <label>Labels (comma)<input name="labels" list="lbl" value="${esc(csv(t.labels).map(lbShow).join(", "))}"><datalist id="lbl">${SUG_LB.map(l => `<option value="${esc(lbShow(l))}">`).join("")}</datalist></label>
    <div class="row2"><label>PR URL<input name="pr_url" value="${esc(t.pr_url)}"></label><label>SHA / deployed.txt<input name="sha" value="${esc(t.sha)}"></label></div>
    <label>Blocker note<input name="blocker" value="${esc(t.blocker)}"></label>
    <label>Blocked by (task ids)<input name="blocked_by" value="${esc(csv(t.blocked_by).join(", "))}"></label>
    <label>Decline reason (required when declined)<input name="decline_reason" value="${esc(t.decline_reason)}"></label></details>
    </div>
    ${id ? '<div class="tp" data-p="activity" hidden><div id="actl" class="feed2"><span class="muted">Loading…</span></div></div>' : ""}
    ${id ? '<div class="tp" data-p="comments" hidden><div class="cmts" id="cmts"><span class="muted">Loading…</span></div><div class="cmtform"><input id="cin" placeholder="Write a comment — the assignee is notified"><button type="button" id="cgo">Send</button></div></div>' : ""}
    ${id ? '<div class="tp" data-p="files" hidden><div id="filesbox"><span class="muted">Loading…</span></div></div><div class="tp" data-p="review" hidden><div id="rvbox"><span class="muted">Loading…</span></div></div>' : ""}`,
    async d => {
      d.sprint_id = d.sprint_id ? +d.sprint_id : null;
      d.priority = +d.priority;
      d.labels = csv(d.labels).join(",");
      d.depends = csv(d.depends).map(Number).filter(Number.isFinite); d.points = +d.points || 0;
      const moveTo = id && d.move_board ? +d.move_board : 0, moving = !!moveTo && moveTo !== bid; delete d.move_board;
      if (moving) { delete d.sprint_id; delete d.parent; }
      d.parent = d.parent ? +d.parent : null; if (moving) delete d.parent; if ("target" in d) { d.target = d.target === "" ? null : +d.target; d.current = d.current === "" ? 0 : +d.current; } d.dod_done = [...document.querySelectorAll("#f [name=dodi]:checked")].map(x => +x.value); delete d.dodi;
      if (id) d.ver = t.version;
      d.blocked_by = csv(d.blocked_by).map(Number).filter(Number.isFinite);
      if (d.status !== "blocked") { delete d.blocker; delete d.blocked_by; }
      if (d.status === "declined") {
        const dr = (d.decline_reason || "").trim();
        if (!dr) { toast("Decline needs a reason"); throw {e: "missing", f: "dr"}; }
        d.decline_reason = dr;
      } else { d.decline_reason = ""; }
      if (d.channel) localStorage.tb_ch = d.channel;
      if (id) {
        try {
          const r = await api("PATCH", "/tasks/" + id, d);
          if (moving) {
            const mv = await api("POST", `/tasks/${id}/move`, {board: moveTo}), dest = boards.find(b => b.id === moveTo);
            toast(`#${id} moved to ${dest ? dest.name : "the other board"}${mv.moved ? " with " + mv.moved.length + " child task(s)" : ""}${(mv.w || []).length ? " — " + mv.w[0] : ""}`);
          } else if (r && r.w) toast(r.w[0]); else ensureVisible(id);
        }
        catch (e) {
          if (e && e.e === "conflict") { toast("Someone changed this task while you were editing — board reloaded, open it again"); await loadBoard(); }
          else if (e && e.e === "dod") toast("Tick every definition-of-done item before moving this to Done");
          else if (e && e.e === "review") toast("A review is still open on this task — approve it first");
          throw e;
        }
      }
      else { d.board_id = bid; const nt = await api("POST", "/tasks", d); undoTask(nt); ensureVisible(nt.id); }
    },
    id ? '<button type="button" id="handoff">Hand off…</button><button type="button" class="danger sp" id="del">Delete task</button>' : "", id ? "Save" : "Create task");
  {
    const dq = $("dpq"), dl = $("dpl"); let dqT = null;
    if (dq) {
      dq.oninput = () => {
        clearTimeout(dqT); const v = dq.value.trim(); if (v.length < 2 || v[0] === "#") return;
        dqT = setTimeout(async () => {
          try { const r = await api("GET", `/tasks?q=${encodeURIComponent(v)}&limit=8`); dl.innerHTML = (r.items || r.i || []).map(x => `<option value="#${x.id} ${esc(x.title)}" label="${esc(((BS[x.board_id] || boards.find(b => b.id === x.board_id)) || {}).name || "")}"></option>`).join(""); } catch (_) {}
        }, 250);
      };
      dq.onchange = () => {
        const m = /^#(\d+)/.exec(dq.value); if (!m) return;
        const f = $("f").querySelector("[name=depends]"), ids = csv(f.value);
        if (!ids.includes(m[1]) && +m[1] !== id) ids.push(m[1]);
        f.value = ids.join(", "); dq.value = ""; dirty = true;
      };
    }
  }
  if (id) {
    api("GET", `/tasks/${id}/links`).then(l => {
      const chip = x => `<button type="button" class="link lk${x.open ? "" : " done"}" data-open="${x.id}" title="${esc(x.t)}">#${x.id} ${esc(x.t.slice(0, 36))}<small>${x.x ? esc(x.bn) + " · " : ""}${esc(x.st)}</small></button>`;
      const box = $("linksbox"); if (!box) return;
      box.innerHTML = (l.dp.length ? `<div class="lkrow"><b>Depends on</b>${l.dp.map(chip).join("")}</div>` : "") + (l.bl.length ? `<div class="lkrow"><b>Blocks</b>${l.bl.map(chip).join("")}</div>` : "");
      box.onclick = e => { const b = e.target.closest("[data-open]"); if (b) { closeDlg(true); setTimeout(() => openTask(+b.dataset.open), 60); } };
    }).catch(() => {});
    const cl = $("costlog");
    if (cl) cl.onclick = async () => {
      const r = await api("GET", `/tasks/${id}/cost`);
      $("costbox").innerHTML = r.i.map(c => `<div class="cbrow"><span class="chip cost">${money(c.usd)}</span><span>${esc(c.by)} · ${kfmt(c.in + c.out)} tokens${c.model ? " · " + esc(c.model) : ""} · ${esc(ago(c.ts))} ago${c.note ? " — " + esc(c.note) : ""}</span></div>`).join("") || '<span class="muted">No entries.</span>';
      cl.remove();
    };
    $("f").querySelector("[name=move_board]").onchange = e => {
      const to = +e.target.value, mv = to !== bid, h = $("movehint");
      h.hidden = !mv; h.textContent = mv ? "On save the task moves to that board's active sprint (no sprint if it has none) and its epic link is cleared" + (t.type === "epic" ? "; its child tasks move with it" : "") + "." : "";
      const sps = $("f").querySelector("[name=sprint_id]"); if (sps) sps.disabled = mv;
    };
    $("handoff").onclick = () => { $("dlg").close(); dirty = false; handoffDlg(id); };
    let actLoaded = false;
    const loadAct = async () => {
      if (actLoaded) return; actLoaded = true;
      const r = await api("GET", `/tasks/${id}/activity`);
      $("actl").innerHTML = r.i.length ? r.i.map(a => `<div class="act-row ${a.ty}"><time>${esc(ago(a.ts))}</time><span><b>${esc(a.by || "")}</b> ${a.ty === "comment" ? "commented: " : ""}${esc(a.m)}</span></div>`).join("") : '<span class="muted">No activity yet.</span>';
    };
    const loadFiles = async () => {
      const r = await api("GET", `/tasks/${id}/attachments`);
      $("filesbox").innerHTML = (r.i.length ? r.i.map(a => `<div class="filerow"><span class="chip">${esc(a.k || "link")}</span>${a.u ? `<a href="${esc(a.u)}" target="_blank" rel="noopener">${esc(a.n)}</a>` : esc(a.n)}<span class="muted">${esc(a.by || "")} · ${esc(ago(a.c))} ago</span><button type="button" class="mini" data-delatt="${a.id}">Remove</button></div>`).join("") : '<span class="muted">No files yet. Agents register outputs (reports, builds, PRs) here with the attach tool.</span>') +
        `<div class="filesadd"><input id="fan" placeholder="Name"><input id="fau" placeholder="https://…"><select id="fak">${["link", "file", "pr", "report", "log", "other"].map(k => `<option>${k}</option>`).join("")}</select><button type="button" class="mini primary" id="faadd">Add</button></div>`;
    };
    const loadReviews = async () => {
      const r = await api("GET", `/reviews?task=${id}&st=all`);
      $("rvbox").innerHTML = (r.i.length ? r.i.map(v => `<div class="rvrow"><div><b>${esc(v.to)}</b> <span class="badge ${v.st === "approved" ? "teal" : v.st === "changes" ? "red" : ""}">${esc(v.st === "changes" ? "changes requested" : v.st)}</span><div class="muted">requested by ${esc(v.from)} · ${esc(ago(v.c))} ago${v.note ? " — " + esc(v.note) : ""}${v.dn ? "<br>Reviewer: " + esc(v.dn) : ""}</div></div>${v.st === "pending" ? `<div class="rvact"><input class="rvnote" data-rv="${v.id}" placeholder="Note (required for changes)"><button type="button" class="mini primary" data-rvdec="approve" data-rv="${v.id}">Approve</button><button type="button" class="mini danger" data-rvdec="changes" data-rv="${v.id}">Request changes</button></div>` : ""}</div>`).join("") : '<span class="muted">No review requested. While a review is pending or has requested changes this task cannot be marked done.</span>') +
        `<div class="filesadd"><input id="rvto" placeholder="Reviewer (agent or person)"><input id="rvn" placeholder="What should they look at?"><button type="button" class="mini primary" id="rvreq">Request review</button></div>`;
    };
    $("f").addEventListener("click", async e => {
      const t_ = e.target;
      try {
        if (t_.id === "faadd") { const n = $("fan").value.trim(); if (!n) return; await api("POST", `/tasks/${id}/attachments`, {name: n, url: $("fau").value.trim(), kind: $("fak").value}); loadFiles(); }
        else if (t_.dataset && t_.dataset.delatt) { await api("DELETE", "/attachments/" + t_.dataset.delatt); loadFiles(); }
        else if (t_.id === "rvreq") { const to = $("rvto").value.trim(); if (!to) return; await api("POST", `/tasks/${id}/review`, {reviewer: to, note: $("rvn").value.trim()}); loadReviews(); }
        else if (t_.dataset && t_.dataset.rvdec) { const note = document.querySelector(`.rvnote[data-rv="${t_.dataset.rv}"]`).value.trim(); await api("POST", `/reviews/${t_.dataset.rv}/decide`, {decision: t_.dataset.rvdec, note}); loadReviews(); }
      } catch (_) { /* api() toasted */ }
    });
    document.querySelector(".dtabs").onclick = e => {
      const b = e.target.closest("[data-tab]"); if (!b) return;
      document.querySelectorAll(".dtab").forEach(x => x.classList.toggle("on", x === b));
      document.querySelectorAll(".tp").forEach(x => { x.hidden = x.dataset.p !== b.dataset.tab; });
      if (b.dataset.tab === "activity") loadAct();
      if (b.dataset.tab === "files") loadFiles();
      if (b.dataset.tab === "review") loadReviews();
      if (b.dataset.tab === "comments") $("cin").focus();
    };
    const load = async () => {
      const r = await api("GET", `/tasks/${id}/comments`);
      $("cmts").innerHTML = r.i.length ? r.i.map(c => `<div class="cmt"><small>${esc(c.by)} · ${esc(ago(c.ts))}</small><br>${esc(c.c)}</div>`).join("") : '<span class="muted">No comments yet.</span>';
    };
    load().catch(() => {});
    const send = async () => {
      const v = $("cin").value.trim(); if (!v) return;
      await api("POST", `/tasks/${id}/comments`, {c: v}); $("cin").value = ""; load();
    };
    $("cgo").onclick = send;
    $("cin").onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); send(); } };
    const del = $("del");
    del.onclick = async () => {
      if (!del.classList.contains("arm")) {
        del.classList.add("arm"); del.textContent = "Click again to confirm";
        setTimeout(() => { del.classList.remove("arm"); del.textContent = "Delete task"; }, 4000);
        return;
      }
      await api("DELETE", "/tasks/" + id); $("dlg").close();
    };
  }
}


// ---- open a task from any view (switches board if needed) ----
async function openTask(id, boardId) {
  if (boardId && !(B && (B.multi ? B.boardIds.includes(+boardId) : B.id == boardId))) { $("board").value = "b:" + boardId; autoSprint = true; await loadBoard(); }
  setView("board"); $("sprint").value = ""; autoSprint = false; render(); taskDlg(+id);
}

// ---- cost, links, chat channels ----
const money = v => "$" + (v >= 100 ? Math.round(v) : v >= 1 ? v.toFixed(2) : (+v.toFixed(3)).toString());
const kfmt = n => n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? Math.round(n / 1e3) + "k" : String(n || 0);
const CHAT_EV = [["blocked", "A task is blocked"], ["done", "A task is done"], ["review", "A task goes to review"], ["declined", "A task is declined"], ["assigned", "A task waits on a human"],
  ["escalation", "Escalations to the supervisor"], ["sprint", "Sprint started, closed or at risk"], ["budget", "Budget warnings"], ["git", "Pull requests merged, CI failures"]];
let CHATS = [];
const boardPick = sel => `<fieldset class="fs"><legend>Limit to boards <span class="muted">(none ticked = every board)</span></legend><div class="evgrid">${boards.map(b => `<label class="chk"><input type="checkbox" name="tb_${b.id}" ${sel.includes(b.id) ? "checked" : ""}> ${esc(b.name)}</label>`).join("")}</div></fieldset>`;
function msgDlg(to) {
  dlg(`<h3>Message${to ? " to " + esc(to) : ""}</h3><div class="row2"><label>To (agent or person, * = everyone)<input name="to" required value="${esc(to)}"></label><label>From<input name="as" required value="${esc(localStorage.tb_from || "Operator")}"></label></div>
    <label>Message<textarea name="c" required rows="4" placeholder="What should they know or do?"></textarea></label><label>About task # (optional)<input name="task" type="number" min="1"></label>`,
    async d => { localStorage.tb_from = d.as; const body = {to: d.to, c: d.c, as: d.as}; if (d.task) body.task = +d.task; const r = await api("POST", "/messages", body); toast(`Sent to ${r.n} recipient(s)`); if (view === "agents") renderAgents(); }, "", "Send");
}

function codeBox(t) {
  const cm = t.commits || [], pr = /^https:\/\//.test(t.pr_url || "");
  if (!pr && !cm.length && !t.cost) return "";
  return `<div class="codebox"><b>Code &amp; cost</b>
    ${pr ? `<div class="cbrow"><span class="chip link2">PR</span><a href="${esc(t.pr_url)}" target="_blank" rel="noopener">${esc(t.pr_url.replace("https://", ""))}</a></div>` : ""}
    ${cm.slice(-6).reverse().map(c => `<div class="cbrow"><span class="chip mono">${esc((c.h || "").slice(0, 7) || "commit")}</span>${/^https:\/\//.test(c.u || "") ? `<a href="${esc(c.u)}" target="_blank" rel="noopener">${esc(c.m || c.u)}</a>` : `<span>${esc(c.m || "")}</span>`}</div>`).join("")}
    ${t.cost ? `<div class="cbrow"><span class="chip cost">${money(t.cost)}</span><span>${kfmt(t.tokens)} tokens spent by agents</span><button type="button" class="link" id="costlog">Show log</button></div><div id="costbox"></div>` : ""}</div>`;
}

function costPanel(c) {
  if (!c || !c.tot || !c.tot.n) return "";
  const mx = Math.max(0.0001, ...c.day.map(d => d.usd));
  return `<div class="panel"><h2>Agent spend <span class="muted">last ${c.days} days</span></h2>
    <div class="tiles"><div class="tile"><div class="n">${money(c.tot.usd)}</div><div class="l">Spent</div></div><div class="tile"><div class="n">${kfmt(c.tot.in + c.tot.out)}</div><div class="l">Tokens</div></div><div class="tile"><div class="n">${c.tot.n}</div><div class="l">Reports</div></div></div>
    ${c.bud.length ? `<div class="budgets">${c.bud.map(b => `<div class="bud"><span>${esc(b.n)}</span><div class="bar"><i class="${b.pct >= 100 ? "bad" : b.pct >= 80 ? "warn" : ""}" style="width:${Math.min(100, b.pct)}%"></i></div><b>${money(b.spent)} / ${money(b.limit)}</b></div>`).join("")}</div>` : ""}
    <div class="grid2"><div><table class="tb"><tr><th>Agent</th><th>Spent</th><th>Tokens</th><th>Tasks</th></tr>${c.ag.slice(0, 8).map(a => `<tr><td>${esc(a.n)}</td><td class="num">${money(a.usd)}</td><td class="num">${kfmt(a.in + a.out)}</td><td class="num">${a.tasks}</td></tr>`).join("")}</table></div>
    <div><table class="tb"><tr><th>Costliest tasks</th><th>Spent</th></tr>${c.tk.slice(0, 6).map(x => `<tr><td><button type="button" class="link" data-open="${x.id}" data-b="${x.b || ""}"><span class="mono">#${x.id}</span> ${esc((x.t || "").slice(0, 40))}</button></td><td class="num">${money(x.usd)}</td></tr>`).join("")}</table></div></div>
    ${c.day.length > 1 ? `<div class="spark" title="Spend per day">${c.day.map(d => `<i style="height:${Math.max(6, Math.round(d.usd / mx * 100))}%" title="${esc(d.d)} ${money(d.usd)}"></i>`).join("")}</div>` : ""}</div>`;
}

function chatDlg() {
  dlg(`<h3>New chat channel</h3><p class="muted hint">The board posts a short message here when something needs a human. The webhook address is a secret: it is stored on the server and never shown again.</p>
    <div class="row2"><label>Name<input name="n" required placeholder="e.g. Ops team"></label><label>Service<select name="kind">${["slack", "discord", "teams", "telegram", "generic"].map(k => `<option value="${k}">${k[0].toUpperCase() + k.slice(1)}</option>`).join("")}</select></label></div>
    <label>Webhook URL <span class="muted">(Slack, Discord, Teams incoming webhook; generic = any address that accepts JSON)</span><input name="url" placeholder="https://hooks.slack.com/services/…"></label>
    <div class="row2"><label>Telegram bot token<input name="token" placeholder="only for Telegram"></label><label>Telegram chat id, or generic signing secret<input name="target"></label></div>
    <fieldset class="fs"><legend>Tell this channel when</legend><div class="evgrid">${CHAT_EV.map(([k, l]) => `<label class="chk"><input type="checkbox" name="ev_${k}" ${["blocked", "escalation", "sprint", "budget"].includes(k) ? "checked" : ""}> ${esc(l)}</label>`).join("")}</div></fieldset>
    <div class="row2"><label>Only tasks up to priority<select name="minpr"><option value="4">Any priority</option><option value="3">P3 and higher</option><option value="2">P2 and higher</option><option value="1">P1 only</option></select></label>
    <label>Only this board<select name="b"><option value="">All boards</option>${boards.map(b => `<option value="${b.id}">${esc(b.name)}</option>`).join("")}</select></label></div>`,
    async d => {
      const events = CHAT_EV.map(x => x[0]).filter(k => d["ev_" + k]);
      if (!events.length) { toast("Pick at least one event"); throw {e: "missing"}; }
      await api("POST", "/chats", {n: d.n, kind: d.kind, url: d.url, token: d.token, target: d.target, events, minpr: +d.minpr, b: d.b ? +d.b : null});
      renderAdmin();
    }, "", "Add channel");
}

async function adminExtras(e) {
  const t = e.target, b = t.closest("[data-chat]");
  if (t.id === "newchat") { chatDlg(); return true; }
  const ts = t.closest("[data-tokscope]");
  if (ts) {
    const cur = ts.dataset.bs ? ts.dataset.bs.split(",").map(Number) : [];
    dlg(`<h3>Boards this token may use</h3>${boardPick(cur)}`, async d => { await api("POST", "/tokens", {id: +ts.dataset.tokscope, boards: boards.filter(b => d["tb_" + b.id]).map(b => b.id)}); renderAdmin(); }, "", "Save");
    return true;
  }
  if (b) {
    const [act, cid] = b.dataset.chat.split(":");
    try {
      if (act === "test") { const r = await api("POST", `/chats/${cid}/test`, {}); toast(r.ok ? "Test message sent" : "Could not send: " + (r.err || "failed")); renderAdmin(); }
      else if (act === "toggle") { const c = CHATS.find(x => x.id == cid); await api("POST", "/chats", {id: +cid, on: !c.on}); renderAdmin(); }
      else if (act === "del" && confirm("Delete this channel?")) { await api("DELETE", "/chats?id=" + cid); renderAdmin(); }
    } catch (_) {}
    return true;
  }
  if (t.id === "gitsecret") {
    if (!confirm("Create a new secret? The forge webhook must be updated with it.")) return true;
    try { const r = await api("POST", "/admin/git-secret"); dlg(`<h3>Git webhook secret</h3><p class="muted hint">Paste it into the webhook settings of GitHub, GitLab or Gitea. It is also kept in Settings.</p><label>Secret<input readonly value="${esc(r.secret)}" id="gsec"></label>`, async () => {}, "", "Done"); renderAdmin(); } catch (_) {}
    return true;
  }
  return false;
}

// ---- bulk actions ----
function bulkBar() {
  const el = $("bulkbar");
  if (view !== "board" || !picked.size) { el.hidden = true; return; }
  const people = [...new Set(B.tasks.map(x => x.assignee).filter(Boolean))];
  el.hidden = false;
  el.innerHTML = `<b>${picked.size} selected</b>
    <select id="bst" aria-label="Status"><option value="">Status…</option>${ST.filter(x => x !== "declined").map(x => `<option value="${x}">${LBL[x]}</option>`).join("")}</select>
    <input id="bas" list="bpl" placeholder="Assign to…" aria-label="Assignee"><datalist id="bpl">${people.map(p => `<option value="${esc(p)}">`).join("")}</datalist>
    <select id="bsp" aria-label="Sprint"><option value="">Sprint…</option><option value="none">No sprint</option>${B.sprints.map(x => `<option value="${x.id}">${esc(x.name)}</option>`).join("")}</select>
    <select id="bpr" aria-label="Priority"><option value="">Priority…</option>${[1, 2, 3, 4].map(x => `<option>${x}</option>`).join("")}</select>
    <select id="bmv" aria-label="Move to board"><option value="">Move to board…</option>${boards.map(b => `<option value="${b.id}">${esc(b.name)}</option>`).join("")}</select>
    <button type="button" class="primary mini" id="bapply">Apply</button><button type="button" class="mini" id="bclear">Clear</button>`;
}
$("bulkbar").addEventListener("click", async e => {
  if (e.target.id === "bclear") { picked.clear(); return render(); }
  if (e.target.id !== "bapply") return;
  const body = {ids: [...picked]};
  if ($("bst").value) body.st = $("bst").value;
  if ($("bas").value.trim()) body.as = $("bas").value.trim();
  if ($("bsp").value) body.s = $("bsp").value === "none" ? null : +$("bsp").value;
  if ($("bpr").value) body.pr = +$("bpr").value;
  const mv = $("bmv").value, edits = Object.keys(body).length > 1;
  if (!edits && !mv) return toast("Pick at least one change to apply");
  try {
    const msg = [];
    if (edits) { const r = await api("POST", "/tasks/bulk", body); msg.push(`Updated ${r.total ?? r.n ?? picked.size} task(s)`); }
    if (mv) { const r = await api("POST", "/tasks/move", {ids: [...picked], b: +mv}); msg.push(`Moved ${r.n}${r.skipped.length ? `, ${r.skipped.length} already there` : ""}${r.failed.length ? `, ${r.failed.length} failed (${r.failed[0].e})` : ""}`); }
    toast(msg.join(" · ")); picked.clear(); await loadBoard();
  } catch (_) {}
});

// ---- saved views (per browser) ----
const getViews = () => { try { return JSON.parse(localStorage.tb_views || "[]"); } catch (_) { return []; } };
function viewOptions(sel) {
  const vs = getViews();
  $("views").innerHTML = '<option value="">Saved views…</option>' + vs.map((v, i) => `<option value="${i}" ${String(i) === sel ? "selected" : ""}>${esc(v.name)}</option>`).join("") + (vs.length ? '<option value="del">— delete a view…</option>' : "");
}
$("views").onchange = e => {
  const v = e.target.value, vs = getViews();
  if (v === "del") {
    const name = prompt("Delete which view? Type its name:", vs[0] && vs[0].name); if (name === null) return viewOptions("");
    localStorage.tb_views = JSON.stringify(vs.filter(x => x.name !== name.trim())); return viewOptions("");
  }
  const x = vs[+v]; if (!x) return;
  applyPreset(x.preset || "", false); $("asg").value = x.asg || ""; $("chan").value = x.chan || ""; $("sprint").value = x.sprint || ""; $("search").value = x.q || "";
  autoSprint = false; render();
};
$("saveview").onclick = () => dlg(`<h3>Save view</h3><label>Name<input name="name" required maxlength="40" placeholder="e.g. My blocked work"></label><p class="muted hint">Saves preset, assignee, app/channel, sprint and search for this browser.</p>`,
  async d => {
    const vs = getViews().filter(x => x.name !== d.name.trim());
    vs.push({name: d.name.trim(), preset, asg: $("asg").value, chan: $("chan").value, sprint: $("sprint").value, q: $("search").value});
    localStorage.tb_views = JSON.stringify(vs); viewOptions(String(vs.length - 1)); toast("View saved");
  }, "", "Save view");
viewOptions("");

// ---- keyboard shortcuts ----
const SHORTCUTS = [["n", "New task"], ["/", "Search the board"], ["1-6", "Overview / Board / My work / Specs / Agents / Admin"], ["?", "This help"], ["Esc", "Close dialog or inline add"], ["Cmd/Ctrl + Enter", "Save the open dialog"]];
document.addEventListener("keydown", e => {
  if (e.metaKey || e.ctrlKey || e.altKey || $("dlg").open) return;
  const tag = (e.target.tagName || "").toLowerCase();
  if (["input", "textarea", "select"].includes(tag) || e.target.isContentEditable) return;
  if (e.key === "n") { e.preventDefault(); taskDlg(); }
  else if (e.key === "/") { e.preventDefault(); if (view !== "board") setView("board"); $("search").focus(); }
  else if (/^[1-6]$/.test(e.key)) setView(["overview", "board", "mywork", "specs", "agents", "admin"][+e.key - 1]);
  else if (e.key === "?") dlg(`<h3>Keyboard shortcuts</h3><table class="tb">${SHORTCUTS.map(([k, v]) => `<tr><td><kbd>${esc(k)}</kbd></td><td>${esc(v)}</td></tr>`).join("")}</table>`, async () => {}, "", "Close");
});

// ---- wiring (id/delegated handlers only; CSP-safe) ----
const cols = $("v-board");
cols.addEventListener("click", e => {
  const ad = e.target.closest("[data-add]");
  const ap = e.target.closest("[data-addp]"); if (ap) return taskDlg(0, "todo", "", 0, +ap.dataset.addp);
  if (e.target.closest("[data-addobj]")) return okrDlg(0);
  const ak = e.target.closest("[data-addkr]"); if (ak) return okrDlg(+ak.dataset.addkr);
  const ot = e.target.closest("[data-opent]"); if (ot) return taskDlg(+ot.dataset.opent);
  if (ad) { if (B.multi) return taskDlg(0, ad.dataset.add); qa = qa && qa.status === ad.dataset.add ? null : {status: ad.dataset.add, text: ""}; return render(); }
  if (e.target.closest("[data-qacancel]")) { qa = null; return render(); }
  const dec = e.target.closest("[data-decline]");
  if (dec) { e.stopPropagation(); return declineTask(+dec.dataset.decline); }
  const reo = e.target.closest("[data-reopen]");
  if (reo) { e.stopPropagation(); return reopenTask(+reo.dataset.reopen); }
  const sl = e.target.closest("[data-sel]");
  if (sl) { sl.checked ? picked.add(+sl.dataset.sel) : picked.delete(+sl.dataset.sel); sl.closest(".card").classList.toggle("picked", sl.checked); return bulkBar(); }
  const c = e.target.closest(".card");
  if (c) taskDlg(+c.dataset.id);
});
cols.addEventListener("input", e => { if (qa && e.target.closest(".qa")) qa.text = e.target.value; });
cols.addEventListener("submit", async e => {
  const f = e.target.closest(".qa"); if (!f) return;
  e.preventDefault();
  try { await quickAdd(f.dataset.qa, f.t.value); } catch (_) { /* toasted */ }
});
cols.addEventListener("keydown", e => {
  if (e.key === "Escape" && e.target.closest(".qa")) { qa = null; render(); return; }
  const c = e.target.closest(".card");
  if (c && e.target === c && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); taskDlg(+c.dataset.id); }
});
cols.addEventListener("dragstart", e => { const c = e.target.closest(".card"); if (c) { dragId = c.dataset.id; c.classList.add("dragging"); e.dataTransfer.setData("text/plain", dragId); } });
cols.addEventListener("dragend", () => document.querySelectorAll(".dragging,.over").forEach(x => x.classList.remove("dragging", "over")));
cols.addEventListener("dragover", e => { const c = e.target.closest(".col"); if (c) { e.preventDefault(); c.classList.add("over"); } });
cols.addEventListener("dragleave", e => { const c = e.target.closest(".col"); if (c && !c.contains(e.relatedTarget)) c.classList.remove("over"); });
cols.addEventListener("change", async e => {
  const k = e.target.closest(".krcur"); if (!k) return;
  const v = parseFloat(k.value); if (!(v >= 0)) return toast("Enter a number");
  try { await api("PATCH", "/tasks/" + k.dataset.id, {cr: v}); const t = B.tasks.find(x => x.id == k.dataset.id); if (t) t.current = v; render(); } catch (_) { loadBoard(); }
});
cols.addEventListener("drop", async e => {
  const c = e.target.closest(".col");
  if (!c) return;
  e.preventDefault(); c.classList.remove("over");
  const t = B.tasks.find(x => x.id == dragId);
  if (c.dataset.p) {
    if (t && t.priority != c.dataset.p) { const prev = t.priority; t.priority = +c.dataset.p; render(); try { await api("PATCH", "/tasks/" + dragId, {pr: +c.dataset.p}); } catch (_) { t.priority = prev; loadBoard(); } }
    return;
  }
  if (t && t.status !== c.dataset.s) {
    const ns = c.dataset.s;
    if (ns === "declined") return declineTask(+dragId);
    if (t.status === "declined" || t.status === "done") {
      if (ns === "todo" || ns === "in_progress") { try { await api("POST", `/tasks/${dragId}/reopen`, {to: ns}); } catch (_) { loadBoard(); } return; }
    }
    const body = {st: ns};
    if (t.status === "blocked" && ns !== "blocked") { body.bl = ""; body.bb = []; }
    const prev = t.status; t.status = ns; if (ns !== "blocked") { t.blocker = ""; t.blocked_by = ""; }
    if (ns !== "declined") t.decline_reason = "";
    render();
    try { await api("PATCH", "/tasks/" + dragId, body); } catch (_) { t.status = prev; loadBoard(); }
  }
});
const ovEl = $("v-overview"), agEl = $("v-agents");
ovEl.addEventListener("click", async e => {
  const op0 = e.target.closest("[data-open]"); if (op0) return openTask(+op0.dataset.open);
  const ob = e.target.closest("[data-board]"); if (ob) { $("board").value = "b:" + ob.dataset.board; autoSprint = true; await loadBoard(); return; }
  const c = e.target.closest("[data-sclose]"); if (c) return sprintAction(c.dataset.sclose, "close");
  if (e.target.id === "ovnew") sprintDlg(null, "active");
  const fz = e.target.closest("[data-freeze]");
  if (fz) return dlg(`<h3>Freeze ${esc(fz.dataset.freeze)}</h3><p class="muted hint">Deploys to this environment are refused until the freeze ends (supervisors can force). Every agent is notified.</p><label>For how long (minutes)<input type="number" name="until" min="5" value="1440"></label><label>Reason<input name="note" placeholder="e.g. release candidate under test"></label>`,
    async d => { await api("POST", `/envs/${fz.dataset.freeze}/freeze`, {until: +d.until, note: d.note}); renderOverview(); }, "", "Freeze");
  const uf = e.target.closest("[data-unfreeze]");
  if (uf) { await api("POST", `/envs/${uf.dataset.unfreeze}/unfreeze`, {}); return renderOverview(); }
});
agEl.addEventListener("click", async e => {
  const i = e.target.closest("[data-inbox]"); if (i) return showInbox(i.dataset.inbox);
  const h = e.target.closest("[data-hook]"); if (h) return hookDlg(h.dataset.hook, h.dataset.url, h.dataset.cap, h.dataset.skills);
  const mg = e.target.closest("[data-msg]"); if (mg) return msgDlg(mg.dataset.msg);
  const wk = e.target.closest("[data-wake]"); if (wk) { try { const r = await api("POST", "/agents/wake", {n: wk.dataset.wake}); toast(`Wake-up sent: ${r.unread} unread, ${r.ready} ready task(s)`); } catch (_) {} return; }
  const a = e.target.closest("[data-ackall]"); if (a) { await api("POST", "/inbox/ack", {as: a.dataset.ackall}); showInbox(a.dataset.ackall); refreshDash(); return; }
  const ul = e.target.closest("[data-unlock]"); if (ul) { await api("DELETE", "/locks?name=" + encodeURIComponent(ul.dataset.unlock) + "&as=" + encodeURIComponent(ul.dataset.owner)); return renderAgents(); }
  const an = e.target.closest("[data-answer]");
  if (an) return dlg(`<h3>Answer question #${an.dataset.answer}</h3><p class="muted hint">Asked of ${esc(an.dataset.to)}: ${esc(an.dataset.q)}</p><label>Answer<textarea name="a" required></textarea></label>`,
    async d => { await api("POST", `/asks/${an.dataset.answer}/answer`, {a: d.a, as: an.dataset.to}); renderAgents(); }, "", "Send answer");
  const op = e.target.closest("[data-open]"); if (op) return openTask(+op.dataset.open);
  if (e.target.id === "addagent") hookDlg("", "");
});
$("tabs").onclick = e => { const b = e.target.closest("[data-view]"); if (b) setView(b.dataset.view); };
$("bset").onclick = boardSetDlg;
$("sretro").onclick = () => retroDlg($("sprint").value);
$("sstart").onclick = () => sprintAction($("sprint").value, "start");
$("sclose").onclick = () => sprintAction($("sprint").value, "close");
$("asg").onchange = render;
function openPlaybook() {
  const f = $("f");
  f.innerHTML = `<h3>Agent guide</h3>
<p class="pb-lead">Everything in this UI is also available to software agents over a small REST API and an MCP server, so agents never need the screen.</p>
<ul class="pb-list">
  <li><strong>API base</strong> <code>${esc(location.origin + BASE)}/api</code> (health: <code>GET /api/health</code>)</li>
  <li><strong>Auth</strong> <code>Authorization: Bearer &lt;token&gt;</code>. Create one token per agent in Admin → Access.</li>
  <li><strong>MCP</strong> run <code>python3 mcp_server.py</code> with <code>TASKBOARD_URL</code> and <code>TASKBOARD_TOKEN</code> set.</li>
  <li><strong>Start here</strong> <code>dashboard</code>, <code>inbox</code>, <code>claim_next</code>, <code>risks</code>.</li>
</ul>
<p class="pb-link"><a href="AGENTS.md" target="_blank" rel="noopener">Open the full agent guide</a></p>
<menu><button value="x" class="primary">Got it</button></menu>`;
  f.onsubmit = e => { e.preventDefault(); $("dlg").close(); };
  $("dlg").showModal();
}
$("playbook").onclick = openPlaybook;
$("theme").onclick = () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
$("nb").onclick = newBoard;
$("ns").onclick = () => sprintDlg();
$("es").onclick = editSprint;
$("nt").onclick = () => taskDlg();
$("board").onchange = () => { autoSprint = true; loadBoard(); };
$("sprint").onchange = () => { autoSprint = false; render(); };
$("search").oninput = render;
$("chan").onchange = render;
$("presets").onclick = e => {
  const b = e.target.closest("[data-preset]");
  if (b) applyPreset(b.dataset.preset);
};
document.querySelectorAll("#presets .preset").forEach(b => b.classList.toggle("on", b.dataset.preset === preset));
setView(view);
function startLoad() {
  loadBoards().then(() => { const m = /^#t\/(\d+)/.exec(location.hash); if (m && B) openTask(+m[1]); }).catch(() => {
    if (B) return;
    $("main").insertAdjacentHTML("afterbegin", '<div class="pad" id="loadfail"><div class="empty"><b>Could not load the boards</b><br>Retrying…<br><button type="button" class="mini primary" id="loadretry">Try again now</button></div></div>');
    const go = () => { const e = $("loadfail"); if (e) e.remove(); startLoad(); };
    $("loadretry").onclick = go; setTimeout(() => { if ($("loadfail")) go(); }, 5000);
  });
}
startLoad();
addEventListener("hashchange", () => { const m = /^#t\/(\d+)/.exec(location.hash); if (m && B) openTask(+m[1]); });

// ---- collapsible menu (remembered per browser) ----
const shellEl = document.querySelector(".shell");
function setNav(c) {
  shellEl.classList.toggle("collapsed", c);
  $("collapse").setAttribute("aria-expanded", String(!c));
  $("collapse").title = $("collapse").ariaLabel = c ? "Expand menu" : "Collapse menu";
  try { localStorage.tb_nav = c ? "1" : ""; } catch (_) {}
}
$("collapse").onclick = () => setNav(!shellEl.classList.contains("collapsed"));
setNav(localStorage.tb_nav === "1");
