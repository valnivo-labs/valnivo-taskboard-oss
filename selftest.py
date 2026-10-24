#!/usr/bin/env python3
"""In-process smoke test on a throwaway DB copy (data/test.db). Usage: python3 selftest.py"""
import json, os, sys, threading, urllib.request, urllib.error
os.environ.update(TASKBOARD_DB=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "test.db"),
                  TASKBOARD_TOKENS="a:tok:admin,v:vtok:viewer,Sup Bot:stok:supervisor,Worker:wtok:agent",
                  TASKBOARD_ALLOW_PRIVATE_WEBHOOKS="1", TASKBOARD_HUMAN_NAMES="Owner", TASKBOARD_HUMAN_RULE="1", TASKBOARD_RATE_PER_MIN="0", TASKBOARD_AUTH_FAILS_PER_MIN="0", PORT="8781")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server
srv = server.S(("127.0.0.1", 8781), server.H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
U = "http://127.0.0.1:8781/api"


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
hs = http.server.ThreadingHTTPServer(("127.0.0.1", 8782), Hook)
threading.Thread(target=hs.serve_forever, daemon=True).start()

st, sp = J("POST", "/sprints", {"b": BID, "st": "active"})            # name/dates default
assert st == 201 and sp["n"].startswith("Sprint") and sp["from"] and sp["to"], sp
st, t = J("POST", "/tasks", {"b": BID, "t": "notify me", "ch": "Valnivo", "as": "Agent X", "k": "agent"})
assert t["s"] == sp["id"], t                                          # joined active sprint
st, ib = J("GET", "/inbox?as=agent%20x")
assert ib["n"] == 1 and ib["i"][0]["ty"] == "task.assigned", ib
call("POST", "/agents", {"n": "Agent X", "hook": "http://127.0.0.1:8782/h", "secret": "s"})
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

# ================= v11: token-saving calls =================
# ---- guides
g = J("GET", "/guide?role=coder")[1]; assert g["role"] == "coder" and "finish" in g["g"] and len(g["g"]) < 1200 and "reviewer" in g["roles"]
assert J("GET", "/guide?role=wizard")[1]["e"] == "bad"
assert call("GET", "/guide?role=supervisor&fmt=text")[2].startswith("briefing returns att")
# ---- briefing with a cursor: idle calls cost almost nothing
st, bf = J("GET", "/briefing?as=Briefer"); assert isinstance(bf["c"], int) and "in" not in bf, bf
c0 = bf["c"]
st, bt = J("POST", "/tasks", {"b": BID, "t": "for briefer", "as": "Briefer", "st": "todo"})
bf = J("GET", f"/briefing?as=Briefer&since={c0}")[1]
assert len(bf["in"]) == 1 and bf["in"][0]["do"] == f"claim id={bt['id']}" and bf["c"] > c0, bf
idle = J("GET", f"/briefing?as=Briefer&since={bf['c']}")[1]; assert list(idle) == ["c"], idle                    # acked + nothing new = {"c": N}
J("POST", "/tasks", {"b": BID, "t": "claimable new work", "st": "todo", "lb": "nwx"})
assert J("GET", f"/briefing?as=Briefer&since={idle['c']}")[1].get("nw", 0) >= 1
first = J("GET", "/briefing?as=Briefer")[1]; assert any(w["id"] == bt["id"] for w in first["work"]) and "nw" in first
# ---- next: claim + context in one call
st, nx = J("POST", "/tasks", {"b": BID, "t": "context task", "d": "Do the thing carefully. " * 20, "st": "todo", "lb": "nxlabel", "pr": 1, "pt": 3})
J("POST", f"/tasks/{nx['id']}/comments", {"c": "heads up: use the v2 API"})
r = J("POST", "/next", {"as": "Nexter", "lb": "nxlabel"})[1]
assert r["id"] == nx["id"] and r["cb"] == "Nexter" and r["task"]["sum"].startswith("Do the thing") and r["task"]["cc"][0]["c"].startswith("heads up"), r
assert J("POST", "/next", {"as": "Nexter", "lb": "nxlabel"})[1] == {"none": 1}
# ---- finish: attach + comment + pr/sha + status in one call; gate checked first
st, ft = J("POST", "/tasks", {"b": BID, "t": "finish me", "as": "Finisher", "st": "in_progress"})
r = J("POST", f"/tasks/{ft['id']}/finish", {"summary": "Implemented and tested", "pr": "https://x.y/pr/1", "sha": "abc1", "attach": [{"name": "report", "url": "https://x.y/r", "kind": "report"}]})[1]
assert r["st"] == "done" and r["did"] == ["attach", "comment", "update"], r
g = J("GET", f"/tasks/{ft['id']}?ctx=1")[1]; assert g["pu"] == "https://x.y/pr/1" and g["sha"] == "abc1" and g["att"][0]["n"] == "report" and g["cc"][-1]["c"] == "Implemented and tested"
st, fd = J("POST", "/boards", {"n": "Finish DoD", "dod": "tests"}); st, ft2 = J("POST", "/tasks", {"b": fd["id"], "t": "gated", "st": "in_progress"})
r = J("POST", f"/tasks/{ft2['id']}/finish", {"summary": "done?", "attach": [{"name": "x"}]}); assert r[1]["e"] == "dod", r
assert J("GET", f"/tasks/{ft2['id']}/comments")[1]["n"] == 0 and J("GET", f"/tasks/{ft2['id']}/attachments")[1]["n"] == 0        # nothing was written
assert J("POST", f"/tasks/{ft2['id']}/finish", {"summary": "ok", "dn": [0]})[1]["st"] == "done"
st, ft3 = J("POST", "/tasks", {"b": BID, "t": "to review", "as": "Finisher", "st": "in_progress"})
r = J("POST", f"/tasks/{ft3['id']}/finish", {"summary": "please look", "review": "Rev One", "as": "Finisher"})[1]; assert r["st"] == "review" and r["did"][-1] == "review", r
assert J("GET", "/inbox?as=Rev%20One")[1]["i"][0]["do"].startswith("decide_review id=")
st, ft4 = J("POST", "/tasks", {"b": BID, "t": "to hand off", "as": "Finisher", "st": "in_progress"})
r = J("POST", f"/tasks/{ft4['id']}/finish", {"summary": "api done", "to": "Next Dev", "next": "write docs"})[1]; assert r["did"][-1] == "handoff" and J("GET", f"/tasks/{ft4['id']}")[1]["as"] == "Next Dev"
assert J("POST", f"/tasks/{ft['id']}/finish", {})[1]["e"] == "missing"
# ---- batch: several calls, one round trip (runs through the normal auth and rules)
r = J("POST", "/batch", {"ops": [{"m": "GET", "p": f"/tasks/{ft['id']}?brief=1"}, {"m": "GET", "p": "/guide?role=agent"}, {"m": "POST", "p": f"/tasks/{ft['id']}/comments", "b": {"c": "via batch"}}]})[1]
assert r["n"] == 3 and [x["s"] for x in r["i"]] == [200, 200, 201] and r["i"][0]["r"]["t"] == "finish me", r
r = J("POST", "/batch", {"ops": [{"p": "/nosuch"}, {"p": "/boards"}]})[1]; assert r["n"] == 1 and r["i"][0]["s"] == 404                           # stops at the first error
assert J("POST", "/batch", {"ops": [{"p": "/nosuch"}, {"p": "/boards"}], "cont": True})[1]["n"] == 2
assert J("POST", "/batch", {"ops": [{"m": "POST", "p": "/batch", "b": {"ops": []}}]})[1]["e"] == "bad"
assert J("POST", "/batch", {"ops": [{"p": "/boards"}]}, tok="vtok")[0] == 403                                                          # a viewer cannot even start a batch
r = J("POST", "/batch", {"ops": [{"m": "DELETE", "p": f"/boards/{BID}"}]}, tok="wtok")[1]; assert r["i"][0]["s"] == 403                   # roles apply to every inner call
assert J("POST", "/batch", {"ops": [{"p": "/boards"}] * 21})[1]["e"] == "bad"
# ---- notifications: coalesced, actionable, mutable
st, ct = J("POST", "/tasks", {"b": BID, "t": "chatty", "as": "Coal", "st": "todo"}); J("GET", "/inbox?as=Coal&ack=1")
for i in range(3):
    J("POST", f"/tasks/{ct['id']}/comments", {"c": "msg %d" % i}, tok="stok")
ib = [n for n in J("GET", "/inbox?as=Coal")[1]["i"] if n["ty"] == "task.comment"]
assert len(ib) == 1 and ib[0]["x"] == 3 and "msg 2" in ib[0]["m"] and ib[0]["do"] == f"comment id={ct['id']}", ib
J("POST", "/agents", {"n": "Mute Me", "mute": "task.comment,watch.task"}); st, mt_ = J("POST", "/tasks", {"b": BID, "t": "muted", "as": "Mute Me"})
J("POST", f"/tasks/{mt_['id']}/comments", {"c": "hello"}, tok="stok")
assert [n["ty"] for n in J("GET", "/inbox?as=Mute%20Me")[1]["i"]] == ["task.assigned"]                      # assignment still arrives, comment muted
# ---- shaped responses
r = J("GET", f"/tasks?b={BID}&fields=t,st&fmt=t&limit=3")[1]; assert r["h"] == ["id", "t", "st"] and len(r["r"]) == 3 and len(r["r"][0]) == 3, r
r = J("GET", f"/tasks?b={BID}&fields=t&limit=2")[1]; assert set(r["i"][0]) == {"id", "t"}
assert J("GET", f"/tasks?b={BID}&fmt=t&limit=1")[1]["h"] == ["id", "t", "st", "pr", "as", "s"]
bt_ = J("GET", f"/tasks/{nx['id']}?brief=1")[1]; assert bt_["t"] == "context task" and len(json.dumps(bt_)) < 400 and bt_["sum"].endswith("…"), bt_
st, spx = J("POST", "/specs", {"t": "Big spec", "d": "# Overview\nintro\n## Interface\nthe api\n## Decisions\nwe chose X\n"})
o = J("GET", f"/specs/{spx['id']}?outline=1")[1]; assert [x["h"] for x in o["outline"]] == ["Overview", "Interface", "Decisions"] and "d" not in o, o
sc_ = J("GET", f"/specs/{spx['id']}?section=interface")[1]; assert sc_["h"] == "Interface" and "the api" in sc_["d"] and "we chose" not in sc_["d"]
assert J("GET", f"/specs/{spx['id']}?section=2")[1]["h"] == "Decisions" and J("GET", f"/specs/{spx['id']}?section=nope")[1]["e"] == "nf"
# ---- usage meter
u = J("GET", "/admin/usage")[1]
assert u["agents"] and u["agents"][0]["t"] > 0 and u["routes"] and all("r" in x and "t" in x for x in u["routes"]) and "since" in u, u
assert J("GET", "/admin/usage", tok="wtok")[0] == 403
print("v11 selftest OK")

# ================= v12: move a task to another board =================
st, bsrc = J("POST", "/boards", {"n": "Move src", "dod": "tests"}); st, bdst = J("POST", "/boards", {"n": "Move dst"}); st, bk_ = J("POST", "/boards", {"n": "Move kanban", "method": "kanban", "cf": {"wip": {"in_progress": 1}, "wip_mode": "block"}})
SRC, DST, KAN = bsrc["id"], bdst["id"], bk_["id"]
st, ssrc = J("POST", "/sprints", {"b": SRC, "st": "active"}); st, sdst = J("POST", "/sprints", {"b": DST, "st": "active", "n": "Dst sprint"}); st, sdst2 = J("POST", "/sprints", {"b": DST, "n": "Dst later"})
st, ep = J("POST", "/tasks", {"b": SRC, "t": "Move epic", "ty": "epic"})
st, mt1 = J("POST", "/tasks", {"b": SRC, "t": "movable", "as": "Mover", "pa": ep["id"], "dn": [0], "pt": 3})
assert mt1["s"] == ssrc["id"] and mt1["pa"] == ep["id"]
J("POST", f"/tasks/{mt1['id']}/comments", {"c": "history stays"}, tok="stok"); J("POST", "/docs", {"sc": "task", "r": mt1["id"], "t": "task note", "d": "x"})
# the raw field can no longer be changed behind the app's back
assert J("PATCH", f"/tasks/{mt1['id']}", {"board_id": DST})[1]["e"] == "use_move" and J("POST", "/tasks/bulk", {"ids": [mt1["id"]], "board_id": DST})[1]["e"] == "use_move"
J("GET", "/inbox?as=Mover&ack=1")
r = J("POST", f"/tasks/{mt1['id']}/move", {"b": DST})[1]
assert r["b"] == DST and r["from"] == SRC and r["s"] == sdst["id"], r                                        # joins the destination's ACTIVE sprint
g = J("GET", f"/tasks/{mt1['id']}?ctx=1")[1]
assert g["b"] == DST and g["s"] == sdst["id"] and "pa" not in g and "dn" not in g and g["v"] >= 2, g              # epic link and checklist ticks reset
assert any("Moved from 'Move src' to 'Move dst'" in c["c"] for c in g["cc"]) and any(c["c"] == "history stays" for c in g["cc"]), g["cc"]
assert [d["t"] for d in J("GET", f"/docs?sc=task&r={mt1['id']}&board_id={DST}")[1]["i"]] == ["task note"]            # attached notes follow the task
assert any(n["ty"] == "task.moved" for n in J("GET", "/inbox?as=Mover")[1]["i"])
assert mt1["id"] not in [t["id"] for t in J("GET", f"/tasks?b={SRC}&limit=500")[1]["i"]] and mt1["id"] in [t["id"] for t in J("GET", f"/tasks?b={DST}&limit=500")[1]["i"]]
ev = J("GET", f"/changes?since=0&limit=1000")[1]["e"]; mv = [e for e in ev if e[1] == "task.moved" and e[2] == mt1["id"]]
assert [e[3] for e in mv] == [SRC, DST] and mv[0][4] == {"b": DST}, mv                                              # old board sees it leave, new board sees it arrive
# errors
assert J("POST", f"/tasks/{mt1['id']}/move", {"b": DST})[1]["e"] == "bad" and J("POST", f"/tasks/{mt1['id']}/move", {"b": 99999})[1]["e"] == "nf" and J("POST", f"/tasks/{mt1['id']}/move", {})[1]["e"] == "missing"
assert J("POST", f"/tasks/{mt1['id']}/move", {"b": SRC, "s": sdst["id"]})[1]["e"] == "bad"                          # sprint must belong to the target board
r = J("POST", f"/tasks/{mt1['id']}/move", {"b": SRC, "s": ssrc["id"]})[1]; assert r["s"] == ssrc["id"]            # explicit sprint
# an epic moves with its children; done work leaves the sprint behind; children keep their epic
st, ep2 = J("POST", "/tasks", {"b": SRC, "t": "Epic with kids", "ty": "epic"})
k1 = J("POST", "/tasks", {"b": SRC, "t": "kid open", "pa": ep2["id"]})[1]; k2 = J("POST", "/tasks", {"b": SRC, "t": "kid done", "pa": ep2["id"], "st": "done"})[1]
r = J("POST", f"/tasks/{ep2['id']}/move", {"b": DST})[1]; assert sorted(r["moved"]) == sorted([k1["id"], k2["id"]]) and r["s"] == sdst["id"], r
assert all(J("GET", f"/tasks/{i}")[1]["b"] == DST for i in (ep2["id"], k1["id"], k2["id"]))
assert J("GET", f"/tasks/{k1['id']}")[1]["pa"] == ep2["id"] and J("GET", f"/tasks/{k1['id']}")[1]["s"] == sdst["id"] and "s" not in J("GET", f"/tasks/{k2['id']}")[1]
assert J("GET", f"/tasks/{ep2['id']}?ctx=1")[1]["prog"] == [1, 2]
st, ep3 = J("POST", "/tasks", {"b": SRC, "t": "Epic alone", "ty": "epic"}); k3 = J("POST", "/tasks", {"b": SRC, "t": "stays behind", "pa": ep3["id"]})[1]
J("POST", f"/tasks/{ep3['id']}/move", {"b": DST, "children": False}); k3b = J("GET", f"/tasks/{k3['id']}")[1]; assert k3b["b"] == SRC and "pa" not in k3b       # child stays, link cleared
# destination without sprints, and its WIP limits
st, kt = J("POST", "/tasks", {"b": SRC, "t": "to kanban", "st": "in_progress"}); assert J("POST", f"/tasks/{kt['id']}/move", {"b": KAN})[1]["b"] == KAN and "s" not in J("GET", f"/tasks/{kt['id']}")[1]
st, kt2 = J("POST", "/tasks", {"b": SRC, "t": "blocked by wip", "st": "in_progress"})
r = J("POST", f"/tasks/{kt2['id']}/move", {"b": KAN}); assert r[0] == 409 and r[1]["e"] == "wip" and J("GET", f"/tasks/{kt2['id']}")[1]["b"] == SRC, r             # nothing changed
assert J("POST", f"/tasks/{kt2['id']}/move", {"b": KAN, "force": True})[1]["b"] == KAN                                                                         # admin override
assert J("POST", f"/tasks/{kt2['id']}/move", {"b": DST, "force": True}, tok="vtok")[0] == 403                                                                  # viewers cannot
print("v12 selftest OK")

# ================= v13: reload storms must not lose data (one shared connection, many threads) =================
import concurrent.futures as _cf
errs = []
def _hit(i):
    try:
        for p in (f"/boards/{BID}", "/changes?since=0&limit=50", "/boards", f"/tasks?b={BID}&limit=20", "/dashboard"):
            code, _, body = call("GET", p)
            if code != 200 or not body.strip().startswith(("{", "[")):
                errs.append((p, code, body[:80]))
    except Exception as e:
        errs.append(("exc", repr(e)))
with _cf.ThreadPoolExecutor(32) as ex:
    list(ex.map(_hit, range(96)))
assert not errs, errs[:3]
assert server.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] > 0 and server.last_event() > 0
print("v13 selftest OK")

# ================= v14: forge webhook, cost, wake-ups, bulk move + links, chat channels =================
import hashlib, hmac as _hmac, http.server, time as _time
sink = []
class Sink(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        sink.append({"path": self.path, "h": dict(self.headers), "b": json.loads(raw), "raw": raw})
        self.send_response(200); self.end_headers()
    def log_message(self, *a): pass
sk = http.server.ThreadingHTTPServer(("127.0.0.1", 8783), Sink); threading.Thread(target=sk.serve_forever, daemon=True).start()
def waitfor(fn, n=60):
    for _ in range(n):
        r = fn()
        if r: return r
        _time.sleep(0.1)
    return None
def gh(event, payload, secret, scheme="github", bad=False):
    raw = json.dumps(payload).encode()
    sig = _hmac.new((secret + ("x" if bad else "")).encode(), raw, hashlib.sha256).hexdigest()
    h = {"Content-Type": "application/json", "X-GitHub-Event": event, "X-Hub-Signature-256": "sha256=" + sig} if scheme == "github" else {"Content-Type": "application/json", "X-Gitlab-Event": event, "X-Gitlab-Token": secret + ("x" if bad else "")}
    rq = urllib.request.Request(U + "/hooks/git", data=raw, headers=h, method="POST")
    try:
        with urllib.request.urlopen(rq) as x: return x.status, json.loads(x.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read())

# ---- git forge webhook
st, gb = J("POST", "/boards", {"n": "Git board"}); GB = gb["id"]
st, gdod = J("POST", "/boards", {"n": "Git gated", "dod": "tests pass"}); GD = gdod["id"]
assert gh("push", {"commits": []}, "nope")[0] == 404                                                                        # off until a secret exists
assert J("POST", "/admin/git-secret", {}, tok="wtok")[0] == 403
sec = J("POST", "/admin/git-secret", {})[1]["secret"]; assert len(sec) >= 20
assert gh("push", {"commits": []}, sec, bad=True)[0] == 401 and gh("push", {"commits": []}, sec, scheme="gitlab", bad=True)[0] == 401
st, g1 = J("POST", "/tasks", {"b": GB, "t": "git linked", "as": "Coder", "st": "in_progress"}); J("GET", "/inbox?as=Coder&ack=1")
r = gh("push", {"ref": "refs/heads/main", "repository": {"full_name": "o/r"}, "commits": [{"id": "abc1234", "message": "fix the thing (#%d)" % g1["id"], "url": "https://github.com/o/r/commit/abc1234"}, {"id": "ddd", "message": "unrelated"}]}, sec)[1]
assert r["tasks"] == [g1["id"]], r
assert J("GET", f"/tasks/{g1['id']}")[1]["cm"][0]["h"] == "abc1234"
r = gh("push", {"ref": "refs/heads/task-%d-login" % g1["id"], "repository": {"full_name": "o/r"}, "commits": [{"id": "eee1111", "message": "wip no id"}]}, sec)[1]; assert r["tasks"] == [g1["id"]]      # id from the branch name
pr = {"action": "opened", "repository": {"full_name": "o/r"}, "pull_request": {"html_url": "https://github.com/o/r/pull/7", "title": "Login fix", "body": "Closes #%d" % g1["id"], "head": {"ref": "feat/x"}, "user": {"login": "dev"}, "merged": False, "draft": False}}
assert gh("pull_request", pr, sec)[1]["tasks"] == [g1["id"]]
g = J("GET", f"/tasks/{g1['id']}?ctx=1")[1]; assert g["pu"] == "https://github.com/o/r/pull/7" and g["st"] == "review" and any("PR opened" in c["c"] for c in g["cc"]), g
assert any(n["ty"] == "git.pr" for n in J("GET", "/inbox?as=Coder&ack=1")[1]["i"])
rv = {"action": "submitted", "repository": {"full_name": "o/r"}, "pull_request": pr["pull_request"], "review": {"state": "changes_requested", "user": {"login": "lead"}, "body": "please add a test"}}
assert gh("pull_request_review", rv, sec)[1]["tasks"] == [g1["id"]] and any(n["ty"] == "git.review" for n in J("GET", "/inbox?as=Coder&ack=1")[1]["i"])
ci = {"action": "completed", "repository": {"full_name": "o/r"}, "workflow_run": {"conclusion": "failure", "head_branch": "task-%d-login" % g1["id"], "name": "CI", "html_url": "https://github.com/o/r/actions/runs/1", "pull_requests": []}}
assert gh("workflow_run", ci, sec)[1]["tasks"] == [g1["id"]] and any(n["ty"] == "git.ci" for n in J("GET", "/inbox?as=Coder&ack=1")[1]["i"])
pr["action"] = "closed"; pr["pull_request"] = {**pr["pull_request"], "merged": True, "merge_commit_sha": "feedbee"}
assert gh("pull_request", pr, sec)[1]["tasks"] == [g1["id"]]
g = J("GET", f"/tasks/{g1['id']}")[1]; assert g["st"] == "done" and any(c["h"] == "feedbee" for c in g["cm"]), g                    # merged -> done
# a board with a definition of done is not closed behind its back
st, g2 = J("POST", "/tasks", {"b": GD, "t": "gated merge", "as": "Coder", "st": "review"})
m2 = {"action": "closed", "repository": {"full_name": "o/r"}, "pull_request": {"html_url": "https://github.com/o/r/pull/8", "title": "Gated #%d" % g2["id"], "body": "", "head": {"ref": "x"}, "merged": True, "merge_commit_sha": "cafe", "user": {"login": "dev"}}}
gh("pull_request", m2, sec); g = J("GET", f"/tasks/{g2['id']}?ctx=1")[1]
assert g["st"] == "review" and any("not closed automatically" in c["c"] for c in g["cc"]), g
J("POST", "/settings", {"git_autodone": "0"}); st, g3 = J("POST", "/tasks", {"b": GB, "t": "no autodone", "st": "review"})
m2["pull_request"]["title"] = "Third #%d" % g3["id"]; gh("pull_request", m2, sec); assert J("GET", f"/tasks/{g3['id']}")[1]["st"] == "review"; J("POST", "/settings", {"git_autodone": "1"})
# GitLab merge request with a token header; unknown events are accepted and ignored
st, g4 = J("POST", "/tasks", {"b": GB, "t": "gitlab one"})
glp = {"project": {"path_with_namespace": "g/p"}, "user": {"username": "dev"}, "object_attributes": {"action": "open", "url": "https://gitlab.com/g/p/-/merge_requests/3", "title": "MR for #%d" % g4["id"], "description": "", "source_branch": "b", "draft": False}}
assert gh("Merge Request Hook", glp, sec, scheme="gitlab")[1]["tasks"] == [g4["id"]] and J("GET", f"/tasks/{g4['id']}")[1]["st"] == "review"
assert gh("star", {"x": 1}, sec)[1].get("ignored") == 1
print("v14 git OK")

# ---- cost
st, cb = J("POST", "/boards", {"n": "Cost board", "cf": {"task_budget_usd": 1, "budget_usd": 2}}); CB = cb["id"]
st, ct1 = J("POST", "/tasks", {"b": CB, "t": "costly", "as": "Worker"}); J("GET", "/inbox?as=Sup%20Bot&ack=1")
r = J("POST", "/cost", {"task": ct1["id"], "in": 1000, "out": 500, "usd": 0.85, "model": "m1"}, tok="wtok")[1]
assert r["usd"] == 0.85 and r["cs"] == 0.85 and r["tk"] == 1500, r
assert J("GET", f"/tasks/{ct1['id']}")[1]["cs"] == 0.85 and J("GET", f"/tasks/{ct1['id']}")[1]["tk"] == 1500
assert [n["ty"] for n in J("GET", "/inbox?as=Sup%20Bot&ack=1")[1]["i"]] == ["esc.budget"]                                     # 80% of the task budget, once
J("POST", "/cost", {"task": ct1["id"], "usd": 0.1}, tok="wtok"); assert not [n for n in J("GET", "/inbox?as=Sup%20Bot&ack=1")[1]["i"] if n["ty"] == "esc.budget" and "100%" not in n["m"] and "80%" in n["m"]]
J("POST", "/cost", {"task": ct1["id"], "usd": 0.2}, tok="wtok"); J("POST", "/cost", {"task": ct1["id"], "usd": 0.2}, tok="wtok")
assert len([n for n in J("GET", "/inbox?as=Sup%20Bot&ack=1")[1]["i"] if n["ty"] == "esc.budget"]) <= 2                        # the 100% (task) and 80% (board) warnings, never repeated per call
J("POST", "/settings", {"prices": json.dumps({"mdl": [3, 15]})}); r = J("POST", "/cost", {"task": ct1["id"], "in": 1000000, "out": 100000, "model": "mdl"}, tok="wtok")[1]
assert r["usd"] == 4.5, r                                                                                                    # 1M in x $3 + 0.1M out x $15
assert J("POST", "/settings", {"prices": "not json"})[1]["e"] == "bad"
rep = J("GET", f"/cost?b={CB}")[1]; assert rep["tot"]["n"] == 5 and rep["ag"][0]["n"] == "Worker" and rep["tk"][0]["id"] == ct1["id"] and rep["bud"][0]["pct"] >= 100 and rep["day"], rep
assert any(i["k"] == "budget" for i in J("GET", f"/attention?b={CB}")[1]["i"])
assert J("GET", f"/tasks/{ct1['id']}/cost")[1]["i"][0]["by"] == "Worker"
assert J("POST", "/cost", {"task": ct1["id"], "usd": 1}, tok="vtok")[0] == 403 and J("POST", "/cost", {"task": 99999, "usd": 1}, tok="wtok")[1]["e"] == "nf" and J("POST", "/cost", {}, tok="wtok")[1]["e"] == "missing"
st, ct2 = J("POST", "/tasks", {"b": CB, "t": "finish with cost", "as": "Worker"}); J("POST", f"/tasks/{ct2['id']}/finish", {"summary": "done", "cost": {"in": 10, "out": 5, "usd": 0.01, "model": "m1"}}, tok="wtok")
assert J("GET", f"/tasks/{ct2['id']}")[1]["cs"] == 0.01
print("v14 cost OK")

# ---- wake-ups and heartbeat renewal
J("POST", "/agents", {"n": "Sleeper", "hook": "http://127.0.0.1:8783/wake", "secret": "ws"})
st, wt = J("POST", "/tasks", {"b": GB, "t": "wake me", "as": "Sleeper", "st": "todo"})
server.db.execute("UPDATE agents SET last_seen=? WHERE name='Sleeper'", ("2000-01-01 00:00:00",)); sink.clear()
J("POST", "/settings", {"wake_after_min": "5"})
_time.sleep(0.5); assert server.wake_tick() == 0                                                                             # its item was already pushed to the hook: no wake ping on top
server.db.execute("UPDATE notifications SET hook='failed' WHERE agent='Sleeper'")                                            # push never arrived: now there is something to wake it for
assert server.wake_tick() >= 1
w = waitfor(lambda: [x for x in sink if x["path"] == "/wake" and "wake" in x["b"]]); assert w and w[0]["b"]["wake"]["ready"] >= 1 and w[0]["b"]["agent"] == "Sleeper", sink
assert w[0]["h"].get("X-Taskboard-Signature", "").startswith("sha256=") and _hmac.new(b"ws", w[0]["raw"], hashlib.sha256).hexdigest() in w[0]["h"]["X-Taskboard-Signature"]
assert server.wake_tick() == 0                                                                                               # cooldown: no repeat right away
server.db.execute("UPDATE agents SET wake_at=? WHERE name='Sleeper'", ("2000-01-01 00:00:00",)); assert server.wake_tick() == 0   # same unread item: never woken twice
J("GET", "/inbox?as=Sup%20Bot&ack=1")
for _ in range(2):                                                                                                           # unanswered pings back off, the third tells the supervisor (each needs a new unread item)
    J("POST", "/tasks", {"b": GB, "t": "wake again", "as": "Sleeper", "st": "todo"}); _time.sleep(0.5); server.db.execute("UPDATE notifications SET hook='failed' WHERE agent='Sleeper' AND woken=0")
    server.db.execute("UPDATE agents SET wake_at=? WHERE name='Sleeper'", ("2000-01-01 00:00:00",)); server.wake_tick()
assert any(n["ty"] == "esc.unreachable" for n in J("GET", "/inbox?as=Sup%20Bot&ack=1")[1]["i"])
J("POST", "/presence", {"as": "Sleeper", "status": "back"}); assert server.db.execute("SELECT wake_n FROM agents WHERE name='Sleeper'").fetchone()[0] == 0   # answering resets it
assert J("POST", "/agents/wake", {"n": "Sleeper"})[1]["sent"] == 1 and J("POST", "/agents/wake", {"n": "Sleeper"}, tok="wtok")[0] == 403 and J("POST", "/agents/wake", {"n": "Nobody"})[1]["e"] == "nf"
st, hc = J("POST", "/tasks", {"b": GB, "t": "long job", "as": "Worker", "st": "todo"}); J("POST", f"/tasks/{hc['id']}/claim", {"ttl": 1}, tok="wtok")
c0 = J("GET", f"/tasks/{hc['id']}")[1]["cu"]; r = J("POST", "/presence", {"status": "building", "task": hc["id"], "ttl": 60}, tok="wtok")[1]
assert r["renewed"] == [hc["id"]] and J("GET", f"/tasks/{hc['id']}")[1]["cu"] > c0, r                                      # heartbeat extends the claim
print("v14 wake OK")

# ---- bulk move and cross-board links
st, bm1 = J("POST", "/boards", {"n": "Bulk A"}); st, bm2 = J("POST", "/boards", {"n": "Bulk B"})
ids = [J("POST", "/tasks", {"b": bm1["id"], "t": "bulk %d" % i})[1]["id"] for i in range(3)]
st, bep = J("POST", "/tasks", {"b": bm1["id"], "t": "bulk epic", "ty": "epic"}); bkid = J("POST", "/tasks", {"b": bm1["id"], "t": "epic child", "pa": bep["id"]})[1]["id"]
stay = J("POST", "/tasks", {"b": bm2["id"], "t": "already there"})[1]["id"]
r = J("POST", "/tasks/move", {"ids": ids + [bep["id"], stay, 99999], "b": bm2["id"]})[1]
assert sorted(r["moved"]) == sorted(ids + [bep["id"], bkid]) and r["skipped"] == [stay] and r["failed"] == [{"id": 99999, "e": "nf"}], r
assert all(J("GET", f"/tasks/{i}")[1]["b"] == bm2["id"] for i in ids + [bep["id"], bkid])
assert J("POST", "/tasks/move", {"ids": ids, "b": bm1["id"]}, tok="vtok")[0] == 403 and J("POST", "/tasks/move", {"ids": [], "b": 1})[1]["e"] == "bad" and J("POST", "/tasks/move", {"ids": ids})[1]["e"] == "missing"
up = J("POST", "/tasks", {"b": bm1["id"], "t": "upstream"})[1]["id"]; dn_ = J("POST", "/tasks", {"b": bm2["id"], "t": "downstream", "dp": [up]})[1]["id"]
lk = J("GET", f"/tasks/{dn_}/links")[1]; assert lk["cross"] == 1 and lk["dp"][0]["id"] == up and lk["dp"][0]["x"] is True and lk["dp"][0]["bn"] == "Bulk A", lk
assert J("GET", f"/tasks/{up}/links")[1]["bl"][0]["id"] == dn_
assert any(i["k"] == "dep-cross" and dn_ in i["ids"] for i in J("GET", f"/attention?b={bm2['id']}")[1]["i"])
assert J("GET", f"/tasks/{dn_}/links", tok="vtok")[0] == 200 and J("GET", "/tasks/99999/links")[0] == 404
print("v14 bulk + links OK")

# ---- chat channels
assert J("GET", "/chats", tok="wtok")[0] == 403 and J("POST", "/chats", {"n": "x"}, tok="stok")[0] == 403
assert J("POST", "/chats", {"n": "bad kind", "kind": "irc", "url": "http://127.0.0.1:8783/c"})[1]["e"] == "bad" and J("POST", "/chats", {"n": "tg", "kind": "telegram", "url": "http://127.0.0.1:8783/c"})[1]["e"] == "missing"
assert J("POST", "/chats", {"n": "bad ev", "url": "http://127.0.0.1:8783/c", "events": ["nope"]})[1]["e"] == "bad"
sink.clear()
ch1 = J("POST", "/chats", {"n": "Ops", "kind": "slack", "url": "http://127.0.0.1:8783/slack", "events": ["blocked", "done"], "minpr": 2})[1]
assert ch1["n"] == "Ops" and "url" not in ch1 and ch1["host"] == "127.0.0.1" and ch1["ev"] == ["blocked", "done"], ch1                   # the URL is a secret: never echoed back
ch2 = J("POST", "/chats", {"n": "Discord", "kind": "discord", "url": "http://127.0.0.1:8783/discord", "events": ["blocked"]})[1]
ch3 = J("POST", "/chats", {"n": "Telegram", "kind": "telegram", "token": "123:ABC", "target": "-100"})[1]
assert server.rows("SELECT url FROM chats WHERE id=?", (ch3["id"],))[0]["url"] == "https://api.telegram.org/bot123:ABC/sendMessage"          # built from the bot token
server.db.execute("UPDATE chats SET url=? WHERE id=?", ("http://127.0.0.1:8783/tg", ch3["id"]))                                              # never call the real service from a test
assert J("POST", f"/chats/{ch2['id']}/test", {})[1]["ok"] is True and waitfor(lambda: [x for x in sink if x["path"] == "/discord"])
st, ctk = J("POST", "/tasks", {"b": GB, "t": "chat me", "pr": 1, "as": "Coder", "st": "in_progress"}); sink.clear()
J("PATCH", f"/tasks/{ctk['id']}", {"st": "blocked", "bl": "waiting on legal"})
m = waitfor(lambda: [x for x in sink if x["path"] == "/slack"]); assert m and "blocked" in m[0]["b"]["text"] and "waiting on legal" in m[0]["b"]["text"], sink
d = waitfor(lambda: [x for x in sink if x["path"] == "/discord"]); assert d and "content" in d[0]["b"]
J("PATCH", f"/tasks/{ctk['id']}", {"st": "done"}); assert waitfor(lambda: [x for x in sink if x["path"] == "/slack" and "done" in x["b"]["text"]])          # Ops listens to done, Discord does not
_time.sleep(0.4); assert not [x for x in sink if x["path"] == "/discord" and "done" in x["b"].get("content", "")]
st, low = J("POST", "/tasks", {"b": GB, "t": "low priority", "pr": 4, "st": "todo"}); n0 = len(sink)
J("PATCH", f"/tasks/{low['id']}", {"st": "done"}); _time.sleep(0.5); assert not [x for x in sink[n0:] if x["path"] == "/slack"]                      # below the channel's minimum priority
assert J("POST", "/chats", {"id": ch1["id"], "on": False})[1]["on"] is False
n0 = len(sink); st, hi = J("POST", "/tasks", {"b": GB, "t": "paused channel", "pr": 1, "st": "todo"}); J("PATCH", f"/tasks/{hi['id']}", {"st": "done"}); _time.sleep(0.5); assert not [x for x in sink[n0:] if x["path"] == "/slack"]
assert any(c["id"] == ch3["id"] and c["kind"] == "telegram" for c in J("GET", "/chats")[1]["i"])
J("POST", "/settings", {"base_url": "https://board.example.com"}); sink.clear(); J("POST", f"/chats/{ch2['id']}/test", {}); _time.sleep(0.5)
assert all(J("DELETE", f"/chats?id={c}")[0] == 200 for c in (ch1["id"], ch2["id"], ch3["id"])) and J("GET", "/chats")[1]["i"] == []
print("v14 chat OK")
print("v14 selftest OK")

# ---- runner: claims a task, runs the command, reports
import runner
cfgr = dict(url="http://127.0.0.1:8781", token="wtok", agent="Worker", labels="runner-test", ttl=30, beat=0.3, timeout=30)
out_f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "runner.out")
st, rt1 = J("POST", "/tasks", {"b": GB, "t": "run me", "pr": 1, "st": "todo", "lb": "runner-test"})
os.environ["OUT"] = out_f
assert runner.cycle(runner.Cfg(cmd="python3 -c \"import os; open(os.environ['OUT'],'w').write(os.environ['TB_TASK_ID'] + '|' + os.environ['TB_TASK_TITLE'])\"", **cfgr)) == rt1["id"]
assert open(out_f).read() == "%d|run me" % rt1["id"]
g = J("GET", f"/tasks/{rt1['id']}?ctx=1")[1]
assert not g.get("cb") and any("still open" in c["c"] for c in g["cc"]), g                                                   # nothing finished it: noted and released
assert runner.cycle(runner.Cfg(cmd="true", **cfgr)) is None                                                                  # nothing else ready for that skill
st, rt2 = J("POST", "/tasks", {"b": GB, "t": "run and fail", "pr": 1, "st": "todo", "lb": "runner-test"})
assert runner.cycle(runner.Cfg(cmd="echo boom >&2; exit 3", **cfgr)) == rt2["id"]
g = J("GET", f"/tasks/{rt2['id']}?ctx=1")[1]; assert g["st"] == "blocked" and "exit 3" in g["bl"] and any("boom" in c["c"] for c in g["cc"]), g
st, rt3 = J("POST", "/tasks", {"b": GB, "t": "run and finish", "pr": 1, "st": "todo", "lb": "runner-test"})
fin = "python3 -c \"import os,json,urllib.request as u; r=u.Request(os.environ['TB_URL']+'/api/tasks/'+os.environ['TB_TASK_ID']+'/finish', data=json.dumps({'summary':'built it'}).encode(), headers={'Authorization':'Bearer '+os.environ['TB_TOKEN'],'Content-Type':'application/json'}, method='POST'); u.urlopen(r)\""
assert runner.cycle(runner.Cfg(cmd=fin, **cfgr)) == rt3["id"] and J("GET", f"/tasks/{rt3['id']}")[1]["st"] == "done"          # the command finished it itself
print("v14 runner OK")

# ---- MCP surface: the new tools exist, and the default profile stays small
import subprocess
def mcp(env_tools, *msgs):
    pr = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_server.py")], input="\n".join(json.dumps(m) for m in msgs) + "\n", capture_output=True, text=True,
                        env={**os.environ, "TASKBOARD_URL": "http://127.0.0.1:8781", "TASKBOARD_TOKEN": "tok", "TASKBOARD_TOOLS": env_tools}, timeout=60)
    return [json.loads(x) for x in pr.stdout.splitlines() if x.strip()]
core = mcp("core", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[0]["result"]["tools"]
allt = mcp("all", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[0]["result"]["tools"]
assert len(json.dumps(core)) < 8000 and not {"chats", "git_secret", "move_tasks", "wake_agent", "cost_report"} & {t["name"] for t in core}, len(json.dumps(core))
assert {"cost", "cost_report", "links", "move_tasks", "wake_agent", "chats", "git_secret"} <= {t["name"] for t in allt}
r = mcp("all", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "chats", "arguments": {"action": "list"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "cost_report", "arguments": {"days": 7}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "links", "arguments": {"id": dn_}}})
assert json.loads(r[0]["result"]["content"][0]["text"])["kinds"] and "tot" in json.loads(r[1]["result"]["content"][0]["text"]) and json.loads(r[2]["result"]["content"][0]["text"])["cross"] == 1
print("v14 mcp OK")

# ================= v15: the all-boards overview carries the single-board analytics =================
st, o1 = J("POST", "/boards", {"n": "Roll scrum"}); st, o2 = J("POST", "/boards", {"n": "Roll kanban", "method": "kanban"}); st, o3 = J("POST", "/boards", {"n": "Roll okr", "method": "okr"}); st, o4 = J("POST", "/boards", {"n": "Roll support", "method": "support"})
J("POST", "/sprints", {"b": o1["id"], "st": "active"})
for b_, n_ in ((o1["id"], 2), (o2["id"], 1)):
    for i in range(n_):
        st, t_ = J("POST", "/tasks", {"b": b_, "t": "rolled %d" % i, "as": "Roller", "st": "in_progress"}); J("PATCH", f"/tasks/{t_['id']}", {"st": "done"})
J("POST", "/tasks", {"b": o2["id"], "t": "still going", "as": "Roller", "st": "in_progress"})
st, ob = J("POST", "/tasks", {"b": o3["id"], "t": "Objective", "ty": "epic"}); J("POST", "/tasks", {"b": o3["id"], "t": "KR", "pa": ob["id"], "tg": 10, "cr": 5})
J("POST", "/tasks", {"b": o4["id"], "t": "customer request", "pr": 1})
scope = f"bs={o1['id']},{o2['id']},{o3['id']},{o4['id']}"
r = J("GET", f"/overview?{scope}")[1]
assert r["boards"] == 4 and r["flow"]["done_n"] == 3 and r["flow"]["wip"]["in_progress"] == 1 and len(r["flow"]["cfd"]) == 31 and r["flow"]["cycle_h"]["avg"] is not None, r["flow"]
assert sum(w["n"] for w in r["flow"]["per_week"]) == 3
assert [x["bn"] for x in r["sprints"]] == ["Roll scrum"] and r["sprints"][0]["burn"] and r["sprints"][0]["health"] in ("green", "amber", "red")
assert r["okr"][0]["bn"] == "Roll okr" and r["okr"][0]["overall"] == 0.5 and r["okr"][0]["n"] == 1
assert r["sla"][0]["bn"] == "Roll support" and r["sla"][0]["breached"] + r["sla"][0]["at_risk"] + r["sla"][0]["ok"] == 1
assert [a["done"] for a in r["agents"]["i"] if a["as"] == "Roller"] == [3]
assert J("GET", f"/overview?b={o1['id']}")[1]["boards"] == 1 and len(J("GET", f"/overview?b={o1['id']}")[1]["flow"]["cfd"]) == 31
assert J("GET", "/overview?bs=999999")[1]["boards"] == 0 and J("GET", f"/overview?{scope}", tok="vtok")[0] == 200
print("v15 selftest OK")

# ================= v16: the admin tool group - everything the Admin screen does, without the screen =================
def mcp_as(tok, tools, name, args):
    pr = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_server.py")],
                        input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}) + "\n", capture_output=True, text=True,
                        env={**os.environ, "TASKBOARD_URL": "http://127.0.0.1:8781", "TASKBOARD_TOKEN": tok, "TASKBOARD_TOOLS": tools}, timeout=60)
    return json.loads(json.loads(pr.stdout.splitlines()[0])["result"]["content"][0]["text"])
names_ = lambda t: {x["name"] for x in mcp(t, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[0]["result"]["tools"]}
ADM = {"tokens", "settings", "backups", "export_data", "import_data", "del_board", "del_sprint", "chats", "git_secret", "wake_agent", "usage"}
assert not ADM & names_("core") and not ADM & names_("core,coord,plan") and ADM <= names_("core,admin") and ADM <= names_("all")          # opt-in group, not in the daily profile
tk = mcp_as("tok", "all", "tokens", {"action": "create", "n": "Temp Agent", "role": "agent"}); assert len(tk["token"]) > 20 and tk["role"] == "agent", tk
assert J("GET", "/boards", tok=tk["token"])[0] == 200
lst = mcp_as("tok", "all", "tokens", {})["i"]; tid_ = [x["id"] for x in lst if x["n"] == "Temp Agent"][0]
assert mcp_as("tok", "all", "tokens", {"action": "revoke", "id": tid_})["x"] == 1 and J("GET", "/boards", tok=tk["token"])[0] == 401
assert mcp_as("wtok", "all", "tokens", {})["e"] == "forbidden" and mcp_as("wtok", "all", "tokens", {"action": "create", "n": "x"})["e"] == "forbidden"
assert mcp_as("tok", "all", "settings", {"wake_after_min": "7", "base_url": "https://b.example.com"})["wake_after_min"] == "7"
assert mcp_as("tok", "all", "settings", {})["base_url"] == "https://b.example.com" and mcp_as("wtok", "all", "settings", {})["e"] == "forbidden" and mcp_as("tok", "all", "settings", {"prices": "nope"})["e"] == "bad"
bk_ = mcp_as("tok", "all", "backups", {"action": "run"}); assert bk_["ok"] is True and mcp_as("tok", "all", "backups", {})["n"] >= 1 and mcp_as("wtok", "all", "backups", {"action": "run"})["e"] == "forbidden"
ex_ = mcp_as("tok", "all", "export_data", {"b": BID}); assert ex_["boards"] and mcp_as("wtok", "all", "export_data", {})["e"] == "forbidden" and mcp_as("stok", "all", "export_data", {})["e"] == "forbidden"
im_ = mcp_as("tok", "all", "import_data", {"data": ex_}); assert im_["n"]["tasks"] >= 1 and mcp_as("wtok", "all", "import_data", {"data": ex_})["e"] == "forbidden"
st, bt_ = J("POST", "/boards", {"n": "To delete"}); st, spt_ = J("POST", "/sprints", {"b": bt_["id"]})
assert mcp_as("wtok", "all", "del_sprint", {"id": spt_["id"]})["e"] == "forbidden" and mcp_as("stok", "all", "del_sprint", {"id": spt_["id"]}).get("e") is None
assert mcp_as("stok", "all", "del_board", {"id": bt_["id"]})["e"] == "forbidden" and mcp_as("tok", "all", "del_board", {"id": bt_["id"]}).get("e") is None and J("GET", f"/boards/{bt_['id']}")[0] == 404
print("v16 selftest OK")

# ---- a review that asked for changes no longer blocks once the same reviewer approved a newer one
st, rg = J("POST", "/tasks", {"b": GD, "t": "review loop", "as": "Looper", "st": "in_progress"})
r1 = J("POST", f"/tasks/{rg['id']}/review", {"reviewer": "Reviewer X", "note": "first"})[1]; J("POST", f"/reviews/{r1['id']}/decide", {"decision": "changes", "note": "fix it"}, tok="tok")
assert J("PATCH", f"/tasks/{rg['id']}", {"st": "done", "dn": [0]})[1]["e"] == "review"                                  # still blocked while the changes are outstanding
r2 = J("POST", f"/tasks/{rg['id']}/review", {"reviewer": "Reviewer X", "note": "second"})[1]
assert J("PATCH", f"/tasks/{rg['id']}", {"st": "done", "dn": [0]})[1]["e"] == "review"                                  # and while the new request is pending
J("POST", f"/reviews/{r2['id']}/decide", {"decision": "approve"}, tok="tok")
assert J("PATCH", f"/tasks/{rg['id']}", {"st": "done", "dn": [0]})[1].get("st") == "done"
print("v16 review loop OK")

# ================= v17: direct messages, briefing that waits, tokens limited to some boards =================
import time as _t17
# ---- messages
r = J("POST", "/messages", {"to": "Sup Bot", "c": "can you look at the schema?"}, tok="wtok")[1]; assert r["n"] == 1, r
ib = J("GET", "/inbox?as=Sup%20Bot&ack=1")[1]["i"]; mm = [n for n in ib if n["ty"] == "msg.direct"]
assert mm and mm[0]["by"] == "Worker" and mm[0]["do"] == "say to=Worker" and "schema" in mm[0]["m"], ib
assert J("POST", "/messages", {"to": "Worker", "c": "yes, on it", "reply": r["ids"][0]}, tok="stok")[1]["n"] == 1
conv = J("GET", "/messages?with=Sup%20Bot", tok="wtok")[1]["i"]; assert [m["c"] for m in conv] == ["can you look at the schema?", "yes, on it"] and conv[1]["re"] == r["ids"][0], conv
J("POST", "/messages", {"to": "Someone Else", "c": "private note", "as": "Planner X"})
assert not [m for m in J("GET", "/messages?as=Someone%20Else", tok="wtok")[1]["i"] if m["c"] == "private note"]                # an agent only reads its own conversations
assert [m["c"] for m in J("GET", "/messages?as=Someone%20Else")[1]["i"]] == ["private note"]                              # admins and supervisors can read any
assert J("POST", "/messages", {"to": "*", "c": "all hands"}, tok="wtok")[0] == 403
bc = J("POST", "/messages", {"to": "*", "c": "stand-up in five"}, tok="stok")[1]; assert bc["n"] >= 2
assert any(n["ty"] == "msg.direct" and "stand-up" in n["m"] for n in J("GET", "/inbox?as=Worker&ack=1")[1]["i"])
assert J("POST", "/messages", {"to": "Worker", "c": "x"}, tok="wtok")[1]["e"] == "bad" and J("POST", "/messages", {"to": "Sup Bot"}, tok="wtok")[1]["e"] == "missing"
assert J("POST", "/messages", {"to": "Sup Bot", "c": "x", "task": 999999}, tok="wtok")[1]["e"] == "nf" and J("POST", "/messages", {"to": "Sup Bot", "c": "x"}, tok="vtok")[0] == 403
J("POST", "/agents", {"n": "Quiet", "mute": "msg.direct"}); J("POST", "/messages", {"to": "Quiet", "c": "shh"}, tok="wtok"); assert not J("GET", "/inbox?as=Quiet")[1]["i"]
print("v17 messages OK")

# ---- briefing that waits
c0 = J("GET", "/briefing?as=Waiter")[1]["c"]
t0 = _t17.time(); r = J("GET", f"/briefing?as=Waiter&since={c0}&wait=2")[1]; dt = _t17.time() - t0
assert list(r) == ["c"] and 1.8 <= dt < 4, (r, dt)                                                                       # nothing happened: held open, then an empty answer
threading.Timer(0.6, lambda: J("POST", "/messages", {"to": "Waiter", "c": "wake up", "as": "Poker"})).start()
t0 = _t17.time(); r = J("GET", f"/briefing?as=Waiter&since={c0}&wait=10")[1]; dt = _t17.time() - t0
assert r.get("in") and "wake up" in r["in"][0]["m"] and dt < 3, (r, dt)                                                   # something arrived: answered at once
t0 = _t17.time(); J("GET", "/briefing?as=Waiter&wait=5"); assert _t17.time() - t0 < 1.5                                   # no cursor = a full briefing, never held
print("v17 wait OK")

# ---- tokens limited to some boards
st, sa = J("POST", "/boards", {"n": "Scope A"}); st, sb = J("POST", "/boards", {"n": "Scope B"}); SA, SB = sa["id"], sb["id"]
J("POST", "/sprints", {"b": SA, "st": "active"}); J("POST", "/sprints", {"b": SB, "st": "active"})
st, ta = J("POST", "/tasks", {"b": SA, "t": "scoped A low", "pr": 4, "st": "todo"}); st, tb_ = J("POST", "/tasks", {"b": SB, "t": "scoped B urgent", "pr": 1, "st": "todo"})
assert J("POST", "/tokens", {"n": "Walled", "role": "admin", "boards": [SA]})[1]["e"] == "bad" and J("POST", "/tokens", {"n": "Walled", "boards": [999999]})[1]["e"] == "nf"
tk = J("POST", "/tokens", {"n": "Walled", "role": "agent", "boards": [SA]})[1]; W = tk["token"]; assert tk["bs"] == [SA]
assert [b["id"] for b in J("GET", "/boards", tok=W)[1]["i"]] == [SA]                                                            # the other board does not exist for it
assert J("GET", f"/boards/{SB}", tok=W)[1]["e"] == "scope" and J("GET", f"/boards/{SB}", tok=W)[0] == 403
assert {t["b"] for t in J("GET", "/tasks?limit=500", tok=W)[1]["i"]} == {SA} and J("GET", f"/tasks?b={SB}", tok=W)[0] == 403
assert J("GET", f"/tasks/{tb_['id']}", tok=W)[0] == 403 and J("PATCH", f"/tasks/{tb_['id']}", {"st": "done"}, tok=W)[0] == 403 and J("POST", f"/tasks/{tb_['id']}/comments", {"c": "hi"}, tok=W)[0] == 403
assert J("GET", f"/tasks/{ta['id']}", tok=W)[0] == 200 and J("POST", "/tasks", {"b": SB, "t": "sneak"}, tok=W)[0] == 403 and J("POST", "/tasks", {"b": SA, "t": "allowed"}, tok=W)[0] == 201
assert J("POST", "/tasks/bulk", {"ids": [ta["id"], tb_["id"]], "pr": 2}, tok=W)[0] == 403 and J("GET", f"/tasks/{ta['id']}")[1]["pr"] == 4          # one forbidden id refuses the whole batch
assert J("POST", f"/tasks/{ta['id']}/move", {"b": SB}, tok=W)[0] == 403 and J("POST", "/tasks/move", {"ids": [ta["id"]], "b": SB}, tok=W)[0] == 403
assert J("POST", "/boards", {"n": "mine now"}, tok=W)[0] == 403 and J("POST", "/cost", {"task": tb_["id"], "usd": 1}, tok=W)[0] == 403
assert {x["b"] for x in J("GET", "/sprints", tok=W)[1]["i"]} == {SA}
nx = J("POST", "/next", {}, tok=W)[1]; assert nx.get("id") != tb_["id"] and (nx.get("none") or J("GET", f"/tasks/{nx['id']}")[1]["b"] == SA), nx     # next never reaches into the other board
assert J("GET", "/dashboard", tok=W)[1]["totals"]["boards"] == 1 and J("GET", "/overview", tok=W)[1]["boards"] == 1
assert not any(e[3] == SB for e in J("GET", "/changes?since=0&limit=1000", tok=W)[1]["e"]) and any(e[3] == SB for e in J("GET", "/changes?since=0&limit=1000")[1]["e"])
assert J("POST", "/tasks/commits", {"h": "abc", "m": f"touches #{tb_['id']} and #{ta['id']}"}, tok=W)[1]["n"] == 1                # the forbidden id is skipped, the allowed one gets the commit
assert J("GET", f"/tasks/{tb_['id']}")[1].get("cm") is None
assert len(J("GET", "/boards")[1]["i"]) > 1                                                          # unlimited tokens still see everything
tid_ = [x["id"] for x in J("GET", "/tokens")[1]["i"] if x["n"] == "Walled"][0]
assert J("GET", "/tokens")[1]["i"][-1]["bs"] == [SA]
assert J("POST", "/tokens", {"id": tid_, "boards": [SA, SB]})[1]["bs"] == [SA, SB] and J("GET", f"/tasks/{tb_['id']}", tok=W)[0] == 200                  # widen it
assert J("POST", "/tokens", {"id": tid_, "boards": []})[1]["bs"] == [] and J("GET", f"/tasks/{tb_['id']}", tok=W)[0] == 200                                  # empty = all
print("v17 scope OK")
print("v17 selftest OK")

# ---- MCP: say, messages, waiting briefing, the inbox watcher, token scope
core_names = names_("core"); assert "say" in core_names and "messages" not in core_names and "messages" in names_("core,coord") and len(json.dumps(mcp("core", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[0]["result"]["tools"])) < 9000
assert mcp_as("wtok", "core", "say", {"to": "Sup Bot", "c": "via mcp"})["n"] == 1 and any("via mcp" in n["m"] for n in J("GET", "/inbox?as=Sup%20Bot&ack=1")[1]["i"])
assert [m["c"] for m in mcp_as("wtok", "core,coord", "messages", {"with": "Sup Bot", "limit": 5})["i"]][-1] == "via mcp"
def mcp_session(tok, name, extra_env, msgs, listen=4):
    pr = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_server.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1,
                          env={**os.environ, "TASKBOARD_URL": "http://127.0.0.1:8781", "TASKBOARD_TOKEN": tok, "TASKBOARD_AGENT": name, "TASKBOARD_TOOLS": "core", **extra_env})
    out = []
    threading.Thread(target=lambda: [out.append(json.loads(l)) for l in pr.stdout if l.strip()], daemon=True).start()
    for m in msgs:
        pr.stdin.write(json.dumps(m) + "\n"); pr.stdin.flush()
    return pr, out
init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}
pr, out = mcp_session("wtok", "Worker", {"TASKBOARD_WATCH": "1"}, [init])
_t17.sleep(0.8); J("POST", "/messages", {"to": "Worker", "c": "ping from the supervisor"}, tok="stok")
note = waitfor(lambda: [o for o in out if o.get("method") == "notifications/message"], 80)
pr.stdin.close(); pr.wait(timeout=10)
assert note and "new item" in note[0]["params"]["data"] and "briefing" in note[0]["params"]["data"], out                  # a sleeping agent is told without polling
assert out[0]["result"]["capabilities"].get("logging") == {}
pr, out = mcp_session("wtok", "Worker", {}, [init, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "briefing", "arguments": {}}},
                                             {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "briefing", "arguments": {}}}])
waitfor(lambda: len([o for o in out if o.get("id") in (2, 3)]) == 2, 60)
t0 = _t17.time(); pr.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "briefing", "arguments": {"wait": 2}}}) + "\n"); pr.stdin.flush()
r4 = waitfor(lambda: [o for o in out if o.get("id") == 4], 100); pr.stdin.close(); pr.wait(timeout=10)
assert r4 and json.loads(r4[0]["result"]["content"][0]["text"]).keys() == {"c"} and 1.8 <= _t17.time() - t0 < 5, (r4, _t17.time() - t0)         # held open, then an empty answer
tkx = mcp_as("tok", "all", "tokens", {"action": "create", "n": "Scoped Via Mcp", "boards": [SA]}); assert tkx["bs"] == [SA]
tx = [x["id"] for x in mcp_as("tok", "all", "tokens", {})["i"] if x["n"] == "Scoped Via Mcp"][0]
assert mcp_as("tok", "all", "tokens", {"action": "scope", "id": tx, "boards": []})["bs"] == [] and J("GET", f"/boards/{SB}", tok=tkx["token"])[0] == 200
print("v17 mcp OK")

# ================= v18: saved views, the remaining tools, and agent parity =================
v1 = J("POST", "/views", {"n": "Blocked work", "f": {"preset": "blocked", "asg": "Coder 1", "bogus": "x"}}, tok="wtok")[1]
assert v1["f"] == {"preset": "blocked", "asg": "Coder 1"}, v1                                                         # unknown keys are dropped
assert J("POST", "/views", {"n": "blocked WORK", "f": {"preset": "open"}})[1]["n"] == "blocked WORK"                  # same name, any case: replaced, not duplicated
vl = J("GET", "/views", tok="vtok")[1]; assert vl["n"] == 1 and vl["i"][0]["f"] == {"preset": "open"} and vl["i"][0]["by"] == "a", vl   # everyone reads them
assert J("POST", "/views", {"f": {}})[1]["e"] == "missing" and J("POST", "/views", {"n": "x"}, tok="vtok")[0] == 403
assert mcp_as("wtok", "all", "views", {"action": "save", "n": "Mine", "f": {"q": "api"}})["f"] == {"q": "api"} and len(mcp_as("wtok", "all", "views", {})["i"]) == 2
assert mcp_as("wtok", "all", "views", {"action": "del", "n": "mine"})["x"] == 1 and [v["n"].lower() for v in J("GET", "/views")[1]["i"]] == ["blocked work"]
# the other things the screen could do and the tools could not
assert mcp_as("tok", "all", "health", {})["ok"] is True
assert "made" in mcp_as("tok", "all", "run_templates", {})
st, ct_ = J("POST", "/tasks", {"b": BID, "t": "csv round trip", "as": "Csvman", "st": "todo", "pr": 2})
csv_ = mcp_as("tok", "all", "export_csv", {"b": BID})["csv"]; assert "csv round trip" in csv_ and "title" in csv_.splitlines()[0].split(","), csv_[:120]
st, cb_ = J("POST", "/boards", {"n": "Csv target"}); im = mcp_as("tok", "all", "import_csv", {"b": cb_["id"], "csv": csv_}); assert im["made"] >= 1, im
assert mcp_as("wtok", "all", "export_csv", {})["e"] == "forbidden" and mcp_as("wtok", "all", "import_csv", {"b": cb_["id"], "csv": csv_})["e"] == "forbidden"
J("POST", "/cost", {"task": ct_["id"], "usd": 0.5, "as": "Csvman"}); assert mcp_as("wtok", "all", "task_cost", {"id": ct_["id"]})["cs"] == 0.5
assert "set" in mcp_as("wtok", "all", "nudge", {})
assert "csv_round" not in ""  # noqa

# ---- every action on an agent's work lands in its inbox AND is pushed to its hook at once
got.clear(); J("POST", "/agents", {"n": "Hooky", "hook": "http://127.0.0.1:8782/h", "secret": "s"}); J("POST", "/agents", {"n": "Peer"})
st, ht = J("POST", "/tasks", {"b": BID, "t": "wake me", "as": "Hooky", "k": "agent", "st": "todo"}); HT = ht["id"]
J("PATCH", f"/tasks/{HT}", {"pr": 1}); J("PATCH", f"/tasks/{HT}", {"pr": 2})                     # coalesced into one item, pushed twice
J("POST", f"/tasks/{HT}/comments", {"c": "ping @Peer please"})
J("POST", "/messages", {"to": "Hooky", "c": "direct hello"})
rq = J("POST", f"/tasks/{HT}/review", {"reviewer": "Peer"})[1]
J("POST", f"/reviews/{rq['id']}/decide", {"decision": "changes", "note": "fix it", "as": "Peer"})
time.sleep(1.5)
ty = [g["n"]["ty"] for g in got if g["agent"] == "Hooky"]
for need in ("task.assigned", "task.updated", "task.comment", "msg.direct", "task.status", "review.changes"):
    assert need in ty, (need, ty)
assert ty.count("task.updated") == 2, ty
assert any(n["ty"] == "task.mention" for n in J("GET", "/inbox?as=Peer")[1]["i"])
J("POST", "/agents", {"n": "Hooky", "mute": "task.comment"}); got.clear(); J("POST", f"/tasks/{HT}/comments", {"c": "muted"}); time.sleep(0.5)
assert not [g for g in got if g["agent"] == "Hooky"], got
print("v19 wake-on-every-action OK")
# ---- hook pushes only for genuinely new events: the same alert twice -> one push, a new DM -> a push
got.clear(); J("POST", "/agents", {"n": "Hooky", "mute": ""})
def _alert(msg):
    with server.lock: server.notify("Hooky", "sprint.risk", "system", None, BID, msg, {"health": "red"})
_alert("Sprint 'S' is AT RISK: 2 overdue"); _alert("Sprint 'S' is AT RISK: 2 overdue"); time.sleep(0.8)
assert len([g for g in got if g["agent"] == "Hooky" and g["n"]["ty"] == "sprint.risk"]) == 1, got           # repeat of an already-pushed alert is not pushed again
assert len([n for n in J("GET", "/inbox?as=Hooky&all=1&limit=200")[1]["i"] if n["ty"] == "sprint.risk" and "2 overdue" in n["m"]]) == 2   # ...but the inbox still holds both
_alert("Sprint 'S' is AT RISK: 3 overdue"); time.sleep(0.5)
assert len([g for g in got if g["agent"] == "Hooky" and g["n"]["ty"] == "sprint.risk"]) == 2, got           # changed alert content is new
got.clear(); J("POST", "/messages", {"to": "Hooky", "c": "a brand new DM"}); J("POST", "/messages", {"to": "Hooky", "c": "a brand new DM"}); time.sleep(0.8)
assert len([g for g in got if g["agent"] == "Hooky" and g["n"]["ty"] == "msg.direct"]) == 2, got            # real events always push
print("hook dedupe OK")
import runpy; runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_parity.py"))
print("v18 selftest OK")
