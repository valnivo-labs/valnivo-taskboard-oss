# Changelog

- Git: a signed forge webhook (GitHub, GitLab, Gitea) links commits and pull requests to tasks, moves a task to review when a PR opens and closes it when the PR merges (respecting the definition of done), and reports requested changes and failed CI.
- Cost: agents report tokens and USD per task (`cost`, or `cost` on `finish`); per-agent, task, board and day reports; monthly board and task budgets warn the supervisor.
- Wake-ups: signed pings to agents that have work waiting but are silent, with backoff and escalation; heartbeats extend a claim; `runner.py` runs a command per task for an agent.
- Cross-board dependencies in the task dialog (search any board), `links`, an attention item for work waiting on another board, and bulk move (`move_tasks`, bulk bar).
- Chat channels for humans: Slack, Discord, Teams, Telegram or generic JSON for blocked, done, escalations, sprints, budgets and git events.
- Browser end-to-end tests (`make e2e`, a CI job); reloading many times no longer loses data (the shared database connection is serialised, errors answer 500 instead of dropping the connection, larger listen backlog, client retries).
## Unreleased
- Agent parity: saved views are shared and stored on the server; new tools `views`, `run_templates`, `task_cost`, `nudge`, `health`, `export_csv`, `import_csv`; `test_parity.py` fails when the web UI can do something no MCP tool can.
- Easier to use: a **Today** start screen (what needs a person, with an action beside each item, agents right now, sprints, spend), a command bar (`Ctrl/Cmd K`) that searches tasks, boards and agents and runs actions, tasks open in a side drawer, secondary filters under "More filters", Admin split into sections, agent state and filter in Agents, and a phone layout with a bottom tab bar. Admin tables no longer spill out of their panels.
- Direct messages between agents and humans (`say`, `messages`, Agents → Message); `briefing wait` and an optional MCP inbox watcher so agents without a webhook are told when work arrives; tokens limited to some boards (Admin → Create agent token, `tokens` tool) enforced across boards, sprints, tasks, docs, reviews, dashboards, costs, events and `next`.
- Admin MCP tool group (tokens, settings, backups, export/import, delete board/sprint) so nothing needs the screen; exporting everything is now admin-only. A review that asked for changes no longer blocks Done once the same reviewer approved a newer one. `e2e/agent_day.py` runs a scripted day of an agent team over MCP, webhooks and chat.
- The all-boards overview now carries the single-board analytics: flow (throughput, cycle time, cumulative flow, aging), active sprints with burndown and risks, OKR and SLA per board, environments and agent stats. `GET /overview` and the `overview` MCP tool.
- Move a task (and an epic with its children) to another board: task dialog Board field, `POST /tasks/{id}/move`, MCP `move_task`. Raw `board_id` changes via PATCH are refused.
- Token savings for agents: MCP tool profiles (default ~1.4k tokens instead of ~7.2k) with `help` and `tb`; one-call `briefing` (delta cursor), `next`, `finish` and `batch`; actionable, coalesced and mutable notifications; `fields`/table/`brief` reads and spec outline/section; role guides; a per-agent token usage meter.
- Six more ways of working: waterfall stage-gates (phases with gates), lean startup, OKRs (objectives, key results, progress roll-up), a priority matrix (Eisenhower), a support desk with SLAs and escalation, and a personal GTD list.
- Board menu starts on "All boards"; also by board type and single boards. Cross-board scope (`tm`, `bs`) on tasks, dashboard, attention, risks and agent stats, plus `/portfolio` and a `portfolio` MCP tool.
- Ways of working per board: scrum, kanban, scrumban, shape up, simple. WIP limits (warn/block), flow metrics and cumulative flow, velocity, retrospectives with actions, auto-rollover, replenish signal, work item types and epics.

## 0.1.0 — first public release
- Boards, sprints, tasks, comments; sprint statistics, burndown and risk detection.
- Agent-first API and MCP server (72 tools): inbox and webhooks, handoff, decline/reopen, claims, locks, questions with deadlines, reviews, definition of done, dependencies, points and capacity, skills routing, environments and freezes, watchers, attachments, specs library, templates and recurring tasks, supervisor escalations.
- Web UI: board, overview, my work, specs, agents, admin; dark mode; collapsible menu.
- Hardening: first-run admin token, roles, rate limits, SSRF guard, CSP, size caps.
- Docker image, backups, JSON/CSV import and export.
