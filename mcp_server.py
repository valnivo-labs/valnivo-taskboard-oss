#!/usr/bin/env python3
"""Stdio MCP server over the compact Taskboard REST API. Env: TASKBOARD_URL, TASKBOARD_TOKEN.

Responses are the compact REST JSON as-is (short keys: id,b,s,t,st,pr,as,k,u,d / n,g / p=[done,total]).
Repeated GETs send If-None-Match; an unchanged resource returns {"same":1}.
"""
import json, os, re, sys, threading, time, urllib.request, urllib.error
from urllib.parse import urlencode

URL = os.environ.get("TASKBOARD_URL", "http://127.0.0.1:8790").rstrip("/")
TOKEN = os.environ.get("TASKBOARD_TOKEN", "")
ME = os.environ.get("TASKBOARD_AGENT", "")  # default agent name for inbox/dashboard
ST = ["backlog", "todo", "in_progress", "review", "blocked", "done"]
etags = {}
_ctx = threading.local()      # set by the taskboard's own /api/mcp endpoint: the caller's address and token for this one call


def _creds():
    return (getattr(_ctx, "url", None) or URL, getattr(_ctx, "token", None) or TOKEN, not getattr(_ctx, "token", None))


def api(method, path, body=None, timeout=20):
    url, tok, own = _creds()      # own: a stdio server has one token, so its ETag cache is safe; a web caller never shares one
    h = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}
    if own and method == "GET" and path in etags:
        h["If-None-Match"] = etags[path][0]
    req = urllib.request.Request(url + "/api" + path, method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.load(r)
            if own and method == "GET" and r.headers.get("ETag"):
                etags[path] = (r.headers["ETag"], out)
            return out
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return {"same": 1}
        try:
            return json.loads(e.read())
        except Exception:
            return {"e": str(e.code)}


def idn(a):
    """Default the acting agent from $TASKBOARD_AGENT (the server ignores `as` for named non-admin tokens)."""
    return {**a, "as": a.get("as") or ME} if (a.get("as") or ME) else a


_BRIEF = {}                   # briefing cursor per caller (one key for a stdio server, one per token over the web)


def _briefing(a):
    """Remembers the cursor of the last briefing so repeat calls return only changes."""
    since = None if a.get("full") else _BRIEF.get(getattr(_ctx, "key", "") or "")
    wait = min(int(a.get("wait") or 0), 30) if since is not None else 0           # hold the call open until something arrives (needs the cursor of an earlier briefing)
    r = api("GET", "/briefing" + qs({"as": a.get("as") or ME, "since": since, "ack": a.get("ack"), "tm": a.get("tm"), "bs": a.get("bs"), "full": 1 if a.get("full") else None, "wait": wait or None},
                                    ["as", "since", "ack", "tm", "bs", "full", "wait"]), timeout=wait + 20)
    if isinstance(r, dict) and isinstance(r.get("c"), int):
        _BRIEF[getattr(_ctx, "key", "") or ""] = r["c"]
    return r


def qs(a, keys):
    p = {k: a[k] for k in keys if a.get(k) not in (None, "")}
    return "?" + urlencode(p) if p else ""


def pop(a, k="id"):
    return a.pop(k)


i, s = {"type": "integer"}, {"type": "string"}
CM = {"type": "array", "items": {"type": "object", "properties": {"h": s, "u": s, "m": s, "r": s}}}
stenum = {"type": "string", "enum": ST}
sps = {"type": "string", "enum": ["planned", "active", "closed"]}


def S(req=(), **p):
    return {"type": "object", "properties": p, "required": list(req)}


def api_text(method, path, text=None, timeout=120):
    """Endpoints that speak CSV instead of JSON."""
    req = urllib.request.Request(_creds()[0] + "/api" + path, method=method, headers={"Authorization": "Bearer " + _creds()[1], "Content-Type": "text/csv; charset=utf-8"}, data=text.encode() if text is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return json.loads(body) if r.headers.get("Content-Type", "").startswith("application/json") else body
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"e": str(e.code)}


def _views(a):
    act = a.pop("action", "list")
    if act == "list":
        return api("GET", "/views")
    if act == "save":
        return api("POST", "/views", a)
    if act == "del":
        return api("DELETE", "/views" + qs(a, ["id", "n"]))
    return {"e": "bad", "f": "action"}


def _roles(a):
    act = a.pop("action", "list")
    if act == "list":
        return api("GET", "/roles")
    if act == "create" and a.get("n"):
        return api("POST", "/roles", a)
    if act == "set" and a.get("n"):
        return api("PATCH", "/roles/%s" % a["n"], {"p": a.get("p")})
    if act == "delete" and a.get("n"):
        return api("DELETE", "/roles/%s" % a["n"])
    return {"e": "bad", "f": "action"}


def _tokens(a):
    act = a.pop("action", "list")
    if act == "list":
        return api("GET", "/tokens")
    if act == "create":
        return api("POST", "/tokens", a)
    if act == "revoke" and "id" in a:
        return api("DELETE", "/tokens?id=%s" % a["id"])
    if act == "scope" and "id" in a:
        return api("POST", "/tokens", a)
    if act == "role" and "id" in a and a.get("role"):
        return api("PATCH", "/tokens/%s" % a["id"], {"role": a["role"]})
    return {"e": "bad", "f": "action"}


def _backups(a):
    act = a.get("action", "list")
    if act == "list":
        return api("GET", "/admin/backups")
    if act == "run":
        return api("POST", "/admin/backup", {})
    if act == "digest":
        return api("POST", "/admin/digest", {})
    return {"e": "bad", "f": "action"}


def _chats(a):
    act = a.pop("action", "list")
    if act == "list":
        return api("GET", "/chats")
    if act in ("add", "set"):
        return api("POST", "/chats", a)
    if act in ("del", "test") and "id" in a:
        return api("DELETE", "/chats?id=%s" % a["id"]) if act == "del" else api("POST", "/chats/%s/test" % a["id"], {})
    return {"e": "bad", "f": "action"}


TOOLS = {
    "boards": ("List boards", S(), lambda a: api("GET", "/boards")),
    "board": ("Board snapshot: sprints (p=[done,total]) + tasks without descriptions. Pass d=1 for descriptions.",
              S(["id"], id=i, d=i), lambda a: api("GET", f"/boards/{a['id']}" + qs(a, ["d"]))),
    "tasks": ("List tasks. Filters: board_id|b, sprint_id|s('none'), status|st (single, comma-list, or 'open'/=exclude done), as|assignee, kind|k, lb, ch, open=1, blocked=1, q. Paging: limit,offset. Result {n,o,i}.",
              S(fields=s, fmt=s, board_id=i, b=i, tm=s, bs=s, sprint_id=s, s=s, status=s, st=s, assignee=s, **{"as": s}, kind=s, k=s, q=s, lb=s, ch=s, blocked=i, open=i, ready=i, limit=i, offset=i, d=i),
              lambda a: api("GET", "/tasks" + qs({"limit": 25, "fmt": "t", **a}, ["fields", "fmt", "board_id", "b", "tm", "bs", "sprint_id", "s", "status", "st", "assignee", "as", "kind", "k", "q", "lb", "ch", "blocked", "open", "ready", "limit", "offset", "d"]))),
    "task": ("Get one task incl. description. ctx=1 adds dc: docs/rules of task+sprint+board (with bodies). since=<ev from an earlier read> returns only what changed ({same:1} if nothing).", S(["id"], id=i, ctx=i, brief=i, since=i),
             lambda a: api("GET", f"/tasks/{a['id']}" + qs(a, ["ctx", "brief", "since"]))),
    "add_task": ("Create a task. Keys: ty(type: task|story|bug|epic|spike|chore) pa(parent epic id) b(board) t(title) d(description) st pr(1-4) as(assignee; 'auto' routes by label/skills) k(human|agent) s(sprint) bl bb(blockers) lb(labels csv) ch(area, optional) pu(PR url) sha dp(depends on ids) pt(points) ac(acceptance criteria, one per line) pl(pipeline) rg(rigor: loose|standard|strict, ask for more discipline on this task). New tasks join the active sprint unless s is given. Common labels: gate (review inbox), next (pick-up queue), bug, feature. Blocked waiting: st=blocked bl=why bb=[ids]",
                 S(["b", "t"], b=i, t=s, d=s, st=stenum, pr=i, **{"as": s}, k={"type": "string", "enum": ["human", "agent"]}, s=i, bl=s, bb={"type": ["array", "string"]}, lb=s, ch=s,  pu=s, sha=s, du=s, cm=CM, dp={"type": ["array", "string"]}, pt=i, ty=s, pa=i, tg=s, cr=s),
                 lambda a: api("POST", "/tasks", a)),
    "set_task": ("Patch one task by id; changed keys only (ty(type: task|story|bug|epic|spike|chore) pa(parent epic id) t d st pr as s k bl bb lb ch w sh vid at pu sha cm dp pt dn). ver=the v you read: 409 conflict if someone changed the task since. dn=ticked definition-of-done item indexes (done is refused until all board DoD items are ticked and no review is open). sha=TEST DEPLOY sha; cm=[{h,u,m,r?}] commits (replaces list). Block={id,st:'blocked',bl,bb}; leaving blocked clears bl/bb.",
                 S(["id"], id=i, t=s, d=s, st=stenum, pr=i, **{"as": s}, s=i, k=s, bl=s, bb={"type": ["array", "string"]}, lb=s, ch=s,  pu=s, sha=s, du=s, cm=CM, dp={"type": ["array", "string"]}, pt=i, ty=s, pa=i, tg=s, cr=s, dn={"type": ["array", "string"]}, ver=i),
                 lambda a: api("PATCH", f"/tasks/{a.pop('id')}", a)),
    "set_tasks": ("Bulk patch: ids=[…] plus same field keys as set_task (ch/s sweeps). POST /tasks/bulk or PATCH /tasks. Returns {n,i:[{id,…changed}]}.",
                  S(["ids"], ids={"type": "array", "items": i}, t=s, d=s, st=stenum, pr=i, **{"as": s}, s=i, k=s, bl=s, bb={"type": ["array", "string"]}, lb=s, ch=s,  pu=s, sha=s, du=s, cm=CM, dp={"type": ["array", "string"]}, pt=i, ty=s, pa=i, tg=s, cr=s),
                  lambda a: api("POST", "/tasks/bulk", a)),
    "add_commit": ("Append commit to task (cm). id=task id; h=sha u=github commit URL m=message r=owner/repo (builds u). Without id, task ids are parsed from m (#42 / task:42). NOT the test-deploy `sha` field.",
                   S(["h"], id=i, h=s, u=s, m=s, r=s),
                   lambda a: api("POST", f"/tasks/{a.pop('id')}/commits" if "id" in a else "/tasks/commits", a)),
    "del_task": ("Delete task", S(["id"], id=i), lambda a: api("DELETE", f"/tasks/{a['id']}")),
    "sprints": ("List sprints (optional b=board id); p=[done,total]", S(b=i), lambda a: api("GET", "/sprints" + (f"?board_id={a['b']}" if "b" in a else ""))),
    "add_sprint": ("Create sprint: b(board) only required; n defaults to \"Sprint N\"; st=active starts it now (dates default to 14d, previous active sprint closes and its unfinished tasks carry over). New tasks without s join the active sprint.",
                   S(["b"], b=i, n=s, g=s, **{"from": s, "to": s}, st=sps), lambda a: api("POST", "/sprints", a)),
    "set_sprint": ("Patch sprint by id (n g from to st)", S(["id"], id=i, n=s, g=s, **{"from": s, "to": s}, st=sps),
                   lambda a: api("PATCH", f"/sprints/{a.pop('id')}", a)),
    "add_board": ("Create board: n(name) d tm(team label)", S(["n"], n=s, d=s, tm=s), lambda a: api("POST", "/boards", a)),
    "docs": ("List docs/rules (no bodies unless d=1). Filter sc(board|sprint|task)+r(ref id) or board_id.", S(sc=s, r=i, board_id=i, d=i),
             lambda a: api("GET", "/docs" + qs(a, ["sc", "r", "board_id", "d"]))),
    "add_doc": ("Attach short markdown (d) and/or link (u) to board|sprint|task: sc, r(ref id), t(title).", S(["sc", "r", "t"], sc={"type": "string", "enum": ["board", "sprint", "task"]}, r=i, t=s, d=s, u=s),
                lambda a: api("POST", "/docs", a)),
    "set_doc": ("Patch doc by id (t d u)", S(["id"], id=i, t=s, d=s, u=s), lambda a: api("PATCH", f"/docs/{a.pop('id')}", a)),
    "del_doc": ("Delete doc", S(["id"], id=i), lambda a: api("DELETE", f"/docs/{a['id']}")),
    "portfolio": ("Cross-board rollup for a supervisor or any agent that spans projects: totals, every board with method, status counts, active-sprint health, blocked ids, WIP breaches, per-agent load and what needs attention. Scope it with tm=<board type/team label, e.g. app> or bs=<board ids, comma separated>; no scope = all boards. Same data as `dashboard`, which accepts the same scope.",
                  S(tm=s, bs=s, **{"as": s}), lambda a: api("GET", "/portfolio" + qs({"as": a.get("as") or ME, "tm": a.get("tm"), "bs": a.get("bs")}, ["as", "tm", "bs"]))),
    "dashboard": ("One-call overview (add tm= or bs= to span several boards): boards (status counts, active sprint p=[done,total], blocked/nudged ids), per-agent load + unread inbox. Pass as=<agent> to add `me`: your inbox count and open work (in_progress first).",
                  S(b=i, tm=s, bs=s, **{"as": s}), lambda a: api("GET", "/dashboard" + qs({"as": a.get("as") or ME, "b": a.get("b"), "tm": a.get("tm"), "bs": a.get("bs")}, ["as", "b", "tm", "bs"]))),
    "inbox": ("Your notifications (task assigned/updated/status/comment/nudged/unblocked). Unread only by default. wait=N (<=30) long-polls until something arrives; reading your own inbox marks the returned items read (ack=0 to only peek). as defaults to $TASKBOARD_AGENT.",
              S(**{"as": s}, wait=i, ack=i, all=i, since=i, limit=i),
              lambda a: api("GET", "/inbox" + qs({**a, "as": a.get("as") or ME}, ["as", "wait", "ack", "all", "since", "limit"]), timeout=40)),
    "ack": ("Mark notifications read: ids=[...] or upto=<id> or neither (=all). as defaults to $TASKBOARD_AGENT.",
            S(**{"as": s}, ids={"type": "array", "items": i}, upto=i), lambda a: api("POST", "/inbox/ack", {**a, "as": a.get("as") or ME})),
    "comment": ("Comment on a task (notifies its assignee + anyone in to=[agent names]). quiet=true: a note for the record, nobody is notified.", S(["id", "c"], id=i, c=s, to={"type": "array", "items": s}, quiet={"type": "boolean"}),
                lambda a: api("POST", f"/tasks/{a.pop('id')}/comments", a)),
    "decline_task": ("Decline a task you will not do (any assignee, human or agent). r=reason REQUIRED and concrete. to=[agent names] to also notify (e.g. the requester). Logged as a comment; status -> declined.",
                     S(["id", "r"], id=i, r=s, to={"type": "array", "items": s}), lambda a: api("POST", f"/tasks/{a.pop('id')}/decline", a)),
    "reopen_task": ("Reopen a declined/blocked/done task: clears reason/blockers. to=todo(default)|in_progress|backlog|review, note=why, notify=[agent names].",
                    S(["id"], id=i, to=s, note=s, notify={"type": "array", "items": s}), lambda a: api("POST", f"/tasks/{a.pop('id')}/reopen", a)),
    "comments": ("All comments of a task.", S(["id"], id=i), lambda a: api("GET", f"/tasks/{a['id']}/comments")),
    "start_sprint": ("Start a sprint now (dates default 14d). Closes the board's previous active sprint and carries its unfinished tasks into this one.",
                     S(["id"], id=i), lambda a: api("POST", f"/sprints/{a['id']}/start")),
    "close_sprint": ("Close a sprint. Unfinished tasks move to `to` (sprint id or 'backlog'); default = next planned sprint, else backlog.",
                     S(["id"], id=i, to=s), lambda a: api("POST", f"/sprints/{a.pop('id')}/close", a)),
    "sprint_stats": ("Sprint health: progress vs time, pace, projected finish, burndown [[date,ideal,actual]], load per agent, health green|amber|red and risks [{k,sev,m,ids}] (behind, overdue, late-start, blocked, owner-wait, stale, unassigned, overload, review-queue, scope-creep).",
                     S(["id"], id=i), lambda a: api("GET", f"/sprints/{a['id']}/stats")),
    "risks": ("Risks of every active sprint (optionally one board b). Check this at the start of a work session; owners of open tasks are also pushed a sprint.risk notification when the picture changes.",
              S(b=i, tm=s, bs=s), lambda a: api("GET", "/risks" + qs(a, ["b", "tm", "bs"]))),
    "specs": ("Shared technical specs library (all agents read/write). Search q (title/body/tags), tag, st(draft|review|approved|deprecated), b(board; also returns global specs), tk(task id). Returns {id,t,tg,st,au,rv,tk,u,sn(snippet when q)}; d=1 adds bodies. Search here BEFORE designing something others may have specced.",
              S(q=s, tag=s, st=s, b=i, tk=i, d=i, limit=i), lambda a: api("GET", "/specs" + qs(a, ["q", "tag", "st", "b", "tk", "d", "limit"]))),
    "spec": ("Read one spec with its markdown body (d). rev=N reads an older revision.", S(["id"], id=i, rev=i, outline=i, section=s),
             lambda a: api("GET", f"/specs/{a['id']}" + (f"/revs/{a['rev']}" if a.get("rev") else "") + qs(a, ["outline", "section"]))),
    "add_spec": ("Publish a technical spec for other agents: t(title) d(markdown body) tg(tags csv, e.g. api,auth) st(draft default) b(board id; omit = global) tk(linked task ids) to=[agent names to notify; assignees of linked tasks are always notified].",
                 S(["t"], t=s, d=s, tg=s, st={"type": "string", "enum": ["draft", "review", "approved", "deprecated"]}, b=i, tk={"type": ["array", "string"]}, to={"type": "array", "items": s}, note=s),
                 lambda a: api("POST", "/specs", a)),
    "set_spec": ("Update a spec. d replaces the body; append=text adds to the end (safest for concurrent agents); base=rev you read -> 409 conflict if someone changed it since. note=what changed. Changing body/title creates a new revision; st/tg/tk/b don't.",
                 S(["id"], id=i, t=s, d=s, append=s, tg=s, st={"type": "string", "enum": ["draft", "review", "approved", "deprecated"]}, b=i, tk={"type": ["array", "string"]}, base=i, note=s, to={"type": "array", "items": s}),
                 lambda a: api("PATCH", f"/specs/{a.pop('id')}", a)),
    "spec_history": ("Revision list of a spec (rv, by, note, time).", S(["id"], id=i), lambda a: api("GET", f"/specs/{a['id']}/revs")),
    "del_spec": ("Delete a spec (prefer st=deprecated).", S(["id"], id=i), lambda a: api("DELETE", f"/specs/{a['id']}")),
    "handoff": ("Hand a task to another agent/person with a structured summary: to(name) summary(what you did, REQUIRED) next(what they should do) st(optional). One task.handoff notification to the receiver with summary+next; you stay out of their inbox. Owner handoffs need 'Missing:' and 'Do exactly:' lines in summary/next or description.",
                S(["id", "to", "summary"], id=i, to=s, summary=s, next=s, st=stenum), lambda a: api("POST", f"/tasks/{a.pop('id')}/handoff", a)),
    "task_activity": ("Chronological timeline of a task: field changes (who/when) + comments.", S(["id"], id=i), lambda a: api("GET", f"/tasks/{a['id']}/activity")),
    "attention": ("Supervisor view: what needs a human/lead now (red sprints, blocked high-priority, owner-wait, stale important tasks, unassigned p1/p2, recent declines). Optional board b.",
                  S(b=i, tm=s, bs=s), lambda a: api("GET", "/attention" + qs(a, ["b", "tm", "bs"]))),
    "agent_stats": ("Per-agent stats over `days` (default 30): done, declined, open, blocked, avg cycle hours, avg hours in in_progress/review/blocked, events, comments, decline_rate.",
                    S(b=i, tm=s, bs=s, days=i), lambda a: api("GET", "/stats/agents" + qs(a, ["b", "tm", "bs", "days"]))),
    "templates": ("List task templates (and recurring rules). Recur rule grammar: daily | weekdays | weekly:mon..sun | monthly:1..31.", S(b=i), lambda a: api("GET", "/templates" + qs(a, ["b"]))),
    "add_template": ("Create a template: n(name) b(board; omit=any) t(task title) d as k pr ch lb pt cl(checklist lines) rc(recur rule) nx(first run YYYY-MM-DD). Recurring templates create a task on schedule (skipped while the previous one is still open).",
                     S(["n"], n=s, b=i, t=s, d=s, k=s, pr=i, ch=s, lb=s, pt=i, cl={"type": ["array", "string"]}, rc=s, nx=s, **{"as": s}), lambda a: api("POST", "/templates", a)),
    "set_template": ("Patch a template (same keys; on=false pauses a recurring rule).", S(["id"], id=i, n=s, b=i, t=s, d=s, k=s, pr=i, ch=s, lb=s, pt=i, cl={"type": ["array", "string"]}, rc=s, nx=s, on={"type": "boolean"}, **{"as": s}),
                     lambda a: api("PATCH", f"/templates/{a.pop('id')}", a)),
    "use_template": ("Create a task from a template now. Overrides: t(title) as d b s(sprint).", S(["id"], id=i, t=s, d=s, b=i, s=i, **{"as": s}), lambda a: api("POST", f"/templates/{a.pop('id')}/use", a)),
    "del_template": ("Delete a template.", S(["id"], id=i), lambda a: api("DELETE", f"/templates/{a['id']}")),
    "claim": ("Claim a task so no other agent takes it: ttl minutes (default 120). Sets you as assignee and moves todo->in_progress. 409 claimed if someone else holds it. Renew by claiming again; the claim expires on its own if you go quiet.",
              S(["id"], id=i, ttl=i, **{"as": s}), lambda a: api("POST", f"/tasks/{a['id']}/claim", {k: v for k, v in idn(a).items() if k != 'id'})),
    "release": ("Release your claim on a task.", S(["id"], id=i, **{"as": s}), lambda a: api("POST", f"/tasks/{a['id']}/release", {k: v for k, v in idn(a).items() if k != 'id'})),
    "claim_next": ("Atomically claim the best ready task for you (highest priority, todo, no open dependencies, unclaimed, unassigned or yours). lb=comma labels/skills to match, b=board. Returns {none:1} when the queue is empty.",
                   S(lb=s, b=i, ttl=i, **{"as": s}), lambda a: api("POST", "/claim-next", idn(a))),
    "lock": ("Take a named lock for a shared resource, e.g. env:test, repo:valnivo, spec:12. ttl minutes (default 30). 409 locked (f = owner) if another agent holds it; same owner renews.",
             S(["name"], name=s, ttl=i, note=s, **{"as": s}), lambda a: api("POST", "/locks", idn(a))),
    "unlock": ("Release a lock you hold.", S(["name"], name=s, **{"as": s}), lambda a: api("DELETE", "/locks" + qs(idn(a), ["name", "as"]))),
    "locks": ("List active locks.", S(), lambda a: api("GET", "/locks")),
    "ask": ("Ask another agent a question that needs an answer by a deadline: to, q, task(optional id), due (minutes from now, default 60, or ISO time). Overdue questions nudge both sides and escalate to the supervisor.",
            S(["to", "q"], to=s, q=s, task=i, due=s, **{"as": s}), lambda a: api("POST", "/asks", idn(a))),
    "answer": ("Answer a question addressed to you.", S(["id", "a"], id=i, a=s, **{"as": s}), lambda a: api("POST", f"/asks/{a['id']}/answer", {k: v for k, v in idn(a).items() if k != 'id'})),
    "asks": ("List questions (default: open). Filter to=, from=, task=, st=open|answered|cancelled|all.", S(to=s, **{"from": s}, task=i, st=s), lambda a: api("GET", "/asks" + qs(a, ["to", "from", "task", "st"]))),
    "cancel_ask": ("Cancel a question you asked.", S(["id"], id=i, **{"as": s}), lambda a: api("POST", f"/asks/{a['id']}/cancel", {k: v for k, v in idn(a).items() if k != 'id'})),
    "request_review": ("Ask an agent to review a task (moves it to review). reviewer, note. The task cannot reach done while a review is pending or has requested changes.",
                       S(["id", "reviewer"], id=i, reviewer=s, note=s, **{"as": s}), lambda a: api("POST", f"/tasks/{a['id']}/review", {k: v for k, v in idn(a).items() if k != 'id'})),
    "decide_review": ("Decide a review assigned to you: decision approve|changes (changes needs a note; it sends the task back to in_progress).",
                      S(["id", "decision"], id=i, decision={"type": "string", "enum": ["approve", "changes"]}, note=s, **{"as": s}), lambda a: api("POST", f"/reviews/{a['id']}/decide", {k: v for k, v in idn(a).items() if k != 'id'})),
    "reviews": ("List reviews (default pending). Filter reviewer=, from=, task=, st=pending|approved|changes|all.", S(reviewer=s, **{"from": s}, task=i, st=s), lambda a: api("GET", "/reviews" + qs(a, ["reviewer", "from", "task", "st"]))),
    "heartbeat": ("Tell the board you are alive and what you are doing: status (one line), task (id). Agents silent for 60+ min with in-progress work are flagged and escalated.",
                  S(status=s, task=i, ttl=i, **{"as": s}), lambda a: api("POST", "/presence", idn(a))),
    "route": ("Which agent should take this? Candidates with the skill (or any label match), least loaded first, silent agents last. skill=, lb=. Create a task with as='auto' to assign automatically.",
              S(skill=s, lb=s), lambda a: api("GET", "/route" + qs(a, ["skill", "lb"]))),
    "envs": ("Environment registry: what is deployed where (sha, who, when) and freeze windows.", S(), lambda a: api("GET", "/envs")),
    "deploy_env": ("Record a deploy: n(env name e.g. test|prod) sha task(optional id; updates the task's deployed sha) url. 423 frozen during a freeze window; 409 locked if another agent holds lock env:<n>. Take lock env:<n> before deploying.",
                   S(["n", "sha"], n=s, sha=s, task=i, url=s, force={"type": "boolean"}, **{"as": s}), lambda a: api("POST", "/envs", idn(a))),
    "freeze_env": ("Freeze an environment (supervisor/admin): n, until(minutes or ISO, default 24h), note. Everyone is notified.", S(["n"], n=s, until=s, note=s),
                   lambda a: api("POST", f"/envs/{a.pop('n')}/freeze", a)),
    "unfreeze_env": ("End a freeze (supervisor/admin).", S(["n"], n=s), lambda a: api("POST", f"/envs/{a['n']}/unfreeze", {})),
    "watch": ("Get notified about a task/spec/label/board/tag without owning it: kind task|spec|label|board|tag, ref (id or name).", S(["kind", "ref"], kind=s, ref=s, **{"as": s}), lambda a: api("POST", "/watch", idn(a))),
    "unwatch": ("Stop watching.", S(["kind", "ref"], kind=s, ref=s, **{"as": s}), lambda a: api("DELETE", "/watch" + qs(idn(a), ["kind", "ref", "as"]))),
    "watches": ("List your watches.", S(**{"as": s}), lambda a: api("GET", "/watch" + qs(idn(a), ["as"]))),
    "attach": ("Register an output on a task (test report, build, PR, log, file): id, name, url, kind file|pr|report|log|link|other, note. The assignee is notified.",
               S(["id", "name"], id=i, name=s, url=s, kind=s, note=s), lambda a: api("POST", f"/tasks/{a.pop('id')}/attachments", a)),
    "attachments": ("List a task's attachments.", S(["id"], id=i), lambda a: api("GET", f"/tasks/{a['id']}/attachments")),
    "del_attachment": ("Remove an attachment.", S(["id"], id=i), lambda a: api("DELETE", f"/attachments/{a['id']}")),
    "methods": ("Work methodologies a board can use (scrum, kanban, scrumban, shapeup, simple, waterfall, gtd, lean, eisenhower, okr, support): what each does, whether it has sprints/cycles or WIP limits, plus the work item types.",
                S(), lambda a: api("GET", "/methods")),
    "set_board": ("Configure a board: n(name) d tm dod(definition of done, one per line) mt(method: scrum|kanban|scrumban|shapeup|simple|waterfall|gtd|lean|eisenhower|okr|support) cf(settings object: wip{status:limit} e.g. {in_progress:3}, wip_mode warn|block, sprint_days, auto_rollover, replenish_below, phases[names] for waterfall, sla_hours{1..4:hours} for support). Kanban/simple boards have no sprints; WIP limits apply to kanban and scrumban.",
                  S(["id"], id=i, n=s, d=s, tm=s, dod=s, mt=s, cf={"type": ["object", "string"]}), lambda a: api("PATCH", f"/boards/{a.pop('id')}", a)),
    "board_flow": ("Kanban flow metrics for a board: WIP vs limits, throughput per week, cycle and lead time (avg, p50, p85), aging work (stuck=true when unusually old), cumulative flow data. days default 30.",
                   S(["id"], id=i, days=i), lambda a: api("GET", f"/boards/{a['id']}/flow" + qs(a, ["days"]))),
    "board_velocity": ("Scrum velocity: per sprint committed vs done (points or tasks) and the average of closed sprints. n = how many sprints (default 8).",
                       S(["id"], id=i, n=i), lambda a: api("GET", f"/boards/{a['id']}/velocity" + qs(a, ["n"]))),
    "okr": ("OKR rollup for an OKR board: objectives (epics) with key results (their child tasks), each key result's current/target and progress, objective progress and overall progress. Update a key result with set_task cr=<current value> (and tg=<target>).",
            S(["id"], id=i), lambda a: api("GET", f"/boards/{a['id']}/okr")),
    "board_sla": ("Support-desk SLA state for a board: requests past their response target (breached), those over 75% of it (at_risk) and the targets in hours per priority (cf.sla_hours via set_board).",
                  S(["id"], id=i), lambda a: api("GET", f"/boards/{a['id']}/sla")),
    "retro": ("Read a sprint's retrospective and result summary.", S(["id"], id=i), lambda a: api("GET", f"/sprints/{a['id']}/retro")),
    "save_retro": ("Record a sprint retrospective: well, improve (text), actions (list of improvement actions; each becomes a backlog task labelled 'retro' unless make_tasks=false; saving again never duplicates them).",
                   S(["id"], id=i, well=s, improve=s, actions={"type": ["array", "string"]}, make_tasks={"type": "boolean"}), lambda a: api("POST", f"/sprints/{a.pop('id')}/retro", a)),
    "move_task": ("Move a task to another board: b = target board id; s = target sprint (optional); children (default true) = an epic's child tasks move with it; force = supervisor/admin override of a WIP block. It joins the target's active sprint (none if the board has no sprints or the task is done), its epic link is cleared, and the target board's limits apply.",
                  S(["id", "b"], id=i, b=i, s=i, children={"type": "boolean"}, force={"type": "boolean"}), lambda a: api("POST", f"/tasks/{a.pop('id')}/move", a)),
    "briefing": ("Session start and idle check in ONE call: your unread notifications (each with a `do` hint for the next call), your open work, what needs attention, sprints at risk, and `nw` = ready tasks you could claim. Call it again with no arguments: it then returns only what changed, or just {c:N} when nothing did. full=1 resets. tm/bs scope it across boards.",
                 S(**{"as": s}, full=i, ack=i, tm=s, bs=s, wait=i), lambda a: _briefing(a)),
    "next": ("Claim the best ready task for you (priority, todo, no open dependencies, unclaimed) and get its context in the same response: description, open deps, latest comments, rules, attachment count. lb=labels/skills to match, b=board, ttl=minutes. {none:1} when the queue is empty.",
             S(lb=s, b=i, ttl=i, **{"as": s}), lambda a: api("POST", "/next", idn(a))),
    "finish": ("Wrap up a task in ONE call: summary (required) is posted as a comment; optional attach=[{name,url,kind}], pr, sha, dn=[ticked checklist items]; then st=done (default), or review=<reviewer> to ask for review, or to=<agent> (+next) to hand it over. The done gate (checklist, open reviews) is checked before anything is written.",
               S(["id", "summary"], id=i, summary=s, pr=s, sha=s, dn={"type": ["array", "string"]}, attach={"type": "array", "items": {"type": "object", "properties": {"name": s, "url": s, "kind": s}}}, to=s, next=s, review=s, st=stenum, cost={"type": "object", "properties": {"in": i, "out": i, "usd": {"type": "number"}, "model": s}}, **{"as": s}),
               lambda a: api("POST", f"/tasks/{a['id']}/finish", {k: v for k, v in idn(a).items() if k != 'id'})),
    "cost": ("Log what work cost: in/out tokens and usd (or only model: priced from the admin price table), optional task id and note. Task, board and monthly budgets warn the supervisor. finish takes the same as cost={...}.",
             S(task=i, usd={"type": "number"}, model=s, note=s, **{"in": i, "out": i, "as": s}), lambda a: api("POST", "/cost", idn(a))),
    "overview": ("Single-board analytics rolled up over several boards: flow (throughput, cycle time, cumulative flow, aging), active sprints with burndown and risks, OKR and SLA per board, agent stats. Scope b|tm|bs, days.",
                 S(days=i, b=i, tm=s, bs=s), lambda a: api("GET", "/overview" + qs(a, ["days", "b", "tm", "bs"]))),
    "cost_report": ("Spend per agent, task, board and day, plus monthly budget use. days (default 30), b|tm|bs scope, as=agent.",
                    S(days=i, b=i, tm=s, bs=s, **{"as": s}), lambda a: api("GET", "/cost" + qs(a, ["days", "b", "tm", "bs", "as"]))),
    "links": ("A task's dependencies (dp) and the open tasks waiting on it (bl), across boards: each has board name bn and x=true when it is on another board.",
              S(["id"], id=i), lambda a: api("GET", f"/tasks/{a['id']}/links")),
    "move_tasks": ("Move several tasks (ids, up to 100) to board b; epics bring their children. Same rules as move_task. Returns moved, skipped (already there) and failed [{id,e}].",
                   S(["ids", "b"], ids={"type": "array", "items": i}, b=i, s=i, children={"type": "boolean"}, force={"type": "boolean"}), lambda a: api("POST", "/tasks/move", a)),
    "wake_agent": ("Supervisor/admin: send a wake-up ping to an agent's webhook now (it also happens by itself when work waits and the agent is silent). n=agent name.",
                   S(["n"], n=s), lambda a: api("POST", "/agents/wake", a)),
    "chats": ("Admin: chat channels for humans (slack|discord|teams|telegram|generic). action list|add|set|del|test; add/set take n, kind, url (telegram: token + target chat id), events (blocked,done,review,declined,assigned,escalation,sprint,budget,git), minpr 1-4, b (board), on.",
              S(action=s, id=i, n=s, kind=s, url=s, token=s, target=s, events={"type": "array", "items": s}, minpr=i, b=i, on={"type": "boolean"}), lambda a: _chats(dict(a))),
    "git_secret": ("Admin: create a new secret for the git webhook (POST /api/hooks/git from GitHub, GitLab or Gitea). Returns the secret once to paste into the forge.",
                   S(), lambda a: api("POST", "/admin/git-secret", {})),
    "roles": ("Admin: roles = named sets of permissions. action list (roles + all permission names) | create (n, p=[permissions]) | set (n, p=[permissions]: replace; admin can never lose its admin permissions) | delete (n; not built-in, not in use). Then tokens role sets a token's role.",
              S(action=s, n=s, p={"type": "array", "items": s}), lambda a: _roles(dict(a))),
    "tokens": ("Admin: access tokens. action list | create (n=name, role: any role name, see roles; the token is returned once) | revoke (id) | scope (id, boards=[ids]: limit an existing token to those boards, [] = all) | role (id, role: change an existing token's role; the last admin cannot be demoted, admin clears board limits). create also takes boards=[ids].",
               S(action=s, n=s, role=s, id=i, boards={"type": "array", "items": i}), lambda a: _tokens(dict(a))),
    "say": ("Send a direct message to an agent or a human by name: to (a name, a list of names, or '*' for everyone - supervisors and admins only), c (text), optional task (id it is about) and reply (message id). The recipient sees it in briefing with do='say to=<you>'.",
            S(["to", "c"], to={"type": ["string", "array"]}, c=s, task=i, reply=i, kind={"type": "string", "enum": ["ask", "fyi", "ack"]}, **{"as": s}), lambda a: api("POST", "/messages", idn(a))),
    "messages": ("Conversation log: with=<name>, limit, since (message id). You only read messages you sent or received; supervisors and admins may pass as=<name> to read anyone's. Reading marks the direct messages you received as read (ack=0 to peek).",
                 S(**{"with": s}, limit=i, since=i, **{"as": s}), lambda a: api("GET", "/messages" + qs(a, ["with", "limit", "since", "as"]))),
    "settings": ("Admin: with no arguments reads the settings; with keys changes them: digest_url, digest_hour, alert_url, backup_keep, supervisors (csv names), base_url, wake_after_min, budget_usd, prices (JSON string), git_autodone (1|0).",
                 S(digest_url=s, digest_hour=s, alert_url=s, backup_keep=s, supervisors=s, base_url=s, wake_after_min=s, budget_usd=s, prices=s, git_autodone=s, agent_limits=s, media_hosts=s),
                 lambda a: api("POST", "/settings", a) if a else api("GET", "/settings")),
    "backups": ("Admin: action list (default) | run (verified backup now) | digest (send the daily digest now).", S(action=s), lambda a: _backups(dict(a))),
    "export_data": ("Admin: export everything, or one board (b), as JSON: tasks, sprints, docs, specs, comments. Can be large.", S(b=i), lambda a: api("GET", "/export" + qs(a, ["b"]))),
    "import_data": ("Admin: import an export (data = the object export_data returned). Always creates new copies; nothing is overwritten.", S(["data"], data={"type": "object"}), lambda a: api("POST", "/import", a["data"])),
    "del_board": ("Admin: delete a board and everything on it. Irreversible - export_data first.", S(["id"], id=i), lambda a: api("DELETE", f"/boards/{a['id']}")),
    "del_sprint": ("Supervisor/admin: delete a sprint (its tasks stay, without a sprint).", S(["id"], id=i), lambda a: api("DELETE", f"/sprints/{a['id']}")),
    "views": ("Saved views of the board's filter menu, shared by everyone. action list (default) | save (n=name, f={preset: open|blocked|gate|owner|nudged, asg, chan, sprint, q}) | del (id or n).",
              S(action=s, n=s, id=i, f={"type": "object"}), lambda a: _views(dict(a))),
    "run_templates": ("Create the tasks of every recurring template that is due now (the Admin button 'Run due recurring now'). Returns {made, skipped, failed}.", S(), lambda a: api("POST", "/templates/run", {})),
    "task_cost": ("What agents spent on one task: total (cs usd, tk tokens) and the last 50 entries.", S(["id"], id=i), lambda a: api("GET", f"/tasks/{a['id']}/cost")),
    "nudge": ("Flag stale open tasks now (the periodic check): sets the nudge flag, notifies assignees, auto-unblocks tasks whose blockers are done. b = one board.", S(b=i), lambda a: api("POST", "/nudge" + qs(a, ["b"]), {})),
    "health": ("Is the board up? {ok:true}.", S(), lambda a: api("GET", "/health")),
    "export_csv": ("Admin: tasks as CSV text (all boards, or b). Columns include id,board_id,sprint_id,title,description,status,priority,assignee,kind,channel,labels,points; import_csv reads the same file back.", S(b=i), lambda a: (lambda r: r if isinstance(r, dict) else {"csv": r})(api_text("GET", "/export.csv" + qs(a, ["b"])))),
    "import_csv": ("Admin: import tasks into board b from CSV text (header: title,description,status,priority,assignee,kind,channel,labels,points). Returns {made, bad}.",
                   S(["b", "csv"], b=i, csv=s), lambda a: api_text("POST", "/import/csv?b=%s" % a["b"], a["csv"])),
    "run_start": ("Record that a job for a task has started: task (id), runtime (the CLI or model that ran it, e.g. agent-cli), job (your job id), log (url). Refused with `quota` while the agent is out of quota and `limit` once a daily cap (agent_limits) is used up. Keeps your claim alive. Returns {id}.",
                  S(task=i, runtime=s, job=s, log=s, **{"as": s}), lambda a: api("POST", "/runs", idn(a))),
    "run_end": ("Finish a run: id, st (ok|failed|stopped|quota|timeout), out (what happened, 2000 chars), usd/tokens (optional spend), log, quota_min (with st=quota: no new work for that long), comment=true to post the outcome on the task. failed/timeout/quota escalate to supervisors.",
                S(["id"], id=i, st=s, out=s, usd={"type": "number"}, tokens=i, log=s, quota_min=i, comment={"type": "boolean"}, **{"as": s}), lambda a: api("POST", f"/runs/{a['id']}/end", {k: v for k, v in idn(a).items() if k != "id"})),
    "runs": ("Job history. With task: that task's runs; else filters agent, st, since (UTC 'YYYY-MM-DD HH:MM:SS'), limit. Each run: ag, rt, job, st, at, end, secs, out, log, usd.",
             S(task=i, agent=s, st=s, since=s, limit=i), lambda a: api("GET", f"/tasks/{a['task']}/runs" + qs(a, ["limit"])) if "task" in a else api("GET", "/runs" + qs(a, ["agent", "st", "since", "limit"]))),
    "request_approval": ("Ask a person before doing something risky (deploy, spend, delete, publish): id (task), action (what you want to do), note, to (approver; default the supervisors). The task is blocked until they decide_review approve (continue) or changes (denied, with a note).",
                         S(["id", "action"], id=i, action=s, note=s, to=s, **{"as": s}), lambda a: api("POST", f"/tasks/{a['id']}/approval", {k: v for k, v in idn(a).items() if k != "id"})),
    "verify": ("Independent acceptance of a task in review/done: check it against its acceptance criteria (ac), then ok=true (default) or ok=false with note (sends it back to in_progress). The worker cannot verify their own work.",
               S(["id"], id=i, ok={"type": "boolean"}, note=s, **{"as": s}), lambda a: api("POST", f"/tasks/{a.pop('id')}/verify", a)),
    "timeline": ("One task's whole story: items (ts, by, ty, m, src=recorded|reported) in order, batons (who held it when), lanes (runs per agent with start/end/state/cost) and graph (nodes + edges: delegation, dependency, blocks).",
                 S(["id"], id=i), lambda a: api("GET", f"/tasks/{a['id']}/timeline")),
    "node_report": ("A machine that runs agents (the bridge) reports itself: name, machine, version, agents[], running[], dispatch{}, quota[{source,label,used_pct,resets_at}]. Replaces its last report.",
                    S(["name"], name=s, machine=s, version=s, agents={"type": "array", "items": s}, running={"type": "array"}, caps={"type": "object"}, dispatch={"type": "object"}, quota={"type": "array"}, up=i, taskboard={"type": "boolean"}, worktrees={"type": "boolean"}, note=s), lambda a: api("POST", "/nodes", a)),
    "nodes": ("Machines that run agents and whether they are up (up <=3 min since last report, stale <=15 min, else down), with what they report.", S(), lambda a: api("GET", "/nodes")),
    "quota": ("What can stop an agent taking work: agents with a quota hit or daily limits (used/max today) and the subscription usage machines report.", S(), lambda a: api("GET", "/quota")),
    "system": ("System overview: days=1|7|30. Machines, quota, tasks done/created per day, runs per agent (ok/failed/cost/time), cost coverage, acceptance coverage, receipt freshness, audit chain (admin).", S(days=i), lambda a: api("GET", "/system" + qs(a, ["days"]))),
    "stage": ("Pipeline tasks (pl = ordered stages name|holder|gate; gate auto|review|human): action=start begins stage 1; done = the holder passes the baton on (past the last stage = done); reject + note = send back one stage (review/human stages). A human stage only moves on decide_review.",
              S(["id", "action"], id=i, action=s, note=s, **{"as": s}), lambda a: api("POST", f"/tasks/{a.pop('id')}/stage", a)),
    "history": ("Replay a task's event log and compare it with the card: state in_sync | partial (card has values the log never recorded) | drift (card disagrees with its history), plus diff and unknown fields. Events carry p = earlier values.", S(["id"], id=i), lambda a: api("GET", f"/tasks/{a['id']}/history")),
    "decision": ("Record a settled decision on a story or epic (c, one sentence); everything below it sees the latest in its deep brief. Without c it lists them.",
                 S(["id"], id=i, c=s), lambda a: api("POST", f"/tasks/{a['id']}/decisions", {"c": a["c"]}) if a.get("c") else api("GET", f"/tasks/{a['id']}/decisions")),
    "scaffold": ("Create the usual child tasks under a story/epic in one call: preset standard|bugfix|research, or titles=[string | {t,ty,as}]; chain=true makes each depend on the one before.",
                 S(["id"], id=i, preset=s, titles={"type": "array"}, chain={"type": "boolean"}), lambda a: api("POST", f"/tasks/{a.pop('id')}/scaffold", a)),
    "verify_queue": ("Finished work with acceptance criteria that nobody else has checked, with vf = who should check it (not the worker; qa/review skills first, then least loaded). Agents see their own; as=<name>.", S(**{"as": s}), lambda a: api("GET", "/verify/queue" + qs(a, ["as"]))),
    "faq": ("Answered questions that look like q (shared words): read these before asking someone again.", S(["q"], q=s, b=i), lambda a: api("GET", "/faq" + qs(a, ["q", "b"]))),
    "fit": ("Who has finished work with these labels (lb=a,b) and how often reviewers asked them for changes. `route` uses it as a tie-breaker.", S(["lb"], lb=s), lambda a: api("GET", "/fit" + qs(a, ["lb"]))),
    "policy": ("Which rules apply: b=<board> or task=<id>. Returns the presets (loose|standard|strict), every rule with its meaning, the board's preset and effective rules, and for a task its own rigor and effective rules. Read it before acting if a call is refused.", S(b=i, task=i), lambda a: api("GET", "/policy" + qs(a, ["b", "task"]))),
    "set_policy": ("Supervisor/admin: choose how strict a board is. preset=loose|standard|strict|custom, rules={strict_moves,holder_only,accept_before_start,claim_to_start,verify_before_done,require_parent: true|false} to switch single rules, pipeline='Name | holder | auto|review|human' lines + pipeline_types (default story) to start new stories with that pipeline. A task can ask for more with rg=standard|strict (agents can raise it, only a supervisor can lower it).", S(["id"], id=i, preset=s, rules={"type": "object"}, pipeline=s, pipeline_types={"type": "array", "items": s}, scaffolds={"type": "object"}), lambda a: api("POST", f"/boards/{a.pop('id')}/policy", a)),
    "evidence": ("Say which acceptance criterion was met: id, n (the line number of the criterion), note, ref (a link or label). Without n it lists the evidence and the criteria still open. finish also takes met=[{n,note,ref}].",
                 S(["id"], id=i, n=i, note=s, ref=s), lambda a: api("POST", f"/tasks/{a.pop('id')}/evidence", a) if a.get("n") else api("GET", f"/tasks/{a['id']}/evidence")),
    "propose": ("Suggest work without creating it: b (board), t (title), d, ty (type), pa (parent story/epic), why. A supervisor or admin accepts it (it becomes a task) or rejects it with a reason; you are told.", S(["b", "t"], b=i, t=s, d=s, ty=s, pa=i, why=s, **{"as": s}), lambda a: api("POST", "/proposals", a)),
    "proposals": ("List proposals: st=pending (default)|accepted|rejected|all, b.", S(st=s, b=i), lambda a: api("GET", "/proposals" + qs(a, ["st", "b"]))),
    "decide_proposal": ("Supervisor/admin: id, decision=accept|reject, note (required to reject), optional as (assignee) and ch.", S(["id", "decision"], id=i, decision=s, note=s, ch=s, **{"as": s}), lambda a: api("POST", f"/proposals/{a.pop('id')}/decide", a)),
    "lesson": ("Suggest a lesson for the board (one or two sentences: c, optional task id). It waits out a cooling period; a supervisor then promotes it into the board's rules, which later deep briefs show. finish also takes lesson=.", S(["c"], c=s, task=i, b=i), lambda a: api("POST", "/lessons", a)),
    "lessons": ("List lessons: st=candidate (default)|promoted|rejected|all, b.", S(st=s, b=i), lambda a: api("GET", "/lessons" + qs(a, ["st", "b"]))),
    "decide_lesson": ("Supervisor/admin: id, decision=promote|reject (note required to reject). Promoting before the cooling period ends answers 409 cooling.", S(["id", "decision"], id=i, decision=s, note=s), lambda a: api("POST", f"/lessons/{a.pop('id')}/decide", a)),
    "wait": ("Say you are waiting for someone: id, kind=input|review|human|external|dependency, owner (who), reason. They are asked; long waits go to the supervisors. clear=true when it is over.", S(["id"], id=i, kind=s, owner=s, reason=s, clear={"type": "boolean"}), lambda a: api("POST", f"/tasks/{a.pop('id')}/wait", a)),
    "waits": ("Tasks someone is waiting on, optionally for owner=<name>.", S(owner=s), lambda a: api("GET", "/waits" + qs(a, ["owner"]))),
    "admit_node": ("Admin/supervisor: admit (action=admit) or retire (action=retire) a machine that reports. With setting node_admission=manual a new machine shows as pending and counts for nothing until admitted; a retired one is refused.", S(["name", "action"], name=s, action={"type": "string", "enum": ["admit", "retire"]}), lambda a: api("POST", f"/nodes/{urllib.request.quote(a['name'])}/{a['action']}", {})),
    "snapshot": ("Supervisor/admin: write the read-only board snapshot now (also written every 5 minutes); without write it reports when it was last written.", S(write={"type": "boolean"}), lambda a: api("POST", "/snapshot", {}) if a.get("write") else api("GET", "/snapshot")),
    "today": ("What needs a person, in one call: proposals, lessons, waits, machines, quota, work waiting for an independent check.", S(), lambda a: api("GET", "/today")),
    "audit_verify": ("Supervisor/admin: check the audit chain. ok=false names the first broken event; unchained = older events that predate the chain.", S(), lambda a: api("GET", "/audit/verify")),
    "audit": ("Supervisor/admin: who changed what. Newest first: ts, by, ty (event type), e (entity id), bn (board), m (what changed). Filters actor, type (prefix, e.g. task. or run.), task, b, since (event id), limit.",
              S(actor=s, type=s, task=i, b=i, since=i, limit=i), lambda a: api("GET", "/audit" + qs(a, ["actor", "type", "task", "b", "since", "limit"]))),
    "activity": ("Who did what last: per agent the last task (tk, t), action (ty), one-line m, ts and seen. by=Name adds that agent's last n items (default 10).",
                 S(by=s, n=i), lambda a: api("GET", "/activity" + qs(a, ["by", "n"]))),
    "batch": ("Run up to 20 calls in one round trip: ops=[{m:'GET'|'POST'|'PATCH'|'DELETE', p:'/tasks/7', b:{...}}]. Stops at the first error unless cont=true. Returns [{s:status, r:result}].",
              S(["ops"], ops={"type": "array", "items": {"type": "object", "properties": {"m": s, "p": s, "b": {"type": "object"}}}}, cont={"type": "boolean"}), lambda a: api("POST", "/batch", a)),
    "guide": ("A short playbook for your role (~100-250 tokens): agent, coder, reviewer, supervisor or planner.", S(role=s), lambda a: api("GET", "/guide" + qs(a, ["role"]))),
    "usage": ("Admin: approximate tokens spent per agent and per route since the server started (bytes/4), to see what costs the most.", S(), lambda a: api("GET", "/admin/usage")),
    "agents": ("List agents (registered + seen as assignees) with unread inbox counts and webhook URL if set.", S(), lambda a: api("GET", "/agents")),
    "set_agent": ("Register/update an agent: n(name) hook(push webhook URL, '' clears) secret(optional, HMAC-SHA256 in X-Taskboard-Signature) capacity(points or tasks per sprint; 0 = none, drives the capacity risk) skills(csv, e.g. frontend,qa: used by route and as='auto'). Notifications POST {agent,n} there; the inbox always works too.",
                  S(["n"], n=s, hook=s, secret=s, capacity=i, skills=s, role=s, goal=s, mute=s, quota_min=i, quota_until=s, quota_note=s), lambda a: api("POST", "/agents", a)),
    "del_agent": ("Remove an agent's webhook registration.", S(["n"], n=s), lambda a: api("DELETE", "/agents?n=" + urllib.request.quote(a["n"]))),
    "changes": ("Event deltas since event id: {c:last_id,e:[[id,type,entity,board,delta]]}. Store c, pass back as since.",
                S(["since"], since=i, board_id=i, limit=i), lambda a: api("GET", "/changes" + qs(a, ["since", "board_id", "limit"]))),
}


CORE = ["briefing", "next", "finish", "inbox", "claim", "set_task", "add_task", "tasks", "task", "comment", "say", "handoff", "heartbeat", "answer", "decide_review", "lock", "unlock"]
COORD = ["messages", "claim_next", "release", "locks", "ask", "asks", "cancel_ask", "request_review", "reviews", "watch", "unwatch", "watches", "attach", "attachments", "del_attachment",
         "cost", "links", "route", "envs", "deploy_env", "freeze_env", "unfreeze_env", "decline_task", "reopen_task", "add_commit", "task_activity", "comments", "verify", "timeline", "decision", "verify_queue", "faq", "policy", "evidence", "propose", "proposals", "lesson", "wait"]
ADMIN = ["tokens", "roles", "settings", "backups", "export_data", "export_csv", "import_csv", "import_data", "del_board", "del_sprint", "chats", "git_secret", "wake_agent", "usage"]
GROUPS = {"core": CORE, "coord": COORD, "admin": ADMIN, "plan": [n for n in TOOLS if n not in CORE + COORD + ADMIN]}
PROFILE = os.environ.get("TASKBOARD_TOOLS", "core")      # core (default) | core,coord | core,coord,plan | core,admin | all | tool names


def profile_names(profile):
    if profile == "all":
        return list(TOOLS)
    return [n for n in dict.fromkeys(x for part in profile.split(",") for x in (GROUPS.get(part.strip()) or ([part.strip()] if part.strip() in TOOLS else [])))]


VISIBLE = profile_names(PROFILE)

# one-line descriptions with a key legend for the daily tools; the rest use the first sentence. help(op) returns the full text.
SHORT = {
    "briefing": "Start/idle check in one call: unread items (with `do` hint), your work, attention, sprints at risk, nw=claimable tasks. Repeat calls return only changes; wait=<=30 holds the call open until something arrives.",
    "say": "Message an agent or human: to (name|[names]|'*' supervisors only), c, task?, reply?, kind? (ask = default, wakes them; fyi/ack = for their next briefing, no wake-up).",
    "next": "Claim the best ready task and get its context (desc, deps, comments, rules). lb,b,ttl. {none:1} if empty.",
    "finish": "Wrap up: summary(required)+attach[],pr,sha,dn[],cost{in,out,usd,model},tests,met[{n,note,ref}],lesson,lt; then st=done | review=<who> | to=<who>,next. Gates and the card's check: lines are tested first.",
    "inbox": "Notifications (unread). Reading marks them read (ack=0 to peek). wait<=30 long-polls. Prefer briefing.",
    "claim": "Claim task id (ttl min): you become assignee, todo->in_progress. 409 claimed if held.",
    "set_task": "Patch task id. Keys: t d st pr(1-4) as k s bl bb lb ch pu sha dp pt ty pa dn cr tg po(state of play) nd mk ver. ver=v you read (409 on conflict). dn=ticked checklist idx.",
    "add_task": "Create task. Keys: b(board) t d st pr as('auto'=route) k(human|agent) s bl bb lb ch pu sha dp pt ty(epic|story|task|bug|spike|chore) pa(parent id: an epic, or a story under an epic) ac pl rg.",
    "tasks": "List tasks (default 25, table form). Filters: b tm bs s st(csv|open) as(~contains) k lb ch q ready=1 ty parent; fields=t,st,pr; limit offset d=1.",
    "task": "One task. brief=1 (~60 tokens, has po = state of play, ev) | since=ev (only what changed) | ctx=1 (docs, comments, attachments, reviews) | default full.",
    "comment": "Comment on task id (c); notifies the assignee and to=[names]; quiet=true = no notifications.",
    "handoff": "Reassign id to `to` with summary and next; one notification to them.",
    "heartbeat": "Say you are alive: status (one line), task id (also extends your claim on it, ttl min). Silent agents with open work are escalated.",
    "answer": "Answer question id (a) addressed to you.",
    "decide_review": "Decide review id: decision approve|changes (changes needs note).",
    "lock": "Take lock `name` (env:test, repo:x) ttl min. 409 locked if held.",
    "unlock": "Release lock `name`.",
}


def slim(sc):
    """Drop bare string/integer type declarations: the argument names are self-explanatory and every property costs tokens in every session."""
    props = {k: ({} if v in ({"type": "string"}, {"type": "integer"}) else v) for k, v in sc.get("properties", {}).items()}
    out = {"type": "object", "properties": props}
    if sc.get("required"):
        out["required"] = sc["required"]
    return out


def short(n, d):
    if n in SHORT:
        return SHORT[n]
    x = re.split(r"(?<=[a-z\)])\. ", d, maxsplit=1)[0]
    return x if len(x) <= 130 else x[:127] + "..."


META = {
    "help": ("Full description and arguments of any operation (including ones not listed here): op.", {"type": "object", "properties": {"op": {"type": "string"}}, "required": ["op"]}),
    "tb": ("Call ANY operation by name even if it is not listed: op, a={args}. Use help(op) first.", {"type": "object", "properties": {"op": {"type": "string"}, "a": {"type": "object"}}, "required": ["op"]}),
}


def meta_call(name, a):
    op = a.get("op")
    if op not in TOOLS:
        return {"e": "bad", "f": "op", "ops": sorted(TOOLS)}
    if name == "help":
        d, sc, _ = TOOLS[op]
        return {"op": op, "d": d, "args": sc.get("properties", {}), "req": sc.get("required", [])}
    return TOOLS[op][2](dict(a.get("a") or {}))


INSTRUCTIONS = ("Valnivo Taskboard. Loop: briefing (call again with no args: returns only changes) -> next -> work -> finish. Reply to inbox items with their `do` hint. "
                "help(op) explains any operation, tb(op,a) calls any operation not listed, guide(role) gives a short playbook. Lists: use fields= and brief=1 to save tokens.")


_out = threading.Lock()


def send(o):
    with _out:
        sys.stdout.write(json.dumps(o, separators=(",", ":")) + "\n")
        sys.stdout.flush()


_watching = []


def watch():
    """TASKBOARD_WATCH=1: tell the client (a notifications/message) when something new lands in the inbox, so a sleeping agent does not have to poll.
    Long-polls the inbox without acknowledging anything; the agent still reads and clears items with briefing."""
    seen = 0
    while True:
        try:
            r = api("GET", "/inbox" + qs({"as": ME, "since": seen, "wait": 25}, ["as", "since", "wait"]), timeout=45)
            if not isinstance(r, dict) or "e" in r:
                time.sleep(10)
                continue
            fresh = r.get("i") or []
            if fresh:
                seen = max(n["id"] for n in fresh)
                send({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info", "logger": "taskboard",
                      "data": "taskboard: %d new item(s) for %s (%s) - call briefing" % (len(fresh), ME, fresh[-1].get("m", "")[:80])}})
        except Exception:
            time.sleep(10)


def handle(m, profile=None):
    """One JSON-RPC message -> the response, or None for a notification. Used by the stdio loop below and by the taskboard's /api/mcp (profile = tools=...)."""
    mid, meth = m.get("id"), m.get("method")
    if mid is None:
        return None
    if meth == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": (m.get("params") or {}).get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}, "logging": {}}, "serverInfo": {"name": "valnivo-taskboard", "version": "0.4"}, "instructions": INSTRUCTIONS}}
    if meth == "tools/list":
        tl = [{"name": n, "description": short(n, TOOLS[n][0]), "inputSchema": slim(TOOLS[n][1])} for n in (VISIBLE if profile is None else profile_names(profile))]
        tl += [{"name": n, "description": d, "inputSchema": sc} for n, (d, sc) in META.items()]
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": tl}}
    if meth == "tools/call":
        p = m.get("params") or {}
        try:
            args = dict(p.get("arguments") or {})
            out = meta_call(p["name"], args) if p["name"] in META else TOOLS[p["name"]][2](args)
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps(out, separators=(",", ":"), ensure_ascii=False)}],
                    "isError": isinstance(out, dict) and "e" in out}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": '{"e":"%s"}' % type(e).__name__}], "isError": True}}
    if meth == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unknown method"}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        m = json.loads(line)
        out = handle(m)
        if out is not None:
            send(out)
        if m.get("method") == "initialize" and os.environ.get("TASKBOARD_WATCH") in ("1", "true", "yes") and ME and not _watching:
            _watching.append(threading.Thread(target=watch, daemon=True))
            _watching[0].start()


if __name__ == "__main__":
    main()
