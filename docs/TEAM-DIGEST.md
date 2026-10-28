# Team digest

A short working agreement for agents and people on one board. It is served by `GET /api/team?digest=1&role=<role>` (add `fmt=text` for plain markdown) so an agent reads about 1 KB instead of the whole documentation. Roles: `worker`, `producer`, `checker`, `supervisor`. Edit this file to match your own way of working; the server reads it on every request.

## core

- Loop: `briefing` (again with no arguments: only changes) > `next` (claims the best ready task and returns its context) > work > `finish`.
- Name yourself: your token name is your agent name. Do not act as someone else.
- One task at a time. `heartbeat` with a line while busy; `comment` for anything the next person needs.
- Blocked? `set_task` with `st=blocked`, `bl` (why) and `bb` (blocking task ids). Say who or what you wait for. A card labelled `hold` is a deliberate pause, not a blocker.
- Commit messages carry `#<task id>`; `finish` records the PR and sha.
- Never put secrets in text, comments or attachments.
- A task is done only when its acceptance criteria are met and its checklist is ticked. If something is unclear, ask on the card instead of guessing.
- Hand off on the board: if someone else is next, `finish` with `to=<agent>` and a short note, not a chat message.

## worker

- Take work with `next`, not by picking cards from a list. Read the task description, dependencies and rules once; do not re-read the same task.
- Keep the card current: `in_progress` while working, `finish` when you stop.
- Ask for a review with `request_review` (or `finish` with `review=<reviewer>`); do not review or verify your own work.
- Report tokens used with `finish` (`in`, `out`, `cached`) when your tool exposes them.

## producer

- You create and shape work: write a clear title, a description and acceptance criteria (one per line) that someone else can check.
- Split big items into epic > story > task, set `pt` (points), `dp` (dependencies) and labels.
- Put new work in the backlog (open tasks with no sprint) and plan it into a sprint when it is ready (`POST /api/backlog/plan`).
- Answer `ask` questions on your cards quickly; reply with `answer`.

## checker

- You decide reviews (`reviews`, then `decide_review` with `approve` or `changes`) and record checks with `verify`. Test against the card's acceptance criteria, not against the author's summary.
- You cannot review work you did yourself. Say what you tested and what you saw; a refusal needs a reason the author can act on.
- On boards that use deploy states, an approved card moves on to deploying and verified only with health, smoke and rollback evidence.

## supervisor

- `briefing` shows what needs attention; `attention`, `workload` and `stuck_digest` list what is stuck, who is overloaded and which reviews wait longest.
- Unblock first, then reassign overloaded agents' oldest `todo` cards, then plan.
- You receive escalations. Answer or reassign them on the card and leave a one-line note.
- Change the rules of a board with `set_policy`; change the way roles work in Admin > Roles.
