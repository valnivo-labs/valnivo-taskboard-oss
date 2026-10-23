# Changelog

## Unreleased
- Six more ways of working: waterfall stage-gates (phases with gates), lean startup, OKRs (objectives, key results, progress roll-up), a priority matrix (Eisenhower), a support desk with SLAs and escalation, and a personal GTD list.
- Board menu starts on "All boards"; also by board type and single boards. Cross-board scope (`tm`, `bs`) on tasks, dashboard, attention, risks and agent stats, plus `/portfolio` and a `portfolio` MCP tool.
- Ways of working per board: scrum, kanban, scrumban, shape up, simple. WIP limits (warn/block), flow metrics and cumulative flow, velocity, retrospectives with actions, auto-rollover, replenish signal, work item types and epics.

## 0.1.0 — first public release
- Boards, sprints, tasks, comments; sprint statistics, burndown and risk detection.
- Agent-first API and MCP server (72 tools): inbox and webhooks, handoff, decline/reopen, claims, locks, questions with deadlines, reviews, definition of done, dependencies, points and capacity, skills routing, environments and freezes, watchers, attachments, specs library, templates and recurring tasks, supervisor escalations.
- Web UI: board, overview, my work, specs, agents, admin; dark mode; collapsible menu.
- Hardening: first-run admin token, roles, rate limits, SSRF guard, CSP, size caps.
- Docker image, backups, JSON/CSV import and export.
