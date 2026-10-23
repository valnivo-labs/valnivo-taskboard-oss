#!/usr/bin/env python3
"""Stdio MCP server over the compact Taskboard REST API. Env: TASKBOARD_URL, TASKBOARD_TOKEN.

Responses are the compact REST JSON as-is (short keys: id,b,s,t,st,pr,as,k,u,d / n,g / p=[done,total]).
Repeated GETs send If-None-Match; an unchanged resource returns {"same":1}.
"""
import json, os, sys, urllib.request, urllib.error
from urllib.parse import urlencode

URL = os.environ.get("TASKBOARD_URL", "http://127.0.0.1:8790").rstrip("/")
TOKEN = os.environ.get("TASKBOARD_TOKEN", "")
ME = os.environ.get("TASKBOARD_AGENT", "")  # default agent name for inbox/dashboard
ST = ["backlog", "todo", "in_progress", "review", "blocked", "done"]
etags = {}


def api(method, path, body=None, timeout=20):
    h = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}
    if method == "GET" and path in etags:
        h["If-None-Match"] = etags[path][0]
    req = urllib.request.Request(URL + "/api" + path, method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.load(r)
            if method == "GET" and r.headers.get("ETag"):
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


TOOLS = {
    "boards": ("List boards", S(), lambda a: api("GET", "/boards")),
    "board": ("Board snapshot: sprints (p=[done,total]) + tasks without descriptions. Pass d=1 for descriptions.",
              S(["id"], id=i, d=i), lambda a: api("GET", f"/boards/{a['id']}" + qs(a, ["d"]))),
    "tasks": ("List tasks. Filters: board_id|b, sprint_id|s('none'), status|st (single, comma-list, or 'open'/=exclude done), as|assignee, kind|k, lb, ch, open=1, blocked=1, q. Paging: limit,offset. Result {n,o,i}.",
              S(board_id=i, b=i, tm=s, bs=s, sprint_id=s, s=s, status=s, st=s, assignee=s, **{"as": s}, kind=s, k=s, q=s, lb=s, ch=s, blocked=i, open=i, ready=i, limit=i, offset=i, d=i),
              lambda a: api("GET", "/tasks" + qs(a, ["board_id", "b", "tm", "bs", "sprint_id", "s", "status", "st", "assignee", "as", "kind", "k", "q", "lb", "ch", "blocked", "open", "ready", "limit", "offset", "d"]))),
    "task": ("Get one task incl. description. ctx=1 adds dc: docs/rules of task+sprint+board (with bodies).", S(["id"], id=i, ctx=i),
             lambda a: api("GET", f"/tasks/{a['id']}" + qs(a, ["ctx"]))),
    "add_task": ("Create a task. Keys: ty(type: task|story|bug|epic|spike|chore) pa(parent epic id) b(board) t(title) d(description) st pr(1-4) as(assignee; 'auto' routes by label/skills) k(human|agent) s(sprint) bl bb(blockers) lb(labels csv) ch(area, optional) pu(PR url) sha dp(depends on ids) pt(points). New tasks join the active sprint unless s is given. Common labels: gate (review inbox), next (pick-up queue), bug, feature. Blocked waiting: st=blocked bl=why bb=[ids]",
                 S(["b", "t"], b=i, t=s, d=s, st=stenum, pr=i, **{"as": s}, k={"type": "string", "enum": ["human", "agent"]}, s=i, bl=s, bb={"type": ["array", "string"]}, lb=s, ch=s,  pu=s, sha=s, cm=CM, dp={"type": ["array", "string"]}, pt=i, ty=s, pa=i),
                 lambda a: api("POST", "/tasks", a)),
    "set_task": ("Patch one task by id; changed keys only (ty(type: task|story|bug|epic|spike|chore) pa(parent epic id) t d st pr as s k bl bb lb ch w sh vid at pu sha cm dp pt dn). ver=the v you read: 409 conflict if someone changed the task since. dn=ticked definition-of-done item indexes (done is refused until all board DoD items are ticked and no review is open). sha=TEST DEPLOY sha; cm=[{h,u,m,r?}] commits (replaces list). Block={id,st:'blocked',bl,bb}; leaving blocked clears bl/bb.",
                 S(["id"], id=i, t=s, d=s, st=stenum, pr=i, **{"as": s}, s=i, k=s, bl=s, bb={"type": ["array", "string"]}, lb=s, ch=s,  pu=s, sha=s, cm=CM, dp={"type": ["array", "string"]}, pt=i, ty=s, pa=i, dn={"type": ["array", "string"]}, ver=i),
                 lambda a: api("PATCH", f"/tasks/{a.pop('id')}", a)),
    "set_tasks": ("Bulk patch: ids=[…] plus same field keys as set_task (ch/s sweeps). POST /tasks/bulk or PATCH /tasks. Returns {n,i:[{id,…changed}]}.",
                  S(["ids"], ids={"type": "array", "items": i}, t=s, d=s, st=stenum, pr=i, **{"as": s}, s=i, k=s, bl=s, bb={"type": ["array", "string"]}, lb=s, ch=s,  pu=s, sha=s, cm=CM, dp={"type": ["array", "string"]}, pt=i, ty=s, pa=i),
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
    "inbox": ("Your notifications (task assigned/updated/status/comment/nudged/unblocked). Unread only by default. wait=N (<=30) long-polls until something arrives; ack=1 marks the returned items read. as defaults to $TASKBOARD_AGENT.",
              S(**{"as": s}, wait=i, ack=i, all=i, since=i, limit=i),
              lambda a: api("GET", "/inbox" + qs({**a, "as": a.get("as") or ME}, ["as", "wait", "ack", "all", "since", "limit"]), timeout=40)),
    "ack": ("Mark notifications read: ids=[...] or upto=<id> or neither (=all). as defaults to $TASKBOARD_AGENT.",
            S(**{"as": s}, ids={"type": "array", "items": i}, upto=i), lambda a: api("POST", "/inbox/ack", {**a, "as": a.get("as") or ME})),
    "comment": ("Comment on a task (notifies its assignee + anyone in to=[agent names]).", S(["id", "c"], id=i, c=s, to={"type": "array", "items": s}),
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
    "spec": ("Read one spec with its markdown body (d). rev=N reads an older revision.", S(["id"], id=i, rev=i),
             lambda a: api("GET", f"/specs/{a['id']}" + (f"/revs/{a['rev']}" if a.get("rev") else ""))),
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
                  S(status=s, task=i, **{"as": s}), lambda a: api("POST", "/presence", idn(a))),
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
    "methods": ("Work methodologies a board can use (scrum, kanban, scrumban, shapeup, simple): what each does, whether it has sprints/cycles or WIP limits, plus the work item types.",
                S(), lambda a: api("GET", "/methods")),
    "set_board": ("Configure a board: n(name) d tm dod(definition of done, one per line) mt(method: scrum|kanban|scrumban|shapeup|simple) cf(settings object: wip{status:limit} e.g. {in_progress:3}, wip_mode warn|block, sprint_days, auto_rollover, replenish_below). Kanban/simple boards have no sprints; WIP limits apply to kanban and scrumban.",
                  S(["id"], id=i, n=s, d=s, tm=s, dod=s, mt=s, cf={"type": ["object", "string"]}), lambda a: api("PATCH", f"/boards/{a.pop('id')}", a)),
    "board_flow": ("Kanban flow metrics for a board: WIP vs limits, throughput per week, cycle and lead time (avg, p50, p85), aging work (stuck=true when unusually old), cumulative flow data. days default 30.",
                   S(["id"], id=i, days=i), lambda a: api("GET", f"/boards/{a['id']}/flow" + qs(a, ["days"]))),
    "board_velocity": ("Scrum velocity: per sprint committed vs done (points or tasks) and the average of closed sprints. n = how many sprints (default 8).",
                       S(["id"], id=i, n=i), lambda a: api("GET", f"/boards/{a['id']}/velocity" + qs(a, ["n"]))),
    "retro": ("Read a sprint's retrospective and result summary.", S(["id"], id=i), lambda a: api("GET", f"/sprints/{a['id']}/retro")),
    "save_retro": ("Record a sprint retrospective: well, improve (text), actions (list of improvement actions; each becomes a backlog task labelled 'retro' unless make_tasks=false; saving again never duplicates them).",
                   S(["id"], id=i, well=s, improve=s, actions={"type": ["array", "string"]}, make_tasks={"type": "boolean"}), lambda a: api("POST", f"/sprints/{a.pop('id')}/retro", a)),
    "agents": ("List agents (registered + seen as assignees) with unread inbox counts and webhook URL if set.", S(), lambda a: api("GET", "/agents")),
    "set_agent": ("Register/update an agent: n(name) hook(push webhook URL, '' clears) secret(optional, HMAC-SHA256 in X-Taskboard-Signature) capacity(points or tasks per sprint; 0 = none, drives the capacity risk) skills(csv, e.g. frontend,qa: used by route and as='auto'). Notifications POST {agent,n} there; the inbox always works too.",
                  S(["n"], n=s, hook=s, secret=s, capacity=i, skills=s), lambda a: api("POST", "/agents", a)),
    "del_agent": ("Remove an agent's webhook registration.", S(["n"], n=s), lambda a: api("DELETE", "/agents?n=" + urllib.request.quote(a["n"]))),
    "changes": ("Event deltas since event id: {c:last_id,e:[[id,type,entity,board,delta]]}. Store c, pass back as since.",
                S(["since"], since=i, board_id=i, limit=i), lambda a: api("GET", "/changes" + qs(a, ["since", "board_id", "limit"]))),
}


def send(o):
    sys.stdout.write(json.dumps(o, separators=(",", ":")) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    m = json.loads(line)
    mid, meth = m.get("id"), m.get("method")
    if mid is None:
        continue
    if meth == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": m["params"].get("protocolVersion", "2024-11-05"),
              "capabilities": {"tools": {}}, "serverInfo": {"name": "valnivo-taskboard", "version": "0.3"}}})
    elif meth == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [{"name": n, "description": d, "inputSchema": sc} for n, (d, sc, _) in TOOLS.items()]}})
    elif meth == "tools/call":
        p = m["params"]
        try:
            out = TOOLS[p["name"]][2](dict(p.get("arguments") or {}))
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps(out, separators=(",", ":"), ensure_ascii=False)}],
                  "isError": isinstance(out, dict) and "e" in out}})
        except Exception as e:
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": '{"e":"%s"}' % type(e).__name__}], "isError": True}})
    elif meth == "ping":
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    else:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unknown method"}})
