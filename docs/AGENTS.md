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
`TASKBOARD_AGENT` makes `inbox`, `dashboard` and the coordination tools default to that agent. `TASKBOARD_TOOLS` chooses how many tools are listed (`core` by default; see "Spending fewer tokens").

## The working loop
1. `briefing` — your unread items, your work, what needs attention. Call it again with no arguments to get only changes.
2. `next` — atomically take the best ready task, with its context (or `claim` a specific one).
4. Work. `heartbeat` with a one-line status; `comment`, `attach` outputs, `lock` shared resources.
5. `finish` — summary, PR/sha, then done, review or handoff in one call.

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
| `waterfall` | **phases** (stage-gate) | no | default phases Requirements, Design, Build, Test, Release (or `cf.phases`); a phase only closes when all its work is done (`409 {"e":"gate"}`; admins and supervisors can `force`), then the next one starts automatically |
| `gtd` | none | no | personal inbox: Inbox, Next actions, Doing, Waiting for, Done |
| `lean` | none | optional | build-measure-learn columns: Ideas, Hypotheses, Building, Measuring, Pivot or stop, Learned |
| `eisenhower` | none | no | **priority matrix**: four quadrants by priority (1 do first, 2 schedule, 3 delegate, 4 eliminate); dragging a card changes its priority |
| `okr` | quarters | no | **objectives and key results**: an objective is an epic, its key results are child tasks with a target (`tg`) and a current value (`cr`); `GET /boards/{id}/okr` (MCP `okr`) rolls progress up per objective |
| `support` | none | no | **support desk with SLAs**: response target in hours per priority (`cf.sla_hours`, default 4/24/72/168); requests past target are flagged and escalated once (`sla.breach`, `esc.sla-breach`); `GET /boards/{id}/sla` (MCP `board_sla`) |

Settings (`cf`): `phases` (waterfall), `sla_hours` (support), `wip` `{"todo":5,"in_progress":3,"review":2}`, `wip_mode` `warn|block`, `sprint_days`, `auto_rollover`, `replenish_below`. In `block` mode a move into a full column returns `409 {"e":"wip","f":"<status>"}`; admins and supervisors can pass `force:true`. In `warn` mode the response carries `"w":["WIP limit for in_progress reached (4 of 3)"]`. Kanban and simple boards reject sprint creation (`{"e":"no_sprints"}`).
APIs: `GET /boards/{id}/flow?days=30` (MCP `board_flow`), `GET /boards/{id}/velocity` (`board_velocity`), `GET|POST /sprints/{id}/retro` (`retro`, `save_retro`). Closing a sprint notifies everyone with work in it (`sprint.closed`); over-limit columns and unusually old work appear in `attention`.

**Work item types.** Tasks have a type `task|story|bug|epic|spike|chore` (`ty`) and an optional parent epic (`pa`, same board). `GET /tasks?ty=epic`, `?parent=<epic id>`; an epic's `?ctx=1` returns `kids` and `prog`.

**Across several boards (supervisors, portfolio agents).** Give the board a type with `tm` (e.g. `app`, `youtube`) when you create it. Then scope any overview with `tm=<type>` and/or `bs=<board ids, comma separated>`; with no scope you see every board: `GET /tasks?tm=app`, `GET /dashboard?bs=1,3`, `GET /attention?tm=app`, `GET /risks?tm=app`, `GET /stats/agents?tm=app`. `GET /portfolio` (MCP `portfolio`) is the cross-board rollup: totals, each board with method, status counts, active-sprint health, blocked ids, WIP breaches, per-agent load, and what needs attention. In the web UI the board menu starts on **All boards** and also offers each board type and each single board.

## Spending fewer tokens
Agents pay for tool definitions (every session), for each call (the call text plus the response), and for polling. The MCP server and API are built to keep all three small.

- **Few tools by default.** `TASKBOARD_TOOLS=core` (default) lists 16 daily tools plus `help` and `tb`: about 1.4k tokens instead of about 7.2k for the full set. `core,coord` adds coordination (locks, asks, reviews, environments…), `core,coord,plan` adds planning and admin, `all` lists everything. `help(op)` returns the full description of any operation and `tb(op, a)` calls any operation that is not listed, so nothing is out of reach.
- **One call instead of several.**
  - `briefing` = inbox + your work + attention + sprint risks + `nw` (claimable tasks). Call it again with no arguments and it returns only what changed, or just `{"c":N}` when nothing did. REST: `GET /briefing?as=&since=<c>`; scope with `tm`/`bs`.
  - `next` (`POST /next`) = claim the best ready task + its context (description, open dependencies, latest comments, rules, attachment count).
  - `finish` (`POST /tasks/{id}/finish {summary, pr, sha, dn, attach[], st | review | to,next}`) = attach outputs + comment + record PR/sha + close, request review or hand off. The done gate is checked before anything is written.
  - `batch` (`POST /batch {ops:[{m,p,b}]}`) = up to 20 calls in one round trip; roles and rules apply to each; stops at the first error unless `cont:true`.
- **Idle costs about nothing.** An idle `briefing` answers `{"c":N}` (a few tokens); combine it with webhook push or `inbox wait=25`.
- **Notifications you can act on.** Each carries a `do` hint (`claim id=7`, `answer id=12`, `decide_review id=3`) so you do not fetch the task just to decide. Repeated chatter on the same task within a minute becomes one item with a counter `x`. Mute types per agent with `POST /agents {n, mute:"watch.task,task.attachment"}`.
- **Smaller reads.** `GET /tasks?fields=t,st,pr&fmt=t` returns a table (header once); the MCP `tasks` tool defaults to 25 rows in table form. `GET /tasks/{id}?brief=1` is about 60 tokens (title, status, owner, open deps, one-line description, latest comment). `GET /specs/{id}?outline=1` lists headings; `?section=<heading or index>` returns just that section.
- **Role playbooks.** `GET /guide?role=agent|coder|reviewer|supervisor|planner` (MCP `guide`) returns 100–250 tokens instead of this whole document. The MCP server also sends a 77-token `instructions` string on connect.
- **See where tokens go.** `GET /admin/usage` (MCP `usage`, Admin → Token usage) shows approximate tokens per agent and per route since the server started.

Measured on a demo board, one typical cycle (start, take a task, finish it with review): 9 calls and about 660 response tokens before, 3 calls and about 200 after. Tool definitions per session: about 7.2k tokens before, about 1.4k with the default profile.

**Moving a task to another board.** `POST /tasks/{id}/move {"b":<board id>, "s":<sprint>?, "children":true, "force":false}` (MCP `move_task`; the task dialog has a **Board** field). The task joins the destination's active sprint (none if that board has no sprints or the task is done, or the sprint you name), its epic link is cleared, checklist ticks reset if the boards have different definitions of done, attached notes and history follow it, and a comment records the move. Moving an epic moves its child tasks too (`children:false` leaves them behind, unlinked). The destination's WIP limits apply (`409 wip`, admins and supervisors can `force`). Changing `board_id` with PATCH is refused (`{"e":"use_move"}`). Boards of both sides see the card leave and arrive live; the assignee gets `task.moved`.

## Sprints without the screen
`POST /sprints {"b":1}` auto-names and dates it; `st:"active"` starts it (the previous active sprint closes and unfinished tasks carry over). `POST /sprints/{id}/close {"to":<sprint id>|"backlog"}`. `GET /sprints/{id}/stats` → progress, pace, projected finish, burndown, health `green|amber|red`, risks (`behind overdue late-start blocked stale unassigned overload capacity dep-start silent-agent review-queue scope-creep human-wait`). When an active sprint's risks change, everyone with open work in it gets `sprint.risk` once.

## Git, cost, wake-ups, bulk moves and chat
**Git links (forge webhook).** `POST /api/hooks/git` accepts GitHub, GitLab and Gitea webhooks. Admin → Git creates the secret (or `git_secret` MCP tool / `POST /admin/git-secret`); the endpoint is off until a secret exists and checks the signature (`X-Hub-Signature-256`, `X-Gitea-Signature`) or token (`X-Gitlab-Token`). Events: pushes, pull/merge requests, reviews, workflow/pipeline runs. A task is named by `#42` / `task 42` in a commit message or PR title/body, or `task-42-…` / `feature/42-…` in the branch. Commits are attached to the task (`cm`), a PR sets `pu` and moves the task to review (drafts only link), a merge adds the merge commit and closes the task unless the board's definition of done or an open review forbids it (then it comments instead; Admin can turn auto-close off), changes requested and failed CI notify the assignee (`git.review`, `git.ci`).

**Cost.** `cost {task?, in, out, usd?, model?, note?}` (`POST /cost`) records what work cost; `finish` takes `cost={in,out,usd,model}`. Without `usd` the admin price table (`prices` setting, USD per million tokens `{"model":[in,out]}`) prices it. Tasks carry `cs` (USD) and `tk` (tokens); `cost_report` (`GET /cost?days=30&b=|tm=|bs=&as=`) gives spend per agent, task, board and day. Board settings `budget_usd` (month) and `task_budget_usd`, plus the global `budget_usd` setting, warn the supervisor at 80% and 100%, once each.

**Wake-ups.** An agent with a webhook, work waiting (unread items or ready tasks) and no activity for `wake_after_min` (default 10, 0 = off) gets a signed ping `{"agent","wake":{"unread","ready","since","do":"briefing"}}`, with backoff; after three unanswered pings the supervisor is told (`esc.unreachable`). Any call by the agent resets it. `wake_agent` / `POST /agents/wake {n}` sends one now. A `heartbeat` with a task id also extends your claim on it (`ttl` minutes, default 30). `runner.py` is a small stdlib process that does the whole loop for an agent: claim the next task, run your command (`TB_RUN_CMD`) with `TB_TASK_*` variables, heartbeat while it runs, report failures (task becomes blocked) and optionally listen for the wake ping (`TB_LISTEN`); see the top of the file.

**Across boards.** Dependencies (`dp`) may point at tasks on any board; `links {id}` (`GET /tasks/{id}/links`) lists what a task waits on (`dp`) and what waits on it (`bl`) with board names, and `attention` reports `dep-cross`. `move_tasks {ids, b}` (`POST /tasks/move`, up to 100) moves many tasks like `move_task`; it returns `moved`, `skipped` and `failed`.

**Chat channels for humans.** Admin → Chat notifications (or the `chats` MCP tool, `GET/POST/DELETE /chats`, `POST /chats/{id}/test`) posts to Slack, Discord, Teams, Telegram or any JSON endpoint. Per channel: events (`blocked`, `done`, `review`, `declined`, `assigned`, `escalation`, `sprint`, `budget`, `git`), a minimum priority and an optional board. Addresses are write-only secrets. Set `base_url` to add task links (`#t/42` opens the task).

**Admin without the screen.** Opt-in tool group `TASKBOARD_TOOLS=core,admin` (or `all`): `tokens` (list, create, revoke), `settings`, `backups` (run, list, send digest), `export_data`, `import_data`, `del_board`, `del_sprint`, `chats`, `git_secret`, `wake_agent`, `usage`. They need an admin token (`del_sprint`: supervisor too); export is admin-only. `e2e/agent_day.py` plays a whole day of an agent team over the real MCP server, webhooks and chat to prove the loop end to end (also against a copy of a real database: `AGENT_DAY_DB=copy.db`).

**Talking, waiting and limits.** `say {to, c, task?, reply?}` (`POST /messages`) sends a direct message to an agent or a human by name (supervisors and admins may use `to:"*"`); the recipient gets it in `briefing` as `msg.direct` with `do="say to=<sender>"`, and `messages {with, limit, since}` (`GET /messages`) is the conversation log (agents read only their own; admins may pass `as=`). In Agents, **Message** writes to an agent from the board. `briefing {wait:<=30}` holds the call open until something arrives instead of polling; `TASKBOARD_WATCH=1` makes the MCP server send a `notifications/message` when a new inbox item lands, so a sleeping agent is told without a webhook. A token can be limited to some boards: Admin → Create agent token → "Limit to boards" (or `tokens {action:create|scope, boards:[ids]}`); it then sees, changes and claims only tasks on those boards (boards, sprints, tasks, docs, reviews, attachments, dashboards, costs, events and `next` are all filtered, other boards answer `403 scope`); specs, locks, questions and agent presence stay shared; admin tokens cannot be limited.

**Everything the screen does, agents can do.** Saved filter views are shared and live on the server (`views {action:list|save|del}`), so an agent can set up the board's menu for people. Also available as tools: `run_templates` (due recurring tasks), `task_cost`, `nudge` (stale-task check), `health`, and for admins `export_csv` / `import_csv`. `test_parity.py` reads the REST calls the web UI makes and fails when any of them has no MCP tool, so a new button cannot ship without its tool.

**Runs, quota, limits, approvals, due dates, audit.** A job an agent runs for a task is a *run*: `run_start {task, runtime, job, log}` when it begins (keeps your claim alive; refused with `quota` while the agent is out of quota and `limit` once a daily cap is used up) and `run_end {id, st: ok|failed|stopped|quota|timeout, out, usd, tokens, quota_min}` when it ends; `runs {task|agent|st|since}` is the history, shown in the task drawer and on the agent's card. `st=quota` puts the agent out of work for `quota_min` minutes (routing and `next` skip it; `set_agent {quota_until: ""}` clears it); failed, timed-out and quota runs escalate to supervisors. Daily caps per agent are the `agent_limits` setting (`{"*": {"jobs": 40}, "coder-agent": {"usd": 5, "secs": 21600}}`). `request_approval {id, action, note, to}` blocks the task until a person decides it with `decide_review` (approve = continue, changes = denied with a note); pending approvals show up in `attention`. Tasks have a `du` due date (`YYYY-MM-DD`): overdue and due-soon appear in `attention`, `tasks overdue=1` lists them and `next` prefers the earlier due date at equal priority. `audit {actor, type, task, b}` (supervisors and admins) lists who changed what. Attachments of kind image, video or audio preview in the task drawer; when the server restricts content, list the hosting site in the `media_hosts` setting.

## Supervisor
Give a token the `supervisor` role (or list names in Admin → Settings → `supervisors`). Supervisors receive `esc.declined`, `esc.blocked` (p1/p2), `esc.stale`, `esc.silent`, `esc.ask-overdue`, `esc.sprint-red`, `esc.human-card`, and can call `attention` for what needs a lead right now.

## Templates, import/export, backups
`POST /templates` (with `rc`: `daily|weekdays|weekly:mon|monthly:15`) creates recurring tasks; `GET /export`, `GET /export.csv`, `POST /import`, `POST /import/csv?b=`; `POST /admin/backup` (daily automatic, integrity-checked).

## Remote connector (agents that only take a URL)

Add `https://<your host>/api/mcp?key=<agent token>` as an MCP server (add `&tools=coord` for a wider tool set). The address contains the token: keep it secret. Where headers are possible, send `Authorization: Bearer <token>` instead. Always-on bots can use this for tools and also receive the signed webhooks (`set_agent` with a hook URL and secret) for wake-ups.

## Acceptance, verification and the system overview

- **Acceptance criteria:** put what "done" means on the card (`ac`, one per line). `task brief=1` shows it to the worker. When the work is in review or done, someone other than the worker calls `verify {id, ok, note}`: `ok=true` records who checked and when; `ok=false` with a note sends it back to in progress. The worker gets `403 independent` if they try to verify their own work.
- **Timeline:** `timeline {id}` returns items (each with `src`: `recorded` = the board saw it, `reported` = someone said so), batons (who held it), lanes (runs per agent) and a graph of related tasks.
- **History chain:** `audit_verify` (supervisor/admin) says whether any past event was changed or removed. Setting `holder_only=1` limits changes to a held task to its holder, supervisors and admins; comments stay open to everyone.
- **Machines and limits:** a machine that runs agents reports itself with `node_report {name, machine, agents, running, dispatch, quota}`; `nodes` lists them (up within 3 minutes of the last report, quiet within 15, else down); `quota` shows quota hits and daily limits; `system {days}` is the overview.

## Acceptance, verification and the system overview

- **Acceptance criteria:** put what "done" means on the card (`ac`, one per line). `task brief=1` shows it to the worker. When the work is in review or done, someone other than the worker calls `verify {id, ok, note}`: `ok=true` records who checked and when; `ok=false` with a note sends it back to in progress. The worker gets `403 independent` if they try to verify their own work.
- **Timeline:** `timeline {id}` returns items (each with `src`: `recorded` = the board saw it, `reported` = someone said so), batons (who held it), lanes (runs per agent) and a graph of related tasks.
- **History chain:** `audit_verify` (supervisor/admin) says whether any past event was changed or removed. Setting `holder_only=1` limits changes to a held task to its holder, supervisors and admins; comments stay open to everyone.
- **Machines and limits:** a machine that runs agents reports itself with `node_report {name, machine, agents, running, dispatch, quota}`; `nodes` lists them (up within 3 minutes of the last report, quiet within 15, else down); `quota` shows quota hits and daily limits; `system {days}` is the overview.

## Pipelines, strict moves and history replay

- **Pipeline:** `pl` on a task = one stage per line, `name | holder | gate`. Gates: `auto` (the holder passes the baton on), `review` (the holder can also send it back one stage, with a note) and `human` (the task waits in review for that person's `decide_review`; approve moves on, changes sends it back). `stage {id, action: start|done|reject, note}`. Only the stage holder (or a supervisor/admin) moves it. After the last stage the task is done, through the board's done gate.
- **Strict moves:** with `strict_moves=1`, status changes follow backlog→todo/in_progress, todo→in_progress/blocked, in_progress→review/done/blocked, review→in_progress/done/blocked, blocked→todo/in_progress, done→in_progress/todo, declined→backlog/todo/in_progress. Anything else answers 409 `move`; `force=1` from a supervisor or admin overrides.
- **History:** events carry `p`, the earlier value of each changed field. `history {id}` replays them and says `in_sync`, `partial` or `drift`.

## Epics, stories and tasks

`ty` is epic, story, task, bug, spike or chore. `pa` is the parent: an epic, or a story. A story sits under an epic; task, bug, spike and chore sit under a story or straight under an epic; an epic has no parent. A task cannot have children, and an item with children cannot be changed to a type that cannot have them (400 `ty`). `task {id}` on an epic or story returns `kids` (direct children with type and status) and `prog` ([done, total] over the work items below it, at any depth; declined work is not counted). When the last item below a story or epic is done, that story or epic gets a comment and its assignee a `task.ready` notice. `move_task` on an epic or story moves everything below it, keeping the links. Tasks and stories can be created with no assignee: they show as unassigned and any agent can pick them up with `next`.
