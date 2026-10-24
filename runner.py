#!/usr/bin/env python3
"""Taskboard runner: keeps one agent working without anyone watching the screen.

It claims the next ready task for the agent, runs your command for it, keeps the claim and the agent's presence alive while the
command runs, and reports what happened on the task. It can also listen for the board's wake-up ping so an idle agent starts at once.

    TASKBOARD_URL=http://localhost:8790 TASKBOARD_TOKEN=... TASKBOARD_AGENT=coder-agent \\
    TB_RUN_CMD='my-agent-cli --task "$TB_TASK_ID" --title "$TB_TASK_TITLE"' python3 runner.py

The command gets TB_TASK_ID, TB_TASK_TITLE, TB_TASK_JSON (the same brief `next` returns), TB_URL, TB_TOKEN and TB_AGENT.
Exit 0 and the task still open: the runner notes it and lets go of the claim. Non-zero exit or timeout: the task is set to blocked with the reason.

Settings (environment): TB_RUN_CMD (required), TB_POLL=20 seconds between checks, TB_TTL=30 claim minutes, TB_BEAT=60 heartbeat seconds,
TB_TIMEOUT=0 (no limit) seconds per task, TB_LABELS= skills to match (csv), TB_BOARD= only this board id, TB_ONCE=1 run one task and stop,
TB_LISTEN=8799 port for the wake-up ping, TB_PUBLIC_URL= address the board can reach that port at (registers the webhook), TB_WAKE_SECRET= signing secret.
"""
import hashlib, hmac, http.server, json, os, subprocess, sys, threading, time, urllib.error, urllib.request


class Cfg:
    def __init__(self, **kw):
        e = os.environ.get
        self.url = kw.get("url") or e("TASKBOARD_URL", "http://localhost:8790")
        self.token = kw.get("token") or e("TASKBOARD_TOKEN", "")
        self.agent = kw.get("agent") or e("TASKBOARD_AGENT", "")
        self.cmd = kw.get("cmd") or e("TB_RUN_CMD", "")
        self.poll = float(kw.get("poll") or e("TB_POLL", "20"))
        self.ttl = int(kw.get("ttl") or e("TB_TTL", "30"))
        self.beat = float(kw.get("beat") or e("TB_BEAT", "60"))
        self.timeout = float(kw.get("timeout") or e("TB_TIMEOUT", "0"))
        self.labels = kw.get("labels") or e("TB_LABELS", "")
        self.board = kw.get("board") or e("TB_BOARD", "")
        self.once = bool(kw.get("once") or e("TB_ONCE"))
        self.listen = int(kw.get("listen") or e("TB_LISTEN", "0"))
        self.public = kw.get("public") or e("TB_PUBLIC_URL", "")
        self.secret = kw.get("secret") or e("TB_WAKE_SECRET", "")


def api(cfg, method, path, body=None):
    req = urllib.request.Request(cfg.url.rstrip("/") + "/api" + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + cfg.token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"e": str(e.code)}
    except Exception as e:
        return {"e": "net", "m": str(e)[:100]}


def cycle(cfg):
    """One pass: claim the next ready task and run the command for it. Returns the task id, or None when nothing was ready."""
    body = {"as": cfg.agent, "ttl": cfg.ttl}
    if cfg.labels:
        body["lb"] = cfg.labels
    if cfg.board:
        body["b"] = int(cfg.board)
    res = api(cfg, "POST", "/next", body)
    if "id" not in res:
        return None
    tid, task = res["id"], res.get("task") or {}
    title = res.get("t") or task.get("t") or ""
    env = {**os.environ, "TB_TASK_ID": str(tid), "TB_TASK_TITLE": title, "TB_TASK_JSON": json.dumps(task), "TB_URL": cfg.url, "TB_TOKEN": cfg.token, "TB_AGENT": cfg.agent}
    stop = threading.Event()

    def beat():
        while not stop.wait(cfg.beat):
            api(cfg, "POST", "/presence", {"as": cfg.agent, "status": ("running: " + title)[:150], "task": tid, "ttl": cfg.ttl})
    api(cfg, "POST", "/presence", {"as": cfg.agent, "status": ("starting: " + title)[:150], "task": tid, "ttl": cfg.ttl})
    threading.Thread(target=beat, daemon=True).start()
    try:
        p = subprocess.run(cfg.cmd, shell=True, env=env, timeout=cfg.timeout or None, capture_output=True, text=True)
        rc, tail = p.returncode, (p.stdout + p.stderr)[-500:].strip()
    except subprocess.TimeoutExpired:
        rc, tail = 124, "timed out after %gs" % cfg.timeout
    finally:
        stop.set()
    now = api(cfg, "GET", "/tasks/%d?brief=1" % tid)
    if rc != 0:
        api(cfg, "POST", "/tasks/%d/comments" % tid, {"c": "Runner: the command failed (exit %d). %s" % (rc, tail)[:900], "as": cfg.agent})
        api(cfg, "PATCH", "/tasks/%d" % tid, {"st": "blocked", "bl": "runner command failed (exit %d)" % rc})
    elif now.get("st") in ("todo", "in_progress"):
        api(cfg, "POST", "/tasks/%d/comments" % tid, {"c": "Runner: the command finished (exit 0) but the task is still open - nobody called finish. Releasing it.", "as": cfg.agent})
        api(cfg, "POST", "/tasks/%d/release" % tid, {"as": cfg.agent})
    return tid


def listen(cfg, wake):
    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if cfg.secret and not hmac.compare_digest(self.headers.get("X-Taskboard-Signature", ""), "sha256=" + hmac.new(cfg.secret.encode(), raw, hashlib.sha256).hexdigest()):
                self.send_response(401); self.end_headers(); return
            wake.set()
            self.send_response(200); self.end_headers()

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", cfg.listen), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    cfg = Cfg()
    if not (cfg.token and cfg.agent and cfg.cmd):
        sys.exit("set TASKBOARD_TOKEN, TASKBOARD_AGENT and TB_RUN_CMD (see the top of this file)")
    wake = threading.Event()
    if cfg.listen:
        listen(cfg, wake)
        if cfg.public:
            api(cfg, "POST", "/agents", {"n": cfg.agent, "hook": cfg.public, "secret": cfg.secret})
    print("runner for %s on %s" % (cfg.agent, cfg.url), flush=True)
    while True:
        tid = cycle(cfg)
        if tid is not None:
            print("task #%s handled" % tid, flush=True)
            if cfg.once:
                return
            continue
        if cfg.once:
            return
        wake.wait(cfg.poll)
        wake.clear()


if __name__ == "__main__":
    main()
