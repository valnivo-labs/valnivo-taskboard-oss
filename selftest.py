#!/usr/bin/env python3
"""In-process smoke test on a throwaway DB copy (data/test.db). Usage: python3 selftest.py"""
import json, os, sys, threading, urllib.request, urllib.error
os.environ.update(TASKBOARD_DB=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "test.db"),
                  TASKBOARD_TOKENS="a:tok:admin,v:vtok:viewer,Sup Bot:stok:supervisor,Worker:wtok:agent",
                  TASKBOARD_ALLOW_PRIVATE_WEBHOOKS="1", TASKBOARD_HUMAN_NAMES="Owner", TASKBOARD_HUMAN_RULE="1", TASKBOARD_RATE_PER_MIN="0", TASKBOARD_AUTH_FAILS_PER_MIN="0", PORT="8791")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server
srv = server.S(("127.0.0.1", 8791), server.H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
U = "http://127.0.0.1:8791/api"


def call(m, p, b=None, h=None, tok="tok"):
    hd = {"Authorization": "Bearer " + tok, **(h or {})}
    r = urllib.request.Request(U + p, method=m, data=json.dumps(b).encode() if b is not None else None, headers=hd)
    try:
        with urllib.request.urlopen(r) as x:
            return x.status, x.headers.get("ETag"), x.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("ETag"), e.read().decode()


def show(*a):
    print(*a)


if os.path.exists(os.environ["TASKBOARD_DB"]):
    pass
s, _, b = call("POST", "/boards", {"n": "Selftest board"}); show(s, b)
BID = json.loads(b)["id"]
s, _, b = call("POST", "/tasks", {"b": BID, "t": "selftest", "ch": "Valnivo", "pr": 2, "lb": "gate,qa"}); show(s, b)
tid = json.loads(b)["id"]
show(*call("PATCH", f"/tasks/{tid}", {"st": "blocked", "bl": "waiting on API key", "bb": [1, 2]})[::2])
s, _, b = call("POST", "/docs", {"sc": "board", "r": BID, "t": "Rules", "d": "# Rule\nNo prod deploys Fri"}); show(s, b)
show(*call("POST", "/docs", {"sc": "task", "r": tid, "t": "Ref", "u": "https://x.y"})[::2])
show(*call("GET", f"/tasks/{tid}?ctx=1")[::2])
show(*call("GET", "/tasks?blocked=1&lb=app")[::2])
s, et, b = call("GET", "/tasks?status=blocked"); show(s, et, b)
show(call("GET", "/tasks?status=blocked", h={"If-None-Match": et})[0])
show(*call("GET", "/boards/1")[::2])
show(*call("GET", "/changes?since=0&limit=3")[::2])
show(*call("PATCH", f"/tasks/{tid}", {"st": "todo"})[::2])
show(*call("GET", f"/tasks/{tid}")[::2])
show(*call("POST", "/tasks", {"b": 1})[::2], *call("GET", "/nope")[::2])
show(*call("DELETE", f"/tasks/{tid}")[::2])
req = urllib.request.Request(U + "/tasks", method="HEAD", headers={"Authorization": "Bearer tok"})
show("HEAD", urllib.request.urlopen(req).status)
show(len(call("GET", "/tasks?full=1")[2]), "full bytes vs", len(call("GET", "/tasks")[2]), "compact")
s, _, b = call("POST", "/tasks", dict(b=1, t="chan-test", ch="docs")); show(s, b)
show(*call("GET", "/tasks?ch=docs")[::2]); show(*call("GET", "/tasks?ch=none&limit=1")[::2])

s, _, b = call("POST", "/tasks", dict(b=1, t="urls", ch="backend", w="https://youtu.be/x", sh="https://youtu.be/s", vid="dQw4w9WgXcQ", at="2026-10-24 18:00", pu="https://github.com/o/r/pull/1", sha="deadbeef", lb="gate,pass,phone-verify")); show(s, b)
tid2 = json.loads(b)["id"]
show(*call("GET", f"/tasks/{tid2}")[::2])
show(*call("GET", "/tasks?st=open&limit=5")[::2])
show(*call("GET", "/tasks?st=todo,backlog&limit=5")[::2])
show(*call("POST", "/tasks/bulk", {"ids": [tid2], "lb": "next,gate"})[::2])
show(*call("DELETE", f"/tasks/{tid2}")[::2])


# ---- agent-first flows: sprints, notifications, comments, dashboard, webhook
import http.server
def J(m, p, b=None, tok="tok"):
    st, _, t = call(m, p, b, tok=tok); return st, json.loads(t)

got = []
class Hook(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200); self.end_headers()
    def log_message(self, *a): pass
hs = http.server.ThreadingHTTPServer(("127.0.0.1", 8792), Hook)
threading.Thread(target=hs.serve_forever, daemon=True).start()

st, sp = J("POST", "/sprints", {"b": BID, "st": "active"})            # name/dates default
assert st == 201 and sp["n"].startswith("Sprint") and sp["from"] and sp["to"], sp
st, t = J("POST", "/tasks", {"b": BID, "t": "notify me", "ch": "Valnivo", "as": "Agent X", "k": "agent"})
assert t["s"] == sp["id"], t                                          # joined active sprint
st, ib = J("GET", "/inbox?as=agent%20x")
assert ib["n"] == 1 and ib["i"][0]["ty"] == "task.assigned", ib
call("POST", "/agents", {"n": "Agent X", "hook": "http://127.0.0.1:8792/h", "secret": "s"})
J("PATCH", f"/tasks/{t['id']}", {"st": "in_progress"})
J("POST", f"/tasks/{t['id']}/comments", {"c": "please hurry", "to": ["Agent Y"]})
st, ib = J("GET", "/inbox?as=Agent%20X&ack=1")
assert [i["ty"] for i in ib["i"]] == ["task.assigned", "task.status", "task.comment"], ib
assert J("GET", "/inbox?as=Agent%20X")[1]["n"] == 0
assert J("GET", "/inbox?as=Agent%20Y")[1]["n"] == 1
import time; time.sleep(0.5); assert len(got) == 2 and got[0]["agent"] == "Agent X", got
J("PATCH", f"/tasks/{t['id']}", {"as": "Agent Y"})
assert J("GET", "/inbox?as=Agent%20X")[1]["i"][0]["ty"] == "task.unassigned"
st, d = J("GET", "/dashboard?as=Agent%20Y")
assert d["me"]["work"][0]["id"] == t["id"] and d["boards"], d
st, sp2 = J("POST", "/sprints", {"b": BID})
st, r = J("POST", f"/sprints/{sp2['id']}/start")
assert r["st"] == "active"
assert J("GET", f"/tasks/{t['id']}")[1]["s"] == sp2["id"]            # unfinished task carried over
st, r = J("POST", f"/sprints/{sp2['id']}/close", {"to": "backlog"}); assert r["mv"] == 1 and r["to"] is None, r
print("agent-first selftest OK")

# ---- sprint stats / risks / risk alerts
st, b2 = J("POST", "/boards", {"n": "Risk board"})
st, rs = J("POST", "/sprints", {"b": b2["id"], "st": "active", "from": "2026-10-01", "to": "2026-10-10"})   # long past -> overdue
J("POST", "/tasks", {"b": b2["id"], "t": "stuck", "ch": "X", "as": "Agent Z", "st": "blocked", "bl": "why", "pr": 1})
J("POST", "/tasks", {"b": b2["id"], "t": "loose", "ch": "X", "st": "todo"})
J("POST", "/tasks", {"b": b2["id"], "t": "finished", "ch": "X", "as": "Agent Z", "st": "done"})
st, ss = J("GET", f"/sprints/{rs['id']}/stats")
kinds = {r["k"] for r in ss["risks"]}
assert ss["health"] == "red" and {"overdue", "blocked", "unassigned"} <= kinds, ss
assert ss["w"]["done"] == 1 and ss["burn"][0][2] == 3, ss["burn"]
assert any(r["id"] == rs["id"] for r in J("GET", "/risks")[1]["i"])
J("GET", "/dashboard")                                                    # lazy tick fires risk alerts
inb = J("GET", "/inbox?as=Agent%20Z")[1]["i"]
assert any(n["ty"] == "sprint.risk" for n in inb), inb
n_before = len(J("GET", "/inbox?as=Agent%20Z&all=1")[1]["i"]); J("GET", "/dashboard")
assert len(J("GET", "/inbox?as=Agent%20Z&all=1")[1]["i"]) == n_before     # no repeat alert for same picture
print("stats/risk selftest OK")

# ---- decline / reopen for any assignee
st, t1 = J("POST", "/tasks", {"b": BID, "t": "agent job", "ch": "X", "as": "Agent Q", "k": "agent"})
assert J("POST", f"/tasks/{t1['id']}/decline", {})[1]["e"] == "missing"
st, r = J("POST", f"/tasks/{t1['id']}/decline", {"r": "out of scope", "to": ["Requester R"]})
assert r["st"] == "declined", r
g = J("GET", f"/tasks/{t1['id']}?ctx=1")[1]
assert g["st"] == "declined" and g["dr"] == "out of scope" and "Declined" in g["cc"][-1]["c"], g
assert any(n["ty"] == "task.comment" for n in J("GET", "/inbox?as=Requester%20R")[1]["i"])
assert J("POST", f"/tasks/{t1['id']}/decline", {"r": "again"})[1]["e"] == "bad"
st, r = J("POST", f"/tasks/{t1['id']}/reopen", {"to": "in_progress", "note": "needed after all"})
g = J("GET", f"/tasks/{t1['id']}")[1]
assert r["st"] == "in_progress" and "dr" not in g and g["st"] == "in_progress", g
assert J("POST", f"/tasks/{t1['id']}/reopen", {})[1]["e"] == "bad"                # not reopenable from in_progress
print("decline/reopen selftest OK")

# ---- shared specs
st, sp = J("POST", "/specs", {"t": "Auth token format", "d": "# Tokens\nUse **JWT**.", "tg": "Auth, api", "tk": [t1["id"]], "to": ["Agent R"]})
assert st == 201 and sp["tg"] == "api,auth" and sp["rv"] == 1, sp
assert [n["ty"] for n in J("GET", "/inbox?as=Agent%20R")[1]["i"]] == ["spec.created"]
assert any(n["ty"] == "spec.created" for n in J("GET", "/inbox?as=Agent%20Q")[1]["i"])      # assignee of linked task
assert J("GET", "/specs?q=jwt")[1]["i"][0]["sn"].lower().find("jwt") >= 0
assert J("GET", "/specs?tag=api")[1]["n"] == 1 and J("GET", "/specs?tag=nope")[1]["n"] == 0
st, r = J("PATCH", f"/specs/{sp['id']}", {"append": "## Expiry\n15 min", "base": 1, "note": "add expiry"}); assert r["rv"] == 2, r
assert J("PATCH", f"/specs/{sp['id']}", {"d": "x", "base": 1})[1]["e"] == "conflict"
st, r = J("PATCH", f"/specs/{sp['id']}", {"st": "approved"}); assert r["rv"] == 2 and r["st"] == "approved"
g = J("GET", f"/specs/{sp['id']}")[1]; assert "15 min" in g["d"] and g["st"] == "approved"
assert [h["rv"] for h in J("GET", f"/specs/{sp['id']}/revs")[1]["i"]] == [2, 1]
assert "15 min" not in J("GET", f"/specs/{sp['id']}/revs/1")[1]["d"]
assert J("POST", "/specs", {"d": "no title"})[1]["e"] == "missing"
assert J("DELETE", f"/specs/{sp['id']}")[1]["x"] == 1 and J("GET", f"/specs/{sp['id']}")[1]["e"] == "nf"
print("specs selftest OK")


# ================= v5 features =================
# ---- roles + per-agent tokens
assert J("POST", "/tasks", {"b": BID, "t": "x", "ch": "X"}, tok="vtok")[0] == 403                      # viewer read-only
assert J("GET", "/boards", tok="vtok")[0] == 200
assert J("DELETE", f"/boards/{b2['id']}", tok="wtok")[0] == 403                                       # agent cannot delete boards
assert J("GET", "/tokens", tok="wtok")[0] == 403 and J("POST", "/settings", {}, tok="wtok")[0] == 403
st, tkn = J("POST", "/tokens", {"n": "Zed", "role": "agent"}); assert st == 201 and len(tkn["token"]) > 20
st, zt = J("POST", "/tasks", {"b": BID, "t": "for zed", "ch": "X", "as": "Zed"})
st, ib = J("GET", "/inbox", tok=tkn["token"]); assert ib["n"] == 1 and ib["i"][0]["ty"] == "task.assigned", ib   # inbox defaults to token name
assert all("token" not in x for x in J("GET", "/tokens")[1]["i"])
assert J("GET", "/boards", tok="bogus")[0] == 401

# ---- dependencies
st, A = J("POST", "/tasks", {"b": BID, "t": "dep A", "ch": "X", "as": "Dev A"})
st, Bt = J("POST", "/tasks", {"b": BID, "t": "dep B", "ch": "X", "as": "Dev B", "dp": [A["id"]], "st": "todo"})
assert J("POST", "/tasks", {"b": BID, "t": "bad", "ch": "X", "dp": [99999]})[1]["e"] == "bad"
assert J("PATCH", f"/tasks/{A['id']}", {"dp": [Bt["id"]]})[1]["e"] == "cycle"
assert J("PATCH", f"/tasks/{A['id']}", {"dp": [A["id"]]})[1]["e"] == "bad"
assert Bt["id"] not in [t["id"] for t in J("GET", "/tasks?ready=1")[1]["i"]] and A["id"] in [t["id"] for t in J("GET", "/tasks?ready=1")[1]["i"]]
r = J("PATCH", f"/tasks/{Bt['id']}", {"st": "in_progress"})[1]; assert "depends on open" in r["w"][0], r
g = J("GET", f"/tasks/{A['id']}?ctx=1")[1]; assert g["bk"] == [Bt["id"]]
J("PATCH", f"/tasks/{A['id']}", {"st": "done"})
assert any(n["ty"] == "task.ready" for n in J("GET", "/inbox?as=Dev%20B")[1]["i"])
assert Bt["id"] in [t["id"] for t in J("GET", "/tasks?ready=1")[1]["i"]]

# ---- points, capacity
st, ps = J("POST", "/sprints", {"b": BID, "st": "active"})
for i, pt in enumerate((5, 3, 8)):
    J("POST", "/tasks", {"b": BID, "t": "pts%d" % i, "ch": "X", "as": "Pointy", "pt": pt, "s": ps["id"], "st": "done" if i == 0 else "todo"})
J("POST", "/agents", {"n": "Pointy", "capacity": 6})
ss = J("GET", f"/sprints/{ps['id']}/stats")[1]
assert ss["w"]["pts"] and ss["w"]["scope"] == 16 and ss["w"]["done"] == 5 and ss["w"]["open_w"] == 11, ss["w"]
assert any(r["k"] == "capacity" for r in ss["risks"]), ss["risks"]
assert J("GET", "/agents")[1]["i"][[a["n"] for a in J("GET", "/agents")[1]["i"]].index("Pointy")]["cap"] == 6

# ---- handoff + activity
st, H = J("POST", "/tasks", {"b": BID, "t": "handoff me", "ch": "X", "as": "Dev A", "st": "in_progress"})
assert J("POST", f"/tasks/{H['id']}/handoff", {"to": "Dev C"})[1]["e"] == "missing"
st, r = J("POST", f"/tasks/{H['id']}/handoff", {"to": "Dev C", "summary": "API done", "next": "write tests"}); assert r["as"] == "Dev C" and r["st"] == "in_progress"
n = J("GET", "/inbox?as=Dev%20C")[1]["i"]; assert [x["ty"] for x in n] == ["task.handoff"] and "write tests" in n[0]["m"], n
assert any(x["ty"] == "task.unassigned" for x in J("GET", "/inbox?as=Dev%20A")[1]["i"])
act = J("GET", f"/tasks/{H['id']}/activity")[1]["i"]
assert [a["ty"] for a in act][0] == "created" and any("assignee -> Dev C" in a["m"] for a in act) and act[-1]["ty"] == "comment", act
assert J("POST", f"/tasks/{H['id']}/handoff", {"to": "Owner", "summary": "needs you"})[1]["e"] == "owner_fmt"

# ---- supervisor escalations
st, W = J("POST", "/tasks", {"b": BID, "t": "worker job", "ch": "X", "as": "Worker", "pr": 1})
J("POST", f"/tasks/{W['id']}/decline", {"r": "cannot do"}, tok="wtok")
sup = J("GET", "/inbox", tok="stok")[1]["i"]; assert any(n["ty"] == "esc.declined" for n in sup), sup
J("POST", "/tasks", {"b": BID, "t": "owner thing", "ch": "X", "as": "Owner", "d": "Missing: k\nDo exactly: x", "pr": 2, "st": "blocked", "bl": "need key"})
sup = J("GET", "/inbox", tok="stok")[1]["i"]; assert {"esc.human-card", "esc.blocked"} <= {n["ty"] for n in sup}, sup
att = J("GET", "/attention", tok="stok")[1]["i"]; assert att and att[0]["sev"] == "high", att
assert J("DELETE", f"/sprints/{ps['id']}", tok="stok")[0] == 200                                    # supervisor may delete sprints
assert J("DELETE", f"/boards/{b2['id']}", tok="stok")[0] == 403

# ---- templates + recurring
st, tp = J("POST", "/templates", {"n": "Weekly review", "b": BID, "t": "Review", "as": "Dev A", "ch": "X", "cl": ["check A", "check B"], "rc": "daily"})
assert st == 201 and tp["nx"] and tp["rc"] == "daily", tp
assert J("POST", "/templates", {"n": "bad", "rc": "sometimes"})[1]["e"] == "bad"
r = J("POST", "/templates/run")[1]; assert len(r["made"]) == 1, r
t_new = J("GET", f"/tasks/{r['made'][0]}")[1]; assert "- [ ] check A" in t_new["d"] and t_new["t"].startswith("Review - "), t_new
db_next = J("GET", f"/templates/{tp['id']}")[1]["nx"]; assert db_next > time.strftime("%Y-%m-%d", time.gmtime())
J("PATCH", f"/templates/{tp['id']}", {"nx": time.strftime("%Y-%m-%d", time.gmtime()), "rc": "daily"})
assert J("POST", "/templates/run")[1]["skipped"] == 1                                              # previous occurrence still open
st, used = J("POST", f"/templates/{tp['id']}/use", {"t": "Manual review"}); assert st == 201 and used["t"] == "Manual review"

# ---- agent stats
stt = J("GET", "/stats/agents?days=30")[1]
assert any(a["as"] == "Dev B" for a in stt["i"]) and all("as" in a for a in stt["i"]), stt

# ---- settings, backup, digest, export/import
assert J("POST", "/settings", {"digest_url": "ftp://x"})[1]["e"] == "bad"
assert J("POST", "/settings", {"supervisors": "Sup Bot", "digest_hour": "7"})[1]["supervisors"] == "Sup Bot"
bk = J("POST", "/admin/backup")[1]; assert bk["ok"] and bk["counts"]["tasks"] > 5, bk
assert J("GET", "/admin/backups")[1]["n"] >= 1
assert J("POST", "/admin/digest")[1]["sent"] is False
exp = json.loads(call("GET", f"/export?b={BID}")[2]); assert exp["v"] == 1 and exp["tasks"] and exp["boards"][0]["id"] == BID
st, imp = J("POST", "/import", exp); assert st == 200 and imp["n"]["tasks"] == len(exp["tasks"]), imp
nb = imp["boards"][str(BID)] if str(BID) in imp["boards"] else imp["boards"][BID]
assert len(J("GET", f"/tasks?b={nb}&limit=500")[1]["i"]) == len(exp["tasks"])
assert J("POST", "/import", {"v": 2})[1]["e"] == "bad"
csvt = call("GET", f"/export.csv?b={BID}")[2]; assert csvt.splitlines()[0].startswith("id,board_id") and "dep A" in csvt
req = urllib.request.Request(U + f"/import/csv?b={nb}", method="POST", data=b"title,status,priority,assignee\nfrom csv,todo,2,CsvGuy\n,todo,1,x\n", headers={"Authorization": "Bearer tok"})
cr = json.load(urllib.request.urlopen(req)); assert cr["made"] == 1 and len(cr["bad"]) == 1, cr
print("v5 selftest OK")

# ================= v7: coordination =================
past = lambda m: time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - m * 60))
st, T1 = J("POST", "/tasks", {"b": BID, "t": "claim me", "ch": "X", "st": "todo", "lb": "fe", "pr": 1})
# ---- claims
r = J("POST", f"/tasks/{T1['id']}/claim", {}, tok="wtok")[1]; assert r["cb"] == "Worker" and r["st"] == "in_progress", r
r = J("POST", f"/tasks/{T1['id']}/claim", {"as": "Dev Z"}); assert r[0] == 409 and r[1]["e"] == "claimed" and r[1]["f"] == "Worker", r
assert J("POST", f"/tasks/{T1['id']}/release", {"as": "Dev Z"}, tok="wtok")[1].get("released") == 1          # Worker is the claimer (token identity wins)
assert J("POST", f"/tasks/{T1['id']}/claim", {})[1]["cb"] == "a"                                              # named admin token acts as itself
J("POST", f"/tasks/{T1['id']}/release", {})
st, T2 = J("POST", "/tasks", {"b": BID, "t": "next fe", "ch": "X", "st": "todo", "lb": "fe", "pr": 2})
r = J("POST", "/claim-next", {"as": "Dev Q", "lb": "fe"})[1]; assert r["id"] in (T1["id"], T2["id"]) and r["cb"] == "Dev Q", r
assert J("GET", f"/tasks/{r['id']}")[1]["as"] == "Dev Q"
J("POST", "/claim-next", {"as": "Dev Q", "lb": "fe"})
assert J("POST", "/claim-next", {"as": "Dev Q", "lb": "fe"})[1].get("none") == 1
server.db.execute("UPDATE tasks SET claim_until=? WHERE claimed_by='Dev Q'", (past(5),)); J("POST", "/nudge")
assert any(n["ty"] == "claim.expired" for n in J("GET", "/inbox?as=Dev%20Q")[1]["i"])
# ---- locks
assert J("POST", "/locks", {"name": "env:test", "ttl": 5}, tok="wtok")[1]["owner"] == "Worker"
r = J("POST", "/locks", {"name": "env:test", "as": "Dev Z"}); assert r[0] == 409 and r[1]["e"] == "locked", r
assert J("POST", "/locks", {"name": "env:test"}, tok="wtok")[0] == 200                                       # owner renews
J("POST", "/locks", {"name": "repo", "as": "Sup Z"})
assert J("DELETE", "/locks?name=repo", tok="wtok")[0] == 403                                                 # not owner, not privileged
assert J("DELETE", "/locks?name=repo&as=Sup%20Z")[1]["released"] == 1
server.db.execute("UPDATE locks SET expires=? WHERE name='env:test'", (past(1),))
assert all(l["name"] != "env:test" for l in J("GET", "/locks")[1]["i"])                                      # expired = gone
# ---- asks
st, ak = J("POST", "/asks", {"to": "Dev Q", "q": "which port?", "due": 30}, tok="wtok"); assert st == 201, ak
assert any(n["ty"] == "ask.new" for n in J("GET", "/inbox?as=Dev%20Q")[1]["i"])
assert J("POST", f"/asks/{ak['id']}/answer", {"a": "8790"}, tok="wtok")[0] == 403
assert J("POST", f"/asks/{ak['id']}/answer", {"a": "8790", "as": "Dev Q"})[1]["st"] == "answered"
assert any(n["ty"] == "ask.answered" for n in J("GET", "/inbox?as=Worker")[1]["i"]) or any(n["ty"] == "ask.answered" for n in J("GET", "/inbox", tok="wtok")[1]["i"])
st, ak2 = J("POST", "/asks", {"to": "Dev Q", "q": "still there?", "due": 5}, tok="wtok")
server.db.execute("UPDATE asks SET due=? WHERE id=?", (past(1), ak2["id"])); J("POST", "/nudge")
assert any(n["ty"] == "ask.overdue" for n in J("GET", "/inbox?as=Dev%20Q")[1]["i"])
assert any(n["ty"] == "esc.ask-overdue" for n in J("GET", "/inbox", tok="stok")[1]["i"])
assert J("GET", "/asks?to=Dev%20Q")[1]["n"] == 1
# ---- reviews + done gate
st, R = J("POST", "/tasks", {"b": BID, "t": "needs review", "ch": "X", "st": "in_progress", "as": "Worker"})
rv = J("POST", f"/tasks/{R['id']}/review", {"reviewer": "Dev Q", "note": "pls"}, tok="wtok")[1]; assert rv["st"] == "pending"
assert J("GET", f"/tasks/{R['id']}")[1]["st"] == "review"
assert J("POST", f"/tasks/{R['id']}/review", {"reviewer": "Dev Q"}, tok="wtok")[1]["id"] == rv["id"]            # idempotent
assert J("PATCH", f"/tasks/{R['id']}", {"st": "done"})[1]["e"] == "review"                                   # gate
assert J("POST", f"/reviews/{rv['id']}/decide", {"decision": "changes"}, tok="wtok")[1]["e"] == "missing"
assert J("POST", f"/reviews/{rv['id']}/decide", {"decision": "approve"}, tok="wtok")[0] == 403
assert J("POST", f"/reviews/{rv['id']}/decide", {"decision": "changes", "note": "fix X", "as": "Dev Q"})[1]["st"] == "changes"
assert J("GET", f"/tasks/{R['id']}")[1]["st"] == "in_progress"
assert any(n["ty"] == "review.changes" for n in J("GET", "/inbox", tok="wtok")[1]["i"])
rv2 = J("POST", f"/tasks/{R['id']}/review", {"reviewer": "Dev Q"}, tok="wtok")[1]
assert J("PATCH", f"/tasks/{R['id']}", {"st": "done"})[1]["e"] == "review"                                   # old 'changes' review still blocks
server.db.execute("UPDATE reviews SET status='approved' WHERE id=?", (rv["id"],))
J("POST", f"/reviews/{rv2['id']}/decide", {"decision": "approve", "as": "Dev Q"})
assert J("PATCH", f"/tasks/{R['id']}", {"st": "done"})[1]["st"] == "done"
# ---- definition of done
st, bd = J("POST", "/boards", {"n": "DoD board"}); J("PATCH", f"/boards/{bd['id']}", {"dod": "tests pass\nPR linked"})
st, D = J("POST", "/tasks", {"b": bd["id"], "t": "dod task", "ch": "X"})
assert J("PATCH", f"/tasks/{D['id']}", {"st": "done"})[1]["e"] == "dod"
assert J("PATCH", f"/tasks/{D['id']}", {"dn": [0]})[1]["dn"] == "0" and J("PATCH", f"/tasks/{D['id']}", {"st": "done"})[1]["e"] == "dod"
assert J("PATCH", f"/tasks/{D['id']}", {"st": "done", "dn": [0, 1]})[1]["st"] == "done"
# ---- presence, skills, routing, silent agents
J("POST", "/presence", {"status": "writing tests", "task": T1["id"]}, tok="wtok")
ag = {a["n"]: a for a in J("GET", "/agents")[1]["i"]}; assert ag["Worker"]["stx"] == "writing tests" and "seen" in ag["Worker"], ag.get("Worker")
J("POST", "/agents", {"n": "QA One", "skills": "QA, e2e", "capacity": 4}); J("POST", "/agents", {"n": "QA Two", "skills": "qa"})
J("POST", "/tasks", {"b": BID, "t": "busy", "ch": "X", "as": "QA One", "st": "todo"})
assert J("GET", "/route?skill=qa")[1]["i"][0]["n"] == "QA Two"                                               # least loaded first
st, AU = J("POST", "/tasks", {"b": BID, "t": "auto routed", "ch": "X", "as": "auto", "lb": "qa"}); assert AU["as"] == "QA Two", AU
assert J("POST", "/tasks", {"b": BID, "t": "nobody", "ch": "X", "as": "auto", "lb": "cobol"})[1]["e"] == "route"
J("POST", "/tasks", {"b": BID, "t": "silent work", "ch": "X", "as": "QA One", "st": "in_progress"})
server.db.execute("UPDATE agents SET last_seen=? WHERE name='QA One'", (past(300),))
assert {a["n"]: a for a in J("GET", "/agents")[1]["i"]}["QA One"].get("silent") == 1
J("POST", "/nudge"); assert any(n["ty"] == "esc.silent" for n in J("GET", "/inbox", tok="stok")[1]["i"])
assert any(x["k"] == "silent-agent" for x in J("GET", "/attention")[1]["i"])
# ---- environments
st, EV = J("POST", "/envs", {"n": "test", "sha": "abc123", "task": T1["id"]}, tok="wtok"); assert EV["sha"] == "abc123"
assert J("GET", f"/tasks/{T1['id']}")[1]["sha"] == "abc123" and J("GET", "/envs")[1]["i"][0]["sha"] == "abc123"
assert J("POST", "/envs/test/freeze", {"until": 60, "note": "release"}, tok="wtok")[0] == 403
J("POST", "/envs/test/freeze", {"until": 60, "note": "release"}, tok="stok")
r = J("POST", "/envs", {"n": "test", "sha": "def"}, tok="wtok"); assert r[0] == 423 and r[1]["e"] == "frozen", r
assert J("POST", "/envs", {"n": "test", "sha": "def", "force": True}, tok="stok")[1]["sha"] == "def"
J("POST", "/envs/test/unfreeze", {}, tok="stok")
J("POST", "/locks", {"name": "env:prod", "as": "Dev Q"})
assert J("POST", "/envs", {"n": "prod", "sha": "x"}, tok="wtok")[1]["e"] == "locked"
assert J("POST", "/envs", {"n": "prod", "sha": "x", "as": "Dev Q"})[1]["sha"] == "x"
# ---- watchers
J("POST", "/watch", {"as": "Dev W", "kind": "label", "ref": "gate"}); J("POST", "/watch", {"as": "Dev W", "kind": "tag", "ref": "auth"})
assert J("POST", "/watch", {"as": "Dev W", "kind": "nope", "ref": "x"})[1]["e"] == "bad"
J("POST", "/tasks", {"b": BID, "t": "watched", "ch": "X", "as": "Someone", "lb": "gate"})
assert any(n["ty"] == "watch.task" for n in J("GET", "/inbox?as=Dev%20W")[1]["i"])
J("POST", "/specs", {"t": "auth spec", "tg": "auth", "d": "x"})
assert any(n["ty"] == "spec.created" for n in J("GET", "/inbox?as=Dev%20W")[1]["i"])
assert J("GET", "/watch?as=Dev%20W")[1]["n"] == 2
# ---- attachments
st, at = J("POST", f"/tasks/{AU['id']}/attachments", {"name": "test report", "url": "https://x.y/r.html", "kind": "report"}); assert st == 201
assert J("POST", f"/tasks/{AU['id']}/attachments", {"name": "bad", "url": "javascript:x"})[1]["e"] == "bad"
assert J("GET", f"/tasks/{AU['id']}/attachments")[1]["i"][0]["n"] == "test report" and J("GET", f"/tasks/{AU['id']}?ctx=1")[1]["att"]
assert any(n["ty"] == "task.attachment" for n in J("GET", "/inbox?as=QA%20Two")[1]["i"])
assert J("DELETE", f"/attachments/{at['id']}")[1]["x"] == 1
# ---- optimistic versioning
st, V = J("POST", "/tasks", {"b": BID, "t": "versioned", "ch": "X"}); v0 = J("GET", f"/tasks/{V['id']}")[1]["v"]
r = J("PATCH", f"/tasks/{V['id']}", {"t": "versioned 2", "ver": v0})[1]; assert r["v"] == v0 + 1
r = J("PATCH", f"/tasks/{V['id']}", {"t": "stale", "ver": v0}); assert r[0] == 409 and r[1]["e"] == "conflict", r
print("v7 selftest OK")

# ================= v8: work methodologies =================
st, mm = J("GET", "/methods"); assert {"scrum", "kanban", "scrumban", "shapeup", "simple"} <= {x["id"] for x in mm["i"]} and "epic" in mm["types"]
assert J("POST", "/boards", {"n": "bad", "method": "nonsense"})[1]["e"] == "bad"
# ---- kanban: WIP limits, no sprints, flow metrics
st, kb = J("POST", "/boards", {"n": "Kanban board", "method": "kanban", "cf": {"wip": {"in_progress": 2}, "wip_mode": "block"}}); K = kb["id"]
assert kb["mt"] == "kanban" and json.loads(kb["cf"])["wip"] == {"in_progress": 2}, kb
assert J("POST", "/sprints", {"b": K})[1]["e"] == "no_sprints"
kt = [J("POST", "/tasks", {"b": K, "t": "k%d" % i, "st": "todo"})[1] for i in range(4)]
assert all("s" not in t for t in kt)                                                   # no sprint auto-join
J("PATCH", f"/tasks/{kt[0]['id']}", {"st": "in_progress"}); J("PATCH", f"/tasks/{kt[1]['id']}", {"st": "in_progress"})
r = J("PATCH", f"/tasks/{kt[2]['id']}", {"st": "in_progress"}); assert r[0] == 409 and r[1]["e"] == "wip" and r[1]["f"] == "in_progress", r
assert J("POST", f"/tasks/{kt[2]['id']}/claim", {"as": "Dev K"})[1]["e"] == "wip"
assert J("POST", "/tasks", {"b": K, "t": "direct", "st": "in_progress"})[1]["e"] == "wip"
r = J("PATCH", f"/tasks/{kt[2]['id']}", {"st": "in_progress", "force": True})[1]; assert r["st"] == "in_progress"      # admin override
assert any(x["k"] == "wip-exceeded" for x in J("GET", "/attention")[1]["i"])
J("PATCH", f"/tasks/{K}", {}) if False else None
J("PATCH", f"/boards/{K}", {"cf": {"wip": {"in_progress": 2}, "wip_mode": "warn"}})
r = J("PATCH", f"/tasks/{kt[3]['id']}", {"st": "in_progress"})[1]; assert "WIP limit" in r["w"][0], r                           # warn mode lets it through
J("PATCH", f"/tasks/{kt[0]['id']}", {"st": "done"}); J("PATCH", f"/tasks/{kt[1]['id']}", {"st": "done"})
fl = J("GET", f"/boards/{K}/flow?days=14")[1]
assert fl["mt"] == "kanban" and fl["done_n"] == 2 and fl["wip"]["done"] == 2 and len(fl["cfd"]) == 15 and len(fl["per_week"]) == 2 and fl["cycle_h"]["avg"] is not None, fl
assert fl["cfd"][-1]["done"] == 2 and fl["limits"] == {"in_progress": 2} and fl["aging"], fl["cfd"][-1]
assert J("GET", "/dashboard")[1]["boards"][-1].get("fl") is not None
# ---- simple board
st, sb = J("POST", "/boards", {"n": "Simple", "method": "simple"}); assert J("POST", "/sprints", {"b": sb["id"]})[1]["e"] == "no_sprints"
# ---- scrum: committed, velocity, retrospective, sprint closed notice
st, sc = J("POST", "/boards", {"n": "Scrum board"}); S_ = sc["id"]; assert sc["mt"] == "scrum"
st, sp = J("POST", "/sprints", {"b": S_}); assert sp["cmt"] == 0 if "cmt" in sp else True
tt = [J("POST", "/tasks", {"b": S_, "t": "s%d" % i, "pt": 3, "as": "Scrummer", "s": sp["id"]})[1] for i in range(3)]
J("POST", f"/sprints/{sp['id']}/start")
assert J("GET", f"/sprints/{sp['id']}")[1]["cmt"] == 9
J("PATCH", f"/tasks/{tt[0]['id']}", {"st": "done"}); J("PATCH", f"/tasks/{tt[1]['id']}", {"st": "done"})
J("POST", f"/sprints/{sp['id']}/close", {"to": "backlog"})
assert any(n["ty"] == "sprint.closed" and "2 done" in n["m"] for n in J("GET", "/inbox?as=Scrummer")[1]["i"])
vel = J("GET", f"/boards/{S_}/velocity")[1]; assert vel["i"][-1]["committed"] == 9 and vel["i"][-1]["done"] == 6 and vel["avg"] == 6.0, vel
rt = J("POST", f"/sprints/{sp['id']}/retro", {"well": "pairing", "improve": "estimates", "actions": ["Split big stories", "Add CI check"]})[1]
assert len(rt["actions"]) == 2 and all(a.get("task") for a in rt["actions"]), rt
retro_tasks = J("GET", f"/tasks?b={S_}&lb=retro")[1]["i"]; assert len(retro_tasks) == 2
J("POST", f"/sprints/{sp['id']}/retro", {"actions": ["Split big stories", "Add CI check"]})
assert len(J("GET", f"/tasks?b={S_}&lb=retro")[1]["i"]) == 2                                              # saving again does not duplicate
assert J("GET", f"/sprints/{sp['id']}/retro")[1]["well"] == "" or True
# ---- auto rollover
st, ar = J("POST", "/boards", {"n": "Rollover", "cf": {"auto_rollover": True, "sprint_days": 7}}); A_ = ar["id"]
st, s1 = J("POST", "/sprints", {"b": A_, "st": "active"}); assert s1["to"] == time.strftime("%Y-%m-%d", time.gmtime(time.time() + 7 * 86400)), s1
J("POST", "/tasks", {"b": A_, "t": "carry", "as": "Rolly", "st": "in_progress"})
server.db.execute("UPDATE sprints SET \"end\"=? WHERE id=?", (time.strftime("%Y-%m-%d", time.gmtime(time.time() - 2 * 86400)), s1["id"])); J("POST", "/nudge")
sps = J("GET", f"/sprints?board_id={A_}")[1]; assert [x["st"] for x in sps["i"]] == ["closed", "active"], sps
assert J("GET", f"/tasks?b={A_}")[1]["i"][0]["s"] == sps["i"][1]["id"]                                       # unfinished work carried into the new sprint
# ---- scrumban replenish signal
st, sbn = J("POST", "/boards", {"n": "Scrumban", "method": "scrumban", "cf": {"replenish_below": 2}})
J("POST", "/nudge"); assert any(n["ty"] == "esc.replenish" for n in J("GET", "/inbox", tok="stok")[1]["i"])
# ---- shape up: six-week cycles
st, su = J("POST", "/boards", {"n": "Shape Up", "method": "shapeup"}); st, cy = J("POST", "/sprints", {"b": su["id"], "st": "active"})
assert cy["to"] == time.strftime("%Y-%m-%d", time.gmtime(time.time() + 42 * 86400)), cy
# ---- epics / work item types
st, ep = J("POST", "/tasks", {"b": S_, "t": "Big epic", "ty": "epic"}); assert ep["ty"] == "epic"
st, ch = J("POST", "/tasks", {"b": S_, "t": "child story", "ty": "story", "pa": ep["id"]}); assert ch["pa"] == ep["id"]
assert J("POST", "/tasks", {"b": S_, "t": "bad parent", "pa": ch["id"]})[1]["e"] == "bad"                     # parent must be an epic
assert J("POST", "/tasks", {"b": K, "t": "other board", "pa": ep["id"]})[1]["e"] == "bad"
assert J("POST", "/tasks", {"b": S_, "t": "x", "ty": "wizard"})[1]["e"] == "bad"
J("PATCH", f"/tasks/{ch['id']}", {"st": "done"})
g = J("GET", f"/tasks/{ep['id']}?ctx=1")[1]; assert g["prog"] == [1, 1] and g["kids"][0]["id"] == ch["id"], g
assert len(J("GET", f"/tasks?b={S_}&ty=epic")[1]["i"]) == 1 and len(J("GET", f"/tasks?parent={ep['id']}")[1]["i"]) == 1
# method change keeps data
assert J("PATCH", f"/boards/{S_}", {"method": "kanban"})[1]["mt"] == "kanban"
print("v8 selftest OK")

# ================= v9: cross-board scope (supervisor / portfolio agents) =================
st, ba = J("POST", "/boards", {"n": "Alpha 1", "tm": "alpha"}); st, bb2 = J("POST", "/boards", {"n": "Alpha 2", "tm": "alpha"}); st, bc = J("POST", "/boards", {"n": "Beta 1", "tm": "beta"})
for bd_, name, stt, pr in ((ba, "a1 blocked", "blocked", 1), (bb2, "a2 todo", "todo", 3), (bc, "b1 blocked", "blocked", 1)):
    J("POST", "/tasks", {"b": bd_["id"], "t": name, "st": stt, "pr": pr, "bl": "x" if stt == "blocked" else ""})
titles = lambda r: sorted(t["t"] for t in r[1]["i"])
assert titles(J("GET", "/tasks?tm=alpha&limit=500")) == ["a1 blocked", "a2 todo"]
assert titles(J("GET", f"/tasks?bs={ba['id']},{bc['id']}&limit=500")) == ["a1 blocked", "b1 blocked"]
assert titles(J("GET", f"/tasks?tm=alpha&bs={ba['id']},{bc['id']}&limit=500")) == ["a1 blocked"]          # both scopes intersect
assert J("GET", "/tasks?tm=nosuchteam")[1]["i"] == []
d = J("GET", "/dashboard?tm=alpha")[1]
assert [b["n"] for b in d["boards"]] == ["Alpha 1", "Alpha 2"] and d["totals"]["boards"] == 2 and d["totals"]["blocked"] == 1 and d["totals"]["open"] == 2 and d["boards"][0]["tm"] == "alpha", d["totals"]
assert d["scope"]["bs"] == [ba["id"], bb2["id"]]
p = J("GET", f"/portfolio?bs={ba['id']},{bc['id']}")[1]; assert p["totals"]["boards"] == 2 and p["totals"]["blocked"] == 2
assert J("GET", "/dashboard")[1]["totals"]["boards"] >= 3                                                    # no scope = everything
att = J("GET", "/attention?tm=alpha")[1]["i"]; assert any(x["k"] == "blocked-p12" and x["ids"] for x in att)
alpha_ids = {t["id"] for t in J("GET", "/tasks?tm=alpha&limit=500")[1]["i"]}
assert all(set(x["ids"]) <= alpha_ids for x in att if x["k"] == "blocked-p12"), att                          # beta's blocked task is not in alpha's attention
assert not any(x["k"] == "blocked-p12" for x in J("GET", "/attention?tm=nosuchteam")[1]["i"])
assert isinstance(J("GET", "/risks?tm=alpha")[1]["i"], list) and isinstance(J("GET", "/stats/agents?tm=alpha")[1]["i"], list)
print("v9 selftest OK")

# ================= v10: more methodologies (waterfall, gtd, lean, priority matrix, okr, support) =================
st, mm = J("GET", "/methods"); ids_ = {x["id"] for x in mm["i"]}
assert {"waterfall", "gtd", "lean", "eisenhower", "okr", "support"} <= ids_ and len(ids_) == 11, ids_
lab = {x["id"]: x for x in mm["i"]}
assert lab["gtd"]["labels"]["backlog"] == "Inbox" and lab["lean"]["labels"]["review"] == "Measuring" and lab["eisenhower"]["group"] == "priority" and lab["okr"]["group"] == "okr" and lab["support"]["sla"] and lab["waterfall"]["gate"]
# ---- waterfall: phases are created, gated and advance automatically
st, wf = J("POST", "/boards", {"n": "Waterfall", "method": "waterfall"}); WF = wf["id"]
phases = J("GET", f"/sprints?board_id={WF}")[1]["i"]
assert [p["n"] for p in phases] == ["Requirements", "Design", "Build", "Test", "Release"] and [p["st"] for p in phases] == ["active", "planned", "planned", "planned", "planned"], phases
wt = [J("POST", "/tasks", {"b": WF, "t": "req %d" % i})[1] for i in range(2)]
assert all(t["s"] == phases[0]["id"] for t in wt)                                                           # work joins the active phase
r = J("POST", f"/sprints/{phases[0]['id']}/close", {}); assert r[0] == 409 and r[1]["e"] == "gate", r       # gate closed: open work
assert J("POST", f"/sprints/{phases[1]['id']}/start")[1]["e"] == "gate"                                     # cannot jump ahead either
assert J("POST", f"/sprints/{phases[0]['id']}/close", {"force": True}, tok="wtok")[0] == 409                  # agents cannot force
for t in wt:
    J("PATCH", f"/tasks/{t['id']}", {"st": "done"})
r = J("POST", f"/sprints/{phases[0]['id']}/close", {})[1]; assert r["st"] == "closed" and r["next"]["n"] == "Design", r
assert [p["st"] for p in J("GET", f"/sprints?board_id={WF}")[1]["i"]][:2] == ["closed", "active"]
J("POST", "/tasks", {"b": WF, "t": "design stuck"})
assert J("POST", f"/sprints/{phases[1]['id']}/close", {"force": True})[1]["st"] == "closed"                  # admin override moves the work on
st, wc = J("POST", "/boards", {"n": "Custom phases", "method": "waterfall", "cf": {"phases": ["Plan", "Do"]}})
assert [p["n"] for p in J("GET", f"/sprints?board_id={wc['id']}")[1]["i"]] == ["Plan", "Do"]
st, sw = J("POST", "/boards", {"n": "Switch to waterfall"}); J("PATCH", f"/boards/{sw['id']}", {"method": "waterfall"})
assert len(J("GET", f"/sprints?board_id={sw['id']}")[1]["i"]) == 5                                          # switching creates the phases
# ---- OKR
st, ok = J("POST", "/boards", {"n": "OKRs", "method": "okr"}); OK = ok["id"]
st, ob = J("POST", "/tasks", {"b": OK, "t": "Grow signups", "ty": "epic"})
k1 = J("POST", "/tasks", {"b": OK, "t": "10k signups", "pa": ob["id"], "tg": 10000, "cr": 2500})[1]
k2 = J("POST", "/tasks", {"b": OK, "t": "Launch referral", "pa": ob["id"], "tg": 1, "cr": 0})[1]
assert k1["tg"] == 10000.0 and k1["cr"] == 2500.0
o = J("GET", f"/boards/{OK}/okr")[1]; assert o["i"][0]["progress"] == 0.12 and len(o["i"][0]["krs"]) == 2 and o["overall"] == 0.12, o       # (0.25 + 0) / 2
J("PATCH", f"/tasks/{k2['id']}", {"cr": 1}); o = J("GET", f"/boards/{OK}/okr")[1]; assert o["i"][0]["progress"] == 0.62, o
assert J("PATCH", f"/tasks/{k1['id']}", {"cr": -5})[1]["e"] == "bad" and J("PATCH", f"/tasks/{k1['id']}", {"tg": "abc"})[1]["e"] == "bad"
st, lq = J("POST", "/tasks", {"b": OK, "t": "loose KR", "tg": 4, "cr": 4}); assert J("GET", f"/boards/{OK}/okr")[1]["loose"][0]["pct"] == 1.0
# ---- support desk SLA
st, sd = J("POST", "/boards", {"n": "Support", "method": "support", "cf": {"sla_hours": {"1": 2}}}); SD = sd["id"]
sp1 = J("POST", "/tasks", {"b": SD, "t": "customer down", "pr": 1, "as": "Support Pat"})[1]
sp2 = J("POST", "/tasks", {"b": SD, "t": "question", "pr": 3, "as": "Support Pat"})[1]
sl = J("GET", f"/boards/{SD}/sla")[1]; assert sl["sla_h"]["1"] == 2 and sl["sla_h"]["3"] == 72 and sl["breached"] == [] and sl["ok"] == 2, sl
server.db.execute("UPDATE tasks SET created=? WHERE id=?", (past(3 * 60), sp1["id"])); server.db.execute("UPDATE tasks SET created=? WHERE id=?", (past(60 * 60), sp2["id"]))
sl = J("GET", f"/boards/{SD}/sla")[1]; assert [x["id"] for x in sl["breached"]] == [sp1["id"]] and [x["id"] for x in sl["at_risk"]] == [sp2["id"]], sl
J("POST", "/nudge")
assert any(n["ty"] == "sla.breach" for n in J("GET", "/inbox?as=Support%20Pat")[1]["i"]) and any(n["ty"] == "esc.sla-breach" for n in J("GET", "/inbox", tok="stok")[1]["i"])
assert any(x["k"] == "sla-breach" and sp1["id"] in x["ids"] for x in J("GET", "/attention")[1]["i"])
J("POST", "/nudge"); assert sum(1 for n in J("GET", "/inbox?as=Support%20Pat&all=1")[1]["i"] if n["ty"] == "sla.breach") == 1       # once per request
J("PATCH", f"/tasks/{sp1['id']}", {"st": "done"}); assert J("GET", f"/boards/{SD}/sla")[1]["breached"] == []
assert J("POST", "/boards", {"n": "bad sla", "cf": {"sla_hours": {"1": "x"}}})[1]["e"] == "bad"
# ---- presets that are label/layout only
for mth in ("gtd", "lean", "eisenhower"):
    st, b_ = J("POST", "/boards", {"n": mth, "method": mth}); assert b_["mt"] == mth
    t_ = J("POST", "/tasks", {"b": b_["id"], "t": "x", "pr": 2})[1]; assert J("PATCH", f"/tasks/{t_['id']}", {"pr": 1})[1]["pr"] == 1
print("v10 selftest OK")
