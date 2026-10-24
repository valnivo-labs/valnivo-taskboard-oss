#!/usr/bin/env python3
"""A scripted day for a team of agents, over the real MCP server and the real webhooks - no browser, no mocks of the board.

Planner, two coders, a reviewer, a supervisor and an admin work one board through: planning, claiming, dependencies, a review round trip,
questions, handoff, a git pull request, a block with an escalation and a chat message, a wake-up, a budget warning, bulk move, and the admin tools.
Every step asserts what the other agents are told. Exit code 1 on the first broken expectation.

    python3 e2e/agent_day.py                          # throwaway database
    AGENT_DAY_DB=/path/to/copy.db python3 e2e/agent_day.py   # against a COPY of a real database (also proves the upgrade works)
"""
import hashlib, hmac, http.server, json, os, shutil, socket, subprocess, sys, tempfile, threading, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORT, SINK = free_port(), free_port()
URL = "http://127.0.0.1:%d" % PORT
TOK = {"Admin": "adm-tok", "Planner": "plan-tok", "Coder1": "c1-tok", "Coder2": "c2-tok", "Reviewer": "rev-tok", "Boss": "boss-tok"}
ROLE = {"Admin": "admin", "Boss": "supervisor"}
sink = []
steps = []


class Sink(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        sink.append({"path": self.path, "h": dict(self.headers), "b": json.loads(raw), "raw": raw})
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


class Agent:
    """One agent = one MCP server process speaking JSON-RPC over stdio, exactly like an agent client would."""
    def __init__(self, name, tools):
        self.name, self.calls, self.bytes = name, 0, 0
        self.p = subprocess.Popen([sys.executable, os.path.join(ROOT, "mcp_server.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1,
                                  env={**os.environ, "TASKBOARD_URL": URL, "TASKBOARD_TOKEN": TOK[name], "TASKBOARD_AGENT": name, "TASKBOARD_TOOLS": tools})
        self.n = 0
        self.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": name, "version": "1"}})

    def rpc(self, method, params):
        self.n += 1
        line = json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params})
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()
        out = self.p.stdout.readline()
        self.bytes += len(line) + len(out)
        return json.loads(out)

    def call(self, tool, **args):
        self.calls += 1
        r = self.rpc("tools/call", {"name": tool, "arguments": args})
        return json.loads(r["result"]["content"][0]["text"])

    def tb(self, op, **args):
        return self.call("tb", op=op, a=args)

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=10)


def rest(method, path, body=None, tok="adm-tok"):
    req = urllib.request.Request(URL + "/api" + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}")


def step(msg, cond, detail=""):
    steps.append((msg, bool(cond)))
    print(("  ok   " if cond else "  FAIL ") + msg + ("" if cond else "   -> %s" % (detail,)))
    if not cond:
        raise SystemExit(1)


def kinds(brief):
    return [n["ty"] for n in brief.get("in", [])]


def forge(event, payload, secret):
    raw = json.dumps(payload).encode()
    h = {"Content-Type": "application/json", "X-GitHub-Event": event, "X-Hub-Signature-256": "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()}
    return json.load(urllib.request.urlopen(urllib.request.Request(URL + "/api/hooks/git", data=raw, headers=h, method="POST")))


def main():
    tmp = tempfile.mkdtemp(prefix="agent-day-")
    db = os.path.join(tmp, "day.db")
    if os.environ.get("AGENT_DAY_DB"):
        shutil.copy(os.environ["AGENT_DAY_DB"], db)
        print("using a copy of", os.environ["AGENT_DAY_DB"])
    env = {**os.environ, "TASKBOARD_DB": db, "PORT": str(PORT), "HOST": "127.0.0.1", "TASKBOARD_NUDGE_HOURS": "0", "TASKBOARD_ALLOW_PRIVATE_WEBHOOKS": "1",
           "TASKBOARD_TOKENS": ",".join("%s:%s:%s" % (n, t, ROLE.get(n, "agent")) for n, t in TOK.items())}
    srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")], env=env, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    threading.Thread(target=http.server.ThreadingHTTPServer(("127.0.0.1", SINK), Sink).serve_forever, daemon=True).start()
    for _ in range(150):
        try:
            urllib.request.urlopen(URL + "/api/health", timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    else:
        sys.exit("server did not start: " + srv.stderr.read().decode()[-800:])
    agents = []
    try:
        before = rest("GET", "/boards")
        pre_boards = len(before) if isinstance(before, list) else len(before.get("i", before.get("items", [])))
        print("server up with %d existing board(s)" % pre_boards)
        admin = Agent("Admin", "all"); planner = Agent("Planner", "core,coord,plan"); c1 = Agent("Coder1", "core,coord"); c2 = Agent("Coder2", "core,coord")
        rev = Agent("Reviewer", "core,coord"); boss = Agent("Boss", "core,coord,plan")
        agents = [admin, planner, c1, c2, rev, boss]
        for a in (c1, c2, rev, boss, planner):
            a.call("briefing")                                       # everyone starts the day; later calls return only what changed

        print("\nmorning: setup and planning")
        sec = admin.call("git_secret")["secret"]
        admin.call("settings", base_url="https://board.example.com", wake_after_min="5", supervisors="Boss")
        ch = admin.call("chats", action="add", n="Ops", kind="generic", url="http://127.0.0.1:%d/chat" % SINK, events=["blocked", "done", "budget", "git", "escalation"])
        step("admin adds a chat channel and the git secret over MCP", ch.get("n") == "Ops" and len(sec) > 20, ch)
        bid = rest("POST", "/boards", {"n": "Agent day %d" % time.time(), "dod": "tests pass", "cf": {"task_budget_usd": 1}})["id"]
        planner.call("add_sprint", b=bid, st="active")
        mk = lambda **k: planner.call("add_task", b=bid, ch="Sim", st="todo", **k)
        t1 = mk(t="Design the schema", pr=1, **{"as": "Coder1"})["id"]
        t2 = mk(t="Implement the API", pr=2, dp=[t1], **{"as": "Coder2"})["id"]
        t3 = mk(t="Write the docs", pr=2, **{"as": "Coder2"})["id"]
        epic = mk(t="Launch", ty="epic")["id"]
        kid = mk(t="Launch checklist", pa=epic)["id"]
        step("planner creates 5 tasks (a dependency, an epic with a child) in the active sprint", all(isinstance(i, int) for i in (t1, t2, t3, epic, kid)))
        step("the board knows T2 waits on T1", planner.call("links", id=t2)["dp"][0]["id"] == t1)

        print("\nmidday: work, review, dependencies")
        b1 = c1.call("briefing")
        step("Coder1 is told about T1 the moment it was assigned", "task.assigned" in kinds(b1) and b1["in"][0].get("do"), b1)
        nx = c1.call("next", ttl=2)                                  # a short claim, so the heartbeat has something to renew
        step("next claims T1 (the best ready task) and returns its context", nx["id"] == t1 and nx["task"]["t"] == "Design the schema", nx)
        step("Coder2's next skips T2 because its dependency is open", c2.call("next").get("id") == t3)       # T3 is ready, T2 is not
        c2.call("release", id=t3)
        step("Coder1 takes a lock and a heartbeat renews the claim", c1.call("lock", name="repo:main")["name"] == "repo:main" and c1.call("heartbeat", status="schema draft", task=t1).get("renewed") == [t1])
        c1.call("add_commit", id=t1, h="a1b2c3d", m="schema v1 (#%d)" % t1)
        c1.call("cost", task=t1, **{"in": 40000, "out": 6000}, usd=0.4, model="m")
        fin = c1.call("finish", id=t1, summary="Schema drafted, tests added", review="Reviewer", cost={"in": 2000, "out": 500, "usd": 0.05})
        step("finish with review sends T1 to the reviewer in ONE call", fin["st"] == "review" and "review" in fin["did"] and "cost" in fin["did"], fin)
        c1.call("unlock", name="repo:main")
        rb = rev.call("briefing")
        rv_id = [n for n in rb["in"] if n["ty"] == "review.requested"][0]
        step("the reviewer's briefing carries the exact next call", rv_id["do"].startswith("decide_review"), rv_id)
        rid = int(rv_id["do"].split("id=")[1].split()[0])
        rev.call("decide_review", id=rid, decision="changes", note="add an index on owner_id")
        step("changes requested reaches the coder", "review.changes" in kinds(c1.call("briefing")))
        c1.call("finish", id=t1, summary="Index added", review="Reviewer")
        rid2 = int([n for n in rev.call("briefing")["in"] if n["ty"] == "review.requested"][0]["do"].split("id=")[1].split()[0])
        rev.call("decide_review", id=rid2, decision="approve")
        gate = c1.call("finish", id=t1, summary="Approved and merged", dn=[0])
        step("after approval the done gate lets T1 close", gate.get("st") == "done", gate)
        step("Coder2 is told T2 is ready as soon as T1 is done", "task.ready" in kinds(c2.call("briefing")))
        step("Coder2 claims T2 with next", c2.call("next")["id"] == t2)

        print("\nafternoon: questions, handoff, git, blocks")
        q = c2.call("ask", to="Planner", q="Which auth scheme should the API use?", task=t2)
        pb = planner.call("briefing")
        step("the question reaches the planner with the answer call prepared", any(n["ty"] == "ask.new" and n["do"].startswith("answer") for n in pb["in"]), pb)
        planner.call("answer", id=q["id"], a="Bearer tokens, same as the board")
        step("the answer reaches Coder2", "ask.answered" in kinds(c2.call("briefing")))
        c2.call("handoff", id=t2, to="Coder1", summary="API skeleton done", next="wire up auth")
        step("a handoff notifies the receiver once", "task.handoff" in kinds(c1.call("briefing")))
        pr = {"action": "opened", "repository": {"full_name": "acme/api"}, "pull_request": {"html_url": "https://github.com/acme/api/pull/5", "title": "API (#%d)" % t2, "body": "", "head": {"ref": "task-%d-api" % t2}, "user": {"login": "dev"}, "merged": False, "draft": False}}
        forge("pull_request", pr, sec)
        step("a signed pull-request webhook moves T2 to review", c1.call("task", id=t2)["st"] == "review")
        pr["action"] = "closed"; pr["pull_request"].update(merged=True, merge_commit_sha="f00dcafe")
        forge("pull_request", pr, sec)
        t2o = c1.call("task", id=t2)
        step("merging does NOT close T2: the board's definition of done says tests must pass", t2o["st"] == "review", t2o)
        step("Coder1 closes it by ticking the checklist", c1.call("finish", id=t2, summary="CI green, merged", dn=[0])["st"] == "done")
        sink.clear()
        c2.call("set_task", id=t3, st="blocked", bl="waiting for the style guide")
        got = None
        for _ in range(60):
            got = [x for x in sink if x["path"] == "/chat" and "blocked" in x["b"]["text"]]
            if got:
                break
            time.sleep(0.1)
        step("a blocked task is posted to the chat channel with a link", got and "board.example.com/#t/%d" % t3 in got[0]["b"]["text"], sink)
        step("and the supervisor is escalated to (priority 2)", "esc.blocked" in kinds(boss.call("briefing")))

        print("\nevening: supervisor, cost, wake-ups, bulk move")
        att = boss.call("attention")
        step("the supervisor sees the blocked task in attention", any(i["k"] == "blocked-p12" for i in att["i"]), att)
        step("overview rolls the analytics up", boss.call("overview", b=bid)["flow"]["done_n"] >= 2)
        boss.call("comment", id=t3, c="Style guide is in the specs - unblocking")
        boss.call("set_task", id=t3, st="todo", **{"as": "Coder2"})
        step("unblocking tells the assignee", "task.status" in kinds(c2.call("briefing")))
        c1.call("cost", task=t1, usd=0.7, model="m")
        step("crossing 80% of the task budget escalates to the supervisor", "esc.budget" in kinds(boss.call("briefing")))
        rep = boss.call("cost_report", b=bid)
        step("the cost report adds it up per agent and task", rep["tot"]["usd"] >= 1.1 and rep["ag"][0]["n"] == "Coder1", rep)
        planner.call("set_agent", n="Coder2", hook="http://127.0.0.1:%d/wake" % SINK)
        sink.clear()
        w = boss.tb("wake_agent", n="Coder2")
        wk = None
        for _ in range(60):
            wk = [x for x in sink if x["path"] == "/wake" and "wake" in x["b"]]
            if wk:
                break
            time.sleep(0.1)
        step("the supervisor can wake an agent through its webhook", w.get("sent") == 1 and wk and wk[0]["b"]["wake"]["do"] == "briefing", (w, sink))
        b2 = rest("POST", "/boards", {"n": "Agent day archive %d" % time.time()})["id"]
        mv = planner.call("move_tasks", ids=[epic, t1], b=b2)
        step("bulk move takes an epic with its child and a loose task", sorted(mv["moved"]) == sorted([epic, kid, t1]) and not mv["failed"], mv)
        idle = c1.call("briefing")
        idle2 = c1.call("briefing")
        step("an idle briefing costs almost nothing", list(idle2) == ["c"], idle2)

        print("\nclose of day: admin")
        tk = admin.call("tokens", action="create", n="Night Agent", role="agent")
        step("admin mints a token over MCP and it works", len(tk["token"]) > 20 and rest("GET", "/boards", tok=tk["token"]) is not None)
        step("an agent cannot", c1.tb("tokens").get("e") == "forbidden" and c1.tb("settings").get("e") == "forbidden")
        tid = [x["id"] for x in admin.call("tokens")["i"] if x["n"] == "Night Agent"][0]
        admin.call("tokens", action="revoke", id=tid)
        step("revoked tokens stop working", rest("GET", "/boards", tok=tk["token"]).get("e") == "auth")
        step("a verified backup runs on demand", admin.call("backups", action="run")["ok"] is True)
        ex = admin.call("export_data", b=bid)
        step("export returns the board", ex["boards"] and any(t["title"] == "Implement the API" for t in ex["tasks"]))
        dash = rest("GET", "/dashboard")
        step("the dashboard and overview still answer for the whole database", "boards" in dash and "flow" in rest("GET", "/overview"), list(dash))
        print("\nagent traffic (approx. tokens = bytes/4, requests + responses):")
        for a in agents:
            print("  %-9s %3d calls  ~%5d tokens" % (a.name, a.calls, a.bytes // 4))
    finally:
        for a in agents:
            try:
                a.close()
            except Exception:
                pass
        srv.terminate()
        err = srv.stderr.read().decode() if srv.stderr else ""
        if "Traceback" in err:
            print("\nSERVER TRACEBACKS:\n" + err[-1500:])
            sys.exit(1)
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n%d steps passed" % len(steps))


if __name__ == "__main__":
    main()
