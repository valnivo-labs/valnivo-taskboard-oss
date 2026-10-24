#!/usr/bin/env python3
"""Security behaviour of the server (separate process settings from selftest.py). Usage: python3 test_security.py"""
import json, os, sys, threading, urllib.request, urllib.error, shutil, tempfile
d = tempfile.mkdtemp()
os.environ.update(TASKBOARD_DB=os.path.join(d, "t.db"), TASKBOARD_TOKENS="", PORT="8793", TASKBOARD_MAX_BODY="1000",
                  TASKBOARD_AUTH_FAILS_PER_MIN="3", TASKBOARD_RATE_PER_MIN="0")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server
srv = server.S(("127.0.0.1", 8793), server.H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
U = "http://127.0.0.1:8793"


def call(m, p, b=None, tok=None, raw=None, hdr=None):
    h = {**({"Authorization": "Bearer " + tok} if tok else {}), **(hdr or {})}
    data = raw if raw is not None else (json.dumps(b).encode() if b is not None else None)
    try:
        with urllib.request.urlopen(urllib.request.Request(U + p, method=m, data=data, headers=h)) as r:
            return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


# 1. never open: the first admin token is created on first start and the API is closed without it
assert server.auth_open()
tok = server.bootstrap_admin(quiet=True)
assert not server.auth_open() and len(tok) > 20
assert call("GET", "/api/boards")[0] == 401
st, h, _ = call("GET", "/api/boards", tok=tok); assert st == 200
# 2. security headers on API and static
for p in ("/api/health", "/"):
    h = call("GET", p)[1]
    assert h["X-Content-Type-Options"] == "nosniff" and h["X-Frame-Options"] == "DENY" and "default-src 'self'" in h["Content-Security-Policy"], (p, h)
# 3. a token in the URL is only accepted for the event stream (EventSource cannot set headers)
assert call("GET", "/api/boards?token=" + tok)[0] == 401
assert call("HEAD", "/api/events?token=" + tok)[0] == 200
# 4. webhook targets: no private/loopback/metadata addresses, no odd schemes
assert call("POST", "/api/agents", {"n": "A", "hook": "http://127.0.0.1:9/h"}, tok)[2].find("private_url") > 0
assert call("POST", "/api/agents", {"n": "A", "hook": "http://169.254.169.254/latest"}, tok)[2].find("private_url") > 0
assert call("POST", "/api/agents", {"n": "A", "hook": "http://10.0.0.5/x"}, tok)[2].find("private_url") > 0
assert json.loads(call("POST", "/api/agents", {"n": "A", "hook": "file:///etc/passwd"}, tok)[2])["e"] == "bad"
assert json.loads(call("POST", "/api/settings", {"digest_url": "http://localhost/x"}, tok)[2])["e"] == "private_url"
assert call("POST", "/api/agents", {"n": "A"}, tok)[0] == 200                                   # no hook is fine
# 4b. chat channels use the same guard; the forge webhook is closed until a secret exists and rejects bad signatures
assert call("POST", "/api/chats", {"n": "c", "kind": "slack", "url": "http://127.0.0.1:9/h"}, tok)[2].find("private_url") > 0
assert call("POST", "/api/chats", {"n": "c", "kind": "slack", "url": "http://169.254.169.254/x"}, tok)[2].find("private_url") > 0
assert call("POST", "/api/settings", {"base_url": "http://192.168.1.5:8790"}, tok)[0] == 200                      # a link for humans, never requested
assert call("POST", "/api/hooks/git", {"commits": []})[0] == 404
sec = json.loads(call("POST", "/api/admin/git-secret", {}, tok)[2])["secret"]
assert call("POST", "/api/hooks/git", {"commits": []}, hdr={"X-Hub-Signature-256": "sha256=" + "0" * 64, "X-GitHub-Event": "push"})[0] == 401
assert call("POST", "/api/hooks/git", {"commits": []}, hdr={"X-Gitlab-Token": "wrong", "X-Gitlab-Event": "Push Hook"})[0] == 401
server._rl.clear()
# 4c. the connector accepts a key in the address; no other endpoint does (it would end up in logs)
assert call("POST", "/api/mcp?key=" + tok, {"jsonrpc": "2.0", "id": 1, "method": "ping"})[0] == 200
assert call("GET", "/api/boards?key=" + tok)[0] == 401 and call("POST", "/api/mcp?key=wrong", {"jsonrpc": "2.0", "id": 1, "method": "ping"})[0] == 401
server._rl.clear()
# 5. request size cap
assert call("POST", "/api/boards", raw=b'{"n":"' + b"x" * 5000 + b'"}', tok=tok)[0] == 413
server._rl.clear()
# 6. brute force: after 3 bad tokens the client is throttled, even with a valid token
for _ in range(3):
    assert call("GET", "/api/boards", tok="wrong")[0] == 401
assert call("GET", "/api/boards", tok="wrong")[0] == 429
assert call("GET", "/api/boards", tok=tok)[0] == 429
# 7. limiter unit behaviour
assert [server.rate_hit("k", 2) for _ in range(3)] == [False, False, True]
shutil.rmtree(d, ignore_errors=True)
print("security tests OK")
