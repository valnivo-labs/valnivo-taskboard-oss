# Agent guide

Everything the web UI does is available to software agents over a small JSON API and an MCP server. Agents never need the screen.

- **API base:** `http(s)://<host>:<port>/api` · health: `GET /api/health`
- **Auth:** `Authorization: Bearer <token>`. One token per agent (Admin → Access, or `POST /api/tokens {"n":"coder-agent","role":"agent"}`; the token is shown once).
- **Identity:** name the token exactly like the agent's assignee name. A named token always acts as itself; shared or admin tokens may pass `as`.
- **Roles:** `admin` everything · `supervisor` agent + delete sprints + receives escalations · `agent` read/write, no deleting boards/sprints, no admin · `viewer` read-only.

## Connect with MCP
```json
{
  "mcpServers": {
    "taskboard": {
      "command": "python3",
      "args": ["/path/to/mcp_server.py"],
      "env": { "TASKBOARD_URL": "http://localhost:8790", "TASKBOARD_TOKEN": "<agent token>", "TASKBOARD_AGENT": "coder-agent" }
    }
  }
}
```
`TASKBOARD_AGENT` makes `inbox`, `dashboard` and the coordination tools default to that agent.

## The working loop
1. `dashboard` (or `risks`) — what is the state of the sprint, anything blocked?
2. `inbox` with `wait=25&ack=1` — long-poll for anything addressed to you.
3. `claim_next` — atomically take the best ready task (or `claim` a specific one).
4. Work. `heartbeat` with a one-line status; `comment`, `attach` outputs, `lock` shared resources.
5. `request_review` or `set_task` `st=done`. `handoff` if someone else continues.

## Compact REST conventions
Short keys, empty values omitted. `?full=1` returns verbose column names (used by the UI).
Task: `id b(board) s(sprint) t(title) d st pr(1-4) as k(human|agent) u bl bb lb ch dp(depends) pt(points) cb cu(claim) dn(done-checklist) v(version) pu(PR) sha cm(commits)`.
Statuses: `backlog todo in_progress review blocked done declined`.
Errors: `{"e":"auth|nf|bad|missing|empty|json|route|method|forbidden|rate|too_big|conflict|claimed|locked|frozen|dod|review|cycle|private_url","f":"field"}`.

```bash
A="Authorization: Bearer $TOKEN"; API=http://localhost:8790/api
curl -s -H "$A" -d '{"b":1,"t":"Fix login","pr":2,"as":"coder-agent","k":"agent","lb":"bug"}' $API/tasks
curl -s -H "$A" -X PATCH -d '{"st":"in_progress"}' $API/tasks/7          # returns only changed keys (+ new version v)
curl -s -H "$A" "$API/tasks?st=open&as=coder-agent"                      # filters: st(csv|open) as(~contains) b s lb ch q ready=1 limit offset
curl -s -H "$A" "$API/tasks/7?ctx=1"                                     # + docs, last comments, attachments, reviews, what it blocks
```
`GET` supports `ETag`/`If-None-Match` (304). `GET /api/changes?since=<id>` returns event deltas; `GET /api/events` is a server-sent-event stream.

## Notifications (inbox and webhook)
`GET /api/inbox?as=<you>&wait=25&ack=1` (the `as` is implied by a named token). Push instead: `POST /api/agents {"n":"coder-agent","hook":"https://…","secret":"…"}` — each notification is POSTed as `{agent, n}` with `X-Taskboard-Signature: sha256=<hmac>`. Webhook targets must be public addresses (set `TASKBOARD_ALLOW_PRIVATE_WEBHOOKS=1` to allow private ones).

Types: `task.assigned task.unassigned task.status task.updated task.comment task.handoff task.ready task.nudged task.unblocked task.attachment sprint.risk ask.new ask.answered ask.overdue review.requested review.approved review.changes claim.expired env.frozen watch.task spec.created spec.updated` and, for supervisors, `esc.*`.

## Coordination primitives
| Need | Call |
|---|---|
| Don't take the same task twice | `claim` / `release` / `claim_next` (claims expire; renew by claiming again) |
| Don't touch a shared thing together | `lock` / `unlock` (`env:test`, `repo:app`, `spec:12`) |
| Hand work over with context | `handoff {id,to,summary,next}` |
| Ask and get an answer by a deadline | `ask {to,q,due}` / `answer` |
| Get work reviewed | `request_review` / `decide_review` — a task cannot be done while a review is open |
| Agree what "done" means | board `dod` checklist; tick items with `dn` |
| Know who is alive | `heartbeat`; silent agents with open work are escalated |
| Route work to the right agent | agent `skills` + `capacity`; assign `as:"auto"` with a matching label |
| Share what is deployed | `deploy_env`, supervisors can `freeze_env` |
| Follow things you don't own | `watch {kind,ref}` |
| Share technical knowledge | specs: `specs` (search first), `add_spec`, `set_spec` with `append` |
| Avoid overwriting each other | `set_task ver=<v>` → `409 conflict` if it changed |

## Ways of working (scrum, kanban and more)
Each board picks a method; change it any time in **Board settings** (nothing is lost) or `PATCH /boards/{id} {"mt":"kanban","cf":{...}}` (MCP `set_board`). `GET /methods` describes them.

| Method | Sprints/cycles | WIP limits | What you get |
|---|---|---|---|
| `scrum` (default) | yes, with a committed scope | no | sprint health and risks, burndown, **velocity** (committed vs done), **retrospective** with actions that become backlog tasks, optional auto-rollover to the next sprint |
| `kanban` | none | yes | **WIP limits** per column (warn or block), **flow metrics**: throughput per week, cycle/lead time (avg, p50, p85), aging work, cumulative flow diagram |
| `scrumban` | optional | yes | kanban flow plus an optional cadence and a **replenish signal** when the Ready column runs low |
| `shapeup` | 6-week **cycles** | no | cycle length and naming (Shaping, Bets, Building, QA, Stuck, Shipped), everything else as scrum |
| `simple` | none | no | just To do, Doing, Done |

Settings (`cf`): `wip` `{"todo":5,"in_progress":3,"review":2}`, `wip_mode` `warn|block`, `sprint_days`, `auto_rollover`, `replenish_below`. In `block` mode a move into a full column returns `409 {"e":"wip","f":"<status>"}`; admins and supervisors can pass `force:true`. In `warn` mode the response carries `"w":["WIP limit for in_progress reached (4 of 3)"]`. Kanban and simple boards reject sprint creation (`{"e":"no_sprints"}`).
APIs: `GET /boards/{id}/flow?days=30` (MCP `board_flow`), `GET /boards/{id}/velocity` (`board_velocity`), `GET|POST /sprints/{id}/retro` (`retro`, `save_retro`). Closing a sprint notifies everyone with work in it (`sprint.closed`); over-limit columns and unusually old work appear in `attention`.

**Work item types.** Tasks have a type `task|story|bug|epic|spike|chore` (`ty`) and an optional parent epic (`pa`, same board). `GET /tasks?ty=epic`, `?parent=<epic id>`; an epic's `?ctx=1` returns `kids` and `prog`.

**Across several boards (supervisors, portfolio agents).** Give the board a type with `tm` (e.g. `app`, `youtube`) when you create it. Then scope any overview with `tm=<type>` and/or `bs=<board ids, comma separated>`; with no scope you see every board: `GET /tasks?tm=app`, `GET /dashboard?bs=1,3`, `GET /attention?tm=app`, `GET /risks?tm=app`, `GET /stats/agents?tm=app`. `GET /portfolio` (MCP `portfolio`) is the cross-board rollup: totals, each board with method, status counts, active-sprint health, blocked ids, WIP breaches, per-agent load, and what needs attention. In the web UI the board menu starts on **All boards** and also offers each board type and each single board.

## Sprints without the screen
`POST /sprints {"b":1}` auto-names and dates it; `st:"active"` starts it (the previous active sprint closes and unfinished tasks carry over). `POST /sprints/{id}/close {"to":<sprint id>|"backlog"}`. `GET /sprints/{id}/stats` → progress, pace, projected finish, burndown, health `green|amber|red`, risks (`behind overdue late-start blocked stale unassigned overload capacity dep-start silent-agent review-queue scope-creep human-wait`). When an active sprint's risks change, everyone with open work in it gets `sprint.risk` once.

## Supervisor
Give a token the `supervisor` role (or list names in Admin → Settings → `supervisors`). Supervisors receive `esc.declined`, `esc.blocked` (p1/p2), `esc.stale`, `esc.silent`, `esc.ask-overdue`, `esc.sprint-red`, `esc.human-card`, and can call `attention` for what needs a lead right now.

## Templates, import/export, backups
`POST /templates` (with `rc`: `daily|weekdays|weekly:mon|monthly:15`) creates recurring tasks; `GET /export`, `GET /export.csv`, `POST /import`, `POST /import/csv?b=`; `POST /admin/backup` (daily automatic, integrity-checked).
