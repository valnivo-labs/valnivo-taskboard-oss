#!/usr/bin/env python3
"""Valnivo Taskboard (open source edition): stdlib REST + SSE + static UI, SQLite persistence.

API is compact by default (short keys, empties omitted, no description in lists).
Add ?full=1 (or header X-TB-Full:1) for verbose column-named JSON (used by the web UI).
"""
import calendar, collections, csv, http.client, hashlib, hmac, io, ipaddress, json, os, re, secrets, socket, sqlite3, sys, threading, queue, time, urllib.error, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("TASKBOARD_DB", os.path.join(HERE, "data", "taskboard.db"))
PORT = int(os.environ.get("PORT", "8790"))
HOST = os.environ.get("HOST", "127.0.0.1")   # the Docker image sets 0.0.0.0
NUDGE_HOURS = float(os.environ.get("TASKBOARD_NUDGE_HOURS", "24"))  # stale open → set nu
NUDGE_OPEN = frozenset({"backlog", "todo", "in_progress", "review", "blocked"})
NUDGE_INTERVAL_SEC = int(os.environ.get("TASKBOARD_NUDGE_INTERVAL", "1200"))  # 20 min
# Agent notifications: who triggers them ("*" = any actor; else comma list of token names, e.g. "human")
NOTIFY_ACTORS = {x.strip().lower() for x in os.environ.get("TASKBOARD_NOTIFY_ACTORS", "*").split(",") if x.strip()} or {"*"}
SYSTEM_ACTORS = frozenset({"system", "nudge-cron", "recurring", "git", "cost", "wake"})
NOTIFY_KEYS = ("status", "assignee", "priority", "title", "description", "blocker", "blocked_by", "sprint_id", "decline_reason", "depends", "publish_at", "due")
inbox_cv = threading.Condition()
hook_q = queue.Queue()
MAX_BODY = int(os.environ.get("TASKBOARD_MAX_BODY", str(2 * 1024 * 1024)))          # JSON request bodies
MAX_IMPORT = int(os.environ.get("TASKBOARD_MAX_IMPORT", str(32 * 1024 * 1024)))    # /import, /import/csv
RATE_PER_MIN = int(os.environ.get("TASKBOARD_RATE_PER_MIN", "1200"))               # requests per token per minute (0 = off)
AUTH_FAILS_PER_MIN = int(os.environ.get("TASKBOARD_AUTH_FAILS_PER_MIN", "10"))     # bad tokens per client IP per minute (0 = off)
TRUST_PROXY = os.environ.get("TASKBOARD_TRUST_PROXY", "") in ("1", "true", "yes")  # honour X-Forwarded-For / -Proto
ALLOW_PRIVATE_WEBHOOKS = os.environ.get("TASKBOARD_ALLOW_PRIVATE_WEBHOOKS", "") in ("1", "true", "yes")
# Optional "needs a human" convention: assignees starting with one of these names are treated as human-gated.
HUMAN_NAMES = [x.strip().lower() for x in os.environ.get("TASKBOARD_HUMAN_NAMES", "").split(",") if x.strip()]
HUMAN_RULE = os.environ.get("TASKBOARD_HUMAN_RULE", "") in ("1", "true", "yes")   # require 'Missing:' and 'Do exactly:' in human-gated cards
ROLES = ("admin", "supervisor", "agent", "viewer")
GENERIC_ACTORS = frozenset({"agent", "human", "anon", "admin"})
TOKENS = {}   # token -> (name, role). Env: name:token[:role],...  role admin|agent|viewer (default: admin for human/admin/owner, else agent)
for p in os.environ.get("TASKBOARD_TOKENS", "").split(","):
    parts = p.strip().split(":")
    if len(parts) < 2:
        continue
    n, role = parts[0].strip(), None
    if len(parts) >= 3 and parts[-1].strip() in ROLES:
        role, tk = parts[-1].strip(), ":".join(parts[1:-1])
    else:
        tk = ":".join(parts[1:])
    TOKENS[tk.strip()] = (n, role or ("admin" if n.lower() in ("human", "admin", "owner") else "supervisor" if "supervisor" in n.lower() else "agent"))
BACKUP_DIR = os.environ.get("TASKBOARD_BACKUP_DIR", os.path.join(os.path.dirname(DB), "backups"))
STATUSES = ["backlog", "todo", "in_progress", "review", "blocked", "done", "declined"]
SPRINT_STATES = ["planned", "active", "closed"]
SILENT_MIN = int(os.environ.get("TASKBOARD_SILENT_MIN", "60"))
SPRINT_DAYS = int(os.environ.get("TASKBOARD_SPRINT_DAYS", "14"))

os.makedirs(os.path.dirname(DB), exist_ok=True)
lock = threading.Lock()


class _Res:
    """A finished query: rows are read while the connection is locked, so another thread can never disturb them."""
    def __init__(self, rows, lastrowid, rowcount):
        self._r, self._i, self.lastrowid, self.rowcount = rows, 0, lastrowid, rowcount

    def fetchone(self):
        if self._i >= len(self._r):
            return None
        self._i += 1
        return self._r[self._i - 1]

    def fetchall(self):
        out, self._i = self._r[self._i:], len(self._r)
        return out

    def __iter__(self):
        return iter(self.fetchall())


class _SafeDB:
    """Serialises every statement on the single shared connection (threads, SSE streams, hooks all use it)."""
    def __init__(self, conn):
        self._c, self._l = conn, threading.RLock()

    def execute(self, sql, args=()):
        with self._l:
            cur = self._c.execute(sql, args)
            return _Res(cur.fetchall() if cur.description else [], cur.lastrowid, cur.rowcount)

    def executescript(self, sql):
        with self._l:
            return self._c.executescript(sql)

    def backup(self, dst):
        with self._l:
            return self._c.backup(dst)


_conn = sqlite3.connect(DB, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row
db = _SafeDB(_conn)
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA foreign_keys=ON")
db.executescript("""
CREATE TABLE IF NOT EXISTS boards(id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '', created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS sprints(id INTEGER PRIMARY KEY, board_id INTEGER NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
  name TEXT NOT NULL, goal TEXT DEFAULT '', start TEXT, end TEXT, state TEXT DEFAULT 'planned', created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY, board_id INTEGER NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
  sprint_id INTEGER REFERENCES sprints(id) ON DELETE SET NULL, title TEXT NOT NULL, description TEXT DEFAULT '',
  status TEXT DEFAULT 'backlog', priority INTEGER DEFAULT 3, assignee TEXT DEFAULT '', kind TEXT DEFAULT 'human',
  created TEXT DEFAULT CURRENT_TIMESTAMP, updated TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts TEXT DEFAULT CURRENT_TIMESTAMP, actor TEXT, type TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS docs(id INTEGER PRIMARY KEY, scope TEXT NOT NULL, ref_id INTEGER NOT NULL, title TEXT NOT NULL,
  body TEXT DEFAULT '', url TEXT DEFAULT '', board_id INTEGER, created TEXT DEFAULT CURRENT_TIMESTAMP, updated TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS ix_d_ref ON docs(scope, ref_id);
CREATE TABLE IF NOT EXISTS agents(name TEXT PRIMARY KEY COLLATE NOCASE, webhook TEXT DEFAULT '', secret TEXT DEFAULT '', created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY, agent TEXT NOT NULL COLLATE NOCASE, task_id INTEGER, board_id INTEGER,
  type TEXT, actor TEXT, summary TEXT, payload TEXT DEFAULT '', read INTEGER DEFAULT 0, hook TEXT DEFAULT '', ts TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS risk_state(sprint_id INTEGER PRIMARY KEY, sig TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS specs(id INTEGER PRIMARY KEY, board_id INTEGER, title TEXT NOT NULL, body TEXT DEFAULT '', tags TEXT DEFAULT '',
  status TEXT DEFAULT 'draft', author TEXT DEFAULT '', updated_by TEXT DEFAULT '', rev INTEGER DEFAULT 1, tasks TEXT DEFAULT '',
  created TEXT DEFAULT CURRENT_TIMESTAMP, updated TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS spec_revs(id INTEGER PRIMARY KEY, spec_id INTEGER NOT NULL REFERENCES specs(id) ON DELETE CASCADE, rev INTEGER,
  title TEXT, body TEXT, actor TEXT, note TEXT DEFAULT '', ts TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS ix_sr_spec ON spec_revs(spec_id, rev);
CREATE TABLE IF NOT EXISTS tokens(id INTEGER PRIMARY KEY, name TEXT NOT NULL, hash TEXT NOT NULL UNIQUE, role TEXT DEFAULT 'agent',
  created TEXT DEFAULT CURRENT_TIMESTAMP, used TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS templates(id INTEGER PRIMARY KEY, board_id INTEGER REFERENCES boards(id) ON DELETE CASCADE, name TEXT NOT NULL,
  title TEXT DEFAULT '', description TEXT DEFAULT '', assignee TEXT DEFAULT '', kind TEXT DEFAULT 'agent', priority INTEGER DEFAULT 3,
  channel TEXT DEFAULT '', labels TEXT DEFAULT '', points INTEGER DEFAULT 0, checklist TEXT DEFAULT '', recur TEXT DEFAULT '', next_run TEXT DEFAULT '',
  active INTEGER DEFAULT 1, last_task INTEGER, created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS locks(name TEXT PRIMARY KEY, owner TEXT NOT NULL, note TEXT DEFAULT '', expires TEXT NOT NULL, created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS asks(id INTEGER PRIMARY KEY, from_agent TEXT NOT NULL, to_agent TEXT NOT NULL COLLATE NOCASE, task_id INTEGER, board_id INTEGER,
  question TEXT NOT NULL, answer TEXT DEFAULT '', status TEXT DEFAULT 'open', due TEXT, escalated INTEGER DEFAULT 0,
  created TEXT DEFAULT CURRENT_TIMESTAMP, answered TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, requester TEXT NOT NULL,
  reviewer TEXT NOT NULL COLLATE NOCASE, status TEXT DEFAULT 'pending', note TEXT DEFAULT '', decision_note TEXT DEFAULT '',
  created TEXT DEFAULT CURRENT_TIMESTAMP, decided TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS envs(name TEXT PRIMARY KEY, sha TEXT DEFAULT '', url TEXT DEFAULT '', deployed_by TEXT DEFAULT '', deployed_at TEXT DEFAULT '',
  task_id INTEGER, frozen_until TEXT DEFAULT '', freeze_note TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS watches(id INTEGER PRIMARY KEY, agent TEXT NOT NULL COLLATE NOCASE, kind TEXT NOT NULL, ref TEXT NOT NULL, UNIQUE(agent, kind, ref));
CREATE TABLE IF NOT EXISTS attachments(id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, name TEXT NOT NULL,
  url TEXT DEFAULT '', kind TEXT DEFAULT 'link', note TEXT DEFAULT '', actor TEXT DEFAULT '', created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS retros(id INTEGER PRIMARY KEY, sprint_id INTEGER NOT NULL UNIQUE REFERENCES sprints(id) ON DELETE CASCADE,
  well TEXT DEFAULT '', improve TEXT DEFAULT '', actions TEXT DEFAULT '[]', author TEXT DEFAULT '', created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS ix_n_agent ON notifications(agent, read, id);
CREATE TABLE IF NOT EXISTS comments(id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  actor TEXT, body TEXT NOT NULL, ts TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS ix_t_board ON tasks(board_id); CREATE INDEX IF NOT EXISTS ix_t_sprint ON tasks(sprint_id);
""")

for _t, _c, _d in (("tasks", "blocker", "TEXT DEFAULT ''"), ("tasks", "blocked_by", "TEXT DEFAULT ''"),
                   ("tasks", "labels", "TEXT DEFAULT ''"), ("tasks", "channel", "TEXT DEFAULT ''"),
                   ("tasks", "watch", "TEXT DEFAULT ''"), ("tasks", "short_url", "TEXT DEFAULT ''"),
                   ("tasks", "video_id", "TEXT DEFAULT ''"), ("tasks", "publish_at", "TEXT DEFAULT ''"),
                   ("tasks", "pr_url", "TEXT DEFAULT ''"), ("tasks", "sha", "TEXT DEFAULT ''"), ("tasks", "commits", "TEXT DEFAULT ''"), ("tasks", "decline_reason", "TEXT DEFAULT ''"), ("tasks", "nudge", "TEXT DEFAULT ''"),
                   ("tasks", "depends", "TEXT DEFAULT ''"), ("tasks", "points", "INTEGER DEFAULT 0"), ("agents", "capacity", "INTEGER DEFAULT 0"),
                   ("tasks", "claimed_by", "TEXT DEFAULT ''"), ("tasks", "claim_until", "TEXT DEFAULT ''"), ("tasks", "version", "INTEGER DEFAULT 1"), ("tasks", "dod_done", "TEXT DEFAULT ''"),
                   ("boards", "dod", "TEXT DEFAULT ''"), ("agents", "last_seen", "TEXT DEFAULT ''"), ("agents", "status_text", "TEXT DEFAULT ''"), ("agents", "status_task", "INTEGER"), ("agents", "skills", "TEXT DEFAULT ''"),
                   ("boards", "method", "TEXT DEFAULT 'scrum'"), ("boards", "cfg", "TEXT DEFAULT ''"), ("tasks", "type", "TEXT DEFAULT 'task'"), ("tasks", "parent", "INTEGER"),
                   ("sprints", "committed", "INTEGER DEFAULT 0"), ("tasks", "target", "REAL"), ("tasks", "current", "REAL"),
                   ("notifications", "cnt", "INTEGER DEFAULT 1"), ("agents", "mute", "TEXT DEFAULT ''"),
                   ("boards", "team", "TEXT DEFAULT ''"), ("tasks", "cost", "REAL DEFAULT 0"), ("tasks", "tokens", "INTEGER DEFAULT 0"), ("tokens", "boards", "TEXT DEFAULT ''"), ("tasks", "due", "TEXT DEFAULT ''"), ("agents", "quota_until", "TEXT DEFAULT ''"), ("agents", "quota_note", "TEXT DEFAULT ''"),
                   ("reviews", "kind", "TEXT DEFAULT 'review'"),
                   ("tasks", "accept", "TEXT DEFAULT ''"), ("tasks", "verified_by", "TEXT DEFAULT ''"), ("tasks", "verified_at", "TEXT DEFAULT ''"), ("tasks", "verified_note", "TEXT DEFAULT ''"),
                   ("events", "h", "TEXT DEFAULT ''"), ("events", "hp", "TEXT DEFAULT ''"),
                   ("tasks", "pipeline", "TEXT DEFAULT ''"), ("tasks", "stage", "INTEGER DEFAULT 0"),
                   ("tasks", "rigor", "TEXT DEFAULT ''"), ("tasks", "play", "TEXT DEFAULT ''"), ("tasks", "needs", "TEXT DEFAULT ''"), ("tasks", "makes", "TEXT DEFAULT ''"),
                   ("agents", "wake_at", "TEXT DEFAULT ''"), ("agents", "wake_n", "INTEGER DEFAULT 0")):
    if _c not in [r[1] for r in db.execute(f"PRAGMA table_info({_t})")]:
        db.execute(f"ALTER TABLE {_t} ADD COLUMN {_c} {_d}")
if "woken" not in [r[1] for r in db.execute("PRAGMA table_info(notifications)")]:
    db.execute("ALTER TABLE notifications ADD COLUMN woken INTEGER DEFAULT 0")
    db.execute("UPDATE notifications SET woken=1")      # items that predate the marker never trigger a wake ping
db.execute("CREATE TABLE IF NOT EXISTS usage_day(day TEXT, agent TEXT, route TEXT, calls INTEGER DEFAULT 0, ob INTEGER DEFAULT 0, ib INTEGER DEFAULT 0, PRIMARY KEY(day, agent, route))")
db.execute("CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL, body TEXT NOT NULL, actor TEXT DEFAULT '', ts TEXT DEFAULT CURRENT_TIMESTAMP)")
db.execute("CREATE INDEX IF NOT EXISTS ix_dec_task ON decisions(task_id)")
db.execute("CREATE TABLE IF NOT EXISTS task_state(task_id INTEGER NOT NULL, k TEXT NOT NULL, v TEXT, PRIMARY KEY(task_id, k))")
db.execute("CREATE TABLE IF NOT EXISTS nodes(name TEXT PRIMARY KEY COLLATE NOCASE, data TEXT DEFAULT '{}', ts TEXT DEFAULT CURRENT_TIMESTAMP)")
db.execute("CREATE TABLE IF NOT EXISTS hook_sent(agent TEXT COLLATE NOCASE, h TEXT, ts TEXT, PRIMARY KEY(agent, h))")

db.executescript("""
CREATE TABLE IF NOT EXISTS costs(id INTEGER PRIMARY KEY, ts TEXT DEFAULT CURRENT_TIMESTAMP, agent TEXT NOT NULL COLLATE NOCASE, task_id INTEGER, board_id INTEGER,
  model TEXT DEFAULT '', tin INTEGER DEFAULT 0, tout INTEGER DEFAULT 0, usd REAL DEFAULT 0, note TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_cost_task ON costs(task_id); CREATE INDEX IF NOT EXISTS ix_cost_ts ON costs(ts);
CREATE TABLE IF NOT EXISTS chats(id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT DEFAULT 'slack', url TEXT NOT NULL, target TEXT DEFAULT '', events TEXT DEFAULT '',
  board_id INTEGER, minpr INTEGER DEFAULT 4, active INTEGER DEFAULT 1, last TEXT DEFAULT '', last_ok INTEGER DEFAULT 1, created TEXT DEFAULT CURRENT_TIMESTAMP);
""")

db.executescript("""
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, ts TEXT DEFAULT CURRENT_TIMESTAMP, sender TEXT NOT NULL COLLATE NOCASE, recipient TEXT NOT NULL COLLATE NOCASE,
  task_id INTEGER, body TEXT NOT NULL, reply_to INTEGER);
CREATE INDEX IF NOT EXISTS ix_msg_to ON messages(recipient); CREATE INDEX IF NOT EXISTS ix_msg_from ON messages(sender);
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, task_id INTEGER, board_id INTEGER, agent TEXT NOT NULL COLLATE NOCASE, runtime TEXT DEFAULT '', job TEXT DEFAULT '',
  status TEXT DEFAULT 'running', started TEXT DEFAULT CURRENT_TIMESTAMP, ended TEXT DEFAULT '', outcome TEXT DEFAULT '', log_url TEXT DEFAULT '', usd REAL DEFAULT 0, tokens INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_runs_task ON runs(task_id); CREATE INDEX IF NOT EXISTS ix_runs_agent ON runs(agent, started);
CREATE TABLE IF NOT EXISTS views(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, filters TEXT DEFAULT '{}', owner TEXT DEFAULT '', created TEXT DEFAULT CURRENT_TIMESTAMP);
""")

FIELDS = {
    "boards": ["name", "description", "team", "dod", "method", "cfg"],
    "docs": ["scope", "ref_id", "title", "body", "url"],
    "sprints": ["board_id", "name", "goal", "start", "end", "state"],
    "tasks": ["board_id", "sprint_id", "title", "description", "status", "priority", "assignee", "kind", "blocker", "blocked_by", "labels", "channel", "watch", "short_url", "video_id", "publish_at", "pr_url", "sha", "commits", "decline_reason", "nudge", "depends", "points", "dod_done", "type", "parent", "target", "current", "due", "accept", "pipeline", "play", "needs", "makes", "rigor"],
}
REQUIRED = {"docs": ["scope", "ref_id", "title"], "boards": ["name"], "sprints": ["board_id"], "tasks": ["board_id", "title"]}
# db column -> short key (compact mode). Input accepts short or long keys.
CK = {
    "boards": {"id": "id", "name": "n", "description": "d", "team": "tm", "dod": "dd", "method": "mt", "cfg": "cf"},
    "docs": {"id": "id", "scope": "sc", "ref_id": "r", "title": "t", "body": "d", "url": "u", "board_id": "b"},
    "sprints": {"id": "id", "board_id": "b", "name": "n", "goal": "g", "start": "from", "end": "to", "state": "st", "committed": "cmt"},
    "tasks": {"id": "id", "board_id": "b", "sprint_id": "s", "title": "t", "description": "d", "status": "st",
              "priority": "pr", "assignee": "as", "kind": "k", "updated": "u", "blocker": "bl", "blocked_by": "bb", "labels": "lb", "channel": "ch",
              "watch": "w", "short_url": "sh", "video_id": "vid", "publish_at": "at", "pr_url": "pu", "sha": "sha", "commits": "cm", "decline_reason": "dr", "nudge": "nu", "depends": "dp", "points": "pt", "dod_done": "dn", "claimed_by": "cb", "claim_until": "cu", "version": "v", "type": "ty", "parent": "pa", "target": "tg", "current": "cr", "cost": "cs", "tokens": "tk", "due": "du", "accept": "ac", "verified_by": "vb", "verified_at": "va", "verified_note": "vn", "pipeline": "pl", "stage": "sg", "play": "po", "needs": "nd", "makes": "mk", "rigor": "rg"},
}
IN = {t: {**{v: k for k, v in m.items()}, **{k: k for k in m}} for t, m in CK.items()}

subs = set()
subs_lock = threading.Lock()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def check_public_url(url):
    """Webhook targets must be http(s) and, unless TASKBOARD_ALLOW_PRIVATE_WEBHOOKS=1, resolve only to public addresses
    (blocks loopback, private ranges, link-local/cloud-metadata, multicast, reserved). Redirects are never followed."""
    from urllib.parse import urlparse
    u = urlparse(str(url))
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ApiError("bad", 400, "hook")
    if ALLOW_PRIVATE_WEBHOOKS:
        return True
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ApiError("bad", 400, "hook")
    for inf in infos:
        ip = ipaddress.ip_address(inf[4][0].split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise ApiError("private_url", 400, "hook")
    return True


class ApiError(Exception):
    def __init__(self, code, status=400, field=None):
        self.code, self.status, self.field = code, status, field



def is_owner_assignee(asg):
    """Human-gated cards: assignee name starts with one of TASKBOARD_HUMAN_NAMES (off by default)."""
    a = (asg or "").strip().lower()
    return bool(a) and any(a.startswith(n) for n in HUMAN_NAMES)



def _lbl_set(labels):
    return {x.strip().lower() for x in str(labels or "").split(",") if x.strip()}


def _lbl_csv(ls):
    return ",".join(sorted(ls))[:200]


def nudge_tick(board_id=None, actor="system"):
    """Flag stale open tasks: set nu=timestamp + lb contains nudge; clear both when not stale.
    Also auto-unblock: if st=blocked and every bb id is done/declined, clear bl/bb → todo.
    """
    import datetime as _dt
    n_set = n_clr = n_ub = 0
    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    cutoff = now - _dt.timedelta(hours=max(NUDGE_HOURS, 0))
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    with lock:
        wh, args = [], []
        if board_id is not None:
            wh.append("board_id=?")
            args.append(board_id)
        sql = "SELECT id, board_id, status, updated, nudge, labels, blocked_by, blocker, assignee, title, priority FROM tasks" + (" WHERE " + " AND ".join(wh) if wh else "")
        for r in rows(sql, args):
            lbs = _lbl_set(r["labels"])
            has_nu = bool(r["nudge"]) or ("nudge" in lbs)
            try:
                upd = _dt.datetime.strptime((r["updated"] or "")[:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                upd = None
            stale = (NUDGE_HOURS > 0 and upd is not None and upd <= cutoff and r["status"] in NUDGE_OPEN)
            if stale and not has_nu:
                lbs.add("nudge")
                db.execute("UPDATE tasks SET nudge=?, labels=? WHERE id=?", (stamp, _lbl_csv(lbs), r["id"]))
                emit(actor, "task.nudged", r["id"], r["board_id"], {"nu": stamp, "lb": _lbl_csv(lbs)})
                notify(r["assignee"], "task.nudged", actor, r["id"], r["board_id"],
                       "#%d '%s' has had no update for %gh - please act or update it" % (r["id"], r["title"][:80], NUDGE_HOURS), {"nu": stamp})
                if r["priority"] <= 2 or is_owner_assignee(r["assignee"]):
                    escalate("stale", actor, r["id"], r["board_id"], "#%d '%s' (p%s) stale for %gh" % (r["id"], r["title"][:70], r["priority"], NUDGE_HOURS), skip=(r["assignee"],))
                n_set += 1
            elif (not stale) and has_nu and r["status"] not in NUDGE_OPEN:
                # done/declined: clear
                lbs.discard("nudge")
                db.execute("UPDATE tasks SET nudge='', labels=? WHERE id=?", (_lbl_csv(lbs), r["id"]))
                n_clr += 1
            # auto-unblock (#12)
            if r["status"] == "blocked":
                bbs = [int(x) for x in re.findall(r"\d+", str(r["blocked_by"] or ""))]
                if bbs:
                    doneish = {x["id"] for x in rows(
                        "SELECT id FROM tasks WHERE id IN (%s) AND status IN ('done','declined')" % ",".join("?" * len(bbs)), bbs)}
                    if len(doneish) == len(set(bbs)):
                        lbs.discard("nudge")
                        db.execute(
                            "UPDATE tasks SET status='todo', blocker='', blocked_by='', nudge='', labels=?, updated=CURRENT_TIMESTAMP WHERE id=?",
                            (_lbl_csv(lbs), r["id"]))
                        emit(actor, "task.updated", r["id"], r["board_id"], {"st": "todo", "bl": "", "bb": "", "nu": ""})
                        notify(r["assignee"], "task.unblocked", actor, r["id"], r["board_id"],
                               "#%d '%s' is unblocked (all blockers done) -> todo" % (r["id"], r["title"][:80]), {"st": "todo"})
                        n_ub += 1
        try:
            risk_alerts()
            housekeeping(actor)
        except Exception:
            pass
    return {"set": n_set, "cleared": n_clr, "unblocked": n_ub}


def has_owner_steps(desc):
    if not HUMAN_RULE:
        return True
    d = desc or ""
    return "Missing:" in d and "Do exactly:" in d


def rows(sql, args=()):
    return [dict(r) for r in db.execute(sql, args).fetchall()]


def last_event():
    return db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]


STATE_SKIP = frozenset({"u", "v", "id", "b", "d", "nu", "cs", "tk", "cm_id", "dc"})     # bookkeeping and long text: not part of the replayed state


TASK_KEYS = frozenset(CK["tasks"].values())


def _task_state(eid):
    """What each field of task `eid` was after its last change, rebuilt from the event log the first time it is needed."""
    rs = db.execute("SELECT k, v FROM task_state WHERE task_id=?", (eid,)).fetchall()
    if rs:
        return {r["k"]: json.loads(r["v"]) for r in rs}
    st = {}
    for e in db.execute("SELECT payload FROM events WHERE type LIKE 'task.%' AND payload LIKE ? ORDER BY id", ('{"id":%d,"b":%%' % eid,)).fetchall():
        try:
            d = json.loads(e["payload"]).get("d") or {}
        except ValueError:
            continue
        st.update({k: v for k, v in d.items() if k not in STATE_SKIP and k in TASK_KEYS})
    return st


def _before_after(typ, eid, delta):
    """-> the earlier value of every changed field whose earlier value is known (the `p` of an event), and remember the new values."""
    if not (typ.startswith("task.") and eid and isinstance(delta, dict)):
        return None
    if typ == "task.created":
        db.execute("DELETE FROM task_state WHERE task_id=?", (eid,))
        cur = {}
    else:
        cur = _task_state(eid)
    p = {k: cur[k] for k in delta if k not in STATE_SKIP and k in TASK_KEYS and k in cur and cur[k] != delta[k]}
    for k, v in delta.items():
        if k not in STATE_SKIP and k in TASK_KEYS:
            db.execute("INSERT OR REPLACE INTO task_state(task_id,k,v) VALUES(?,?,?)", (eid, k, json.dumps(v, separators=(",", ":"))))
    return p or None


chain_lock = threading.RLock()


def _chain(eid):
    """Link event `eid` to the one before it: h = sha256(previous hash | id | ts | actor | type | payload). Editing or removing a past event breaks every link after it."""
    e = db.execute("SELECT id, ts, actor, type, payload FROM events WHERE id=?", (eid,)).fetchone()
    p = db.execute("SELECT h FROM events WHERE id<? AND h!='' ORDER BY id DESC LIMIT 1", (eid,)).fetchone()
    hp = p["h"] if p else ""
    h = hashlib.sha256(("%s|%d|%s|%s|%s|%s" % (hp, e["id"], e["ts"], e["actor"] or "", e["type"], e["payload"] or "")).encode()).hexdigest()
    db.execute("UPDATE events SET h=?, hp=? WHERE id=?", (h, hp, eid))


def audit_verify(role):
    """Walk the chain: every hashed event must hash to its stored value and point at the hash before it."""
    if not _priv(role):
        raise ApiError("forbidden", 403)
    n, prev, first, bad = 0, None, None, None
    for e in db.execute("SELECT id, ts, actor, type, payload, h, hp FROM events WHERE h!='' ORDER BY id"):
        n += 1
        first = first or e["id"]
        want = hashlib.sha256(("%s|%d|%s|%s|%s|%s" % (e["hp"], e["id"], e["ts"], e["actor"] or "", e["type"], e["payload"] or "")).encode()).hexdigest()
        if want != e["h"]:
            bad = (e["id"], "content changed")
            break
        if prev is not None and e["hp"] != prev:
            bad = (e["id"], "an earlier event was changed or removed")
            break
        prev = e["h"]
    unhashed = db.execute("SELECT COUNT(*) FROM events WHERE h=''").fetchone()[0]
    out = {"ok": bad is None, "n": n, "from": first, "head": (prev or "")[:16], "unchained": unhashed}
    if bad:
        out["broken"], out["why"] = bad
    return out


def emit(actor, typ, eid, board, delta):
    """Event = type + entity id + board + changed fields (short keys). Full compact entity only on create."""
    data = {"id": eid, "b": board}
    if delta is not None:
        data["d"] = delta
    with chain_lock:
        p = _before_after(typ, eid, delta)
        if p:
            data["p"] = p
        cur = db.execute("INSERT INTO events(actor,type,payload) VALUES(?,?,?)",
                         (actor, typ, json.dumps(data, separators=(",", ":"))))
        _chain(cur.lastrowid)
    ev = {"i": cur.lastrowid, "t": typ, "a": actor, **data}
    with subs_lock:
        for q in list(subs):
            q.put(ev)


MAX_CM = 20


def norm_commits(v):
    """Normalize commit list -> [{h,u,m,r?}], deduped by h+u, capped (keeps newest). Accepts h|sha, u|url, m|msg."""
    if isinstance(v, str):
        try:
            v = json.loads(v) if v.strip() else []
        except Exception:
            raise ApiError("bad", 400, "cm")
    if isinstance(v, dict):
        v = [v]
    if not isinstance(v, list):
        raise ApiError("bad", 400, "cm")
    out, seen = [], set()
    for c in v:
        if not isinstance(c, dict):
            raise ApiError("bad", 400, "cm")
        h = str(c.get("h") or c.get("sha") or "").strip()[:64]
        u = str(c.get("u") or c.get("url") or "").strip()[:500]
        m = " ".join(str(c.get("m") or c.get("msg") or "").split())[:200]
        r = str(c.get("r") or c.get("repo") or "").strip()[:100]
        if not u and r and h:
            u = "https://github.com/%s/commit/%s" % (r, h)
        if not (h or u) or (h, u) in seen:
            continue
        seen.add((h, u))
        o = {"h": h, "u": u, "m": m, "r": r}
        out.append({k: x for k, x in o.items() if x or k == "h"})
    return out[-MAX_CM:]


def parse_task_ids(msg):
    """Task ids referenced in a commit message: #42, task:42, task #42, task 42."""
    ids = []
    for m in re.finditer(r"(?:\btask\s*[:#]?\s*|(?<![\w&/])#)(\d+)\b", str(msg or ""), re.I):
        i = int(m.group(1))
        if i not in ids:
            ids.append(i)
    return ids


def commits_of(r):
    try:
        return json.loads(r.get("commits") or "[]")
    except Exception:
        return []


def append_commit(actor, tid, body):
    cs = norm_commits(body)
    if not cs:
        raise ApiError("missing", 400, "h")
    need_task(tid)
    with lock:
        r = rows("SELECT * FROM tasks WHERE id=?", (tid,))
        if not r:
            raise ApiError("nf", 404)
        r = r[0]
        new = norm_commits(commits_of(r) + cs)
        db.execute("UPDATE tasks SET commits=?, updated=CURRENT_TIMESTAMP WHERE id=?", (json.dumps(new, separators=(",", ":")), tid))
        emit(actor, "task.updated", tid, r["board_id"], {"cm": new})
    return {"id": tid, "cm": new}


def append_commit_by_msg(actor, body):
    """POST /tasks/commits {h,u,m,r?,ids?}: attach to tasks named in ids or parsed from m (#N / task:N)."""
    ids = body.get("ids") or parse_task_ids(body.get("m") or body.get("msg"))
    if not ids:
        raise ApiError("missing", 400, "ids")
    res = []
    for i in ids:
        try:
            res.append(append_commit(actor, int(i), body))
        except ApiError as e:
            if e.code not in ("nf", "scope"):
                raise
    return {"n": len(res), "i": res}


def cdict(table, r, full, skip=()):
    if table == "tasks" and "commits" in r:
        r = {**r, "commits": commits_of(r)}
    if full:
        return r
    m = CK[table]
    return {m[k]: v for k, v in r.items() if k in m and k not in skip and v is not None and v != "" and v != [] and not (k in ("points", "target", "current", "cost", "tokens", "stage") and not v)}


def validate(table, body, partial):
    out = {}
    amap = IN[table]
    for k, v in body.items():
        if k in amap and amap[k] in FIELDS[table]:
            out[amap[k]] = v
    if not partial:
        for k in REQUIRED[table]:
            if not out.get(k):
                raise ApiError("missing", 400, CK[table][k])
    if "status" in out and out["status"] not in STATUSES:
        raise ApiError("bad", 400, "st")
    if "state" in out and out["state"] not in SPRINT_STATES:
        raise ApiError("bad", 400, "st")
    if "kind" in out and out["kind"] not in ("human", "agent"):
        raise ApiError("bad", 400, "k")
    if "priority" in out:
        try:
            out["priority"] = int(out["priority"])
            assert 1 <= out["priority"] <= 4
        except Exception:
            raise ApiError("bad", 400, "pr")
    if "blocked_by" in out:
        bb = out["blocked_by"]
        ids = bb if isinstance(bb, list) else re.findall(r"\d+", str(bb or ""))
        out["blocked_by"] = ",".join(str(int(i)) for i in ids)
    if "channel" in out:
        out["channel"] = " ".join(str(out["channel"] or "").split())[:40]
    if "labels" in out:
        ls = out["labels"] if isinstance(out["labels"], list) else str(out["labels"] or "").split(",")
        # stored lowercase
        out["labels"] = ",".join(sorted({x.strip().lower() for x in ls if x.strip()}))[:200]
    if "watch" in out:
        out["watch"] = str(out["watch"] or "").strip()[:500]
    if "short_url" in out:
        out["short_url"] = str(out["short_url"] or "").strip()[:500]
    if "pr_url" in out:
        out["pr_url"] = str(out["pr_url"] or "").strip()[:500]
    if "commits" in out:
        out["commits"] = json.dumps(norm_commits(out["commits"]), separators=(",", ":"))
    if "sha" in out:
        out["sha"] = str(out["sha"] or "").strip()[:200]
    if "video_id" in out:
        vid = re.sub(r"[^A-Za-z0-9_-]", "", str(out["video_id"] or ""))[:32]
        out["video_id"] = vid
    if "publish_at" in out:
        out["publish_at"] = str(out["publish_at"] or "").strip()[:40]
    if "due" in out:
        out["due"] = str(out["due"] or "").strip()
        if out["due"] and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", out["due"]):
            raise ApiError("bad", 400, "du")
    if "decline_reason" in out:
        out["decline_reason"] = str(out["decline_reason"] or "").strip()[:500]
    if "nudge" in out:
        out["nudge"] = str(out["nudge"] or "").strip()[:40]
    if "depends" in out:
        dv = out["depends"]
        ids = dv if isinstance(dv, list) else re.findall(r"\d+", str(dv or ""))
        out["depends"] = ",".join(str(i) for i in sorted({int(x) for x in ids}))[:200]
    if "dod_done" in out:
        dv = out["dod_done"]
        ids = dv if isinstance(dv, list) else re.findall(r"\d+", str(dv or ""))
        out["dod_done"] = ",".join(str(i) for i in sorted({int(x) for x in ids}))[:200]
    if "rigor" in out:
        out["rigor"] = str(out["rigor"] or "").strip().lower()
        if out["rigor"] not in RIGOR_RANK:
            raise ApiError("bad", 400, "rg")
    if "play" in out:
        out["play"] = " ".join(str(out["play"] or "").split())[:600]
    for k_ in ("needs", "makes"):
        if k_ in out:
            out[k_] = "\n".join(ln.strip() for ln in str(out[k_] or "").splitlines() if ln.strip())[:1000]
    if "pipeline" in out:
        out["pipeline"] = pipeline_clean(out["pipeline"])
    if "accept" in out:
        out["accept"] = "\n".join(ln.strip() for ln in str(out["accept"] or "").splitlines() if ln.strip())[:2000]
    if "dod" in out:
        out["dod"] = "\n".join(ln.strip() for ln in str(out["dod"] or "").splitlines() if ln.strip())[:2000]
    if "points" in out:
        try:
            out["points"] = int(out["points"] or 0)
            assert 0 <= out["points"] <= 1000
        except Exception:
            raise ApiError("bad", 400, "pt")
    if "method" in out and out["method"] not in METHODS:
        raise ApiError("bad", 400, "mt")
    if "cfg" in out:
        out["cfg"] = norm_cfg(out["cfg"])
    if "type" in out and out["type"] not in TASK_TYPES:
        raise ApiError("bad", 400, "ty")
    for _k in ("target", "current"):
        if _k in out:
            try:
                out[_k] = None if out[_k] in (None, "") else float(out[_k])
                assert out[_k] is None or out[_k] >= 0
            except (TypeError, ValueError, AssertionError):
                raise ApiError("bad", 400, "tg" if _k == "target" else "cr")
    if "parent" in out:
        try:
            out["parent"] = int(out["parent"]) if out["parent"] not in (None, "", 0, "0") else None
        except (TypeError, ValueError):
            raise ApiError("bad", 400, "pa")
    if table == "docs":
        if "scope" in out and out["scope"] not in DOC_SCOPES:
            raise ApiError("bad", 400, "sc")
        if "ref_id" in out:
            try:
                out["ref_id"] = int(out["ref_id"])
            except Exception:
                raise ApiError("bad", 400, "r")
    return out


DOC_SCOPES = {"board": "boards", "sprint": "sprints", "task": "tasks"}


def doc_board(scope, ref):
    t = DOC_SCOPES[scope]
    r = db.execute(f"SELECT {'id' if t == 'boards' else 'board_id'} FROM {t} WHERE id=?", (ref,)).fetchone()
    if not r:
        raise ApiError("nf", 404, "r")
    return r[0]


def docs_for(pairs, full, body=True):
    """pairs: [(scope, ref_id)] -> compact doc list."""
    out = []
    for sc, rf in pairs:
        for r in rows("SELECT * FROM docs WHERE scope=? AND ref_id=? ORDER BY id", (sc, rf)):
            out.append(cdict("docs", r, full, () if body else ("body",)))
    return out


def sprint_row(s, full):
    by = {r["status"]: r["n"] for r in rows("SELECT status, COUNT(*) n FROM tasks WHERE sprint_id=? GROUP BY status", (s["id"],))}
    total = sum(by.values())
    if full:
        s["progress"] = {"total": total, "done": by.get("done", 0), "by_status": by}
        return s
    o = cdict("sprints", s, False)
    o["p"] = [by.get("done", 0), total]  # [done,total]
    return o


def qint(q, k, default=None):
    try:
        return int(q[k][0])
    except (KeyError, ValueError, IndexError):
        return default


def _qv(q, *keys, default=None):
    """First present query value among short/long aliases."""
    for k in keys:
        if k in q and q[k]:
            return q[k][0]
    return default


def list_tasks(q, full):
    bid0 = _qv(q, "b", "board_id")
    try:
        nudge_tick(int(bid0) if bid0 is not None else None)
    except Exception:
        pass
    where, args = [], []
    # short+long aliases: st/status, as/assignee, k/kind, b/board_id, s/sprint_id
    st = _qv(q, "st", "status")
    if st:
        if st in ("open", "!done"):
            where.append("status NOT IN ('done','declined')")
        else:
            sts = [x.strip() for x in st.split(",") if x.strip()]
            bad = [x for x in sts if x not in STATUSES]
            if bad:
                raise ApiError("bad", 400, "st")
            if len(sts) == 1:
                where.append("status=?")
                args.append(sts[0])
            elif sts:
                where.append("status IN (%s)" % ",".join("?" * len(sts)))
                args.extend(sts)
    if q.get("open", ["0"])[0] == "1":
        where.append("status NOT IN ('done','declined')")
    if q.get("blocked", ["0"])[0] == "1":
        where.append("status='blocked'")
    if q.get("overdue", ["0"])[0] in ("1", "yes", "true"):
        where.append("due!='' AND due<? AND status NOT IN ('done','declined')")
        args.append(time.strftime("%Y-%m-%d", time.gmtime()))
    if q.get("nu", ["0"])[0] in ("1", "yes", "true"):
        where.append("nudge!=''")
    if _qv(q, "ty", "type"):
        where.append("type=?")
        args.append(_qv(q, "ty", "type"))
    if _qv(q, "parent", "pa"):
        where.append("parent=?")
        args.append(_qv(q, "parent", "pa"))
    if q.get("ready", ["0"])[0] in ("1", "yes", "true"):
        where.append("status NOT IN ('done','declined') AND NOT EXISTS (SELECT 1 FROM tasks dd WHERE dd.status NOT IN ('done','declined') AND (',' || tasks.depends || ',') LIKE '%,' || dd.id || ',%')")
    asg = _qv(q, "as", "assignee")
    if asg is not None:
        if asg.startswith("~"):
            where.append("assignee LIKE ? COLLATE NOCASE")
            args.append("%" + asg[1:] + "%")
        else:
            where.append("assignee=?")
            args.append(asg)
    kind = _qv(q, "k", "kind")
    if kind is not None:
        where.append("kind=?")
        args.append(kind)
    bid = _qv(q, "b", "board_id")
    if bid is not None:
        where.append("board_id=?")
        args.append(bid)
    else:
        sids = scope_ids(q)
        if sids is not None:
            frag, fargs = _in("board_id", sids)
            where.append(frag.replace(" AND ", "", 1))
            args.extend(fargs)
    sid = _qv(q, "s", "sprint_id")
    if sid is not None:
        if sid in ("none", "null"):
            where.append("sprint_id IS NULL")
        else:
            where.append("sprint_id=?")
            args.append(sid)
    if "ch" in q:
        if q["ch"][0] in ("none", "null"):
            where.append("channel=''")
        else:
            where.append("channel=? COLLATE NOCASE")
            args.append(q["ch"][0].strip())
    if "lb" in q:
        where.append("(',' || labels || ',') LIKE ?")
        args.append("%," + q["lb"][0].strip().lower() + ",%")
    if "q" in q:
        where.append("title LIKE ?")
        args.append("%" + q["q"][0] + "%")
    w = (" WHERE " + " AND ".join(where)) if where else ""
    total = db.execute(f"SELECT COUNT(*) FROM tasks{w}", args).fetchone()[0]
    limit = max(1, min(qint(q, "limit", 200), 500))
    off = max(0, qint(q, "offset", 0))
    rs = rows(f"SELECT * FROM tasks{w} ORDER BY priority, id LIMIT ? OFFSET ?", args + [limit, off])
    skip = () if ("d" in q or full) else ("description",)
    items = [cdict("tasks", r, full, skip) for r in rs]
    if full:
        return {"total": total, "offset": off, "items": items}
    fl = [x.strip() for x in (_qv(q, "fields") or "").split(",") if x.strip()]
    if fl:
        keep = set(["id"] + fl)
        items = [{k: v for k, v in it.items() if k in keep} for it in items]
    if _qv(q, "fmt") in ("t", "table"):     # header once, rows as arrays: ~40% fewer tokens than repeating every key
        cols = ["id"] + [c for c in (fl or ["t", "st", "pr", "as", "s"]) if c != "id"]
        return {"n": total, "o": off, "h": cols, "r": [[it.get(c, "") for c in cols] for it in items]}
    return {"n": total, "o": off, "i": items}


def bulk_set_tasks(actor, body, full):
    """PATCH /tasks body {"ids":[1,2], ...fields} — multi-id set; tiny response."""
    ids = body.get("ids") or body.get("id")
    if ids is None:
        raise ApiError("missing", 400, "ids")
    if not isinstance(ids, list):
        ids = [ids]
    try:
        ids = [int(i) for i in ids]
    except Exception:
        raise ApiError("bad", 400, "ids")
    if not ids or len(ids) > 200:
        raise ApiError("bad", 400, "ids")
    patch = {k: v for k, v in body.items() if k not in ("ids", "id")}
    d = validate("tasks", patch, True)
    if "board_id" in d:
        raise ApiError("use_move", 400, "b")
    if not d:
        raise ApiError("empty", 400)
    if "status" in d and d["status"] != "blocked":
        d.setdefault("blocker", "")
        d.setdefault("blocked_by", "")
    m = CK["tasks"]
    out = []
    for rid in ids:
        old = rows("SELECT * FROM tasks WHERE id=?", (rid,))
        if not old:
            continue
        old = old[0]
        sets = ",".join(f"{k}=?" for k in d) + ",updated=CURRENT_TIMESTAMP"
        db.execute(f"UPDATE tasks SET {sets} WHERE id=?", list(d.values()) + [rid])
        delta = {m[k]: (json.loads(v) if k == "commits" else v) for k, v in d.items() if old.get(k) != v}
        emit(actor, "task.updated", rid, old["board_id"], delta)
        notify_task(actor, old, rows("SELECT * FROM tasks WHERE id=?", (rid,))[0])
        post_update(actor, old, d, rid)
        if d.get("status") in ("done", "declined") and old["status"] not in ("done", "declined"):
            deps_released(actor, rid)
            parent_ready(actor, rid)
        out.append({"id": rid, **delta} if not full else cdict("tasks", rows("SELECT * FROM tasks WHERE id=?", (rid,))[0], True))
    return {"n": len(out), "i": out} if not full else {"total": len(out), "items": out}


def crud(actor, method, table, rid, body, q, full):
    sing = table[:-1]
    if method == "GET" and rid is None:
        if table == "tasks":
            return list_tasks(q, full)
        if table == "sprints":
            bid = q.get("board_id", [None])[0]
            rs = rows("SELECT * FROM sprints" + (" WHERE board_id=?" if bid else "") + " ORDER BY id", (bid,) if bid else ())
            items = [sprint_row(r, full) for r in rs if _board_ok(r["board_id"])]
        elif table == "docs":
            wh, ar = [], []
            for k, col in (("scope", "scope"), ("sc", "scope"), ("ref", "ref_id"), ("r", "ref_id"), ("board_id", "board_id")):
                if k in q:
                    wh.append(col + "=?")
                    ar.append(q[k][0])
            rs = rows("SELECT * FROM docs" + (" WHERE " + " AND ".join(wh) if wh else "") + " ORDER BY id", ar)
            items = [cdict("docs", r, full, () if ("d" in q or full) else ("body",)) for r in rs if _board_ok(r["board_id"])]
        else:
            bs = rows("SELECT * FROM boards" + (" WHERE team=?" if "tm" in q else "") + " ORDER BY id", (q["tm"][0],) if "tm" in q else ())
            items = [cdict("boards", r, full, () if full else ("description",)) for r in bs if _board_ok(r["id"])]
        return items if full else {"n": len(items), "i": items}
    if method == "GET":
        r = rows(f"SELECT * FROM {table} WHERE id=?", (rid,))
        if not r:
            raise ApiError("nf", 404)
        r = r[0]
        if table == "tasks" and "since" in q:
            return task_since(rid, qint(q, "since", 0) or 0)
        if table == "tasks" and "brief" in q:
            return task_brief(rid)
        if table == "sprints":
            return sprint_row(r, full)
        if table == "boards":
            o = cdict("boards", r, full)
            sp = [sprint_row(s, full) for s in rows("SELECT * FROM sprints WHERE board_id=? ORDER BY id", (rid,))]
            skip = () if ("d" in q or full) else ("description",)
            nudge_tick(rid)
            ts = [cdict("tasks", t, full, skip) for t in rows("SELECT * FROM tasks WHERE board_id=? ORDER BY priority, id", (rid,))]
            o["sprints" if full else "sp"] = sp
            o["tasks" if full else "t"] = ts
            if not full:
                dx = docs_for([("board", rid)] + [("sprint", s_["id"]) for s_ in sp], False, False)
                if dx:
                    o["dc"] = dx
            return o
        if table == "tasks" and "ctx" in q and not full:
            o = cdict("tasks", r, False)
            bk = [x["id"] for x in rows("SELECT id FROM tasks WHERE (',' || depends || ',') LIKE ?", ("%," + str(rid) + ",%",))]
            if bk:
                o["bk"] = bk
            od = open_dep_ids(r["depends"])
            if od:
                o["dpo"] = od
            if r["type"] in ("epic", "story"):
                kids = rows("SELECT id, title, status, type FROM tasks WHERE parent=? ORDER BY id", (rid,))
                o["kids"] = [{"id": k["id"], "t": k["title"], "st": k["status"], "ty": k["type"]} for k in kids]
                o["prog"] = rollup(rid)
            ats = atts_get(rid)["i"]
            if ats:
                o["att"] = ats
            rv = rows("SELECT * FROM reviews WHERE task_id=? ORDER BY id DESC LIMIT 5", (rid,))
            if rv:
                o["rv"] = [review_out(x) for x in rv]
            cc = rows("SELECT id, actor, body, ts FROM comments WHERE task_id=? ORDER BY id DESC LIMIT 5", (rid,))
            if cc:
                o["cc"] = [comment_out(c) for c in reversed(cc)]
            o["dc"] = docs_for([("task", rid)] + ([("sprint", r["sprint_id"])] if r["sprint_id"] else []) + [("board", r["board_id"])], False)
            return o
        return cdict(table, r, full)
    with lock:
        if method == "POST" and rid is None:
            d = validate(table, body, False)
            if table == "tasks":
                if d.get("status") == "declined" and not d.get("decline_reason"):
                    raise ApiError("missing", 400, "dr")
                check_hier(0, d.get("type", "task"), d.get("parent"), d["board_id"])
                _pol = board_policy(d["board_id"])
                if "require_parent" in (board_rules(d["board_id"]) | set(PRESETS.get(d.get("rigor") or "", {"rules": []})["rules"])) and d.get("type", "task") in LEAF_TYPES and not d.get("parent"):
                    raise ApiError("parent", 400, "pa")
                if _pol.get("pipeline") and d.get("type", "task") in (_pol.get("pipeline_types") or ["story"]) and not d.get("pipeline"):
                    d["pipeline"] = pipeline_clean(_pol["pipeline"])
                _w = check_wip(d["board_id"], d.get("status", "backlog"), force=body.get("force"))
                if str(d.get("assignee", "")).lower() == "auto":
                    cand = route_candidates("", d.get("labels", ""))
                    if not cand:
                        raise ApiError("route", 400, "as")
                    d["assignee"] = cand[0]["n"]
                if is_owner_assignee(d.get("assignee")) and not has_owner_steps(d.get("description", "")):
                    raise ApiError("owner_fmt", 400, "d")
                if d.get("depends"):
                    check_deps(0, d["depends"])
            if table == "docs":
                d["board_id"] = doc_board(d["scope"], d["ref_id"])
            if table == "tasks" and "sprint_id" not in d and board_has_sprints(d["board_id"]):
                act = rows("SELECT id FROM sprints WHERE board_id=? AND state='active' ORDER BY id DESC LIMIT 1", (d["board_id"],))
                if act:
                    d["sprint_id"] = act[0]["id"]
            if table == "sprints":
                if not board_has_sprints(d["board_id"]):
                    raise ApiError("no_sprints", 400, "b")
                if not d.get("name"):
                    d["name"] = "Sprint %d" % (db.execute("SELECT COUNT(*) FROM sprints WHERE board_id=?", (d["board_id"],)).fetchone()[0] + 1)
                if d.get("state") == "active":
                    d["start"] = d.get("start") or time.strftime("%Y-%m-%d")
                    d["end"] = d.get("end") or time.strftime("%Y-%m-%d", time.gmtime(time.time() + sprint_days_for(d["board_id"]) * 86400))
            cur = db.execute(f"INSERT INTO {table}({','.join(d)}) VALUES({','.join('?' * len(d))})", list(d.values()))
            r = rows(f"SELECT * FROM {table} WHERE id=?", (cur.lastrowid,))[0]
            bid = r["id"] if table == "boards" else r["board_id"]
            emit(actor, f"{sing}.created", r["id"], bid, cdict(table, r, False))
            if table == "boards":
                ensure_phases(r["id"])
            if table == "tasks":
                notify_task(actor, None, r)
            if table == "sprints" and r["state"] == "active":
                close_other_active(actor, r["id"], r["board_id"])
            return cdict(table, r, full)
        if rid is None and method in ("PATCH", "PUT") and table == "tasks":
            return bulk_set_tasks(actor, body, full)
        if rid is None:
            raise ApiError("method", 405)
        old = rows(f"SELECT * FROM {table} WHERE id=?", (rid,))
        if not old:
            raise ApiError("nf", 404)
        old = old[0]
        bid = rid if table == "boards" else old["board_id"]
        if method in ("PATCH", "PUT"):
            d = validate(table, body, True)
            if not d:
                raise ApiError("empty", 400)
            if table == "tasks" and body.get("ver") not in (None, "") and int(body["ver"]) != (old.get("version") or 1):
                raise ApiError("conflict", 409, "ver")
            if table == "tasks":
                if "board_id" in d and d["board_id"] != old["board_id"]:
                    raise ApiError("use_move", 400, "b")
                _wip = None
                if "status" in d and d["status"] != old["status"]:
                    _wip = check_wip(old["board_id"], d["status"], tid=rid, force=body.get("force"))
                    check_start(old, d["status"])
                if "parent" in d or "type" in d:
                    check_hier(rid, d.get("type", old["type"] or "task"), d["parent"] if "parent" in d else old["parent"], old["board_id"])
                if d.get("status") in ("done", "declined"):
                    d["claimed_by"] = ""
                    d["claim_until"] = ""
                if d.get("status") == "done" and old["status"] != "done":
                    check_done_gate(rid, old["board_id"], d.get("dod_done", old["dod_done"]))
                # Leaving blocked clears bl/bb (Reopen / any non-blocked status)
                if "status" in d and d["status"] != "blocked":
                    d.setdefault("blocker", "")
                    d.setdefault("blocked_by", "")
                # Leaving declined clears dr; entering declined keeps dr (validated above)
                if "status" in d and d["status"] != "declined":
                    d.setdefault("decline_reason", "")
                # Any agent/human bump clears auto-nudge (nu + lb=nudge) unless explicitly setting nu
                if "nudge" not in d and any(k != "nudge" for k in d):
                    d["nudge"] = ""
                    if "labels" in d:
                        lbs = _lbl_set(d["labels"]); lbs.discard("nudge"); d["labels"] = _lbl_csv(lbs)
                    else:
                        lbs = _lbl_set(old["labels"]); lbs.discard("nudge")
                        if "nudge" in _lbl_set(old["labels"]):
                            d["labels"] = _lbl_csv(lbs)
                # Owner-gated cards: description must include Missing: + Do exactly:
                asg = d["assignee"] if "assignee" in d else old["assignee"]
                desc = d["description"] if "description" in d else old["description"]
                if is_owner_assignee(asg) and ("assignee" in d or "description" in d) and not has_owner_steps(desc):
                    raise ApiError("owner_fmt", 400, "d")
                if "depends" in d:
                    check_deps(rid, d["depends"])
                # Decline requires reason even if only patching st without dr when already declined? require on transition
                if d.get("status") == "declined" and not (d.get("decline_reason") or old["decline_reason"]):
                    raise ApiError("missing", 400, "dr")
                if d.get("status") == "declined" and not d.get("decline_reason") and old["decline_reason"]:
                    d["decline_reason"] = old["decline_reason"]
            if table == "sprints" and d.get("state") == "active" and old["state"] != "active":
                d.setdefault("start", old["start"] or time.strftime("%Y-%m-%d"))
                d.setdefault("end", old["end"] or time.strftime("%Y-%m-%d", time.gmtime(time.time() + sprint_days_for(old["board_id"]) * 86400)))
                d["committed"] = sprint_weight(rid)
                close_other_active(actor, rid, bid)
            sets = ",".join(f"{k}=?" for k in d)
            if table in ("tasks", "docs"):
                sets += ",updated=CURRENT_TIMESTAMP"
            if table == "tasks":
                sets += ",version=COALESCE(version,1)+1"
            db.execute(f"UPDATE {table} SET {sets} WHERE id=?", list(d.values()) + [rid])
            m = CK[table]
            emit(actor, f"{sing}.updated", rid, bid, {m[k]: (json.loads(v) if k == "commits" else v) for k, v in d.items() if old.get(k) != v})
            if table == "boards" and ("method" in d or "cfg" in d):
                ensure_phases(rid)
            if table == "tasks":
                notify_task(actor, old, rows("SELECT * FROM tasks WHERE id=?", (rid,))[0])
                post_update(actor, old, d, rid)
                if d.get("status") in ("done", "declined") and old["status"] not in ("done", "declined"):
                    deps_released(actor, rid)
                    parent_ready(actor, rid)
            if full:
                return cdict(table, rows(f"SELECT * FROM {table} WHERE id=?", (rid,))[0], True)
            res = {"id": rid, **{m[k]: (json.loads(v) if k == "commits" else v) for k, v in d.items() if k in m}}
            if table == "tasks":
                res["v"] = (old.get("version") or 1) + 1
            if table == "tasks" and d.get("status") in ("in_progress", "review", "done"):
                od = open_dep_ids(d.get("depends", old["depends"]))
                if od:
                    res["w"] = ["depends on open " + " ".join("#%d" % i for i in od)]
            if table == "tasks" and _wip:
                res["w"] = res.get("w", []) + [_wip]
            return res
        if method == "DELETE":
            db.execute(f"DELETE FROM {table} WHERE id=?", (rid,))
            if table in ("boards", "sprints", "tasks"):
                db.execute("DELETE FROM docs WHERE scope=? AND ref_id=?", (sing, rid))
                if table == "boards":
                    db.execute("DELETE FROM docs WHERE board_id=?", (rid,))
                    db.execute("DELETE FROM runs WHERE board_id=?", (rid,))
                if table == "tasks":
                    db.execute("DELETE FROM runs WHERE task_id=?", (rid,))      # a deleted task leaves no job history behind (spend stays in the cost log)
            emit(actor, f"{sing}.deleted", rid, bid, None)
            return {"deleted": rid} if full else {"id": rid, "x": 1}
    raise ApiError("method", 405)


# ---------------------------------------------------------------- agent notifications
def comment_out(c):
    return {"id": c["id"], "by": c["actor"], "c": c["body"], "ts": c["ts"]}


NOTIF_DO = {"verify.requested": "verify", "task.overlap": "task", "approval.requested": "decide_review", "approval.granted": "task", "approval.denied": "task", "msg.direct": "say", "task.assigned": "claim", "task.handoff": "claim", "task.ready": "claim", "task.unblocked": "claim", "claim.expired": "claim", "task.comment": "comment", "task.mention": "comment",
            "ask.new": "answer", "ask.overdue": "answer", "review.requested": "decide_review", "task.nudged": "set_task", "sla.breach": "set_task",
            "review.changes": "set_task", "review.approved": "finish", "ask.answered": "task", "sprint.risk": "risks", "env.frozen": "envs"}
COALESCE_TYPES = frozenset({"task.updated", "task.status", "task.comment", "watch.task", "task.attachment"})


def ndict(r):
    o = {"id": r["id"], "tk": r["task_id"], "b": r["board_id"], "ty": r["type"], "by": r["actor"], "m": r["summary"], "ts": r["ts"]}
    if r["payload"]:
        o["d"] = json.loads(r["payload"])
    op = NOTIF_DO.get(r["type"])
    if r["type"] == "msg.direct":
        o["do"] = "say to=%s" % r["actor"]
    elif op:      # the next call that acts on this item, so the agent does not have to fetch the task just to decide
        ref = (o.get("d") or {}).get("ask") or (o.get("d") or {}).get("review") or r["task_id"]
        o["do"] = "%s id=%s" % (op, ref) if ref and op not in ("risks", "envs") else op
    if (r.get("cnt") or 1) > 1:
        o["x"] = r["cnt"]
    if r["hook"]:
        o["hk"] = r["hook"]
    return {k: v for k, v in o.items() if v not in (None, "")}


QUIET_TYPES = frozenset({"msg.fyi", "task.fyi"})


def notify(agent, typ, actor, task_id, board_id, summary, delta=None):
    """Queue an inbox item (+ webhook push if the agent registered one). Caller holds `lock`."""
    agent = (agent or "").strip()
    actor = actor or ""
    if not agent or agent.lower() == actor.lower():
        return
    if actor.lower() not in SYSTEM_ACTORS and "*" not in NOTIFY_ACTORS and actor.lower() not in NOTIFY_ACTORS:
        return
    mu = db.execute("SELECT mute FROM agents WHERE name=?", (agent,)).fetchone()
    if mu and mu["mute"] and typ in mu["mute"].split(","):
        return
    if typ in COALESCE_TYPES:       # repeated chatter about the same task becomes one item with a counter
        prev = db.execute("SELECT id FROM notifications WHERE agent=? AND type=? AND task_id IS ? AND read=0 AND ts>=datetime('now','-60 seconds') ORDER BY id DESC LIMIT 1", (agent, typ, task_id)).fetchone()
        if prev:
            db.execute("UPDATE notifications SET cnt=COALESCE(cnt,1)+1, summary=?, actor=?, payload=? WHERE id=?",
                       (summary[:300], actor, json.dumps(delta, separators=(",", ":")) if delta else "", prev["id"]))
            with inbox_cv:
                inbox_cv.notify_all()
            _push_hook(agent, prev["id"])       # grouped in the inbox, but the agent is still woken now
            return
    cur = db.execute("INSERT INTO notifications(agent,task_id,board_id,type,actor,summary,payload) VALUES(?,?,?,?,?,?,?)",
                     (agent, task_id, board_id, typ, actor, summary[:300], json.dumps(delta, separators=(",", ":")) if delta else ""))
    with inbox_cv:
        inbox_cv.notify_all()
    if typ in QUIET_TYPES:           # for the next briefing, never a push, a wake-up or a job
        db.execute("UPDATE notifications SET hook='quiet', woken=1 WHERE id=?", (cur.lastrowid,))
        return
    _push_hook(agent, cur.lastrowid)


# Unchanged alerts (re-raised by the briefing / sweeps) must not wake the agent again: they are pushed once per content hash.
HOOK_ALERT_TYPES = frozenset({"task.overlap", "sprint.risk", "ask.overdue", "claim.expired", "sla.breach", "env.frozen", "task.nudged"})
HOOK_DEDUP_HOURS = 24


def _push_hook(agent, nid):
    """Genuinely new events are pushed to the agent's webhook at once. An alert whose content was already pushed to this agent
    (last-pushed marker in hook_sent) is only kept in the inbox, never pushed again."""
    h = db.execute("SELECT webhook, secret FROM agents WHERE name=?", (agent,)).fetchone()
    if not (h and h["webhook"]):
        return
    n = db.execute("SELECT type, task_id, board_id, summary, payload FROM notifications WHERE id=?", (nid,)).fetchone()
    if n and (n["type"] in HOOK_ALERT_TYPES or n["type"].startswith("esc.")):
        key = hashlib.sha1("|".join(str(n[k]) for k in ("type", "task_id", "board_id", "summary", "payload")).encode()).hexdigest()
        seen = db.execute("SELECT ts FROM hook_sent WHERE agent=? AND h=?", (agent, key)).fetchone()
        if seen and seen["ts"] > plus_min(-HOOK_DEDUP_HOURS * 60):
            db.execute("UPDATE notifications SET hook='dedup' WHERE id=?", (nid,))
            return
        db.execute("INSERT OR REPLACE INTO hook_sent(agent, h, ts) VALUES(?,?,?)", (agent, key, now_s()))
    db.execute("UPDATE notifications SET hook='pending' WHERE id=?", (nid,))
    hook_q.put((nid, agent, h["webhook"], h["secret"]))
    ensure_hook_thread()




def _mentions(text):
    """@Name or @"Two Words" style mentions that match a known agent (case-insensitive)."""
    names = {r["name"].lower(): r["name"] for r in rows("SELECT name FROM agents")}
    out = []
    for m in re.finditer(r'@"([^"]{1,60})"|@([A-Za-z0-9_.-]+(?: [A-Za-z0-9_.-]+)?)', text or ""):
        raw = m.group(1) or m.group(2)
        for cand in (raw, raw.split(" ")[0]):
            if cand.lower() in names:
                out.append(names[cand.lower()])
                break
    return list(dict.fromkeys(out))


def notify_task(actor, old, new):
    _notify_task_core(actor, old, new)
    _notify_watchers(actor, old, new)
    _chat_task(actor, old, new)


def _notify_task_core(actor, old, new):
    _escalate_task(actor, old, new)
    """Tell the assigned agent (and a previous assignee) that a task was created or changed."""
    tid, bid, title = new["id"], new["board_id"], new["title"][:80]
    if old is None:
        notify(new["assignee"], "task.assigned", actor, tid, bid,
               "#%d '%s' created and assigned to you by %s [%s, p%s]" % (tid, title, actor, new["status"], new["priority"]),
               {"st": new["status"], "pr": new["priority"]})
        return
    ch = {k: (old.get(k), new.get(k)) for k in NOTIFY_KEYS if old.get(k) != new.get(k)}
    if not ch:
        return
    short = CK["tasks"]
    delta = {short[k]: v[1] for k, v in ch.items() if k != "description"}
    what = []
    for k, (a, b) in ch.items():
        if k in ("status", "priority", "sprint_id"):
            what.append("%s %s->%s" % (short[k], a, b))
        elif k == "blocker":
            what.append("blocker: %s" % (b or "cleared"))
        elif k == "title":
            what.append("retitled")
        elif k == "description":
            what.append("description edited")
        elif k == "assignee":
            what.append("assignee %s->%s" % (a or "-", b or "-"))
        else:
            what.append("%s changed" % short.get(k, k))
    what = ", ".join(what)
    if "assignee" in ch:
        a, b = ch["assignee"]
        notify(a, "task.unassigned", actor, tid, bid, "#%d '%s' was reassigned to %s by %s" % (tid, title, b or "nobody", actor), delta)
        notify(b, "task.assigned", actor, tid, bid, "#%d '%s' assigned to you by %s [%s, p%s]" % (tid, title, actor, new["status"], new["priority"]), delta)
        return
    typ = "task.status" if "status" in ch else "task.updated"
    notify(new["assignee"], typ, actor, tid, bid, "#%d '%s' updated by %s: %s" % (tid, title, actor, what), delta)


_hook_thread = []


def ensure_hook_thread():
    if not _hook_thread:
        t = threading.Thread(target=_hook_loop, name="hooks", daemon=True)
        _hook_thread.append(t)
        t.start()


def _hook_loop():
    while True:
        nid, agent, url, secret = hook_q.get()
        r = db.execute("SELECT * FROM notifications WHERE id=?", (nid,)).fetchone()
        if not r:
            continue
        body = json.dumps({"agent": agent, "n": ndict(dict(r))}, separators=(",", ":")).encode()
        hd = {"Content-Type": "application/json", "User-Agent": "ValnivoTaskboard"}
        if secret:
            hd["X-Taskboard-Signature"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        status = "failed"
        for wait in (0, 2, 8):
            time.sleep(wait)
            try:
                check_public_url(url)
                with _opener.open(urllib.request.Request(url, data=body, headers=hd, method="POST"), timeout=6) as x:
                    if 200 <= x.status < 300:
                        status = "ok"
                        break
            except Exception:
                pass
        with lock:
            db.execute("UPDATE notifications SET hook=? WHERE id=?", (status, nid))


def inbox_get(q, actor=""):
    agent = _qv(q, "as", "agent") or (actor if actor and actor.lower() not in GENERIC_ACTORS else "")
    if not agent:
        raise ApiError("missing", 400, "as")
    unread = q.get("all", ["0"])[0] in ("0", "")
    since = qint(q, "since", 0)
    lim = max(1, min(qint(q, "limit", 50), 200))
    wait = max(0, min(qint(q, "wait", 0), 30))
    sql = "SELECT * FROM notifications WHERE agent=? AND id>?" + (" AND read=0" if unread else "") + " ORDER BY id LIMIT ?"

    def fetch():
        return rows(sql, (agent, since, lim))
    items = fetch()
    if not items and wait:
        end = time.time() + wait
        while not items and time.time() < end:
            with inbox_cv:
                inbox_cv.wait(min(end - time.time(), 5))
            items = fetch()
    own = bool(actor) and actor.lower() == agent.lower()
    ack = q.get("ack", [None])[0]
    if items and (ack not in (None, "0", "") or (ack is None and own)):
        with lock:
            db.execute("UPDATE notifications SET read=1 WHERE id IN (%s)" % ",".join("?" * len(items)), [i["id"] for i in items])
    left = db.execute("SELECT COUNT(*) FROM notifications WHERE agent=? AND read=0", (agent,)).fetchone()[0]
    return {"n": len(items), "unread": left, "i": [ndict(i) for i in items]}


def inbox_ack(body):
    agent = body.get("as") or body.get("agent")
    if not agent:
        raise ApiError("missing", 400, "as")
    with lock:
        if body.get("ids"):
            ids = [int(i) for i in body["ids"]]
            cur = db.execute("UPDATE notifications SET read=1 WHERE agent=? AND read=0 AND id IN (%s)" % ",".join("?" * len(ids)), [agent] + ids)
        elif body.get("upto"):
            cur = db.execute("UPDATE notifications SET read=1 WHERE agent=? AND read=0 AND id<=?", (agent, int(body["upto"])))
        else:
            cur = db.execute("UPDATE notifications SET read=1 WHERE agent=? AND read=0", (agent,))
    return {"ok": 1, "n": cur.rowcount}


def agents_list():
    reg = {r["name"].lower(): r for r in rows("SELECT * FROM agents")}
    names = {r["assignee"].lower(): r["assignee"] for r in rows("SELECT DISTINCT assignee FROM tasks WHERE assignee!=''")}
    names.update({k: v["name"] for k, v in reg.items()})
    unread = {r["agent"].lower(): r["n"] for r in rows("SELECT agent, COUNT(*) n FROM notifications WHERE read=0 GROUP BY agent COLLATE NOCASE")}
    silent_set = silent_agents()
    items = []
    for k in sorted(names):
        o = {"n": names[k], "inbox": unread.get(k, 0)}
        if k in reg and reg[k]["capacity"]:
            o["cap"] = reg[k]["capacity"]
        if k in reg:
            for key, col in (("seen", "last_seen"), ("stx", "status_text"), ("stk", "status_task"), ("sk", "skills")):
                if reg[k][col]:
                    o[key] = reg[k][col]
            if k in silent_set:
                o["silent"] = 1
            if reg[k]["quota_until"] and reg[k]["quota_until"] > now_s():
                o["q"] = reg[k]["quota_until"]
                if reg[k]["quota_note"]:
                    o["qn"] = reg[k]["quota_note"]
        if k in reg and reg[k]["webhook"]:
            o["hook"] = reg[k]["webhook"]
            o["hs"] = bool(reg[k]["secret"])
        items.append(o)
    return {"n": len(items), "i": items}


def agent_set(body):
    name = " ".join(str(body.get("n") or body.get("name") or body.get("as") or "").split())[:60]
    if not name:
        raise ApiError("missing", 400, "n")
    hook = str(body.get("hook") or body.get("webhook") or "").strip()[:500]
    if hook:
        check_public_url(hook)
    with lock:
        old = db.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone()
        secret = str(body["secret"])[:200] if "secret" in body else (old["secret"] if old else "")
        if old:
            db.execute("UPDATE agents SET webhook=?, secret=? WHERE name=?", (hook if ("hook" in body or "webhook" in body) else old["webhook"], secret, name))
        else:
            db.execute("INSERT INTO agents(name,webhook,secret) VALUES(?,?,?)", (name, hook, secret))
        if "capacity" in body:
            db.execute("UPDATE agents SET capacity=? WHERE name=?", (max(0, int(body["capacity"] or 0)), name))
        if "mute" in body:
            mu_ = ",".join(sorted({x.strip() for x in (body["mute"] if isinstance(body["mute"], list) else str(body["mute"] or "").split(",")) if x.strip() and "." in x}))[:300]
            db.execute("UPDATE agents SET mute=? WHERE name=?", (mu_, name))
        if "quota_min" in body or "quota_until" in body:
            if body.get("quota_until") in ("", None) and not body.get("quota_min"):
                db.execute("UPDATE agents SET quota_until='', quota_note='' WHERE name=?", (name,))
            else:
                until = plus_min(max(1, min(int(body.get("quota_min") or 60), 10080))) if body.get("quota_min") else str(body["quota_until"])[:19]
                db.execute("UPDATE agents SET quota_until=?, quota_note=? WHERE name=?", (until, str(body.get("quota_note") or "")[:120], name))
        if "skills" in body:
            sk = ",".join(sorted({re.sub(r"[^a-z0-9._-]", "", x.strip().lower()) for x in (body["skills"] if isinstance(body["skills"], list) else str(body["skills"] or "").split(",")) if x.strip()}))[:200]
            db.execute("UPDATE agents SET skills=? WHERE name=?", (sk, name))
    return {"n": name, "ok": 1}


def agent_del(name):
    with lock:
        db.execute("DELETE FROM agents WHERE name=?", (name,))
    return {"n": name, "x": 1}


# ---------------------------------------------------------------- comments
def comment_add(actor, tid, body, notify_assignee=True):
    text = str(body.get("c") or body.get("body") or "").strip()[:4000]
    if not text:
        raise ApiError("missing", 400, "c")
    quiet = body.get("quiet") in (True, 1, "1", "true")          # a note for the record: nobody is notified
    with lock:
        t = rows("SELECT * FROM tasks WHERE id=?", (tid,))
        if not t:
            raise ApiError("nf", 404)
        t = t[0]
        cur = db.execute("INSERT INTO comments(task_id,actor,body) VALUES(?,?,?)", (tid, actor, text))
        db.execute("UPDATE tasks SET updated=CURRENT_TIMESTAMP, nudge='' WHERE id=?", (tid,))
        emit(actor, "task.commented", tid, t["board_id"], {"cm_id": cur.lastrowid})
        to = [] if quiet else (body.get("to") or [])
        to = [to] if isinstance(to, str) else to
        told = set()
        for a in {x.strip() for x in ([t["assignee"]] if notify_assignee and not quiet else []) + list(to) if x and x.strip()}:
            notify(a, "task.comment", actor, tid, t["board_id"], "#%d '%s' comment by %s: %s" % (tid, t["title"][:60], actor, text[:140]))
            told.add(a.lower())
        for a in ([] if quiet else _mentions(text)):
            if a.lower() not in told:
                notify(a, "task.mention", actor, tid, t["board_id"], "%s mentioned you on #%d '%s': %s" % (actor, tid, t["title"][:60], text[:140]))
    return {"id": cur.lastrowid, "tk": tid, **({"tip": "long comment: attach the detail and keep the comment short, others read the short one first"} if len(text) > 1200 else {})}


def comments_get(tid):
    if not rows("SELECT id FROM tasks WHERE id=?", (tid,)):
        raise ApiError("nf", 404)
    cs = rows("SELECT id, actor, body, ts FROM comments WHERE task_id=? ORDER BY id", (tid,))
    return {"n": len(cs), "i": [comment_out(c) for c in cs]}


# ---------------------------------------------------------------- decline / reopen (any assignee: human or agent)
REOPEN_FROM = ("declined", "blocked", "done")
REOPEN_TO = ("todo", "in_progress", "backlog", "review")


# ---- ways of working: which rules apply to a board, and to a task that asks for more
RULES = {
    "strict_moves": "A task only moves along the allowed paths (backlog, to do, in progress, review, done).",
    "holder_only": "A task someone is holding can only be changed by that holder, a supervisor or an admin.",
    "accept_before_start": "A story, task or bug needs acceptance criteria before work on it can start.",
    "claim_to_start": "Work starts by claiming the task, not by dragging it to In progress.",
    "verify_before_done": "A task with acceptance criteria can only be done after someone other than the worker verified it.",
    "require_parent": "Every task, bug, spike and chore belongs to a story or an epic.",
}
PRESETS = {
    "loose": {"about": "Move work freely; only the board's own checklist applies.", "rules": []},
    "standard": {"about": "Clear paths and written acceptance, without getting in people's way.", "rules": ["strict_moves", "accept_before_start"]},
    "strict": {"about": "The full discipline: paths, one holder, claims, an independent check, everything under a story.", "rules": list(RULES)},
}
RIGOR_RANK = {"": 0, "loose": 0, "standard": 1, "strict": 2}


def policy_clean(v):
    if isinstance(v, str):
        try:
            v = json.loads(v) if v.strip() else {}
        except ValueError:
            raise ApiError("bad", 400, "pol")
    if not isinstance(v, dict):
        raise ApiError("bad", 400, "pol")
    out = {}
    if v.get("preset") not in (None, ""):
        if v["preset"] not in PRESETS and v["preset"] != "custom":
            raise ApiError("bad", 400, "preset")
        out["preset"] = v["preset"]
    ov = {k: bool(x) for k, x in (v.get("rules") or {}).items() if k in RULES} if isinstance(v.get("rules"), dict) else {}
    if ov:
        out["rules"] = ov
    pl = v.get("pipeline")
    if pl not in (None, "", []):
        out["pipeline"] = json.loads(pipeline_clean(pl))
        types = v.get("pipeline_types") if isinstance(v.get("pipeline_types"), list) else ["story"]
        out["pipeline_types"] = [x for x in types if x in TASK_TYPES] or ["story"]
    return out


def board_policy(bid):
    return (board_cfg(bid)[1] or {}).get("policy") or {}


def board_rules(bid):
    """The rules a board applies: its preset (or the global default), then its own switches on top. `custom` starts from nothing."""
    pol = board_policy(bid)
    preset = pol.get("preset") or get_setting("policy_default", "") or "loose"
    rules = set() if preset == "custom" else set(PRESETS.get(preset, PRESETS["loose"])["rules"])
    for k, on in (pol.get("rules") or {}).items():
        (rules.add if on else rules.discard)(k)
    return rules


def task_rules(t):
    """Board rules, plus whatever the task's own rigor adds (a task can ask for more discipline, never less), plus the older global switches."""
    rules = board_rules(t["board_id"]) | set(PRESETS.get(t["rigor"] or "", {"rules": []})["rules"])
    for k in ("strict_moves", "holder_only"):
        if get_setting(k, "0") == "1":
            rules.add(k)
    return rules


def policy_view(q):
    bid, tid = qint(q, "b"), qint(q, "task")
    out = {"presets": {k: {"about": v["about"], "rules": v["rules"]} for k, v in PRESETS.items()}, "rules": RULES, "default": get_setting("policy_default", "") or "loose"}
    t = None
    if tid:
        r = rows("SELECT * FROM tasks WHERE id=?", (tid,))
        if not r:
            raise ApiError("nf", 404)
        t, bid = r[0], r[0]["board_id"]
    if bid:
        pol = board_policy(bid)
        out["board"] = {"id": bid, "preset": pol.get("preset") or out["default"], "rules": sorted(board_rules(bid)), "overrides": pol.get("rules") or {},
                        **({"pipeline": pol["pipeline"], "pipeline_types": pol.get("pipeline_types")} if pol.get("pipeline") else {})}
    if t:
        out["task"] = {"id": tid, "rigor": t["rigor"] or None, "rules": sorted(task_rules(t))}
    return out


def board_policy_set(actor, role, bid, body):
    """Owner, admin or supervisor chooses how strict a board is."""
    if not _priv(role):
        raise ApiError("forbidden", 403)
    with lock:
        r = rows("SELECT method, cfg FROM boards WHERE id=?", (bid,))
        if not r:
            raise ApiError("nf", 404)
        try:
            cfg = json.loads(r[0]["cfg"] or "{}")
        except ValueError:
            cfg = {}
        pol = policy_clean(body)
        if pol:
            cfg["policy"] = pol
        else:
            cfg.pop("policy", None)
        db.execute("UPDATE boards SET cfg=? WHERE id=?", (json.dumps(cfg, separators=(",", ":")), bid))
        emit(actor, "board.policy", bid, bid, {"pol": pol.get("preset") or "default"})
    return {"id": bid, "policy": pol}


def check_start(t, new_status, claiming=False):
    """Rules that decide whether work may start (status becomes in progress)."""
    if new_status != "in_progress" or t["status"] == "in_progress":
        return
    rules = task_rules(t)
    if "accept_before_start" in rules and (t["type"] or "task") in ("story", "task", "bug") and not t["accept"]:
        raise ApiError("accept", 409, "ac")
    if "claim_to_start" in rules and not claiming and not (t["claimed_by"] and t["claim_until"] > now_s()):
        raise ApiError("claim", 409, "cb")


def check_holder(actor, role, tid):
    """With the setting holder_only=1, a task that someone currently holds (a live claim) can only be changed by that holder, a supervisor or an admin."""
    if _priv(role):
        return
    r = rows("SELECT * FROM tasks WHERE id=?", (tid,))
    if r and "holder_only" in task_rules(r[0]) and r[0]["claimed_by"] and r[0]["claim_until"] > now_s() and r[0]["claimed_by"].lower() != (actor or "").lower():
        raise ApiError("holder", 403, r[0]["claimed_by"])


def task_verify(actor, role, tid, body):
    """Independent acceptance: someone other than the worker checks the work against the card's acceptance criteria.
    ok=true records who and when; ok=false sends it back with the reason. The worker cannot verify their own work."""
    t = rows("SELECT * FROM tasks WHERE id=?", (tid,))
    if not t:
        raise ApiError("nf", 404)
    t = t[0]
    me = " ".join(str(body.get("as") or actor or "").split())[:60]      # a person using the web page is named by their token (admin, human)
    if me.lower() in ("", "agent", "anon"):
        raise ApiError("missing", 400, "as")
    if t["status"] not in ("review", "done"):
        raise ApiError("bad", 400, "st")
    if me.lower() in {(t["assignee"] or "").lower(), (t["claimed_by"] or "").lower()} - {""}:
        raise ApiError("independent", 403, "as")
    note = " ".join(str(body.get("note") or body.get("n") or "").split())[:500]
    ok = body.get("ok", True) not in (False, 0, "0", "false", "no")
    if not ok and not note:
        raise ApiError("missing", 400, "note")
    with lock:
        if ok:
            db.execute("UPDATE tasks SET verified_by=?, verified_at=?, verified_note=?, updated=CURRENT_TIMESTAMP, version=COALESCE(version,1)+1 WHERE id=?", (me, now_s(), note, tid))
            emit(actor, "task.verified", tid, t["board_id"], {"vb": me, "va": now_s(), "vn": note})
        else:
            db.execute("UPDATE tasks SET verified_by='', verified_at='', verified_note='', status='in_progress', updated=CURRENT_TIMESTAMP, version=COALESCE(version,1)+1 WHERE id=?", (tid,))
            emit(actor, "task.updated", tid, t["board_id"], {"st": "in_progress", "vb": "", "vn": note})
            notify(t["assignee"], "review.changes", me, tid, t["board_id"], "#%d '%s': verification failed by %s: %s" % (tid, t["title"][:60], me, note), {"st": "in_progress"})
    comment_add(actor, tid, {"c": ("Verified" if ok else "Verification failed") + (": " + note if note else ""), "as": body.get("as")}, notify_assignee=False)
    return {"id": tid, "verified": ok, "by": me}


def _norm(v):
    if v is None or v == [] or v == "":
        return ""
    if isinstance(v, (list, tuple)):
        return ",".join(str(x) for x in v)
    return str(v).strip()


def task_history(tid):
    """Replay the task's event log and compare it with the stored card. in_sync = every replayed field matches and nothing on the card is unexplained;
    partial = the card has values the log never recorded (older tasks); drift = the card disagrees with its own history."""
    r = rows("SELECT * FROM tasks WHERE id=?", (tid,))
    if not r:
        raise ApiError("nf", 404)
    cur = cdict("tasks", r[0], False)
    fold, n = {}, 0
    for e in rows("SELECT ts, actor, type, payload FROM events WHERE type LIKE 'task.%' AND payload LIKE ? ORDER BY id", ('{"id":%d,"b":%%' % tid,)):
        try:
            d = json.loads(e["payload"]).get("d") or {}
        except ValueError:
            continue
        n += 1
        fold.update({k: v for k, v in d.items() if k not in STATE_SKIP and k in TASK_KEYS})
    diff = [{"k": k, "log": fold[k], "card": cur.get(k)} for k in fold if _norm(fold[k]) != _norm(cur.get(k))]
    unknown = sorted(k for k, v in cur.items() if k not in STATE_SKIP and k not in fold and _norm(v) not in ("", "0", "task", "3", "1"))
    return {"id": tid, "events": n, "state": "drift" if diff else "partial" if unknown else "in_sync", "diff": diff, "unknown": unknown}


# ---- strict state moves (setting strict_moves=1)
MOVES = {"backlog": {"todo", "in_progress", "declined"}, "todo": {"backlog", "in_progress", "blocked", "declined"},
         "in_progress": {"todo", "review", "blocked", "done", "declined"}, "review": {"in_progress", "done", "blocked", "declined"},
         "blocked": {"todo", "in_progress", "declined"}, "done": {"in_progress", "todo"}, "declined": {"backlog", "todo", "in_progress"}}


def check_move(role, tid, body):
    """With strict_moves=1 a task only moves along MOVES. A supervisor or admin can override with force=1."""
    new = body.get("st") or body.get("status")
    if not new:
        return
    r = rows("SELECT * FROM tasks WHERE id=?", (tid,))
    if not r or r[0]["status"] == new or new not in MOVES or "strict_moves" not in task_rules(r[0]):
        return
    if new not in MOVES.get(r[0]["status"], set()) and not (_priv(role) and body.get("force")):
        raise ApiError("move", 409, "%s>%s" % (r[0]["status"], new))


# ---- pipelines: ordered stages, each with a holder and a gate (auto = holder passes it on, review = holder can also send it back, human = a person approves)
GATES = ("auto", "review", "human")


def pipeline_clean(v):
    if isinstance(v, str):
        try:
            v = json.loads(v) if v.strip().startswith("[") else [dict(zip(("name", "holder", "gate"), [x.strip() for x in ln.split("|")])) for ln in v.splitlines() if ln.strip()]
        except ValueError:
            raise ApiError("bad", 400, "pl")
    if v in (None, "", []):
        return ""
    if not isinstance(v, list) or len(v) > 10:
        raise ApiError("bad", 400, "pl")
    out = []
    for s in v:
        if not isinstance(s, dict):
            raise ApiError("bad", 400, "pl")
        name, holder, gate = " ".join(str(s.get("name") or "").split())[:40], " ".join(str(s.get("holder") or "").split())[:60], str(s.get("gate") or "auto").strip().lower() or "auto"
        if not name or not holder or gate not in GATES:
            raise ApiError("bad", 400, "pl")
        out.append({"name": name, "holder": holder, "gate": gate})
    return json.dumps(out, separators=(",", ":"))


def _stages(t):
    try:
        return json.loads(t["pipeline"]) if t["pipeline"] else []
    except ValueError:
        return []


def _stage_enter(actor, t, idx, why):
    """Put the baton in stage `idx` (or finish the task past the last one). Caller holds `lock`."""
    tid, bid, ss = t["id"], t["board_id"], _stages(t)
    if idx >= len(ss):
        check_done_gate(tid, bid, t["dod_done"])
        db.execute("UPDATE tasks SET status='done', stage=?, updated=CURRENT_TIMESTAMP, version=COALESCE(version,1)+1 WHERE id=?", (len(ss) + 1, tid))
        emit(actor, "task.updated", tid, bid, {"st": "done", "sg": len(ss) + 1})
        return {"id": tid, "st": "done", "sg": len(ss) + 1}
    s = ss[idx]
    st = "review" if s["gate"] == "human" else ("todo" if idx > (t["stage"] or 0) - 1 else "in_progress")
    db.execute("UPDATE tasks SET assignee=?, status=?, stage=?, claimed_by='', claim_until='', nudge='', updated=CURRENT_TIMESTAMP, version=COALESCE(version,1)+1 WHERE id=?", (s["holder"], st, idx + 1, tid))
    emit(actor, "task.updated", tid, bid, {"as": s["holder"], "st": st, "sg": idx + 1})
    db.execute("INSERT INTO comments(task_id,actor,body) VALUES(?,?,?)", (tid, actor, "Stage %d/%d \"%s\" -> %s%s" % (idx + 1, len(ss), s["name"], s["holder"], (": " + why) if why else "")))
    if s["gate"] == "human":
        db.execute("INSERT INTO reviews(task_id, requester, reviewer, kind, note) VALUES(?,?,?,?,?)", (tid, actor, s["holder"], "gate", ("stage \"%s\" needs your decision. %s" % (s["name"], why)).strip()[:300]))
        notify(s["holder"], "approval.requested", actor, tid, bid, "#%d '%s': stage \"%s\" needs your approval" % (tid, t["title"][:60], s["name"]), {"st": st})
    else:
        notify(s["holder"], "task.handoff", actor, tid, bid, "#%d '%s': stage \"%s\" is yours%s" % (tid, t["title"][:60], s["name"], (" - " + why[:120]) if why else ""), {"st": st})
    return {"id": tid, "st": st, "sg": idx + 1, "as": s["holder"]}


def task_stage(actor, role, tid, body):
    """start: begin the pipeline at stage 1. done: the holder passes the baton on. reject: send it back one stage (needs a reason; allowed on review and human stages)."""
    action = str(body.get("action") or body.get("a") or "").lower()
    note = " ".join(str(body.get("note") or "").split())[:300]
    with lock:
        r = rows("SELECT * FROM tasks WHERE id=?", (tid,))
        if not r:
            raise ApiError("nf", 404)
        t = r[0]
        ss = _stages(t)
        if not ss:
            raise ApiError("bad", 400, "pl")
        if action == "start":
            if t["status"] not in ("backlog", "todo") or (t["stage"] or 0):
                raise ApiError("bad", 400, "st")
            return _stage_enter(actor, t, 0, note)
        idx = (t["stage"] or 0) - 1
        if idx < 0 or idx >= len(ss) or t["status"] in ("done", "declined"):
            raise ApiError("bad", 400, "st")
        s = ss[idx]
        me = " ".join(str(body.get("as") or actor or "").split())
        if me.lower() != (t["assignee"] or "").lower() and not _priv(role):
            raise ApiError("holder", 403, t["assignee"] or "")
        if s["gate"] == "human":
            raise ApiError("gate", 409, "approval")            # a human gate only moves on an approval decision
        if action == "done":
            return _stage_enter(actor, t, idx + 1, note)
        if action == "reject":
            if s["gate"] != "review" or idx == 0 or not note:
                raise ApiError("bad", 400, "note" if s["gate"] == "review" and idx else "pl")
            return _stage_enter(actor, dict(t, status="in_progress"), idx - 1, note)
    raise ApiError("bad", 400, "action")


def _gate_decided(actor, t, approved, note):
    """A human gate was decided: approve = the baton moves on, changes = it goes back one stage. Caller holds `lock`."""
    idx = (t["stage"] or 0) - 1
    return _stage_enter(actor, t, idx + 1, note) if approved else _stage_enter(actor, dict(t, status="in_progress"), max(0, idx - 1), note or "sent back")


def task_decline(actor, tid, body):
    """Refuse a task with a written reason. Anyone may decline (human or agent); the reason is logged as a comment
    and `to` (agent names, e.g. the requester) is notified in addition to the assignee."""
    reason = " ".join(str(body.get("r") or body.get("reason") or body.get("dr") or "").split())[:500]
    if not reason:
        raise ApiError("missing", 400, "r")
    t = rows("SELECT status, assignee, title, board_id FROM tasks WHERE id=?", (tid,))
    if not t:
        raise ApiError("nf", 404)
    if t[0]["status"] == "declined":
        raise ApiError("bad", 400, "st")
    crud(actor, "PATCH", "tasks", tid, {"st": "declined", "dr": reason}, {}, False)
    with lock:
        escalate("declined", actor, tid, t[0]["board_id"], "#%d '%s' declined by %s: %s" % (tid, t[0]["title"][:60], actor, reason[:140]), skip=(t[0]["assignee"],))
    comment_add(actor, tid, {"c": "Declined: " + reason, "to": body.get("to") or []}, notify_assignee=False)
    return {"id": tid, "st": "declined", "dr": reason}


def task_reopen(actor, tid, body):
    """Put a declined/blocked/done task back in flight (default todo); clears decline reason and blockers."""
    to = str(body.get("to") or body.get("st") or "todo")
    if to not in REOPEN_TO:
        raise ApiError("bad", 400, "to")
    t = rows("SELECT status FROM tasks WHERE id=?", (tid,))
    if not t:
        raise ApiError("nf", 404)
    if t[0]["status"] not in REOPEN_FROM:
        raise ApiError("bad", 400, "st")
    crud(actor, "PATCH", "tasks", tid, {"st": to, "bl": "", "bb": []}, {}, False)
    note = " ".join(str(body.get("note") or body.get("r") or "").split())[:500]
    comment_add(actor, tid, {"c": "Reopened -> %s" % to + (": " + note if note else ""), "to": body.get("to_notify") or body.get("notify") or []}, notify_assignee=False)
    return {"id": tid, "st": to}


# ---------------------------------------------------------------- shared technical specs
SPEC_STATUS = ("draft", "review", "approved", "deprecated")
SPEC_MAX = 100000


def _sp_tags(v):
    ls = v if isinstance(v, list) else str(v or "").split(",")
    return ",".join(sorted({re.sub(r"[^a-z0-9._/-]", "", x.strip().lower()) for x in ls if x.strip()} - {""}))[:200]


def _sp_tasks(v):
    ids = v if isinstance(v, list) else re.findall(r"\d+", str(v or ""))
    return ",".join(str(i) for i in sorted({int(x) for x in ids}))[:200]


def spec_out(r, body=True, q=None):
    o = {"id": r["id"], "b": r["board_id"], "t": r["title"], "tg": r["tags"], "st": r["status"], "au": r["author"],
         "by": r["updated_by"], "rv": r["rev"], "tk": r["tasks"], "u": r["updated"]}
    if body:
        o["d"] = r["body"]
    elif q:
        i = max(r["body"].lower().find(q.lower()), 0)
        o["sn"] = " ".join(r["body"][max(i - 40, 0):i + 140].split())
    return {k: v for k, v in o.items() if v not in (None, "")}


def specs_list(q):
    wh, ar = [], []
    if _qv(q, "q"):
        like = "%" + _qv(q, "q") + "%"
        wh.append("(title LIKE ? OR body LIKE ? OR tags LIKE ?)")
        ar += [like, like, like]
    if _qv(q, "tag"):
        wh.append("(',' || tags || ',') LIKE ?")
        ar.append("%," + _qv(q, "tag").strip().lower() + ",%")
    if _qv(q, "st", "status"):
        wh.append("status=?")
        ar.append(_qv(q, "st", "status"))
    if _qv(q, "b", "board_id"):
        wh.append("(board_id=? OR board_id IS NULL)")
        ar.append(_qv(q, "b", "board_id"))
    if _qv(q, "tk"):
        wh.append("(',' || tasks || ',') LIKE ?")
        ar.append("%," + str(int(_qv(q, "tk"))) + ",%")
    w = (" WHERE " + " AND ".join(wh)) if wh else ""
    lim = max(1, min(qint(q, "limit", 100), 300))
    rs = rows("SELECT * FROM specs" + w + " ORDER BY updated DESC, id DESC LIMIT ?", ar + [lim])
    full = q.get("d", ["0"])[0] not in ("0", "")
    return {"n": len(rs), "i": [spec_out(r, full, _qv(q, "q")) for r in rs]}


def spec_get(sid, rev=None):
    r = rows("SELECT * FROM specs WHERE id=?", (sid,))
    if not r:
        raise ApiError("nf", 404)
    o = spec_out(r[0])
    if rev is not None:
        h = rows("SELECT * FROM spec_revs WHERE spec_id=? AND rev=?", (sid, rev))
        if not h:
            raise ApiError("nf", 404)
        o.update({"t": h[0]["title"], "d": h[0]["body"], "rv": rev, "by": h[0]["actor"], "u": h[0]["ts"], "note": h[0]["note"]})
    return o


def spec_revs(sid):
    if not rows("SELECT id FROM specs WHERE id=?", (sid,)):
        raise ApiError("nf", 404)
    rs = rows("SELECT rev, actor, note, ts, length(body) n FROM spec_revs WHERE spec_id=? ORDER BY rev DESC", (sid,))
    return {"n": len(rs), "i": [{"rv": r["rev"], "by": r["actor"], "note": r["note"], "u": r["ts"], "len": r["n"]} for r in rs]}


def _spec_notify(actor, sp, verb, extra_to, rev):
    who = set(x.strip() for x in (extra_to if isinstance(extra_to, list) else [extra_to]) if x and str(x).strip())
    for tid in re.findall(r"\d+", sp["tasks"] or ""):
        for r in rows("SELECT assignee FROM tasks WHERE id=? AND assignee!=''", (int(tid),)):
            who.add(r["assignee"])
    who |= _watchers([("spec", sp["id"])] + [("tag", x) for x in (sp["tags"] or "").split(",") if x], skip=(actor,))
    for a in who:
        notify(a, "spec." + verb, actor, None, sp["board_id"], "spec #%d '%s' %s by %s (rev %d)" % (sp["id"], sp["title"][:70], verb, actor, rev), {"spec": sp["id"], "rv": rev})


def spec_create(actor, body):
    title = " ".join(str(body.get("t") or body.get("title") or "").split())[:200]
    if not title:
        raise ApiError("missing", 400, "t")
    text = str(body.get("d") or body.get("body") or "")[:SPEC_MAX]
    st = body.get("st") or body.get("status") or "draft"
    if st not in SPEC_STATUS:
        raise ApiError("bad", 400, "st")
    bid = body.get("b") if "b" in body else body.get("board_id")
    if bid not in (None, ""):
        try:
            bid = int(bid)
        except Exception:
            raise ApiError("bad", 400, "b")
        if not rows("SELECT id FROM boards WHERE id=?", (bid,)):
            raise ApiError("bad", 400, "b")
    else:
        bid = None
    with lock:
        cur = db.execute("INSERT INTO specs(board_id,title,body,tags,status,author,updated_by,tasks) VALUES(?,?,?,?,?,?,?,?)",
                         (bid, title, text, _sp_tags(body.get("tg", body.get("tags"))), st, actor, actor, _sp_tasks(body.get("tk", body.get("tasks")))))
        sid = cur.lastrowid
        db.execute("INSERT INTO spec_revs(spec_id,rev,title,body,actor,note) VALUES(?,?,?,?,?,?)", (sid, 1, title, text, actor, str(body.get("note") or "")[:300]))
        sp = rows("SELECT * FROM specs WHERE id=?", (sid,))[0]
        emit(actor, "spec.created", sid, bid, {"t": title})
        _spec_notify(actor, sp, "created", body.get("to") or [], 1)
    return spec_out(sp, False)


def spec_update(actor, sid, body):
    with lock:
        r = rows("SELECT * FROM specs WHERE id=?", (sid,))
        if not r:
            raise ApiError("nf", 404)
        r = r[0]
        if body.get("base") not in (None, "") and int(body["base"]) != r["rev"]:
            raise ApiError("conflict", 409, "rv")
        sets, args = {}, {}
        title = " ".join(str(body["t"] if "t" in body else body.get("title", r["title"])).split())[:200]
        if not title:
            raise ApiError("bad", 400, "t")
        text = r["body"]
        if "d" in body or "body" in body:
            text = str(body.get("d", body.get("body")) or "")
        if body.get("append"):
            text = (text.rstrip() + "\n\n" + str(body["append"]).strip()) if text.strip() else str(body["append"]).strip()
        text = text[:SPEC_MAX]
        st = body.get("st") or body.get("status") or r["status"]
        if st not in SPEC_STATUS:
            raise ApiError("bad", 400, "st")
        tags = _sp_tags(body["tg"] if "tg" in body else body["tags"] if "tags" in body else r["tags"])
        tasks = _sp_tasks(body["tk"] if "tk" in body else body["tasks"] if "tasks" in body else r["tasks"])
        bid = r["board_id"]
        if "b" in body or "board_id" in body:
            v = body.get("b", body.get("board_id"))
            bid = int(v) if v not in (None, "", 0) else None
        content = (title != r["title"]) or (text != r["body"])
        rev = r["rev"] + (1 if content else 0)
        if not content and (st, tags, tasks, bid) == (r["status"], r["tags"], r["tasks"], r["board_id"]):
            raise ApiError("empty", 400)
        db.execute("UPDATE specs SET board_id=?, title=?, body=?, tags=?, status=?, tasks=?, rev=?, updated_by=?, updated=CURRENT_TIMESTAMP WHERE id=?",
                   (bid, title, text, tags, st, tasks, rev, actor, sid))
        if content:
            db.execute("INSERT INTO spec_revs(spec_id,rev,title,body,actor,note) VALUES(?,?,?,?,?,?)", (sid, rev, title, text, actor, str(body.get("note") or "")[:300]))
        sp = rows("SELECT * FROM specs WHERE id=?", (sid,))[0]
        emit(actor, "spec.updated", sid, bid, {"rv": rev, "st": st})
        _spec_notify(actor, sp, "updated" if content else "changed", body.get("to") or [], rev)
    return spec_out(sp, False)


def spec_delete(actor, sid):
    with lock:
        r = rows("SELECT board_id FROM specs WHERE id=?", (sid,))
        if not r:
            raise ApiError("nf", 404)
        db.execute("DELETE FROM specs WHERE id=?", (sid,))
        emit(actor, "spec.deleted", sid, r[0]["board_id"], None)
    return {"id": sid, "x": 1}


# ---------------------------------------------------------------- dependencies, handoff, activity, supervisor
def open_dep_ids(dep_csv):
    ids = [int(x) for x in re.findall(r"\d+", str(dep_csv or ""))]
    if not ids:
        return []
    return [r["id"] for r in rows("SELECT id FROM tasks WHERE id IN (%s) AND status NOT IN ('done','declined')" % ",".join("?" * len(ids)), ids)]


def check_deps(tid, dep_csv):
    ids = [int(x) for x in re.findall(r"\d+", str(dep_csv or ""))]
    if tid and tid in ids:
        raise ApiError("bad", 400, "dp")
    if ids and len(rows("SELECT id FROM tasks WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)) != len(set(ids)):
        raise ApiError("bad", 400, "dp")
    if tid:                                   # cycle: can any dependency (transitively) depend on tid?
        seen, stack = set(), list(ids)
        while stack:
            i = stack.pop()
            if i == tid:
                raise ApiError("cycle", 400, "dp")
            if i in seen:
                continue
            seen.add(i)
            r = rows("SELECT depends FROM tasks WHERE id=?", (i,))
            if r:
                stack += [int(x) for x in re.findall(r"\d+", r[0]["depends"] or "")]


LEAF_TYPES = ("task", "bug", "spike", "chore")


def check_hier(tid, ctype, parent, bid):
    """Work nests as epic > story > task, bug, spike or chore. An epic has no parent, a story sits under an epic, a task sits under a story or an epic,
    and only epics and stories can have children."""
    if ctype == "epic" and parent:
        raise ApiError("bad", 400, "pa")
    if parent:
        r = rows("SELECT id, board_id, type, parent FROM tasks WHERE id=?", (parent,))
        if not r or r[0]["board_id"] != bid or (tid and parent == tid) or r[0]["type"] not in ("epic", "story"):
            raise ApiError("bad", 400, "pa")
        if (r[0]["type"] == "epic" and ctype == "epic") or (r[0]["type"] == "story" and ctype not in LEAF_TYPES):
            raise ApiError("bad", 400, "pa")
        up, seen = r[0]["parent"], set()
        while up and up not in seen:                          # never under its own descendant
            if up == tid:
                raise ApiError("bad", 400, "pa")
            seen.add(up)
            nx = rows("SELECT parent FROM tasks WHERE id=?", (up,))
            up = nx[0]["parent"] if nx else None
    if tid and ctype not in ("epic", "story") and rows("SELECT id FROM tasks WHERE parent=? LIMIT 1", (tid,)):
        raise ApiError("bad", 400, "ty")                      # it still has children, so it must stay an epic or a story


def descendants(tid):
    """Every task below `tid`, at any depth: [(id, type, status, parent)]."""
    out, todo, seen = [], [tid], {tid}
    while todo:
        for r in rows("SELECT id, type, status, parent FROM tasks WHERE parent=? ORDER BY id", (todo.pop(),)):
            if r["id"] not in seen:
                seen.add(r["id"])
                out.append((r["id"], r["type"], r["status"], r["parent"]))
                todo.append(r["id"])
    return out


def rollup(tid):
    """[done, total] over the work items below an epic or a story: the ones with nothing under them (declined work is not counted)."""
    d = descendants(tid)
    parents = {x[3] for x in d}
    leaves = [x for x in d if x[0] not in parents and x[2] != "declined"]
    return [sum(1 for x in leaves if x[2] == "done"), len(leaves)]


def parent_ready(actor, tid):
    """A task finished: when everything under a story or epic is done, say so on it and tell its assignee. Caller holds `lock`."""
    seen = set()
    up = rows("SELECT parent FROM tasks WHERE id=?", (tid,))
    pid = up[0]["parent"] if up else None
    while pid and pid not in seen:
        seen.add(pid)
        p = rows("SELECT * FROM tasks WHERE id=?", (pid,))
        if not p:
            break
        p = p[0]
        if p["status"] not in ("done", "declined"):
            done, total = rollup(pid)
            if total and done == total:
                msg = "All %d items under this are done - ready to review and close." % total
                last = rows("SELECT body FROM comments WHERE task_id=? ORDER BY id DESC LIMIT 1", (pid,))
                if not last or last[0]["body"] != msg:
                    cur = db.execute("INSERT INTO comments(task_id,actor,body) VALUES(?,?,?)", (pid, "system", msg))
                    emit("system", "task.commented", pid, p["board_id"], {"cm_id": cur.lastrowid})
                    notify(p["assignee"], "task.ready", actor, pid, p["board_id"], "#%d '%s': %s" % (pid, p["title"][:60], msg), {"ready": 1})
        pid = p["parent"]


def deps_released(actor, tid):
    """A task finished: tell assignees of tasks that were waiting on it and now have no open dependency left."""
    for r in rows("SELECT * FROM tasks WHERE (',' || depends || ',') LIKE ? AND status NOT IN ('done','declined')", ("%," + str(tid) + ",%",)):
        if not open_dep_ids(r["depends"]):
            notify(r["assignee"], "task.ready", actor, r["id"], r["board_id"], "#%d '%s' is ready: all its dependencies are done" % (r["id"], r["title"][:70]), {"ready": 1})


def task_handoff(actor, tid, body):
    """Reassign with a structured summary: what was done + what is next. One notification (task.handoff) to the receiver."""
    to = " ".join(str(body.get("to") or "").split())[:60]
    summary = str(body.get("summary") or body.get("m") or "").strip()[:2000]
    nxt = str(body.get("next") or "").strip()[:1000]
    if not to:
        raise ApiError("missing", 400, "to")
    if not summary:
        raise ApiError("missing", 400, "summary")
    with lock:
        r = rows("SELECT * FROM tasks WHERE id=?", (tid,))
        if not r:
            raise ApiError("nf", 404)
        t = r[0]
        desc = t["description"]
        if is_owner_assignee(to) and not has_owner_steps(desc):
            combo = summary + "\n" + nxt
            if not has_owner_steps(combo):
                raise ApiError("owner_fmt", 400, "d")
            desc = (desc + "\n\n" + combo).strip()
        st = body.get("st") or ("todo" if t["status"] == "backlog" else t["status"])
        if st not in STATUSES:
            raise ApiError("bad", 400, "st")
        clear = st != "blocked"
        db.execute("UPDATE tasks SET assignee=?, description=?, status=?, nudge='', updated=CURRENT_TIMESTAMP" + (", blocker='', blocked_by=''" if clear else "") + " WHERE id=?",
                   (to, desc, st, tid))
        play = " ".join((summary + ((" Next: " + nxt) if nxt else "")).split())[:600]
        db.execute("UPDATE tasks SET play=? WHERE id=?", (play, tid))
        db.execute("INSERT INTO comments(task_id,actor,body) VALUES(?,?,?)",
                   (tid, actor, "Handoff %s -> %s\\n%s%s" % (t["assignee"] or "-", to, summary, ("\\nNext: " + nxt) if nxt else "")))
        delta = {"as": to, "po": play, **({"st": st} if st != t["status"] else {})}
        emit(actor, "task.updated", tid, t["board_id"], delta)
        notify(to, "task.handoff", actor, tid, t["board_id"],
               "#%d '%s' handed to you by %s: %s%s" % (tid, t["title"][:60], actor, summary[:160], (" | next: " + nxt[:120]) if nxt else ""), {"st": st})
        if t["assignee"] and t["assignee"].lower() not in (to.lower(), actor.lower()):
            notify(t["assignee"], "task.unassigned", actor, tid, t["board_id"], "#%d '%s' was handed to %s by %s" % (tid, t["title"][:60], to, actor), delta)
    return {"id": tid, "as": to, "st": st}


_ACT_LABEL = {"vb": "verified by", "ac": "acceptance criteria", "sg": "stage", "pl": "pipeline", "st": "status", "as": "assignee", "pr": "priority", "s": "sprint", "bl": "blocker", "bb": "blocked by", "dr": "decline reason", "pu": "PR", "sha": "deployed sha", "dp": "depends on", "pt": "points", "t": "title", "lb": "labels", "ch": "channel", "k": "kind"}


NODE_KEYS = ("machine", "version", "agents", "running", "caps", "dispatch", "quota", "up", "taskboard", "worktrees", "note")


def node_report(actor, role, body):
    """A machine that runs agents (a bridge) tells the board it is alive and what it is doing. One row per machine, replaced on every report."""
    if role == "viewer":
        raise ApiError("forbidden", 403)
    name = " ".join(str(body.get("name") or body.get("n") or "").split())[:60]
    if not name:
        raise ApiError("missing", 400, "name")
    data = {k: body[k] for k in NODE_KEYS if k in body}
    raw = json.dumps(data, separators=(",", ":"))
    if len(raw) > 20000:
        raise ApiError("too_big", 413)
    with lock:
        db.execute("INSERT INTO nodes(name, data, ts) VALUES(?,?,CURRENT_TIMESTAMP) ON CONFLICT(name) DO UPDATE SET data=excluded.data, ts=CURRENT_TIMESTAMP", (name, raw))
    return {"ok": 1, "n": name}


def nodes_list():
    out, now = [], time.time()
    for r in rows("SELECT * FROM nodes ORDER BY name"):
        age = int(max(0, now - _ts(r["ts"])))
        try:
            d = json.loads(r["data"])
        except ValueError:
            d = {}
        out.append({"n": r["name"], "age": age, "state": "up" if age <= 180 else "stale" if age <= 900 else "down", **d})
    return {"i": out}


def quota_view():
    """What stops an agent from taking work: its own quota hit, its daily limits, and what each machine reports about subscriptions."""
    day = time.strftime("%Y-%m-%d 00:00:00", time.gmtime())
    agents_out = []
    for a in rows("SELECT name FROM agents ORDER BY name"):
        n = a["name"]
        lim, q = agent_limits(n), quota_of(n)
        meters = []
        if lim.get("jobs"):
            meters.append({"k": "jobs", "used": db.execute("SELECT COUNT(*) FROM runs WHERE agent=? AND started>=?", (n, day)).fetchone()[0], "max": int(lim["jobs"])})
        if lim.get("usd"):
            meters.append({"k": "usd", "used": round(db.execute("SELECT COALESCE(SUM(usd),0) FROM costs WHERE agent=? AND ts>=?", (n, day)).fetchone()[0], 2), "max": float(lim["usd"])})
        if lim.get("secs"):
            meters.append({"k": "secs", "used": sum(_secs(r) for r in rows("SELECT started, ended FROM runs WHERE agent=? AND started>=?", (n, day))), "max": int(lim["secs"])})
        if meters or q:
            agents_out.append({"n": n, "m": meters, **({"q": q[0], "qn": q[1]} if q else {})})
    subs = []
    for nd in nodes_list()["i"]:
        for q in (nd.get("quota") or [])[:12]:
            if isinstance(q, dict):
                subs.append({"node": nd["n"], "age": nd["age"], **{k: q[k] for k in ("source", "label", "used_pct", "resets_at", "note") if k in q}})
    return {"agents": agents_out, "subs": subs}


def system_get(q, role):
    days = {1: 1, 7: 7, 30: 30}.get(qint(q, "days", 7) or 7, 7)
    since = time.strftime("%Y-%m-%d 00:00:00", time.gmtime(time.time() - days * 86400))
    per = lambda typ_like, extra="": {r["d"]: r["n"] for r in rows("SELECT substr(ts,1,10) d, COUNT(*) n FROM events WHERE ts>=? AND type LIKE ? " + extra + " GROUP BY d", (since, typ_like))}
    done_d, made_d = per("task.%", "AND payload LIKE '%\"st\":\"done\"%' AND type!='task.created'"), per("task.created")
    series = []
    for i in range(days - 1, -1, -1):
        d = time.strftime("%Y-%m-%d", time.gmtime(time.time() - i * 86400))
        series.append({"d": d, "done": done_d.get(d, 0), "new": made_d.get(d, 0)})
    runs = rows("SELECT agent, status, usd, tokens, started, ended FROM runs WHERE started>=?", (since,))
    by = {}
    for r in runs:
        e = by.setdefault(r["agent"], {"ag": r["agent"], "n": 0, "ok": 0, "bad": 0, "usd": 0.0, "secs": 0})
        e["n"] += 1
        e["ok"] += r["status"] == "ok"
        e["bad"] += r["status"] in ("failed", "timeout", "lost")
        e["usd"] = round(e["usd"] + (r["usd"] or 0), 4)
        e["secs"] += _secs(r)
    with_cost = sum(1 for r in runs if (r["usd"] or 0) > 0 or (r["tokens"] or 0) > 0)
    ac = rows("SELECT COUNT(*) n, SUM(verified_by!='') v FROM tasks WHERE accept!='' AND status='done'")[0]
    now = time.time()
    rec = [{"by": r["actor"], "last": r["m"], "age": int(max(0, now - _ts(r["m"])))} for r in rows("SELECT actor, MAX(ts) m FROM events WHERE actor NOT IN ('system','wake','nudge-cron','recurring','git','cost') AND actor!='' GROUP BY actor ORDER BY m DESC LIMIT 15")]
    long_c = db.execute("SELECT COUNT(*) FROM comments WHERE ts>=? AND LENGTH(body)>1500", (since,)).fetchone()[0]
    out = {"days": days, "nodes": nodes_list()["i"], "quota": quota_view(), "series": series, "spend": usage_history(days), "long_comments": long_c,
           "runs": {"total": len(runs), "by": sorted(by.values(), key=lambda e: -e["n"]), "with_cost": with_cost},
           "accept": {"done_with_criteria": ac["n"] or 0, "verified": int(ac["v"] or 0)}, "receipts": rec}
    if _priv(role):
        c = audit_verify(role)
        out["chain"] = {"ok": c["ok"], "n": c["n"], **({"broken": c["broken"]} if not c["ok"] else {})}
    return out


def _ids(csv_):
    return [int(x) for x in re.findall(r"\d+", csv_ or "")]


def task_timeline(tid):
    """Everything that happened on one task, in order, with where each fact came from (`recorded` = the board saw it happen,
    `reported` = someone said so), plus who ran it when (lanes) and how it relates to other tasks (graph)."""
    r = rows("SELECT * FROM tasks WHERE id=?", (tid,))
    if not r:
        raise ApiError("nf", 404)
    t = r[0]
    items = [dict(x, src="reported" if x["ty"] == "comment" else "recorded") for x in task_activity(tid)["i"]]
    runs = rows("SELECT * FROM runs WHERE task_id=? ORDER BY id", (tid,))
    for x in runs:
        items.append({"ts": x["started"], "by": x["agent"], "ty": "run", "src": "reported", "run": x["id"],
                      "m": "started a %s run%s" % (x["runtime"] or "agent", " (job %s)" % x["job"] if x["job"] else "")})
        if x["ended"]:
            bits = [x["status"]] + (["$%.2f" % x["usd"]] if x["usd"] else []) + (["%d tokens" % x["tokens"]] if x["tokens"] else [])
            items.append({"ts": x["ended"], "by": x["agent"], "ty": "run", "src": "reported", "run": x["id"],
                          "m": "run ended: " + ", ".join(bits) + (" - " + x["outcome"][:160] if x["outcome"] else "")})
    for v in rows("SELECT * FROM reviews WHERE task_id=? ORDER BY id", (tid,)):
        kind = "approval" if v["kind"] == "approval" else "review"
        items.append({"ts": v["created"], "by": v["requester"], "ty": "review", "src": "recorded", "m": "asked %s for %s%s" % (v["reviewer"], kind, ": " + v["note"][:140] if v["note"] else "")})
        if v["decided"]:
            items.append({"ts": v["decided"], "by": v["reviewer"], "ty": "review", "src": "reported", "m": "%s: %s%s" % (kind, v["status"], " - " + v["decision_note"][:140] if v["decision_note"] else "")})
    for a in rows("SELECT * FROM asks WHERE task_id=? ORDER BY id", (tid,)):
        items.append({"ts": a["created"], "by": a["from_agent"], "ty": "ask", "src": "reported", "m": "asked %s: %s" % (a["to_agent"], a["question"][:160])})
        if a["answered"]:
            items.append({"ts": a["answered"], "by": a["to_agent"], "ty": "ask", "src": "reported", "m": "answered: " + a["answer"][:160]})
    for d_ in rows("SELECT * FROM decisions WHERE task_id=? ORDER BY id", (tid,)):
        items.append({"ts": d_["ts"], "by": d_["actor"], "ty": "decision", "src": "reported", "m": "decided: " + d_["body"]})
    for m in rows("SELECT * FROM messages WHERE task_id=? ORDER BY id", (tid,)):
        items.append({"ts": m["ts"], "by": m["sender"], "ty": "msg", "src": "reported", "m": "to %s: %s" % (m["recipient"], m["body"][:160])})
    items.sort(key=lambda x: x["ts"])
    batons, holder = [], ""
    for e in rows("SELECT ts, actor, payload FROM events WHERE type LIKE 'task.%' AND payload LIKE ? ORDER BY id", ('{"id":%d,"b":%%' % tid,)):
        try:
            d = json.loads(e["payload"]).get("d") or {}
        except ValueError:
            continue
        if "as" in d and (d["as"] or "") != holder:
            batons.append({"ts": e["ts"], "from": holder or None, "to": d["as"] or None, "by": e["actor"]})
            holder = d["as"] or ""
    lanes = {}
    for x in runs:
        lanes.setdefault(x["agent"], []).append({"id": x["id"], "rt": x["runtime"] or None, "from": x["started"], "to": x["ended"] or None, "st": x["status"], "job": x["job"] or None,
                                                  "usd": x["usd"] or None, "tk": x["tokens"] or None})
    nodes, edges = {}, []

    def node(i):
        if i not in nodes:
            n = rows("SELECT id, title, status, assignee FROM tasks WHERE id=?", (i,))
            nodes[i] = {"id": i, "t": n[0]["title"], "st": n[0]["status"], "as": n[0]["assignee"] or None} if n else {"id": i, "t": "#%d (gone)" % i, "st": "?"}
        return i
    node(tid)
    if t["parent"]:
        edges.append({"from": node(t["parent"]), "to": tid, "k": "delegation"})
    for c in rows("SELECT id FROM tasks WHERE parent=? ORDER BY id LIMIT 30", (tid,)):
        edges.append({"from": tid, "to": node(c["id"]), "k": "delegation"})
    for d_ in _ids(t["depends"]):
        edges.append({"from": node(d_), "to": tid, "k": "dependency"})
    for d_ in _ids(t["blocked_by"]):
        edges.append({"from": node(d_), "to": tid, "k": "blocks"})
    for c in rows("SELECT id FROM tasks WHERE (',' || depends || ',') LIKE ? ORDER BY id LIMIT 20", ("%," + str(tid) + ",%",)):
        edges.append({"from": tid, "to": node(c["id"]), "k": "dependency"})
    return {"task": {"id": tid, "t": t["title"], "st": t["status"], "as": t["assignee"] or None, "cb": t["claimed_by"] or None,
                     "ac": t["accept"].splitlines() if t["accept"] else [], "vb": t["verified_by"] or None, "va": t["verified_at"] or None, "vn": t["verified_note"] or None},
            "n": len(items), "i": items[-300:], "batons": batons, "lanes": [{"ag": k, "runs": v} for k, v in lanes.items()],
            "graph": {"nodes": list(nodes.values()), "edges": edges}}


def task_activity(tid):
    """One chronological timeline: field changes (from the event log) + comments."""
    if not rows("SELECT id FROM tasks WHERE id=?", (tid,)):
        raise ApiError("nf", 404)
    items = []
    for e in rows("SELECT id, ts, actor, type, payload FROM events WHERE type LIKE 'task.%' AND payload LIKE ? ORDER BY id", ('{"id":%d,"b":%%' % tid,)):
        if e["type"] == "task.commented":
            continue
        try:
            pl_ = json.loads(e["payload"])
            d, p_ = (pl_.get("d") or {}), (pl_.get("p") or {})
        except Exception:
            d, p_ = {}, {}
        if e["type"] == "task.created":
            msg = "created" + (" [%s]" % d["st"] if d.get("st") else "")
        else:
            parts = []
            for k, v in d.items():
                if k in ("u", "nu", "d", "id", "b"):
                    continue
                if k == "cm":
                    parts.append("commits linked")
                elif k in _ACT_LABEL:
                    parts.append("%s%s -> %s" % (_ACT_LABEL[k], (" " + str(p_[k])) if k in p_ and p_[k] not in ("", None, []) else "", v if v not in ("", None, []) else "cleared"))
            if not parts:
                continue
            msg = ", ".join(parts)
        items.append({"ts": e["ts"], "by": e["actor"], "ty": e["type"].split(".")[1], "m": msg, "_o": e["id"]})
    for c in rows("SELECT id, actor, body, ts FROM comments WHERE task_id=? ORDER BY id", (tid,)):
        items.append({"ts": c["ts"], "by": c["actor"], "ty": "comment", "m": c["body"], "_o": 10 ** 9 + c["id"]})
    items.sort(key=lambda x: (x["ts"], x["_o"]))
    for x in items:
        x.pop("_o")
    return {"n": len(items), "i": items}


def supervisor_names():
    names = {x.strip() for x in get_setting("supervisors").split(",") if x.strip()}
    names |= {n for n, r in TOKENS.values() if r == "supervisor"}
    names |= {r["name"] for r in rows("SELECT name FROM tokens WHERE role='supervisor'")}
    return names


def escalate(typ, actor, tid, bid, msg, delta=None, skip=()):
    """Tell the supervisor agent(s) about things a human/lead should see. Skips people who already got the direct notification."""
    sk = {str(x).lower() for x in skip}
    chat_event("escalation", msg, tid, bid)
    for n in supervisor_names():
        if n.lower() not in sk:
            notify(n, "esc." + typ, actor, tid, bid, "[escalation] " + msg, delta)


def _escalate_task(actor, old, new):
    tid, bid, title, asg = new["id"], new["board_id"], new["title"][:70], new["assignee"]
    if is_owner_assignee(asg) and (old is None or not is_owner_assignee(old["assignee"])):
        escalate("human-card", actor, tid, bid, "#%d '%s' now waits on a human (%s)" % (tid, title, asg), skip=(asg,))
    if new["status"] == "blocked" and (old is None or old["status"] != "blocked") and new["priority"] <= 2:
        escalate("blocked", actor, tid, bid, "#%d '%s' (p%s) is blocked: %s" % (tid, title, new["priority"], new["blocker"] or "no note"), skip=(asg,))


def attention(bid=None):
    """What a supervisor should look at now, most severe first."""
    items = []
    for r in active_risks(bid):
        if r["health"] == "red":
            items.append({"k": "sprint-red", "sev": "high", "m": "Sprint '%s' at risk: %s" % (r["n"], "; ".join(x["m"] for x in r["risks"][:2])), "ids": []})
    wb, a = _in("board_id", bid)
    op = rows("SELECT * FROM tasks WHERE status NOT IN ('done','declined')" + wb, a)
    def add(k, sev, msg, ts):
        if ts:
            items.append({"k": k, "sev": sev, "m": msg % len(ts), "ids": [t["id"] for t in ts][:30]})
    add("blocked-p12", "high", "%d high-priority task(s) blocked", [t for t in op if t["status"] == "blocked" and t["priority"] <= 2])
    add("human-wait", "med", "%d task(s) waiting on a human", [t for t in op if is_owner_assignee(t["assignee"])])
    add("stale", "med", "%d important/owner task(s) stale", [t for t in op if t["nudge"] and (t["priority"] <= 2 or is_owner_assignee(t["assignee"]))])
    xs = []
    for t in op:
        if t["depends"]:
            od = open_dep_ids(t["depends"])
            if od and rows("SELECT id FROM tasks WHERE id IN (%s) AND board_id!=?" % ",".join("?" * len(od)), od + [t["board_id"]]):
                xs.append(t)
    add("dep-cross", "low", "%d task(s) wait on open work in another board", xs)
    today_ = time.strftime("%Y-%m-%d", time.gmtime())
    late = [t for t in op if t["due"] and t["due"] < today_]
    add("overdue", "high" if any(t["priority"] <= 2 for t in late) else "med", "%d task(s) past their due date", late)
    soon = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 2 * 86400))
    add("due-soon", "low", "%d task(s) due within two days", [t for t in op if t["due"] and today_ <= t["due"] <= soon])
    apr = {r["task_id"] for r in rows("SELECT task_id FROM reviews WHERE kind='approval' AND status='pending'")}
    add("approval-wait", "high", "%d task(s) waiting for an approval", [t for t in op if t["id"] in apr])
    add("unassigned-p12", "med", "%d high-priority task(s) without assignee", [t for t in op if not t["assignee"] and t["priority"] <= 2])
    fr, fp = _in("id", bid)
    for fb in rows("SELECT id, name, method FROM boards WHERE 1=1" + fr, fp):
        if METHODS.get(fb["method"] or "scrum", {}).get("wip"):
            fl = board_flow(fb["id"], 14)
            if fl["over"]:
                items.append({"k": "wip-exceeded", "sev": "med", "m": "'%s' is over its WIP limit in: %s" % (fb["name"], ", ".join(fl["over"])), "ids": []})
            if fl["stuck"]:
                items.append({"k": "aging-wip", "sev": "med", "m": "%d task(s) on '%s' have been in flight unusually long" % (len(fl["stuck"]), fb["name"]), "ids": fl["stuck"][:30]})
        if fb["method"] == "support":
            br = board_sla(fb["id"])["breached"]
            if br:
                items.append({"k": "sla-breach", "sev": "high", "m": "%d request(s) on '%s' are past their SLA" % (len(br), fb["name"]), "ids": [x["id"] for x in br][:30]})
    for bb_ in cost_report({"days": ["31"], **({"bs": [",".join(str(x) for x in bid)]} if bid else {})})["bud"]:
        if bb_["pct"] >= 80:
            items.append({"k": "budget", "sev": "high" if bb_["pct"] >= 100 else "med", "m": "'%s' has used %d%% of its monthly budget" % (bb_["n"], bb_["pct"]), "ids": []})
    sil = silent_agents()
    add("silent-agent", "high", "%d in-progress task(s) belong to silent agents", [t for t in op if t["status"] == "in_progress" and (t["assignee"] or "").lower() in sil])
    od_asks = rows("SELECT id, task_id FROM asks WHERE status='open' AND due<?" + wb.replace("board_id", "board_id"), [now_s()] + a)
    if od_asks:
        items.append({"k": "ask-overdue", "sev": "med", "m": "%d question(s) between agents are overdue" % len(od_asks), "ids": [x["task_id"] for x in od_asks if x["task_id"]][:30]})
    old_rv = rows("SELECT task_id FROM reviews WHERE status='pending' AND created<?", (plus_min(-60 * 24),))
    if old_rv:
        items.append({"k": "review-stale", "sev": "med", "m": "%d review request(s) pending for over 24h" % len(old_rv), "ids": [x["task_id"] for x in old_rv][:30]})
    add("declined", "low", "%d task(s) declined in the last 7 days", rows("SELECT id FROM tasks WHERE status='declined' AND updated >= datetime('now','-7 day')" + wb, a))
    items.sort(key=lambda x: -SEV[x["sev"]])
    return items



# ---------------------------------------------------------------- outbound helper (shares the public-URL guard of the hardened edition when it exists)
def _http_post(url, data, headers, timeout=8):
    guard = globals().get("check_public_url")
    if guard:
        guard(url)
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    op = globals().get("_opener")
    with (op.open(req, timeout=timeout) if op else urllib.request.urlopen(req, timeout=timeout)) as x:
        return 200 <= x.status < 300


def push_json(url, secret, obj, tries=(0, 2)):
    body = json.dumps(obj, separators=(",", ":")).encode()
    hd = {"Content-Type": "application/json", "User-Agent": "ValnivoTaskboard"}
    if secret:
        hd["X-Taskboard-Signature"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    for wait in tries:
        time.sleep(wait)
        try:
            if _http_post(url, body, hd, 6):
                return True
        except Exception:
            pass
    return False


# ---------------------------------------------------------------- chat channels for humans (Slack, Discord, Teams, Telegram, generic JSON)
CHAT_KINDS = ("slack", "discord", "teams", "telegram", "generic")
CHAT_EVENTS = ("blocked", "done", "review", "declined", "assigned", "escalation", "sprint", "budget", "git")
CHAT_DEFAULT = ("blocked", "escalation", "sprint", "budget")
chat_q = queue.Queue()
_chat_thread = []
CHAT_ICON = {"blocked": "[blocked]", "done": "[done]", "review": "[review]", "declined": "[declined]", "assigned": "[needs you]", "escalation": "[escalation]", "sprint": "[sprint]", "budget": "[budget]", "git": "[git]"}


def chat_link(tid):
    base = get_setting("base_url").rstrip("/")
    return (" " + base + "/#t/%d" % tid) if base and tid else ""


def chat_send(c, text, ev="", tid=None):
    """One message to one channel -> (ok, error)."""
    msg = ((CHAT_ICON.get(ev, "") + " ") if ev else "") + text + chat_link(tid)
    k = c["kind"]
    if k == "discord":
        obj = {"content": msg[:1900]}
    elif k == "telegram":
        obj = {"chat_id": c["target"], "text": msg[:3900], "disable_web_page_preview": True}
    elif k == "generic":
        obj = {"text": msg, "event": ev, "task": tid}
    else:
        obj = {"text": msg}
    body = json.dumps(obj).encode()
    hd = {"Content-Type": "application/json", "User-Agent": "ValnivoTaskboard"}
    if k == "generic" and c["target"]:
        hd["X-Taskboard-Signature"] = "sha256=" + hmac.new(c["target"].encode(), body, hashlib.sha256).hexdigest()
    err = "failed"
    for wait in (0, 2):
        time.sleep(wait)
        try:
            if _http_post(c["url"], body, hd, 8):
                return True, ""
        except ApiError as e:
            return False, e.code
        except Exception as e:
            err = str(e)[:80]
    return False, err


def _chat_deliver(c, text, ev, tid):
    ok, why = chat_send(c, text, ev, tid)
    with lock:
        db.execute("UPDATE chats SET last=?, last_ok=? WHERE id=?", (now_s() + ("" if ok else " " + why), 1 if ok else 0, c["id"]))


def _chat_loop():
    while True:
        ev, text, tid, bid, pr = chat_q.get()
        try:
            for c in rows("SELECT * FROM chats WHERE active=1"):
                evs = [x for x in (c["events"] or "").split(",") if x] or list(CHAT_DEFAULT)
                if ev not in evs or (c["board_id"] and c["board_id"] != bid) or (pr is not None and pr > (c["minpr"] or 4)):
                    continue
                threading.Thread(target=_chat_deliver, args=(c, text, ev, tid), daemon=True).start()     # a slow channel never holds up the others
        except Exception:
            pass


def chat_event(ev, text, tid=None, bid=None, pr=None):
    """Queue a message for every active channel that subscribed to `ev` (cheap no-op when none exist)."""
    if not db.execute("SELECT 1 FROM chats WHERE active=1 LIMIT 1").fetchone():
        return
    chat_q.put((ev, str(text)[:600], tid, bid, pr))
    if not _chat_thread:
        t = threading.Thread(target=_chat_loop, name="chat", daemon=True)
        _chat_thread.append(t)
        t.start()


def _board_name(bid):
    r = rows("SELECT name FROM boards WHERE id=?", (bid,))
    return r[0]["name"] if r else ""


def _chat_task(actor, old, new):
    st, was = new["status"], (old["status"] if old else None)
    ev = st if (st != was and st in ("blocked", "done", "review", "declined")) else None
    if not ev and old and old["assignee"] != new["assignee"] and is_owner_assignee(new["assignee"]):
        ev = "assigned"
    if not ev and old is None and is_owner_assignee(new["assignee"]):
        ev = "assigned"
    if not ev:
        return
    extra = {"blocked": ": " + (new["blocker"] or "no note"), "declined": ": " + (new["decline_reason"] or "no reason")}.get(ev, "")
    word = {"assigned": "now waits on %s" % new["assignee"]}.get(ev, ev)
    chat_event(ev, "#%d '%s' %s%s (%s, p%s, by %s)" % (new["id"], new["title"][:70], word, extra[:120], _board_name(new["board_id"]), new["priority"], actor), new["id"], new["board_id"], new["priority"])


def chat_out(r):
    from urllib.parse import urlparse
    return {"id": r["id"], "n": r["name"], "kind": r["kind"], "host": urlparse(r["url"]).hostname or "", "ev": [x for x in (r["events"] or "").split(",") if x] or list(CHAT_DEFAULT),
            "minpr": r["minpr"] or 4, "b": r["board_id"], "on": bool(r["active"]), "last": r["last"] or None, "ok": bool(r["last_ok"])}


def chats_list():
    return {"kinds": list(CHAT_KINDS), "events": list(CHAT_EVENTS), "default": list(CHAT_DEFAULT), "i": [chat_out(r) for r in rows("SELECT * FROM chats ORDER BY id")]}


def chat_save(body, cid=None):
    old = rows("SELECT * FROM chats WHERE id=?", (cid,))[0] if cid else None
    if cid and not old:
        raise ApiError("nf", 404)
    name = " ".join(str(body.get("n") if "n" in body else body.get("name", old["name"] if old else "")).split())[:60]
    kind = body.get("kind") or (old["kind"] if old else "slack")
    if kind not in CHAT_KINDS:
        raise ApiError("bad", 400, "kind")
    url = str(body.get("url") or "").strip()
    token = str(body.get("token") or "").strip()
    if kind == "telegram" and token:
        url = "https://api.telegram.org/bot%s/sendMessage" % token
    url = url or (old["url"] if old else "")
    if not name:
        raise ApiError("missing", 400, "n")
    if not re.match(r"https?://", url):
        raise ApiError("missing" if not url else "bad", 400, "url")
    guard = globals().get("check_public_url")
    if guard:
        guard(url)
    target = str(body.get("target") if "target" in body else (old["target"] if old else "")).strip()[:120]
    if kind == "telegram" and not target:
        raise ApiError("missing", 400, "target")
    ev = body.get("events", body.get("ev", [x for x in (old["events"] if old else "").split(",") if x]))
    ev = [x.strip() for x in (ev if isinstance(ev, list) else str(ev or "").split(",")) if x.strip()]
    if any(x not in CHAT_EVENTS for x in ev):
        raise ApiError("bad", 400, "events")
    try:
        minpr = int(body.get("minpr", old["minpr"] if old else 4) or 4)
        assert 1 <= minpr <= 4
    except Exception:
        raise ApiError("bad", 400, "minpr")
    bd = body.get("b", body.get("board_id", old["board_id"] if old else None))
    bd = int(bd) if bd not in (None, "", 0, "0") else None
    if bd and not rows("SELECT id FROM boards WHERE id=?", (bd,)):
        raise ApiError("nf", 404, "b")
    on = 1 if body.get("on", body.get("active", old["active"] if old else 1)) else 0
    with lock:
        if cid:
            db.execute("UPDATE chats SET name=?, kind=?, url=?, target=?, events=?, board_id=?, minpr=?, active=? WHERE id=?", (name, kind, url, target, ",".join(ev), bd, minpr, on, cid))
        else:
            if db.execute("SELECT COUNT(*) FROM chats").fetchone()[0] >= 20:
                raise ApiError("limit", 400)
            cid = db.execute("INSERT INTO chats(name,kind,url,target,events,board_id,minpr,active) VALUES(?,?,?,?,?,?,?,?)", (name, kind, url, target, ",".join(ev), bd, minpr, on)).lastrowid
    return chat_out(rows("SELECT * FROM chats WHERE id=?", (cid,))[0])


def chat_del(cid):
    with lock:
        db.execute("DELETE FROM chats WHERE id=?", (cid,))
    return {"id": cid, "x": 1}


def chat_test(cid, actor):
    r = rows("SELECT * FROM chats WHERE id=?", (cid,))
    if not r:
        raise ApiError("nf", 404)
    ok, why = chat_send(r[0], "Test message from the task board (sent by %s). If you can read this, the channel works." % actor, "")
    with lock:
        db.execute("UPDATE chats SET last=?, last_ok=? WHERE id=?", (now_s() + ("" if ok else " " + why), 1 if ok else 0, cid))
    return {"id": cid, "ok": ok, **({"err": why} if not ok else {})}


# ---------------------------------------------------------------- wake-ups: reach agents that are not polling
def _wake_stats(name):
    unread = db.execute("SELECT COUNT(*) FROM notifications WHERE agent=? AND read=0", (name,)).fetchone()[0]
    ready = db.execute("SELECT COUNT(*) FROM tasks WHERE status='todo' AND assignee=? COLLATE NOCASE AND (claimed_by='' OR claim_until<?) AND " + READY_SQL, (name, now_s())).fetchone()[0]
    return unread, ready


def wake_tick(actor="wake"):
    """An agent with a webhook, work waiting and no activity for `wake_after_min` gets a signed wake-up ping (with backoff); after 3 unanswered pings a supervisor is told."""
    mins = int(get_setting("wake_after_min", "10") or 0)
    if mins <= 0:
        return 0
    n = 0
    for a in rows("SELECT * FROM agents WHERE webhook!=''"):
        name = a["name"]
        unread, ready = _wake_stats(name)
        fresh = db.execute("SELECT COUNT(*) FROM notifications WHERE agent=? AND read=0 AND woken=0 AND hook IN ('','failed')", (name,)).fetchone()[0]
        if not fresh:       # only unread items that were never pushed and never woke the agent; ready tasks alone are not an event
            continue
        if a["last_seen"] and a["last_seen"] > plus_min(-mins):
            continue
        wn = a["wake_n"] or 0
        if a["wake_at"] and a["wake_at"] > plus_min(-mins * (2 ** min(wn, 4))):
            continue
        _wake_send(a, unread, ready)
        with lock:
            db.execute("UPDATE notifications SET woken=1 WHERE agent=? AND read=0 AND woken=0 AND hook IN ('','failed')", (name,))
        n += 1
        if wn + 1 >= 3 and get_setting("wake:esc:" + name) != a["wake_at"]:
            with lock:
                set_setting("wake:esc:" + name, a["wake_at"] or "x")
                escalate("unreachable", actor, None, None, "%s did not answer %d wake-ups; %d unread item(s) and %d ready task(s) are waiting" % (name, wn + 1, unread, ready), skip=(name,))
    return n


def _wake_send(a, unread, ready):
    with lock:
        db.execute("UPDATE agents SET wake_at=?, wake_n=COALESCE(wake_n,0)+1 WHERE name=?", (now_s(), a["name"]))
    threading.Thread(target=push_json, args=(a["webhook"], a["secret"], {"agent": a["name"], "wake": {"unread": unread, "ready": ready, "since": a["last_seen"] or None, "do": "briefing"}}), daemon=True).start()


def wake_now(actor, role, body):
    if not _priv(role):
        raise ApiError("forbidden", 403)
    name = " ".join(str(body.get("n") or body.get("name") or "").split())
    r = rows("SELECT * FROM agents WHERE name=?", (name,))
    if not r:
        raise ApiError("nf", 404)
    if not r[0]["webhook"]:
        raise ApiError("missing", 400, "hook")
    unread, ready = _wake_stats(r[0]["name"])
    _wake_send(r[0], unread, ready)
    return {"n": r[0]["name"], "sent": 1, "unread": unread, "ready": ready}


def _wake_loop():
    while True:
        time.sleep(60)
        try:
            wake_tick()
            runs_tick()
            with lock:
                usage_flush()
        except Exception:
            pass


# ---------------------------------------------------------------- git forge webhook (GitHub, GitLab, Gitea): link commits and PRs, move cards on PR events
def branch_ids(ref):
    b = re.sub(r"^refs/(?:heads|tags)/", "", str(ref or ""))
    ids = [int(x) for x in re.findall(r"task[-_/]?(\d{1,6})", b, re.I)]
    m = re.match(r"^(?:[A-Za-z]+/)?(\d{1,6})(?=$|[-_/])", b)
    if m:
        ids.append(int(m.group(1)))
    return list(dict.fromkeys(ids))


def _existing(ids):
    ids = [int(i) for i in ids]
    if not ids:
        return []
    have = {r["id"] for r in rows("SELECT id FROM tasks WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)}
    return [i for i in ids if i in have]


def git_verify(h, raw):
    sec = get_setting("git_secret")
    if not sec:
        raise ApiError("off", 404)
    hexd = hmac.new(sec.encode(), raw, hashlib.sha256).hexdigest()
    sig = h.get("X-Hub-Signature-256") or ""
    s2 = h.get("X-Gitea-Signature") or h.get("X-Gogs-Signature") or ""
    tk = h.get("X-Gitlab-Token") or ""
    ok = (sig.startswith("sha256=") and hmac.compare_digest(sig[7:], hexd)) or (bool(s2) and hmac.compare_digest(s2, hexd)) or (bool(tk) and hmac.compare_digest(tk, sec))
    if not ok:
        raise ApiError("auth", 401)


def git_normalize(h, p):
    ev = h.get("X-GitHub-Event") or h.get("X-Gitea-Event") or h.get("X-Gogs-Event") or ""
    gl = h.get("X-Gitlab-Event") or ""
    repo = (p.get("repository") or {}).get("full_name") or (p.get("project") or {}).get("path_with_namespace") or ""
    if ev == "push" or gl == "Push Hook":
        return {"k": "push", "repo": repo, "branch": p.get("ref") or "",
                "commits": [{"h": c.get("id") or c.get("sha") or "", "u": c.get("url") or "", "m": c.get("message") or "", "r": repo} for c in (p.get("commits") or [])[:50] if isinstance(c, dict)]}
    pr = p.get("pull_request") if isinstance(p.get("pull_request"), dict) else None
    if ev in ("pull_request", "pull_request_review") and pr:
        head = pr.get("head") or {}
        base = {"repo": repo, "url": pr.get("html_url") or "", "title": pr.get("title") or "", "text": pr.get("body") or "", "branch": head.get("ref") or "",
                "sha": pr.get("merge_commit_sha") or "", "author": (pr.get("user") or {}).get("login") or "", "draft": bool(pr.get("draft"))}
        if ev == "pull_request_review":
            rv = p.get("review") or {}
            return {"k": "review", **base, "state": str(rv.get("state") or "").lower(), "by": (rv.get("user") or {}).get("login") or "", "note": rv.get("body") or ""} if p.get("action") == "submitted" else None
        a = p.get("action") or ""
        act = "open" if a in ("opened", "reopened", "ready_for_review") else ("merge" if pr.get("merged") else "close") if a == "closed" else ""
        return {"k": "pr", **base, "action": act} if act else None
    oa = p.get("object_attributes") if isinstance(p.get("object_attributes"), dict) else None
    if gl == "Merge Request Hook" and oa:
        a = oa.get("action") or ""
        base = {"repo": repo, "url": oa.get("url") or "", "title": oa.get("title") or "", "text": oa.get("description") or "", "branch": oa.get("source_branch") or "",
                "sha": oa.get("merge_commit_sha") or "", "author": (p.get("user") or {}).get("username") or "", "draft": bool(oa.get("draft") or oa.get("work_in_progress"))}
        if a in ("approved", "unapproved"):
            return {"k": "review", **base, "state": "approved" if a == "approved" else "commented", "by": base["author"], "note": ""}
        act = "open" if a in ("open", "reopen") else "merge" if a == "merge" else "close" if a == "close" else ""
        return {"k": "pr", **base, "action": act} if act else None
    if ev == "workflow_run" and isinstance(p.get("workflow_run"), dict) and p.get("action") == "completed":
        w = p["workflow_run"]
        prs = w.get("pull_requests") or []
        return {"k": "ci", "repo": repo, "branch": w.get("head_branch") or "", "state": w.get("conclusion") or "", "name": w.get("name") or "workflow", "url": w.get("html_url") or "",
                "text": (prs[0].get("html_url") if prs and isinstance(prs[0], dict) else "") or ""}
    if gl == "Pipeline Hook" and oa:
        return {"k": "ci", "repo": repo, "branch": oa.get("ref") or "", "state": "failure" if oa.get("status") == "failed" else oa.get("status") or "", "name": "pipeline",
                "url": (p.get("project") or {}).get("web_url") or "", "text": ""}
    return None


def _git_say(tid, text, notify_type=None, summary=""):
    comment_add("git", tid, {"c": text}, notify_assignee=False)
    t = rows("SELECT * FROM tasks WHERE id=?", (tid,))
    if notify_type and t:
        with lock:
            notify(t[0]["assignee"], notify_type, "git", tid, t[0]["board_id"], summary or text[:200])


def git_event(h, p):
    n = git_normalize(h, p)
    if not n:
        return {"ok": 1, "ignored": 1}
    done = []
    if n["k"] == "push":
        for c in n["commits"]:
            ids = _existing(parse_task_ids(c["m"]) or branch_ids(n["branch"]))
            for i in ids:
                append_commit("git", i, c)
                done.append(i)
        return {"ok": 1, "ev": "push", "tasks": sorted(set(done))}
    ids = _existing(parse_task_ids(n.get("title", "") + " " + n.get("text", "")) + branch_ids(n.get("branch")))
    for tid in ids:
        t = rows("SELECT * FROM tasks WHERE id=?", (tid,))[0]
        who = n.get("author") or n.get("by") or "someone"
        if n["k"] == "pr":
            if n["url"] and t["pr_url"] != n["url"]:
                crud("git", "PATCH", "tasks", tid, {"pu": n["url"]}, {}, False)
            if n["action"] == "open":
                _git_say(tid, "PR opened by %s: %s %s" % (who, n["title"][:120], n["url"]), "git.pr", "PR opened for #%d: %s" % (tid, n["title"][:100]))
                if not n["draft"] and t["status"] in ("backlog", "todo", "in_progress"):
                    try:
                        crud("git", "PATCH", "tasks", tid, {"st": "review"}, {}, False)
                    except ApiError:
                        pass
            elif n["action"] == "merge":
                if n["sha"]:
                    append_commit("git", tid, {"h": n["sha"], "m": "merged: " + n["title"], "r": n["repo"]})
                msg = "PR merged: %s %s" % (n["title"][:120], n["url"])
                closed = False
                if get_setting("git_autodone", "1") != "0" and t["status"] not in ("done", "declined"):
                    try:
                        crud("git", "PATCH", "tasks", tid, {"st": "done"}, {}, False)
                        closed = True
                    except ApiError as e:
                        msg += " - not closed automatically (%s): finish the task when ready" % e.code
                _git_say(tid, msg + (" - task done" if closed else ""), "git.merged", "PR merged for #%d%s" % (tid, " - task done" if closed else ", still open"))
                chat_event("git", "PR merged for #%d '%s': %s" % (tid, t["title"][:60], n["title"][:80]), tid, t["board_id"], t["priority"])
            else:
                _git_say(tid, "PR closed without merging: %s %s" % (n["title"][:120], n["url"]), "git.pr", "PR for #%d was closed without merging" % tid)
                if t["status"] == "review":
                    try:
                        crud("git", "PATCH", "tasks", tid, {"st": "in_progress"}, {}, False)
                    except ApiError:
                        pass
        elif n["k"] == "review":
            if n["state"] in ("approved", "changes_requested"):
                word = "approved" if n["state"] == "approved" else "requested changes on"
                _git_say(tid, "%s %s the PR %s %s" % (who, word, n["url"], (n["note"] or "")[:200]), "git.review", "%s %s the PR for #%d" % (who, word, tid))
                if n["state"] == "changes_requested":
                    chat_event("git", "Changes requested on the PR for #%d '%s'" % (tid, t["title"][:60]), tid, t["board_id"], t["priority"])
        done.append(tid)
    if n["k"] == "ci":
        for tid in _existing(parse_task_ids(n.get("text", "")) + branch_ids(n.get("branch"))):
            if n["state"] in ("failure", "timed_out", "cancelled", "failed"):
                t = rows("SELECT * FROM tasks WHERE id=?", (tid,))[0]
                _git_say(tid, "CI %s: %s %s" % (n["state"], n["name"], n["url"]), "git.ci", "CI %s on #%d (%s)" % (n["state"], tid, n["name"]))
                chat_event("git", "CI %s on #%d '%s' (%s)" % (n["state"], tid, t["title"][:60], n["name"]), tid, t["board_id"], t["priority"])
                done.append(tid)
    return {"ok": 1, "ev": n["k"], "tasks": sorted(set(done))}


# ---------------------------------------------------------------- cost: what agents spend on each task
def _num(v, lo, hi, field):
    try:
        x = float(0 if v in (None, "") else v)
        assert lo <= x <= hi
        return x
    except Exception:
        raise ApiError("bad", 400, field)


def cost_price(model, tin, tout):
    try:
        pr = json.loads(get_setting("prices", "{}")).get(model)
    except ValueError:
        pr = None
    return round((tin * pr[0] + tout * pr[1]) / 1e6, 6) if pr else 0.0


def cost_log(actor, body):
    me = _me(actor, body)
    tid = body.get("task") or body.get("t") or body.get("id")
    tin = int(_num(body.get("in", body.get("tin")), 0, 1e10, "in"))
    tout = int(_num(body.get("out", body.get("tout")), 0, 1e10, "out"))
    model = " ".join(str(body.get("model") or "").split())[:60]
    usd = round(_num(body["usd"], 0, 1e6, "usd"), 6) if body.get("usd") not in (None, "") else cost_price(model, tin, tout)
    if not (tin or tout or usd):
        raise ApiError("missing", 400, "usd")
    bid = None
    with lock:
        if tid not in (None, ""):
            t = rows("SELECT * FROM tasks WHERE id=?", (int(tid),))
            if not t:
                raise ApiError("nf", 404, "task")
            tid, bid = int(tid), t[0]["board_id"]
        else:
            tid = None
        cid = db.execute("INSERT INTO costs(agent,task_id,board_id,model,tin,tout,usd,note) VALUES(?,?,?,?,?,?,?,?)", (me, tid, bid, model, tin, tout, usd, str(body.get("note") or "")[:200])).lastrowid
        out = {"id": cid, "usd": usd}
        if tid:
            db.execute("UPDATE tasks SET cost=ROUND(COALESCE(cost,0)+?,6), tokens=COALESCE(tokens,0)+? WHERE id=?", (usd, tin + tout, tid))
            t = rows("SELECT * FROM tasks WHERE id=?", (tid,))[0]
            out.update({"cs": t["cost"], "tk": t["tokens"]})
            emit(actor, "task.updated", tid, bid, {"cs": t["cost"], "tk": t["tokens"]})
            _budget_check(t)
    return out


def _budget_say(key, msg, tid, bid):
    if get_setting(key):
        return
    set_setting(key, "1")
    escalate("budget", "cost", tid, bid, msg)
    chat_event("budget", msg, tid, bid)


def _budget_check(t):
    bid = t["board_id"]
    cfg = board_cfg(bid)[1]
    tb = float(cfg.get("task_budget_usd") or 0)
    if tb and (t["cost"] or 0) >= tb * 0.8:
        lv = 100 if t["cost"] >= tb else 80
        _budget_say("bud:t:%d:%d" % (t["id"], lv), "#%d '%s' has used %d%% of its $%.2f budget ($%.2f)" % (t["id"], t["title"][:60], lv if lv == 100 else round(t["cost"] / tb * 100), tb, t["cost"]), t["id"], bid)
    month = time.strftime("%Y-%m-01", time.gmtime())
    for scope_b, lim, label in ((bid, float(cfg.get("budget_usd") or 0), "Board '%s'" % _board_name(bid)), (None, float(get_setting("budget_usd") or 0), "All boards")):
        if not lim:
            continue
        spent = db.execute("SELECT COALESCE(SUM(usd),0) FROM costs WHERE ts>=?" + (" AND board_id=?" if scope_b else ""), (month,) + ((scope_b,) if scope_b else ())).fetchone()[0]
        if spent >= lim * 0.8:
            lv = 100 if spent >= lim else 80
            _budget_say("bud:b:%s:%s:%d" % (scope_b or "all", month[:7], lv), "%s has spent $%.2f of its $%.2f monthly budget (%d%%)" % (label, spent, lim, round(spent / lim * 100)), t["id"], bid)


def cost_report(q):
    days = max(1, min(qint(q, "days", 30) or 30, 365))
    since = plus_min(-1440 * days)
    fr, fa = _in("board_id", scope_of(q))
    ag = _qv(q, "as", "agent")
    cs = rows("SELECT * FROM costs WHERE ts>=?" + fr + (" AND agent=? COLLATE NOCASE" if ag else ""), [since] + fa + ([ag] if ag else []))
    by_a, by_t, by_b, by_d = {}, {}, {}, {}
    for c in cs:
        a = by_a.setdefault(c["agent"].lower(), {"n": c["agent"], "usd": 0.0, "in": 0, "out": 0, "calls": 0, "_t": set()})
        a["usd"] += c["usd"]; a["in"] += c["tin"]; a["out"] += c["tout"]; a["calls"] += 1
        if c["task_id"]:
            a["_t"].add(c["task_id"])
            x = by_t.setdefault(c["task_id"], {"id": c["task_id"], "usd": 0.0, "tk": 0, "b": c["board_id"]})
            x["usd"] += c["usd"]; x["tk"] += c["tin"] + c["tout"]
        if c["board_id"]:
            by_b[c["board_id"]] = by_b.get(c["board_id"], 0.0) + c["usd"]
        by_d[c["ts"][:10]] = by_d.get(c["ts"][:10], 0.0) + c["usd"]
    top = sorted(by_t.values(), key=lambda z: -z["usd"])[:8]
    names = {r["id"]: r["title"] for r in rows("SELECT id, title FROM tasks WHERE id IN (%s)" % ",".join("?" * len(top)), [x["id"] for x in top])} if top else {}
    bn = {r["id"]: r["name"] for r in rows("SELECT id, name FROM boards")}
    month = time.strftime("%Y-%m-01", time.gmtime())
    bud = []
    for bid_, in [(r["id"],) for r in rows("SELECT id FROM boards")]:
        lim = float(board_cfg(bid_)[1].get("budget_usd") or 0)
        if lim and (not fa or bid_ in fa):
            sp = db.execute("SELECT COALESCE(SUM(usd),0) FROM costs WHERE ts>=? AND board_id=?", (month, bid_)).fetchone()[0]
            bud.append({"b": bid_, "n": bn.get(bid_, ""), "spent": round(sp, 2), "limit": lim, "pct": round(sp / lim * 100)})
    return {"days": days, "since": since[:10], "tot": {"usd": round(sum(c["usd"] for c in cs), 4), "in": sum(c["tin"] for c in cs), "out": sum(c["tout"] for c in cs), "n": len(cs)},
            "ag": sorted(({**{k: v for k, v in a.items() if k != "_t"}, "usd": round(a["usd"], 4), "tasks": len(a["_t"])} for a in by_a.values()), key=lambda z: -z["usd"]),
            "tk": [{**x, "usd": round(x["usd"], 4), "t": names.get(x["id"], "")} for x in top],
            "bd": sorted(({"b": b, "n": bn.get(b, ""), "usd": round(v, 4)} for b, v in by_b.items()), key=lambda z: -z["usd"]),
            "day": [{"d": d, "usd": round(v, 4)} for d, v in sorted(by_d.items())], "bud": bud}


def task_cost(tid):
    t = rows("SELECT cost, tokens FROM tasks WHERE id=?", (tid,))
    if not t:
        raise ApiError("nf", 404)
    return {"id": tid, "cs": t[0]["cost"] or 0, "tk": t[0]["tokens"] or 0,
            "i": [{"id": c["id"], "ts": c["ts"], "by": c["agent"], "model": c["model"] or None, "in": c["tin"], "out": c["tout"], "usd": c["usd"], "note": c["note"] or None}
                  for c in rows("SELECT * FROM costs WHERE task_id=? ORDER BY id DESC LIMIT 50", (tid,))]}


# ---------------------------------------------------------------- links across boards and bulk move
def task_links(tid):
    r = rows("SELECT * FROM tasks WHERE id=?", (tid,))
    if not r:
        raise ApiError("nf", 404)
    t = r[0]
    bn = {b["id"]: b["name"] for b in rows("SELECT id, name FROM boards")}
    ids = [int(x) for x in re.findall(r"\d+", t["depends"] or "")]

    def row(x):
        return {"id": x["id"], "t": x["title"], "st": x["status"], "b": x["board_id"], "bn": bn.get(x["board_id"], ""), "as": x["assignee"] or None,
                "open": x["status"] not in ("done", "declined"), "x": x["board_id"] != t["board_id"]}
    dp = [row(x) for x in rows("SELECT * FROM tasks WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)] if ids else []
    bl = [row(x) for x in rows("SELECT * FROM tasks WHERE (',' || depends || ',') LIKE ? AND status NOT IN ('done','declined') ORDER BY id", ("%," + str(tid) + ",%",))]
    dp, bl = [x for x in dp if _board_ok(x["b"])], [x for x in bl if _board_ok(x["b"])]
    return {"id": tid, "dp": dp, "bl": bl, "cross": sum(1 for x in dp + bl if x["x"])}


def bulk_move(actor, body):
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids or len(ids) > 100:
        raise ApiError("bad", 400, "ids")
    try:
        ids = [int(i) for i in ids]
        dest = int(body.get("board") or body.get("b") or body.get("board_id") or 0)
    except (TypeError, ValueError):
        raise ApiError("bad", 400, "b")
    if not dest:
        raise ApiError("missing", 400, "b")
    opts = {k: body[k] for k in ("s", "sprint", "children", "force") if k in body}
    ok, skipped, failed = [], [], []
    for i in ids:
        r = rows("SELECT board_id FROM tasks WHERE id=?", (i,))
        if not r:
            failed.append({"id": i, "e": "nf"})
        elif r[0]["board_id"] == dest:
            skipped.append(i)
        else:
            try:
                res = move_task(actor, i, {"b": dest, **opts})
                ok += [i] + list(res.get("moved") or [])
            except ApiError as e:
                failed.append({"id": i, "e": e.code, **({"f": e.field} if e.field else {})})
    return {"n": len(ok), "moved": ok, "skipped": skipped, "failed": failed}


# ---------------------------------------------------------------- saved views: named filter sets shared by everyone (the board's "Saved views" menu)
VIEW_KEYS = ("preset", "asg", "chan", "sprint", "q", "st", "lb", "as", "k", "b")


def views_list():
    out = []
    for r in rows("SELECT * FROM views ORDER BY name"):
        try:
            f = json.loads(r["filters"] or "{}")
        except ValueError:
            f = {}
        out.append({"id": r["id"], "n": r["name"], "f": f, "by": r["owner"] or None})
    return {"n": len(out), "i": out}


def view_save(actor, body):
    name = " ".join(str(body.get("n") or body.get("name") or "").split())[:40]
    if not name:
        raise ApiError("missing", 400, "n")
    f = body.get("f", body.get("filters")) or {}
    if not isinstance(f, dict):
        raise ApiError("bad", 400, "f")
    clean = {k: str(v)[:100] for k, v in f.items() if k in VIEW_KEYS and v not in (None, "")}
    with lock:
        if not db.execute("SELECT 1 FROM views WHERE name=?", (name,)).fetchone() and db.execute("SELECT COUNT(*) FROM views").fetchone()[0] >= 100:
            raise ApiError("limit", 400)
        db.execute("INSERT INTO views(name,filters,owner) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET filters=excluded.filters, owner=excluded.owner", (name, json.dumps(clean, separators=(",", ":")), actor or ""))
    return {"n": name, "f": clean}


def view_del(q):
    vid, name = qint(q, "id", 0), _qv(q, "n", "name")
    with lock:
        db.execute("DELETE FROM views WHERE id=? OR name=?", (vid, name or "\x00"))
    return {"x": 1}


# ---------------------------------------------------------------- runs: what an agent's job did, quota state and daily limits
RUN_ENDS = ("ok", "failed", "stopped", "quota", "timeout")


def quota_of(name):
    r = db.execute("SELECT quota_until, quota_note FROM agents WHERE name=?", (name,)).fetchone()
    return (r["quota_until"], r["quota_note"]) if r and r["quota_until"] and r["quota_until"] > now_s() else None


def _ts(v):
    return calendar.timegm(time.strptime(v[:19], "%Y-%m-%d %H:%M:%S"))


def _secs(r):
    try:
        return max(0, (_ts(r["ended"]) if r["ended"] else int(time.time())) - _ts(r["started"]))
    except Exception:
        return 0


def agent_limits(name):
    try:
        lim = json.loads(get_setting("agent_limits", "{}") or "{}")
    except ValueError:
        lim = {}
    return {**(lim.get("*") or {}), **(lim.get(name) or lim.get(name.lower()) or {})}


def limit_check(name):
    """Daily (UTC) caps from the agent_limits setting: jobs, usd, secs. Raises limit (429) when one is used up."""
    lim = agent_limits(name)
    if not lim:
        return
    day = time.strftime("%Y-%m-%d 00:00:00", time.gmtime())
    if lim.get("jobs") and db.execute("SELECT COUNT(*) FROM runs WHERE agent=? AND started>=?", (name, day)).fetchone()[0] >= int(lim["jobs"]):
        raise ApiError("limit", 429, "jobs")
    if lim.get("usd") and db.execute("SELECT COALESCE(SUM(usd),0) FROM costs WHERE agent=? AND ts>=?", (name, day)).fetchone()[0] >= float(lim["usd"]):
        raise ApiError("limit", 429, "usd")
    if lim.get("secs") and sum(_secs(r) for r in rows("SELECT started, ended FROM runs WHERE agent=? AND started>=?", (name, day))) >= int(lim["secs"]):
        raise ApiError("limit", 429, "secs")


def runs_tick(actor="system"):
    """A run still 'running' long after it began, with no sign of its agent, was lost (the session died, the machine restarted). Close it and tell the supervisors."""
    mins = int(get_setting("run_lost_min", "90") or 0)
    if mins <= 0:
        return 0
    cut, n = plus_min(-mins), 0
    for r in rows("SELECT * FROM runs WHERE status='running' AND started<?", (cut,)):
        a = db.execute("SELECT last_seen FROM agents WHERE name=?", (r["agent"],)).fetchone()
        if a and a["last_seen"] and a["last_seen"] > cut:          # the agent is still around: a long job, not a lost one
            continue
        with lock:
            db.execute("UPDATE runs SET status='lost', ended=?, outcome=? WHERE id=?", (now_s(), "no sign of the agent for %d minutes" % mins, r["id"]))
            db.execute("UPDATE agents SET status_text='', status_task=NULL WHERE name=? AND status_text LIKE 'running a%'", (r["agent"],))
            emit(actor, "run.ended", r["id"], r["board_id"], {"st": "lost", "tk": r["task_id"]})
            if r["task_id"]:
                db.execute("INSERT INTO comments(task_id,actor,body) VALUES(?,?,?)", (r["task_id"], actor, "Run lost (%s, started %s UTC): no sign of the agent for %d minutes" % (r["runtime"] or "job", r["started"][11:16], mins)))
            escalate("run-lost", actor, r["task_id"], r["board_id"], "%s's %s job%s was lost: no sign of it for %d minutes" % (r["agent"], r["runtime"] or "", (" on #%d" % r["task_id"]) if r["task_id"] else "", mins), skip=(r["agent"],))
        n += 1
    return n


def run_out(r):
    o = {"id": r["id"], "tk": r["task_id"], "b": r["board_id"], "ag": r["agent"], "rt": r["runtime"], "job": r["job"], "st": r["status"], "at": r["started"], "end": r["ended"],
         "secs": _secs(r), "out": r["outcome"], "log": r["log_url"], "usd": r["usd"] or None, "tok": r["tokens"] or None}
    return {k: v for k, v in o.items() if v not in (None, "")}


def run_start(actor, role, body):
    me = _me(actor, body)
    if quota_of(me):
        raise ApiError("quota", 429, "quota")
    limit_check(me)
    tid = body.get("task")
    runtime = " ".join(str(body.get("runtime") or body.get("rt") or "").split())[:40]
    with lock:
        bid = None
        if tid not in (None, ""):
            t = rows("SELECT board_id, claimed_by FROM tasks WHERE id=?", (int(tid),))
            if not t:
                raise ApiError("nf", 404, "task")
            tid, bid = int(tid), t[0]["board_id"]
            need_board(bid)
            if (t[0]["claimed_by"] or "").lower() == me.lower():
                db.execute("UPDATE tasks SET claim_until=? WHERE id=? AND claim_until<?", (plus_min(30), tid, plus_min(30)))
        else:
            tid = None
        db.execute("INSERT OR IGNORE INTO agents(name) VALUES(?)", (me,))
        db.execute("UPDATE agents SET last_seen=?, status_text=?, status_task=?, wake_n=0 WHERE name=?", (now_s(), ("running a %s job" % runtime if runtime else "running a job"), tid, me))
        rid = db.execute("INSERT INTO runs(task_id,board_id,agent,runtime,job,log_url) VALUES(?,?,?,?,?,?)", (tid, bid, me, runtime, str(body.get("job") or "")[:80], str(body.get("log") or body.get("log_url") or "")[:500])).lastrowid
        emit(actor, "run.started", rid, bid, {"tk": tid, "ag": me, "rt": runtime})
    return {"id": rid, "st": "running"}


def run_end(actor, role, rid, body):
    me = _ident(actor, body)
    st = str(body.get("st") or body.get("status") or "ok")
    if st not in RUN_ENDS:
        raise ApiError("bad", 400, "st")
    outcome = str(body.get("out") or body.get("outcome") or "").strip()[:2000]
    with lock:
        r = rows("SELECT * FROM runs WHERE id=?", (rid,))
        if not r:
            raise ApiError("nf", 404)
        r = r[0]
        if r["agent"].lower() != (me or "").lower() and not _priv(role):
            raise ApiError("forbidden", 403)
        if r["status"] != "running":
            return run_out(r)
        usd = round(float(body.get("usd") or 0), 6) if body.get("usd") not in (None, "") else 0
        tok = int(float(body.get("tokens") or 0))
        db.execute("UPDATE runs SET status=?, ended=?, outcome=?, log_url=COALESCE(NULLIF(?,''),log_url), usd=?, tokens=? WHERE id=?",
                   (st, now_s(), outcome, str(body.get("log") or "")[:500], usd, tok, rid))
        db.execute("UPDATE agents SET status_text='', status_task=NULL, last_seen=? WHERE name=? AND status_text LIKE 'running a%'", (now_s(), r["agent"]))
        r = rows("SELECT * FROM runs WHERE id=?", (rid,))[0]
        emit(actor, "run.ended", rid, r["board_id"], {"st": st, "tk": r["task_id"]})
        if st == "quota":
            until = plus_min(max(1, min(int(body.get("quota_min") or 60), 1440)))
            db.execute("UPDATE agents SET quota_until=?, quota_note=? WHERE name=?", (until, (outcome or "quota reached")[:120], r["agent"]))
            escalate("quota", actor, r["task_id"], r["board_id"], "%s reached its quota; no new work until %s UTC" % (r["agent"], until[11:16]), skip=(r["agent"],))
        elif st in ("failed", "timeout") and r["task_id"]:
            escalate("run-failed", actor, r["task_id"], r["board_id"], "%s job for #%d %s: %s" % (r["agent"], r["task_id"], st, outcome[:140] or "no output"), skip=(r["agent"],))
    if usd or tok:
        cost_log(actor, {"task": r["task_id"], "usd": usd or None, "in": tok, "as": r["agent"], "model": r["runtime"], "note": "run #%d" % rid}) if (usd or tok) else None
    if r["task_id"] and (st != "ok" or body.get("comment")):
        comment_add(actor, r["task_id"], {"c": "Run %s (%s, %ds)%s" % (st, r["runtime"] or "job", _secs(r), (": " + outcome[:300]) if outcome else "")}, notify_assignee=False)
    return run_out(rows("SELECT * FROM runs WHERE id=?", (rid,))[0])


def runs_list(q):
    wh, ar = [], []
    for k, col in (("task", "task_id"), ("tk", "task_id"), ("agent", "agent"), ("as", "agent"), ("status", "status"), ("st", "status")):
        if _qv(q, k):
            wh.append(col + "=?" + (" COLLATE NOCASE" if col == "agent" else ""))
            ar.append(_qv(q, k))
    if _qv(q, "since"):
        wh.append("started>=?")
        ar.append(_qv(q, "since")[:19])
    a = allowed_boards()
    if a is not None:
        frag, fa = _in("board_id", sorted(a))
        wh.append(frag.replace(" AND ", "", 1))
        ar += fa
    lim = max(1, min(qint(q, "limit", 50) or 50, 500))
    rs = rows("SELECT * FROM runs" + (" WHERE " + " AND ".join(wh) if wh else "") + " ORDER BY id DESC LIMIT ?", ar + [lim])
    return {"n": len(rs), "i": [run_out(r) for r in rs]}


# ---------------------------------------------------------------- approvals: a person says yes before an agent does something risky
def approval_request(actor, tid, body):
    me = _me(actor, body)
    action = " ".join(str(body.get("action") or "").split())[:200]
    if not action:
        raise ApiError("missing", 400, "action")
    to = " ".join(str(body.get("to") or "").split())[:60]
    note = str(body.get("note") or "")[:500]
    with lock:
        t = rows("SELECT * FROM tasks WHERE id=?", (tid,))
        if not t:
            raise ApiError("nf", 404)
        t = t[0]
        ex = rows("SELECT * FROM reviews WHERE task_id=? AND kind='approval' AND status='pending' ORDER BY id LIMIT 1", (tid,))
        if ex:
            return review_out(ex[0])
        approvers = [to] if to else (sorted(supervisor_names())[:3] or ["admin"])
        first = None
        for a in approvers:
            rid = db.execute("INSERT INTO reviews(task_id,requester,reviewer,note,kind) VALUES(?,?,?,?,'approval')", (tid, me, a, (action + (": " + note if note else ""))[:1000])).lastrowid
            first = first or rid
            notify(a, "approval.requested", me, tid, t["board_id"], "%s needs your approval on #%d '%s': %s" % (me, tid, t["title"][:60], action), {"review": rid})
        db.execute("UPDATE tasks SET status='blocked', blocker=?, updated=CURRENT_TIMESTAMP, version=COALESCE(version,1)+1 WHERE id=?", ("waiting for approval: " + action, tid))
        emit(actor, "task.updated", tid, t["board_id"], {"st": "blocked", "bl": "waiting for approval: " + action})
        db.execute("INSERT INTO comments(task_id,actor,body) VALUES(?,?,?)", (tid, me, "Approval requested from %s: %s%s" % (", ".join(approvers), action, (" (" + note + ")") if note else "")))
        chat_event("assigned", "#%d '%s' needs approval: %s (asked by %s)" % (tid, t["title"][:60], action, me), tid, t["board_id"], t["priority"])
        return review_out(rows("SELECT * FROM reviews WHERE id=?", (first,))[0])


# ---------------------------------------------------------------- audit log: who changed what (admins and supervisors)
def audit_get(q, role):
    if not _priv(role):
        raise ApiError("forbidden", 403)
    wh, ar = ["id>?"], [qint(q, "since", 0) or 0]
    if _qv(q, "actor", "by"):
        wh.append("actor=? COLLATE NOCASE")
        ar.append(_qv(q, "actor", "by"))
    if _qv(q, "type"):
        wh.append("type LIKE ?")
        ar.append(_qv(q, "type").replace("%", "") + "%")
    lim = max(1, min(qint(q, "limit", 50) or 50, 200))
    task, b = qint(q, "task"), qint(q, "b")
    names = {r["id"]: r["name"] for r in rows("SELECT id, name FROM boards")}
    out = []
    for e in rows("SELECT * FROM events WHERE " + " AND ".join(wh) + " ORDER BY id DESC LIMIT ?", ar + [lim * 4 if (task or b) else lim]):
        try:
            p = json.loads(e["payload"])
        except ValueError:
            continue
        if (task and p.get("id") != task) or (b and p.get("b") != b):
            continue
        d = p.get("d") if isinstance(p.get("d"), dict) else {}
        m = ", ".join("%s: %s" % (_ACT_LABEL.get(k, k), str(v)[:50]) for k, v in list(d.items())[:5]) if d else ""
        out.append({k: v for k, v in {"id": e["id"], "ts": e["ts"], "by": e["actor"], "ty": e["type"], "e": p.get("id"), "b": p.get("b"), "bn": names.get(p.get("b")), "m": m}.items() if v not in (None, "")})
        if len(out) >= lim:
            break
    return {"n": len(out), "i": out}


# ---------------------------------------------------------------- direct messages: agent to agent, human to agent, supervisor to everyone
def say(actor, role, body):
    me = _me(actor, body)
    text = str(body.get("c") or body.get("text") or body.get("body") or "").strip()[:2000]
    if not text:
        raise ApiError("missing", 400, "c")
    to = body.get("to")
    to = [to] if isinstance(to, str) else (to or [])
    to = [" ".join(str(x).split())[:60] for x in to if str(x).strip()]
    if not to:
        raise ApiError("missing", 400, "to")
    kind = str(body.get("kind") or "ask").lower()
    if kind not in ("ask", "fyi", "ack"):
        raise ApiError("bad", 400, "kind")
    tid = body.get("task")
    try:
        reply = int(body["reply"]) if body.get("reply") not in (None, "") else None
    except (TypeError, ValueError):
        raise ApiError("bad", 400, "reply")
    sent = []
    with lock:
        if "*" in to:
            if not _priv(role):
                raise ApiError("forbidden", 403)
            to = [r["name"] for r in rows("SELECT name FROM agents ORDER BY name LIMIT 50")]
        bid = None
        if tid not in (None, ""):
            t = rows("SELECT board_id FROM tasks WHERE id=?", (int(tid),))
            if not t:
                raise ApiError("nf", 404, "task")
            tid, bid = int(tid), t[0]["board_id"]
        else:
            tid = None
        for rc in dict.fromkeys(to):
            if rc.lower() == me.lower():
                continue
            db.execute("INSERT OR IGNORE INTO agents(name) VALUES(?)", (rc,))
            mid = db.execute("INSERT INTO messages(sender,recipient,task_id,body,reply_to) VALUES(?,?,?,?,?)", (me, rc, tid, text, reply)).lastrowid
            notify(rc, "msg.direct" if kind == "ask" else "msg.fyi", me, tid, bid, "%s: %s" % (me, text[:240]), {"msg": mid})
            sent.append(mid)
    if not sent:
        raise ApiError("bad", 400, "to")
    return {"n": len(sent), "ids": sent}


def messages_get(actor, role, q):
    """A conversation log. Agents see only messages they sent or received; admins and supervisors can read any (as=, with=)."""
    me, other = _qv(q, "as", "agent"), _qv(q, "with")
    lim = max(1, min(qint(q, "limit", 30) or 30, 200))
    wh, ar = ["id>?"], [qint(q, "since", 0) or 0]
    if not _priv(role):
        me = actor if actor and actor.lower() not in GENERIC_ACTORS else me
        if not me:
            raise ApiError("missing", 400, "as")
    if me:
        wh.append("(sender=? OR recipient=?)")
        ar += [me, me]
    if other:
        wh.append("(sender=? OR recipient=?)")
        ar += [other, other]
    rs = rows("SELECT * FROM (SELECT * FROM messages WHERE " + " AND ".join(wh) + " ORDER BY id DESC LIMIT ?) ORDER BY id", ar + [lim])
    reader = actor if actor and actor.lower() not in GENERIC_ACTORS else ""
    mine = [m["id"] for m in rs if reader and m["recipient"].lower() == reader.lower()]
    if mine and q.get("ack", ["1"])[0] not in ("0", ""):
        with lock:
            senders = sorted({m["sender"] for m in rs if m["id"] in mine})
            db.execute("UPDATE notifications SET read=1 WHERE agent=? AND read=0 AND type IN ('msg.direct','msg.fyi') AND actor IN (%s) AND CAST(json_extract(payload, '$.msg') AS INTEGER)<=?" % ",".join("?" * len(senders)), [reader] + senders + [max(mine)])
    return {"n": len(rs), "i": [{"id": m["id"], "ts": m["ts"], "from": m["sender"], "to": m["recipient"], "tk": m["task_id"], "c": m["body"], **({"re": m["reply_to"]} if m["reply_to"] else {})} for m in rs]}


# ---------------------------------------------------------------- templates + recurring tasks
RECUR_HELP = "daily | weekdays | weekly:mon..sun | monthly:1..31"
_WD = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _recur_ok(rc):
    return rc == "" or rc in ("daily", "weekdays") or bool(re.fullmatch(r"weekly:(%s)" % "|".join(_WD), rc)) or bool(re.fullmatch(r"monthly:([1-9]|[12]\d|3[01])", rc))


def _recur_match(rc, d):
    if rc == "daily":
        return True
    if rc == "weekdays":
        return d.weekday() < 5
    if rc.startswith("weekly:"):
        return d.weekday() == _WD.index(rc[7:])
    if rc.startswith("monthly:"):
        last = (d.replace(day=28) + _datetime.timedelta(days=4)).replace(day=1) - _datetime.timedelta(days=1)
        return d.day == min(int(rc[8:]), last.day)
    return False


def _recur_next(rc, frm):
    """First date >= frm that matches the rule."""
    for i in range(0, 400):
        d = frm + _datetime.timedelta(days=i)
        if _recur_match(rc, d):
            return d.isoformat()
    return ""


def tpl_out(r):
    o = {"id": r["id"], "b": r["board_id"], "n": r["name"], "t": r["title"], "d": r["description"], "as": r["assignee"], "k": r["kind"], "pr": r["priority"],
         "ch": r["channel"], "lb": r["labels"], "pt": r["points"], "cl": r["checklist"], "rc": r["recur"], "nx": r["next_run"], "on": bool(r["active"]), "last": r["last_task"]}
    return {k: v for k, v in o.items() if v not in (None, "", 0) or k in ("pr", "on")}


def tpl_save(body, tid=None):
    cur = rows("SELECT * FROM templates WHERE id=?", (tid,))[0] if tid else {}
    if tid and not cur:
        raise ApiError("nf", 404)
    g = lambda *ks, default="": next((body[k] for k in ks if k in body), cur.get({"n": "name", "t": "title", "d": "description", "as": "assignee", "k": "kind", "pr": "priority", "ch": "channel", "lb": "labels", "pt": "points", "cl": "checklist", "rc": "recur", "b": "board_id"}.get(ks[0], ks[0]), default))
    name = " ".join(str(g("n", "name")).split())[:80]
    if not name:
        raise ApiError("missing", 400, "n")
    bid = g("b", "board_id", default=None)
    if bid in ("", 0):
        bid = None
    if bid is not None and not rows("SELECT id FROM boards WHERE id=?", (int(bid),)):
        raise ApiError("bad", 400, "b")
    rc = str(g("rc", "recur")).strip().lower()
    if not _recur_ok(rc):
        raise ApiError("bad", 400, "rc")
    pr = int(g("pr", "priority", default=3) or 3)
    if not 1 <= pr <= 4:
        raise ApiError("bad", 400, "pr")
    cl = g("cl", "checklist")
    cl = "\n".join(str(x).strip() for x in cl) if isinstance(cl, list) else str(cl or "").strip()
    nx = str(body.get("nx") or body.get("at") or "")[:10]
    if rc and (not nx or "rc" in body or "recur" in body):
        nx = nx or _recur_next(rc, _datetime.datetime.now(_datetime.timezone.utc).replace(tzinfo=None).date())
    if not rc:
        nx = ""
    vals = (None if bid is None else int(bid), name, str(g("t", "title"))[:200], str(g("d", "description"))[:10000], str(g("as", "assignee"))[:60],
            "human" if g("k", "kind", default="agent") == "human" else "agent", pr, str(g("ch", "channel"))[:40], _lbl_csv(_lbl_set(g("lb", "labels"))),
            int(g("pt", "points", default=0) or 0), cl[:4000], rc, nx, 0 if body.get("on") is False else 1)
    with lock:
        if tid:
            db.execute("UPDATE templates SET board_id=?,name=?,title=?,description=?,assignee=?,kind=?,priority=?,channel=?,labels=?,points=?,checklist=?,recur=?,next_run=?,active=? WHERE id=?", vals + (tid,))
        else:
            tid = db.execute("INSERT INTO templates(board_id,name,title,description,assignee,kind,priority,channel,labels,points,checklist,recur,next_run,active) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", vals).lastrowid
        return tpl_out(rows("SELECT * FROM templates WHERE id=?", (tid,))[0])


def tpl_make_task(actor, t, over=None, suffix=""):
    over = over or {}
    bid = over.get("b") or over.get("board_id") or t["board_id"]
    if not bid:
        raise ApiError("missing", 400, "b")
    desc = (over.get("d") or t["description"] or "").strip()
    if t["checklist"]:
        desc = (desc + "\n\n## Checklist\n" + "\n".join("- [ ] " + ln.lstrip("-[] ").strip() for ln in t["checklist"].splitlines() if ln.strip())).strip()
    body = {"board_id": int(bid), "title": (over.get("t") or t["title"] or t["name"]) + suffix, "description": desc, "assignee": over.get("as") or t["assignee"],
            "kind": t["kind"], "priority": t["priority"], "channel": t["channel"] or "Template", "labels": t["labels"], "points": t["points"]}
    if over.get("s") is not None:
        body["sprint_id"] = over["s"]
    return crud(actor, "POST", "tasks", None, body, {}, False)


def tpl_use(actor, tid, over):
    r = rows("SELECT * FROM templates WHERE id=?", (tid,))
    if not r:
        raise ApiError("nf", 404)
    res = tpl_make_task(actor, r[0], over)
    with lock:
        db.execute("UPDATE templates SET last_task=? WHERE id=?", (res["id"], tid))
    return res


def recur_tick(actor="recurring"):
    today = _datetime.datetime.now(_datetime.timezone.utc).replace(tzinfo=None).date()
    made, skipped, failed = [], 0, 0
    for t in rows("SELECT * FROM templates WHERE active=1 AND recur!='' AND next_run!='' AND next_run<=?", (today.isoformat(),)):
        nxt = _recur_next(t["recur"], today + _datetime.timedelta(days=1))
        prev = rows("SELECT status FROM tasks WHERE id=?", (t["last_task"],)) if t["last_task"] else []
        if prev and prev[0]["status"] not in ("done", "declined"):   # previous occurrence still open: do not pile up
            skipped += 1
        else:
            try:
                res = tpl_make_task(actor, t, suffix=" - " + today.isoformat())
                made.append(res["id"])
                with lock:
                    db.execute("UPDATE templates SET last_task=? WHERE id=?", (res["id"], t["id"]))
            except ApiError:
                failed += 1
        with lock:
            db.execute("UPDATE templates SET next_run=? WHERE id=?", (nxt, t["id"]))
    return {"made": made, "skipped": skipped, "failed": failed}


# ---------------------------------------------------------------- per-agent statistics
def agent_stats(bid=None, days=30):
    now = _datetime.datetime.now(_datetime.timezone.utc).replace(tzinfo=None)
    cut = now - _datetime.timedelta(days=days)
    fr, fp = _in("board_id", bid)
    ts = rows("SELECT * FROM tasks WHERE assignee!=''" + fr, fp)
    byid = {t["id"]: t for t in ts}
    tl = {}
    acts = {}
    for e in rows("SELECT ts, actor, type, payload FROM events WHERE type LIKE 'task.%' ORDER BY id"):
        try:
            pj = json.loads(e["payload"])
        except Exception:
            continue
        when = _datetime.datetime.strptime(e["ts"][:19], "%Y-%m-%d %H:%M:%S")
        if when >= cut:
            acts[e["actor"]] = acts.get(e["actor"], 0) + 1
        d = pj.get("d") or {}
        if pj.get("id") in byid and isinstance(d, dict) and d.get("st"):
            tl.setdefault(pj["id"], []).append((when, d["st"]))
    com = {r["actor"]: r["n"] for r in rows("SELECT actor, COUNT(*) n FROM comments WHERE ts >= ? GROUP BY actor", (cut.strftime("%Y-%m-%d %H:%M:%S"),))}
    agg = {}
    for t in ts:
        a = agg.setdefault(t["assignee"], {"as": t["assignee"], "done": 0, "declined": 0, "open": 0, "blocked": 0, "cyc": [], "dur": {"in_progress": [], "review": [], "blocked": []}})
        line = tl.get(t["id"], [])
        upd = _datetime.datetime.strptime(t["updated"][:19], "%Y-%m-%d %H:%M:%S") if t["updated"] else now
        if t["status"] in ("done", "declined"):
            fin = next((w for w, st in reversed(line) if st == t["status"]), upd)
            if fin >= cut:
                a["done" if t["status"] == "done" else "declined"] += 1
                if t["status"] == "done":
                    a["cyc"].append((fin - _datetime.datetime.strptime(t["created"][:19], "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600)
        else:
            a["open"] += 1
            a["blocked"] += t["status"] == "blocked"
        spent = {}
        for i, (w, st) in enumerate(line):
            end = line[i + 1][0] if i + 1 < len(line) else (now if t["status"] not in ("done", "declined") else w)
            spent[st] = spent.get(st, 0) + max((end - w).total_seconds(), 0) / 3600
        for st in a["dur"]:
            if st in spent:
                a["dur"][st].append(spent[st])
    avg = lambda l: round(sum(l) / len(l), 1) if l else None
    out = []
    for a in sorted(agg.values(), key=lambda x: x["as"]):
        o = {"as": a["as"], "done": a["done"], "declined": a["declined"], "open": a["open"], "blocked": a["blocked"], "cycle_h": avg(a["cyc"]),
             "ip_h": avg(a["dur"]["in_progress"]), "rv_h": avg(a["dur"]["review"]), "bl_h": avg(a["dur"]["blocked"]),
             "events": acts.get(a["as"], 0), "comments": com.get(a["as"], 0)}
        o["decline_rate"] = round(a["declined"] / (a["done"] + a["declined"]), 2) if (a["done"] + a["declined"]) else None
        out.append({k: v for k, v in o.items() if v is not None})
    return {"days": days, "i": out}


# ---------------------------------------------------------------- coordination: claims, locks, asks, reviews, presence, routing, envs, watchers, attachments
def now_s():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def plus_min(m):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + m * 60))


def parse_due(v, default_min=60):
    if v in (None, ""):
        return plus_min(default_min)
    if isinstance(v, (int, float)) or re.fullmatch(r"\d+", str(v)):
        return plus_min(max(1, min(int(v), 60 * 24 * 90)))
    t = str(v).replace("T", " ")[:19]
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.strptime(t, f))
        except ValueError:
            pass
    raise ApiError("bad", 400, "due")


_ctx = threading.local()


def _ident(actor, body):
    """Who is acting. A named, non-privileged token is always itself (cannot impersonate); shared/generic or admin/supervisor tokens may say `as`."""
    named = bool(actor) and actor.lower() not in GENERIC_ACTORS
    asn = " ".join(str(body.get("as") or body.get("agent") or "").split())[:60]
    if named and not (asn and _priv(getattr(_ctx, "role", "agent"))):
        return actor
    return asn or (actor if named else "")


def _me(actor, body):
    n = _ident(actor, body)
    if not n or n.lower() in GENERIC_ACTORS:
        raise ApiError("missing", 400, "as")
    return n


def _priv(role):
    return role in ("admin", "supervisor")


def allowed_boards():
    """Boards the calling token is limited to (a set), or None for all."""
    return getattr(_ctx, "allowed", None)


def _board_ok(bid):
    a = allowed_boards()
    return a is None or (bid is not None and int(bid) in a)


def need_board(bid):
    if not _board_ok(bid):
        raise ApiError("scope", 403, "b")


def need_task(tid):
    if allowed_boards() is None:
        return
    r = db.execute("SELECT board_id FROM tasks WHERE id=?", (int(tid),)).fetchone()
    if r:
        need_board(r[0])


def _clip(sc):
    """Narrow a board scope (None/0 = all, int, or list) to what the token may see."""
    a = allowed_boards()
    if a is None:
        return sc
    if sc is None or sc == 0:
        return sorted(a)
    if isinstance(sc, int):
        sc = [sc]
    return [i for i in sc if i in a]


_seen = {}


def touch_presence(name):
    if not name or name.lower() in GENERIC_ACTORS or time.time() - _seen.get(name, 0) < 20:
        return
    _seen[name] = time.time()
    with lock:
        db.execute("INSERT OR IGNORE INTO agents(name) VALUES(?)", (name,))
        db.execute("UPDATE agents SET last_seen=?, wake_n=0 WHERE name=?", (now_s(), name))


def silent_agents():
    cut = plus_min(-SILENT_MIN)
    return {r["name"].lower() for r in rows("SELECT name FROM agents WHERE last_seen!='' AND last_seen<?", (cut,))}


def presence_set(actor, body):
    me = _me(actor, body)
    st = " ".join(str(body.get("status") or body.get("text") or "").split())[:200]
    tk = body.get("task")
    with lock:
        db.execute("INSERT OR IGNORE INTO agents(name) VALUES(?)", (me,))
        db.execute("UPDATE agents SET last_seen=?, status_text=?, status_task=?, wake_n=0 WHERE name=?", (now_s(), st, int(tk) if tk not in (None, "") else None, me))
        renewed = []
        if tk not in (None, ""):          # a heartbeat on a task you hold extends your claim, so long jobs never lose it
            until = plus_min(max(15, min(int(body.get("ttl") or 30), 1440)))
            for r in rows("SELECT id, board_id FROM tasks WHERE id=? AND claimed_by=? COLLATE NOCASE AND claim_until!='' AND claim_until<?", (int(tk), me, until)):
                db.execute("UPDATE tasks SET claim_until=? WHERE id=?", (until, r["id"]))
                emit(me, "task.updated", r["id"], r["board_id"], {"cu": until})
                renewed.append(r["id"])
    return {"n": me, "ok": 1, **({"renewed": renewed} if renewed else {})}


# ---- claims
READY_SQL = "NOT EXISTS (SELECT 1 FROM tasks dd WHERE dd.status NOT IN ('done','declined') AND (',' || tasks.depends || ',') LIKE '%,' || dd.id || ',%')"


def claim_task(actor, tid, body):
    me = _me(actor, body)
    if quota_of(me):
        raise ApiError("quota", 429, "quota")
    limit_check(me)
    ttl = max(1, min(int(body.get("ttl") or 120), 1440))
    with lock:
        r = rows("SELECT * FROM tasks WHERE id=?", (tid,))
        if not r:
            raise ApiError("nf", 404)
        t = r[0]
        if t["status"] in ("done", "declined"):
            raise ApiError("bad", 400, "st")
        if t["status"] in ("backlog", "todo"):
            check_wip(t["board_id"], "in_progress", tid=tid)
            check_start(t, "in_progress", claiming=True)
        if t["claimed_by"] and t["claimed_by"].lower() != me.lower() and t["claim_until"] > now_s():
            raise ApiError("claimed", 409, t["claimed_by"])
        until = plus_min(ttl)
        st = "in_progress" if t["status"] in ("backlog", "todo") else t["status"]
        db.execute("UPDATE tasks SET claimed_by=?, claim_until=?, assignee=?, status=?, nudge='', updated=CURRENT_TIMESTAMP, version=COALESCE(version,1)+1 WHERE id=?", (me, until, me, st, tid))
        delta = {"cb": me, "cu": until, "as": me, **({"st": st} if st != t["status"] else {})}
        emit(actor, "task.updated", tid, t["board_id"], delta)
        if t["assignee"] and t["assignee"].lower() not in (me.lower(), (actor or "").lower()):
            notify(t["assignee"], "task.unassigned", me, tid, t["board_id"], "#%d '%s' was claimed by %s" % (tid, t["title"][:60], me), delta)
        od = open_dep_ids(t["depends"])
    out = {"id": tid, "cb": me, "cu": until, "st": st}
    if od:
        out["w"] = ["depends on open " + " ".join("#%d" % i for i in od)]
    return out


def release_task(actor, role, tid, body):
    with lock:
        r = rows("SELECT * FROM tasks WHERE id=?", (tid,))
        if not r:
            raise ApiError("nf", 404)
        t = r[0]
        me = _ident(actor, body)
        if t["claimed_by"] and t["claimed_by"].lower() != me.lower() and not _priv(role):
            raise ApiError("forbidden", 403)
        db.execute("UPDATE tasks SET claimed_by='', claim_until='', version=COALESCE(version,1)+1, updated=CURRENT_TIMESTAMP WHERE id=?", (tid,))
        emit(actor, "task.updated", tid, t["board_id"], {"cb": ""})
    return {"id": tid, "released": 1}


def claim_next(actor, body):
    me = _me(actor, body)
    qt = quota_of(me)
    if qt:
        return {"none": 1, "quota": qt[0]}
    limit_check(me)
    wh = ["status='todo'", "(claimed_by='' OR claim_until<?)", "(assignee='' OR assignee=? COLLATE NOCASE)", READY_SQL]
    ar = [now_s(), me]
    if allowed_boards() is not None:
        frag, fa = _in("board_id", sorted(allowed_boards()))
        wh.append(frag.replace(" AND ", "", 1))
        ar += fa
    if body.get("b") or body.get("board_id"):
        wh.append("board_id=?")
        ar.append(int(body.get("b") or body.get("board_id")))
    labels = [x for x in (body.get("lb") or body.get("skill") or "").split(",") if x.strip()] if isinstance(body.get("lb") or body.get("skill") or "", str) else []
    if labels:
        wh.append("(" + " OR ".join("(',' || labels || ',') LIKE ?" for _ in labels) + ")")
        ar += ["%," + x.strip().lower() + ",%" for x in labels]
    r = rows("SELECT id FROM tasks WHERE " + " AND ".join(wh) + " ORDER BY priority, CASE WHEN due='' THEN 1 ELSE 0 END, due, id LIMIT 1", ar)
    if not r:
        return {"none": 1}
    res = claim_task(actor, r[0]["id"], {**body, "as": me})
    return {**res, "t": rows("SELECT title FROM tasks WHERE id=?", (r[0]["id"],))[0]["title"]}


# ---- locks
def locks_list():
    with lock:
        db.execute("DELETE FROM locks WHERE expires<?", (now_s(),))
    return {"n": 0, "i": [{"name": r["name"], "owner": r["owner"], "until": r["expires"], "note": r["note"]} for r in rows("SELECT * FROM locks ORDER BY name")]}


def lock_acquire(actor, body):
    me = _me(actor, body)
    name = re.sub(r"[^A-Za-z0-9_.:/-]", "", str(body.get("name") or ""))[:80]
    if not name:
        raise ApiError("missing", 400, "name")
    ttl = max(1, min(int(body.get("ttl") or 30), 1440))
    with lock:
        db.execute("DELETE FROM locks WHERE expires<?", (now_s(),))
        r = db.execute("SELECT * FROM locks WHERE name=?", (name,)).fetchone()
        if r and r["owner"].lower() != me.lower():
            raise ApiError("locked", 409, r["owner"])
        until = plus_min(ttl)
        db.execute("INSERT OR REPLACE INTO locks(name,owner,note,expires) VALUES(?,?,?,?)", (name, me, str(body.get("note") or "")[:200], until))
    return {"name": name, "owner": me, "until": until}


def lock_release(actor, role, name, body):
    me = _ident(actor, body)
    with lock:
        r = db.execute("SELECT * FROM locks WHERE name=?", (name,)).fetchone()
        if not r:
            return {"name": name, "released": 0}
        if r["owner"].lower() != me.lower() and not _priv(role):
            raise ApiError("forbidden", 403)
        db.execute("DELETE FROM locks WHERE name=?", (name,))
    return {"name": name, "released": 1}


# ---- asks (questions with a deadline)
def ask_out(r):
    o = {"id": r["id"], "from": r["from_agent"], "to": r["to_agent"], "q": r["question"], "st": r["status"], "due": r["due"], "tk": r["task_id"], "a": r["answer"], "c": r["created"]}
    return {k: v for k, v in o.items() if v not in (None, "")}


def ask_create(actor, body):
    frm = _me(actor, body)
    to = " ".join(str(body.get("to") or "").split())[:60]
    q = str(body.get("q") or body.get("question") or "").strip()[:2000]
    if not to:
        raise ApiError("missing", 400, "to")
    if not q:
        raise ApiError("missing", 400, "q")
    tk = body.get("task") or body.get("tk")
    bid = None
    if tk not in (None, ""):
        t = rows("SELECT board_id FROM tasks WHERE id=?", (int(tk),))
        if not t:
            raise ApiError("bad", 400, "task")
        bid, tk = t[0]["board_id"], int(tk)
    else:
        tk = None
    due = parse_due(body.get("due"), 60)
    with lock:
        aid = db.execute("INSERT INTO asks(from_agent,to_agent,task_id,board_id,question,due) VALUES(?,?,?,?,?,?)", (frm, to, tk, bid, q, due)).lastrowid
        notify(to, "ask.new", frm, tk, bid, "question #%d from %s (answer by %s UTC)%s: %s" % (aid, frm, due[11:16], " about task #%d" % tk if tk else "", q[:160]), {"ask": aid})
    sim = faq_search(q, bid, 2, exclude=aid)
    return {"id": aid, "to": to, "due": due, **({"sim": sim} if sim else {})}


def ask_answer(actor, role, aid, body):
    me = _me(actor, body)
    a = str(body.get("a") or body.get("answer") or "").strip()[:4000]
    if not a:
        raise ApiError("missing", 400, "a")
    with lock:
        r = rows("SELECT * FROM asks WHERE id=?", (aid,))
        if not r:
            raise ApiError("nf", 404)
        r = r[0]
        if r["status"] != "open":
            raise ApiError("bad", 400, "st")
        if r["to_agent"].lower() != me.lower() and not _priv(role):
            raise ApiError("forbidden", 403)
        db.execute("UPDATE asks SET answer=?, status='answered', answered=? WHERE id=?", (a, now_s(), aid))
        notify(r["from_agent"], "ask.answered", me, r["task_id"], r["board_id"], "%s answered your question #%d: %s" % (me, aid, a[:200]), {"ask": aid})
    return {"id": aid, "st": "answered"}


def ask_cancel(actor, role, aid, body):
    me = _me(actor, body)
    with lock:
        r = rows("SELECT * FROM asks WHERE id=?", (aid,))
        if not r:
            raise ApiError("nf", 404)
        if r[0]["from_agent"].lower() != me.lower() and not _priv(role):
            raise ApiError("forbidden", 403)
        db.execute("UPDATE asks SET status='cancelled' WHERE id=? AND status='open'", (aid,))
    return {"id": aid, "st": "cancelled"}


def asks_list(q, actor=""):
    wh, ar = [], []
    st = _qv(q, "st", "status", default="open")
    if st != "all":
        wh.append("status=?")
        ar.append(st)
    for k, col in (("to", "to_agent"), ("from", "from_agent")):
        if _qv(q, k):
            wh.append(col + "=? COLLATE NOCASE")
            ar.append(_qv(q, k))
    if _qv(q, "task", "tk"):
        wh.append("task_id=?")
        ar.append(int(_qv(q, "task", "tk")))
    rs = rows("SELECT * FROM asks" + (" WHERE " + " AND ".join(wh) if wh else "") + " ORDER BY due LIMIT 200", ar)
    return {"n": len(rs), "i": [ask_out(r) for r in rs]}


# ---- reviews
def review_out(r):
    o = {"id": r["id"], "tk": r["task_id"], "from": r["requester"], "to": r["reviewer"], "st": r["status"], "note": r["note"], "dn": r["decision_note"], "c": r["created"], "kind": "approval" if r["kind"] == "approval" else None}
    return {k: v for k, v in o.items() if v not in (None, "")}


def review_request(actor, tid, body):
    me = _me(actor, body)
    rv = " ".join(str(body.get("reviewer") or body.get("to") or "").split())[:60]
    if not rv:
        raise ApiError("missing", 400, "reviewer")
    with lock:
        r = rows("SELECT * FROM tasks WHERE id=?", (tid,))
        if not r:
            raise ApiError("nf", 404)
        t = r[0]
        ex = rows("SELECT * FROM reviews WHERE task_id=? AND reviewer=? COLLATE NOCASE AND status='pending'", (tid, rv))
        if ex:
            return review_out(ex[0])
        rid = db.execute("INSERT INTO reviews(task_id,requester,reviewer,note) VALUES(?,?,?,?)", (tid, me, rv, str(body.get("note") or "")[:1000])).lastrowid
        if t["status"] in ("backlog", "todo", "in_progress", "blocked"):
            db.execute("UPDATE tasks SET status='review', updated=CURRENT_TIMESTAMP, version=COALESCE(version,1)+1 WHERE id=?", (tid,))
            emit(actor, "task.updated", tid, t["board_id"], {"st": "review"})
        if t["assignee"] and t["assignee"].lower() not in (me.lower(), rv.lower()):
            notify(t["assignee"], "task.status", me, tid, t["board_id"], "#%d '%s' sent to review (%s) by %s" % (tid, t["title"][:60], rv, me), {"st": "review"})
        notify(rv, "review.requested", me, tid, t["board_id"], "%s asks you to review #%d '%s'%s" % (me, tid, t["title"][:60], ": " + body["note"][:140] if body.get("note") else ""), {"review": rid})
        return review_out(rows("SELECT * FROM reviews WHERE id=?", (rid,))[0])


def review_decide(actor, role, rid, body):
    me = _me(actor, body)
    dec = str(body.get("decision") or "").lower()
    if dec not in ("approve", "changes"):
        raise ApiError("bad", 400, "decision")
    note = str(body.get("note") or "").strip()[:1000]
    if dec == "changes" and not note:
        raise ApiError("missing", 400, "note")
    with lock:
        r = rows("SELECT * FROM reviews WHERE id=?", (rid,))
        if not r:
            raise ApiError("nf", 404)
        r = r[0]
        if r["status"] != "pending":
            raise ApiError("bad", 400, "st")
        if r["reviewer"].lower() != me.lower() and not _priv(role):
            raise ApiError("forbidden", 403)
        st = "approved" if dec == "approve" else "changes"
        db.execute("UPDATE reviews SET status=?, decision_note=?, decided=? WHERE id=?", (st, note, now_s(), rid))
        t = rows("SELECT * FROM tasks WHERE id=?", (r["task_id"],))[0]
        if r["kind"] == "gate":
            _gate_decided(me, t, st == "approved", note)
            db.execute("INSERT INTO comments(task_id,actor,body) VALUES(?,?,?)", (t["id"], me, ("Gate approved" if st == "approved" else "Gate sent back") + (": " + note if note else "")))
            return {"id": rid, "st": st}
        db.execute("INSERT INTO comments(task_id,actor,body) VALUES(?,?,?)", (t["id"], me, (("Approval granted" if st == "approved" else "Approval denied") if r["kind"] == "approval" else ("Review approved" if st == "approved" else "Changes requested")) + (": " + note if note else "")))
        if r["kind"] == "approval":
            if st == "approved":
                if not rows("SELECT id FROM reviews WHERE task_id=? AND kind='approval' AND status='pending'", (t["id"],)) and t["status"] == "blocked" and (t["blocker"] or "").startswith("waiting for approval"):
                    db.execute("UPDATE tasks SET status='in_progress', blocker='', blocked_by='', updated=CURRENT_TIMESTAMP, version=COALESCE(version,1)+1 WHERE id=?", (t["id"],))
                    emit(actor, "task.updated", t["id"], t["board_id"], {"st": "in_progress", "bl": ""})
            else:
                db.execute("UPDATE tasks SET blocker=?, updated=CURRENT_TIMESTAMP, version=COALESCE(version,1)+1 WHERE id=?", ("approval denied: " + note[:200], t["id"]))
                emit(actor, "task.updated", t["id"], t["board_id"], {"bl": "approval denied: " + note[:200]})
        elif st == "changes":
            db.execute("UPDATE tasks SET status='in_progress', updated=CURRENT_TIMESTAMP, version=COALESCE(version,1)+1 WHERE id=?", (t["id"],))
            emit(actor, "task.updated", t["id"], t["board_id"], {"st": "in_progress"})
        pend = rows("SELECT id FROM reviews WHERE task_id=? AND status IN ('pending','changes')", (t["id"],))
        msg = ("%s approved #%d '%s'%s" % (me, t["id"], t["title"][:60], " - all reviews approved" if not pend else "")) if st == "approved" else \
              ("%s requested changes on #%d '%s': %s" % (me, t["id"], t["title"][:60], note[:160]))
        ntype = ("approval." + ("granted" if st == "approved" else "denied")) if r["kind"] == "approval" else "review." + st
        notify(r["requester"], ntype, me, t["id"], t["board_id"], msg, {"review": rid})
        if t["assignee"] and t["assignee"].lower() != (r["requester"] or "").lower():
            notify(t["assignee"], ntype, me, t["id"], t["board_id"], msg, {"review": rid})
    return {"id": rid, "st": st}


def reviews_list(q):
    wh, ar = [], []
    st = _qv(q, "st", "status", default="pending")
    if st != "all":
        wh.append("status=?")
        ar.append(st)
    for k, col in (("reviewer", "reviewer"), ("to", "reviewer"), ("from", "requester")):
        if _qv(q, k):
            wh.append(col + "=? COLLATE NOCASE")
            ar.append(_qv(q, k))
    if _qv(q, "task", "tk"):
        wh.append("task_id=?")
        ar.append(int(_qv(q, "task", "tk")))
    rs = rows("SELECT * FROM reviews" + (" WHERE " + " AND ".join(wh) if wh else "") + " ORDER BY id DESC LIMIT 200", ar)
    return {"n": len(rs), "i": [review_out(r) for r in rs]}


def check_done_gate(tid, bid, dod_done):
    """Definition of done (board checklist) + open reviews must be satisfied before a task can reach done."""
    b = rows("SELECT dod FROM boards WHERE id=?", (bid,))
    items = [x for x in (b[0]["dod"] if b else "").splitlines() if x.strip()]
    if items:
        done = {int(x) for x in re.findall(r"\d+", str(dod_done or ""))}
        if not set(range(len(items))) <= done:
            raise ApiError("dod", 400, "dn")
    # a review blocks while it is pending, or asked for changes and nobody has looked again since (a newer review by the same person supersedes it)
    if rows("SELECT id FROM reviews r WHERE task_id=? AND (status='pending' OR (status='changes' AND NOT EXISTS "
            "(SELECT 1 FROM reviews n WHERE n.task_id=r.task_id AND n.reviewer=r.reviewer COLLATE NOCASE AND n.id>r.id)))", (tid,)):
        raise ApiError("review", 400, "st")
    t_ = rows("SELECT * FROM tasks WHERE id=?", (tid,))
    if t_ and t_[0]["accept"] and not t_[0]["verified_by"] and "verify_before_done" in task_rules(t_[0]):
        raise ApiError("verify", 409, "vb")


# ---- skills routing
def suggest_verifier(t, exclude=()):
    """Someone other than the worker to check the work: not the assignee or claimer, not out of quota or silent; prefer agents whose skills say qa/verify/review, then the least loaded."""
    ex = {str(x).lower() for x in (t["assignee"], t["claimed_by"], *exclude) if x}
    sil = silent_agents()
    best = None
    for a in rows("SELECT * FROM agents"):
        n = a["name"]
        if n.lower() in ex or n.lower() in sil or (a["quota_until"] and a["quota_until"] > now_s()):
            continue
        sk = {x.strip().lower() for x in (a["skills"] or "").split(",") if x.strip()}
        load = db.execute("SELECT COUNT(*) FROM tasks WHERE assignee=? COLLATE NOCASE AND status NOT IN ('done','declined')", (n,)).fetchone()[0]
        key = (not (sk & {"qa", "verify", "review", "checker", "test"}), load / (a["capacity"] or 8), load, n.lower())
        if best is None or key < best[0]:
            best = (key, n)
    return best[1] if best else None


def verify_queue(q, actor, role):
    """Finished work that has acceptance criteria and nobody else's sign-off yet, with who should check it."""
    me = _qv(q, "as") or (actor if actor and actor.lower() not in GENERIC_ACTORS else "")
    out = []
    for t in rows("SELECT * FROM tasks WHERE status IN ('review','done') AND accept!='' AND verified_by='' ORDER BY id DESC LIMIT 60"):
        if not _board_ok(t["board_id"]):
            continue
        who = suggest_verifier(t)
        if me and not _priv(role) and (who or "").lower() != me.lower():
            continue
        if me and _priv(role) and _qv(q, "mine") and (who or "").lower() != me.lower():
            continue
        out.append({"id": t["id"], "t": t["title"][:80], "st": t["status"], "as": t["assignee"] or None, "vf": who, "ac": t["accept"].splitlines()[:6]})
    return {"n": len(out), "i": out[:20]}


def _words(s):
    return {w for w in re.findall(r"[a-z0-9_]{4,}", str(s).lower())} - {"this", "that", "with", "from", "what", "which", "when", "have", "does", "should", "would", "could", "there", "about", "into", "need", "task"}


def faq_search(text, bid=None, limit=3, exclude=None):
    """Answered questions that look like `text` (shared words), best first: agents read these before asking again."""
    want = _words(text)
    if len(want) < 2:
        return []
    out = []
    for a in rows("SELECT id, question, answer, to_agent, board_id FROM asks WHERE status='answered' ORDER BY id DESC LIMIT 400"):
        if exclude and a["id"] == exclude:
            continue
        sc = len(want & _words(a["question"]))
        if sc >= 2 and sc >= len(want) * 0.4:
            out.append((sc + (0.5 if bid and a["board_id"] == bid else 0), {"id": a["id"], "q": a["question"][:140], "a": a["answer"][:220], "by": a["to_agent"]}))
    out.sort(key=lambda x: -x[0])
    return [o[1] for o in out[:limit]]


def _fit_counts(want):
    """Per agent: finished tasks that carried one of the wanted labels, and how often reviewers asked for changes on their work."""
    done, chg = {}, {}
    for t in rows("SELECT id, assignee, labels FROM tasks WHERE status='done' AND assignee!='' ORDER BY id DESC LIMIT 1500"):
        if want & {x.strip().lower() for x in (t["labels"] or "").split(",") if x.strip()}:
            done[t["assignee"].lower()] = done.get(t["assignee"].lower(), 0) + 1
    tot, bad = {}, {}
    for r in rows("SELECT t.assignee a, v.status s FROM reviews v JOIN tasks t ON t.id=v.task_id WHERE v.status IN ('approved','changes') AND t.assignee!='' ORDER BY v.id DESC LIMIT 1500"):
        k = r["a"].lower()
        tot[k] = tot.get(k, 0) + 1
        bad[k] = bad.get(k, 0) + (r["s"] == "changes")
    for k, n in tot.items():
        chg[k] = round(bad.get(k, 0) / n, 2)
    return done, chg


def agent_fit(labels):
    want = {x.strip().lower() for x in str(labels).split(",") if x.strip()}
    done, chg = _fit_counts(want)
    out = [{"n": a["name"], "done": done.get(a["name"].lower(), 0), "chg": chg.get(a["name"].lower())} for a in rows("SELECT name FROM agents ORDER BY name")]
    return {"lb": sorted(want), "i": sorted(out, key=lambda x: (-x["done"], x["chg"] or 0, x["n"].lower()))}


def overlap_check(actor, tid, d):
    """Two open tasks on the same pull request, or in progress under the same area:<name> label: tell both owners once."""
    r = rows("SELECT * FROM tasks WHERE id=?", (tid,))
    if not r or r[0]["status"] in ("done", "declined"):
        return
    t = r[0]
    twins = []
    if t["pr_url"] and ("pr_url" in d or "status" in d):
        twins += [(x, "the same pull request") for x in rows("SELECT * FROM tasks WHERE pr_url=? AND id!=? AND status NOT IN ('done','declined')", (t["pr_url"], tid))]
    areas = {x.strip().lower() for x in (t["labels"] or "").split(",") if x.strip().lower().startswith("area:")}
    if areas and t["status"] == "in_progress" and ("status" in d or "labels" in d):
        for x in rows("SELECT * FROM tasks WHERE id!=? AND status='in_progress'", (tid,)):
            shared = areas & {y.strip().lower() for y in (x["labels"] or "").split(",") if y.strip()}
            if shared:
                twins.append((x, "the area " + sorted(shared)[0][5:]))
    for x, why in twins[:5]:
        msg = "#%d '%s' and #%d '%s' are both open on %s: agree who does what" % (tid, t["title"][:50], x["id"], x["title"][:50], why)
        for who in {(t["assignee"] or ""), (x["assignee"] or "")}:
            if who:
                notify(who, "task.overlap", actor, tid, t["board_id"], msg, {"with": x["id"]})


def post_update(actor, old, d, rid):
    """After a task changed: ask for an independent check when it enters review with acceptance criteria, and warn about overlapping work. Caller holds `lock`."""
    if d.get("status") == "review" and old["status"] != "review" and get_setting("auto_verify", "1") != "0":
        t = rows("SELECT * FROM tasks WHERE id=?", (rid,))
        if t and t[0]["accept"] and not t[0]["verified_by"]:
            who = suggest_verifier(t[0])
            if who:
                notify(who, "verify.requested", actor, rid, t[0]["board_id"], "#%d '%s' needs an independent check against its acceptance criteria" % (rid, t[0]["title"][:60]), {"st": "review"})
    overlap_check(actor, rid, d)


def usage_flush():
    """Add what the in-memory meter counted since the last flush to the per-day table, so spend survives restarts."""
    day = time.strftime("%Y-%m-%d", time.gmtime())
    with _us_lock:
        snap = {k: list(v) for k, v in USAGE.items()}
    for k, v in snap.items():
        prev = _FLUSHED.get(k, [0, 0, 0])
        dn, dob, dib = v[0] - prev[0], v[1] - prev[1], v[2] - prev[2]
        if dn or dob or dib:
            db.execute("INSERT INTO usage_day(day, agent, route, calls, ob, ib) VALUES(?,?,?,?,?,?) ON CONFLICT(day, agent, route) DO UPDATE SET calls=calls+excluded.calls, ob=ob+excluded.ob, ib=ib+excluded.ib",
                       (day, k[0], k[1], dn, dob, dib))
        _FLUSHED[k] = v


_FLUSHED = {}


def usage_history(days):
    usage_flush()
    since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - (days - 1) * 86400))
    ag, rt = {}, {}
    for r in rows("SELECT agent, route, SUM(calls) n, SUM(ob) ob, SUM(ib) ib FROM usage_day WHERE day>=? GROUP BY agent, route", (since,)):
        t = (r["ob"] + r["ib"]) // 4
        x = ag.setdefault(r["agent"], {"n": r["agent"], "calls": 0, "t": 0, "routes": []})
        x["calls"] += r["n"]
        x["t"] += t
        x["routes"].append({"r": r["route"], "calls": r["n"], "t": t})
        y = rt.setdefault(r["route"], {"r": r["route"], "calls": 0, "t": 0})
        y["calls"] += r["n"]
        y["t"] += t
    for x in ag.values():
        x["routes"] = sorted(x["routes"], key=lambda z: -z["t"])[:3]
    return {"agents": sorted(ag.values(), key=lambda z: -z["t"])[:8], "routes": sorted(rt.values(), key=lambda z: -z["t"])[:6]}


def route_candidates(skill="", labels="", bid=None):
    want = {x.strip().lower() for x in (str(skill) + "," + str(labels)).split(",") if x.strip()}
    sil = silent_agents()
    out = []
    fit_done, _ = _fit_counts(want) if want else ({}, {})
    for a in rows("SELECT * FROM agents"):
        sk = {x.strip().lower() for x in (a["skills"] or "").split(",") if x.strip()}
        if not sk or (want and not (sk & want)) or (a["quota_until"] and a["quota_until"] > now_s()):
            continue
        load = db.execute("SELECT COUNT(*) FROM tasks WHERE assignee=? COLLATE NOCASE AND status NOT IN ('done','declined')", (a["name"],)).fetchone()[0]
        cap = a["capacity"] or 8
        out.append({"n": a["name"], "load": load, "cap": a["capacity"] or 0, "silent": a["name"].lower() in sil, "_s": (a["name"].lower() in sil, load >= cap, -min(fit_done.get(a["name"].lower(), 0), 5), load / cap, load)})
    out.sort(key=lambda x: x["_s"])
    for o in out:
        o.pop("_s")
    return out


# ---- environments
def envs_list():
    now = now_s()
    return {"i": [{**{k: v for k, v in {"n": r["name"], "sha": r["sha"], "url": r["url"], "by": r["deployed_by"], "at": r["deployed_at"], "tk": r["task_id"]}.items() if v not in (None, "")},
                   **({"frozen": r["frozen_until"], "fn": r["freeze_note"]} if r["frozen_until"] > now else {})} for r in rows("SELECT * FROM envs ORDER BY name")]}


def _env_name(v):
    n = re.sub(r"[^a-z0-9._-]", "", str(v or "").lower())[:40]
    if not n:
        raise ApiError("missing", 400, "n")
    return n


def env_deploy(actor, role, body):
    me = _me(actor, body)
    name = _env_name(body.get("n") or body.get("name"))
    sha = str(body.get("sha") or "").strip()[:200]
    with lock:
        r = db.execute("SELECT * FROM envs WHERE name=?", (name,)).fetchone()
        if r and r["frozen_until"] > now_s() and not (body.get("force") and _priv(role)):
            raise ApiError("frozen", 423, r["frozen_until"])
        lk = db.execute("SELECT * FROM locks WHERE name=? AND expires>=?", ("env:" + name, now_s())).fetchone()
        if lk and lk["owner"].lower() != me.lower():
            raise ApiError("locked", 409, lk["owner"])
        tk = int(body["task"]) if body.get("task") not in (None, "") else None
        db.execute("INSERT INTO envs(name,sha,url,deployed_by,deployed_at,task_id) VALUES(?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET sha=excluded.sha, url=CASE WHEN excluded.url!='' THEN excluded.url ELSE envs.url END, deployed_by=excluded.deployed_by, deployed_at=excluded.deployed_at, task_id=excluded.task_id",
                   (name, sha, str(body.get("url") or "")[:300], me, now_s(), tk))
        if tk and sha:
            t = rows("SELECT board_id FROM tasks WHERE id=?", (tk,))
            if t:
                db.execute("UPDATE tasks SET sha=?, version=COALESCE(version,1)+1, updated=CURRENT_TIMESTAMP WHERE id=?", (sha, tk))
                emit(actor, "task.updated", tk, t[0]["board_id"], {"sha": sha})
        emit(actor, "env.updated", 0, None, {"n": name, "sha": sha})
    return {"n": name, "sha": sha, "by": me}


def env_freeze(actor, name, body, on=True):
    name = _env_name(name)
    with lock:
        db.execute("INSERT OR IGNORE INTO envs(name) VALUES(?)", (name,))
        if on:
            until = parse_due(body.get("until"), 60 * 24)
            note = str(body.get("note") or "")[:200]
            db.execute("UPDATE envs SET frozen_until=?, freeze_note=? WHERE name=?", (until, note, name))
            for a in rows("SELECT name FROM agents"):
                notify(a["name"], "env.frozen", actor, None, None, "environment '%s' is frozen until %s UTC%s" % (name, until[:16], ": " + note if note else ""), {"env": name})
        else:
            db.execute("UPDATE envs SET frozen_until='', freeze_note='' WHERE name=?", (name,))
    return {"n": name, "frozen": bool(on)}


# ---- watchers
WATCH_KINDS = ("task", "spec", "label", "board", "tag")


def watch_add(actor, body):
    me = _me(actor, body)
    kind, ref = str(body.get("kind") or ""), str(body.get("ref") or "").strip().lower()
    if kind not in WATCH_KINDS:
        raise ApiError("bad", 400, "kind")
    if not ref:
        raise ApiError("missing", 400, "ref")
    with lock:
        db.execute("INSERT OR IGNORE INTO watches(agent,kind,ref) VALUES(?,?,?)", (me, kind, ref[:80]))
    return {"as": me, "kind": kind, "ref": ref}


def watch_del(actor, q):
    me = _qv(q, "as") or actor
    with lock:
        db.execute("DELETE FROM watches WHERE agent=? AND kind=? AND ref=?", (me, _qv(q, "kind", default=""), (_qv(q, "ref", default="")).lower()))
    return {"ok": 1}


def watch_list(q, actor=""):
    me = _qv(q, "as") or actor
    rs = rows("SELECT * FROM watches" + (" WHERE agent=? COLLATE NOCASE" if me and me.lower() not in GENERIC_ACTORS else "") + " ORDER BY id", (me,) if me and me.lower() not in GENERIC_ACTORS else ())
    return {"n": len(rs), "i": [{"as": r["agent"], "kind": r["kind"], "ref": r["ref"]} for r in rs]}


def _watchers(pairs, skip=()):
    sk = {x.lower() for x in skip if x}
    out = set()
    for kind, ref in pairs:
        for r in rows("SELECT agent FROM watches WHERE kind=? AND ref=?", (kind, str(ref).lower())):
            if r["agent"].lower() not in sk:
                out.add(r["agent"])
    return out


def _notify_watchers(actor, old, new):
    if old is not None and not any(old.get(k) != new.get(k) for k in NOTIFY_KEYS):
        return
    pairs = [("task", new["id"]), ("board", new["board_id"])] + [("label", x) for x in _lbl_set(new["labels"])]
    for a in _watchers(pairs, skip=(actor, new["assignee"])):
        notify(a, "watch.task", actor, new["id"], new["board_id"], "watching: #%d '%s' %s by %s [%s]" % (new["id"], new["title"][:60], "created" if old is None else "updated", actor, new["status"]), {"st": new["status"]})


# ---- attachments (artifact registry)
ATT_KINDS = ("file", "pr", "report", "log", "link", "other")


def att_add(actor, tid, body):
    name = " ".join(str(body.get("name") or body.get("n") or "").split())[:120]
    if not name:
        raise ApiError("missing", 400, "name")
    url = str(body.get("url") or body.get("u") or "").strip()[:500]
    if url and not re.match(r"(https?://|/)", url):
        raise ApiError("bad", 400, "url")
    kind = body.get("kind") or body.get("k") or "link"
    if kind not in ATT_KINDS:
        raise ApiError("bad", 400, "kind")
    with lock:
        t = rows("SELECT * FROM tasks WHERE id=?", (tid,))
        if not t:
            raise ApiError("nf", 404)
        t = t[0]
        aid = db.execute("INSERT INTO attachments(task_id,name,url,kind,note,actor) VALUES(?,?,?,?,?,?)", (tid, name, url, kind, str(body.get("note") or "")[:300], actor)).lastrowid
        n = db.execute("SELECT COUNT(*) FROM attachments WHERE task_id=?", (tid,)).fetchone()[0]
        db.execute("UPDATE tasks SET updated=CURRENT_TIMESTAMP, nudge='' WHERE id=?", (tid,))
        emit(actor, "task.updated", tid, t["board_id"], {"att": n})
        notify(t["assignee"], "task.attachment", actor, tid, t["board_id"], "%s attached %s '%s' to #%d" % (actor, kind, name[:60], tid), {"att": aid})
    return {"id": aid, "tk": tid}


def atts_get(tid):
    if not rows("SELECT id FROM tasks WHERE id=?", (tid,)):
        raise ApiError("nf", 404)
    rs = rows("SELECT * FROM attachments WHERE task_id=? ORDER BY id", (tid,))
    return {"n": len(rs), "i": [{k: v for k, v in {"id": r["id"], "n": r["name"], "u": r["url"], "k": r["kind"], "note": r["note"], "by": r["actor"], "c": r["created"]}.items() if v} for r in rs]}


def att_del(aid):
    with lock:
        db.execute("DELETE FROM attachments WHERE id=?", (aid,))
    return {"id": aid, "x": 1}


# ---- housekeeping (runs with the periodic tick, under `lock`)
def housekeeping(actor="system"):
    nowt = now_s()
    rollover_tick(actor)
    replenish_tick(actor)
    sla_tick(actor)
    for t in rows("SELECT * FROM tasks WHERE claimed_by!='' AND claim_until<?", (nowt,)):
        db.execute("UPDATE tasks SET claimed_by='', claim_until='', version=COALESCE(version,1)+1 WHERE id=?", (t["id"],))
        emit(actor, "task.updated", t["id"], t["board_id"], {"cb": ""})
        notify(t["claimed_by"], "claim.expired", actor, t["id"], t["board_id"], "your claim on #%d '%s' expired - renew it or release it" % (t["id"], t["title"][:60]))
    db.execute("DELETE FROM locks WHERE expires<?", (nowt,))
    for a in rows("SELECT * FROM asks WHERE status='open' AND escalated=0 AND due<?", (nowt,)):
        db.execute("UPDATE asks SET escalated=1 WHERE id=?", (a["id"],))
        notify(a["to_agent"], "ask.overdue", actor, a["task_id"], a["board_id"], "question #%d from %s is overdue: %s" % (a["id"], a["from_agent"], a["question"][:140]), {"ask": a["id"]})
        notify(a["from_agent"], "ask.overdue", actor, a["task_id"], a["board_id"], "%s has not answered your question #%d yet" % (a["to_agent"], a["id"]), {"ask": a["id"]})
        escalate("ask-overdue", actor, a["task_id"], a["board_id"], "%s has not answered %s's question #%d (due %s UTC)" % (a["to_agent"], a["from_agent"], a["id"], a["due"][:16]), skip=(a["to_agent"], a["from_agent"]))
    for n in silent_agents():
        busy = rows("SELECT id FROM tasks WHERE assignee=? COLLATE NOCASE AND status='in_progress'", (n,))
        last = db.execute("SELECT last_seen FROM agents WHERE name=?", (n,)).fetchone()["last_seen"]
        if busy and get_setting("silent:" + n) != last:
            set_setting("silent:" + n, last)
            escalate("silent", actor, busy[0]["id"], None, "%s has been silent since %s UTC with %d task(s) in progress (#%s)" % (n, last[:16], len(busy), busy[0]["id"]), skip=(n,))


# ---------------------------------------------------------------- work methodologies: scrum, kanban, scrumban, shape up, simple
ALL_STATUSES = ["backlog", "todo", "in_progress", "review", "blocked", "done"]
METHODS = {
    "scrum": {"name": "Scrum", "about": "Fixed-length sprints with a commitment, velocity and a retrospective after each one.",
              "sprints": True, "wip": False, "term": "Sprint", "sprint_days": 14, "columns": ALL_STATUSES,
              "labels": {}},
    "kanban": {"name": "Kanban", "about": "Continuous flow, no sprints. Limit work in progress and watch cycle time, throughput and aging work.",
               "sprints": False, "wip": True, "term": "", "sprint_days": 14, "columns": ALL_STATUSES,
               "labels": {"todo": "Ready", "in_progress": "Doing"}},
    "scrumban": {"name": "Scrumban", "about": "Kanban flow with an optional cadence: WIP limits, optional sprints, and a signal to replenish the ready column.",
                 "sprints": True, "wip": True, "term": "Sprint", "sprint_days": 14, "columns": ALL_STATUSES,
                 "labels": {"todo": "Ready", "in_progress": "Doing"}},
    "shapeup": {"name": "Shape Up", "about": "Six-week cycles with fixed appetite: shape pitches, bet on a few, build to ship, then cool down.",
                "sprints": True, "wip": False, "term": "Cycle", "sprint_days": 42, "columns": ALL_STATUSES,
                "labels": {"backlog": "Shaping", "todo": "Bets", "in_progress": "Building", "review": "QA", "blocked": "Stuck", "done": "Shipped"}},
    "simple": {"name": "Simple list", "about": "Just to do, doing and done. No sprints, no limits.",
               "sprints": False, "wip": False, "term": "", "sprint_days": 14, "columns": ["todo", "in_progress", "done"],
               "labels": {"in_progress": "Doing"}},
    "waterfall": {"name": "Waterfall (stage-gate)", "about": "Sequential phases with gates: a phase only closes when all its work is done, then the next one starts.",
                  "sprints": True, "wip": False, "term": "Phase", "sprint_days": 30, "columns": ALL_STATUSES, "labels": {}, "gate": True,
                  "phases": ["Requirements", "Design", "Build", "Test", "Release"]},
    "gtd": {"name": "Personal (GTD)", "about": "Capture everything in an inbox, decide the next action, and track what you are waiting for.",
            "sprints": False, "wip": False, "term": "", "sprint_days": 14, "columns": ["backlog", "todo", "in_progress", "blocked", "done"],
            "labels": {"backlog": "Inbox", "todo": "Next actions", "in_progress": "Doing", "blocked": "Waiting for"}},
    "lean": {"name": "Lean startup", "about": "Build, measure, learn: turn ideas into hypotheses, build the smallest test, measure it, record what you learned.",
             "sprints": False, "wip": True, "term": "", "sprint_days": 14, "columns": ALL_STATUSES,
             "labels": {"backlog": "Ideas", "todo": "Hypotheses", "in_progress": "Building", "review": "Measuring", "blocked": "Pivot or stop", "done": "Learned"}},
    "eisenhower": {"name": "Priority matrix", "about": "Four quadrants by urgency and importance: do first, schedule, delegate, eliminate. Drag a card between quadrants to re-prioritise.",
                   "sprints": False, "wip": False, "term": "", "sprint_days": 14, "columns": ["todo", "in_progress", "done"], "labels": {}, "group": "priority",
                   "groups": {"1": "Do first", "2": "Schedule", "3": "Delegate", "4": "Eliminate"},
                   "group_hints": {"1": "Urgent and important", "2": "Important, not urgent", "3": "Urgent, not important", "4": "Neither"}},
    "okr": {"name": "OKRs", "about": "Objectives with measurable key results: set a target, update the current value, and watch progress roll up per objective and quarter.",
            "sprints": True, "wip": False, "term": "Quarter", "sprint_days": 91, "columns": ALL_STATUSES, "labels": {}, "group": "okr"},
    "support": {"name": "Support desk (SLA)", "about": "Incoming requests with a response-time target per priority; breaches are flagged and escalated.",
                "sprints": False, "wip": False, "term": "", "sprint_days": 14, "columns": ALL_STATUSES, "sla": True,
                "labels": {"backlog": "New", "todo": "Triaged", "in_progress": "Investigating", "review": "Resolved - verify", "blocked": "Waiting on customer", "done": "Closed"}},
}
TASK_TYPES = ("task", "story", "bug", "epic", "spike", "chore")


def norm_cfg(v):
    """Board settings as JSON: wip {status: limit}, wip_mode warn|block, sprint_days, auto_rollover, replenish_below."""
    if isinstance(v, str):
        try:
            v = json.loads(v) if v.strip() else {}
        except ValueError:
            raise ApiError("bad", 400, "cf")
    if not isinstance(v, dict):
        raise ApiError("bad", 400, "cf")
    out = {}
    try:
        wip = {k: int(n) for k, n in (v.get("wip") or {}).items() if k in ALL_STATUSES and n not in (None, "", 0, "0")}
        if any(n < 1 or n > 999 for n in wip.values()):
            raise ValueError
        if wip:
            out["wip"] = wip
        if v.get("wip_mode") in ("warn", "block"):
            out["wip_mode"] = v["wip_mode"]
        if v.get("sprint_days") not in (None, "", 0):
            out["sprint_days"] = max(1, min(int(v["sprint_days"]), 120))
        if v.get("auto_rollover"):
            out["auto_rollover"] = True
        if v.get("replenish_below") not in (None, "", 0, "0"):
            out["replenish_below"] = max(1, min(int(v["replenish_below"]), 999))
        if v.get("phases"):
            ph = v["phases"] if isinstance(v["phases"], list) else str(v["phases"]).splitlines()
            ph = [" ".join(str(x).split())[:40] for x in ph if str(x).strip()][:12]
            if ph:
                out["phases"] = ph
        for bk in ("budget_usd", "task_budget_usd"):
            if v.get(bk) not in (None, "", 0, "0"):
                x = float(v[bk])
                if x <= 0 or x > 1e9:
                    raise ValueError
                out[bk] = round(x, 2)
        if v.get("policy"):
            out["policy"] = policy_clean(v["policy"])
        sl = {str(k): int(n) for k, n in (v.get("sla_hours") or {}).items() if str(k) in ("1", "2", "3", "4") and n not in (None, "", 0, "0")}
        if sl:
            if any(n < 1 or n > 24 * 365 for n in sl.values()):
                raise ValueError
            out["sla_hours"] = sl
    except (TypeError, ValueError):
        raise ApiError("bad", 400, "cf")
    return json.dumps(out, separators=(",", ":"))


def board_cfg(bid):
    r = rows("SELECT method, cfg FROM boards WHERE id=?", (bid,))
    if not r:
        return "scrum", {}
    try:
        return r[0]["method"] or "scrum", json.loads(r[0]["cfg"] or "{}")
    except ValueError:
        return r[0]["method"] or "scrum", {}


def board_has_sprints(bid):
    return METHODS.get(board_cfg(bid)[0], METHODS["scrum"])["sprints"]


def sprint_days_for(bid):
    m, cfg = board_cfg(bid)
    return int(cfg.get("sprint_days") or METHODS.get(m, METHODS["scrum"])["sprint_days"] or SPRINT_DAYS)


def check_wip(bid, status, tid=None, force=False):
    """Kanban/scrumban work-in-progress limit. 'block' mode refuses (409 wip); 'warn' mode returns a message. None = fine."""
    m, cfg = board_cfg(bid)
    lim = (cfg.get("wip") or {}).get(status)
    if not METHODS.get(m, {}).get("wip") or not lim:
        return None
    n = db.execute("SELECT COUNT(*) FROM tasks WHERE board_id=? AND status=? AND id!=?", (bid, status, tid or 0)).fetchone()[0]
    if n < lim:
        return None
    if cfg.get("wip_mode") == "block" and not (force and _priv(getattr(_ctx, "role", "agent"))):
        raise ApiError("wip", 409, status)
    return "WIP limit for %s reached (%d of %d)" % (status, n + 1, lim)


def check_parent(tid, parent, bid):
    if parent is None:
        return
    r = rows("SELECT id, board_id, type FROM tasks WHERE id=?", (parent,))
    if not r or r[0]["board_id"] != bid or r[0]["type"] != "epic" or (tid and parent == tid):
        raise ApiError("bad", 400, "pa")


def sprint_weight(sid):
    ts = rows("SELECT status, points FROM tasks WHERE sprint_id=? AND status!='declined'", (sid,))
    pts = any(t["points"] for t in ts)
    return sum((t["points"] or 1) if pts else 1 for t in ts)


# ---- flow metrics (kanban): timelines are rebuilt from the event log
def _tsd(v):
    try:
        return _datetime.datetime.strptime(str(v)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return _datetime.datetime(1970, 1, 1)


def _timelines(ts):
    byid = {t["id"]: t for t in ts}
    tl = {}
    for e in rows("SELECT ts, payload FROM events WHERE type LIKE 'task.%' ORDER BY id"):
        try:
            pj = json.loads(e["payload"])
        except ValueError:
            continue
        d = pj.get("d")
        if pj.get("id") in byid and isinstance(d, dict) and d.get("st"):
            tl.setdefault(pj["id"], []).append((_tsd(e["ts"]), d["st"]))
    for t in ts:
        line = tl.setdefault(t["id"], [])
        created = _tsd(t["created"])
        if not line:
            line.append((created, t["status"]))
        elif line[0][0] > created + _datetime.timedelta(seconds=5):
            line.insert(0, (created, "backlog"))
        line.sort(key=lambda x: x[0])
    return tl


def _pct(vals, p):
    if not vals:
        return None
    v = sorted(vals)
    return round(v[min(len(v) - 1, int(round(p * (len(v) - 1))))], 1)


def board_flow(bid, days=30, raw=False):
    now = _datetime.datetime.now(_datetime.timezone.utc).replace(tzinfo=None)
    cut = now - _datetime.timedelta(days=days)
    ts = rows("SELECT id, title, status, created, assignee, priority FROM tasks WHERE board_id=?", (bid,))
    tl = _timelines(ts)
    m, cfg = board_cfg(bid)
    done_at, start_at = {}, {}
    for t in ts:
        line = tl[t["id"]]
        start_at[t["id"]] = next((w for w, st in line if st == "in_progress"), None)
        if t["status"] == "done":
            done_at[t["id"]] = max((w for w, st in line if st == "done"), default=_tsd(t["created"]))
    comp = [t for t in ts if t["id"] in done_at and done_at[t["id"]] >= cut]
    cyc = [(done_at[t["id"]] - start_at[t["id"]]).total_seconds() / 3600 for t in comp if start_at[t["id"]]]
    lead = [(done_at[t["id"]] - _tsd(t["created"])).total_seconds() / 3600 for t in comp]
    weeks = []
    for i in range(max(1, (days + 6) // 7) - 1, -1, -1):
        hi, lo = now - _datetime.timedelta(days=7 * i), now - _datetime.timedelta(days=7 * (i + 1))
        weeks.append({"from": lo.strftime("%Y-%m-%d"), "n": sum(1 for t in comp if lo < done_at[t["id"]] <= hi)})
    cfd = []
    for i in range(days + 1):
        day = cut.date() + _datetime.timedelta(days=i)
        end = _datetime.datetime.combine(day, _datetime.time.max)
        counts = {k: 0 for k in ALL_STATUSES}
        for t in ts:
            cur = None
            for w, st in tl[t["id"]]:
                if w <= end:
                    cur = st
            if cur in counts:
                counts[cur] += 1
        cfd.append({"d": day.isoformat(), **counts})
    wip = {st: sum(1 for t in ts if t["status"] == st) for st in ALL_STATUSES}
    thr = max(72, 2 * (_pct(cyc, 0.5) or 0))
    aging = []
    for t in ts:
        if t["status"] in ("in_progress", "review", "blocked"):
            since = tl[t["id"]][-1][0]
            age = (now - since).total_seconds() / 3600
            aging.append({"id": t["id"], "t": t["title"], "st": t["status"], "age_h": round(age, 1), "as": t["assignee"], "stuck": age > thr})
    aging.sort(key=lambda x: -x["age_h"])
    limits = cfg.get("wip") or {}
    return {"b": bid, "mt": m, "days": days, "wip": wip, "limits": limits, "over": [k for k, n in limits.items() if wip.get(k, 0) > n],
            "done_n": len(comp), "per_week": weeks, "cycle_h": {"avg": round(sum(cyc) / len(cyc), 1) if cyc else None, "p50": _pct(cyc, 0.5), "p85": _pct(cyc, 0.85)},
            "lead_h": {"avg": round(sum(lead) / len(lead), 1) if lead else None, "p50": _pct(lead, 0.5), "p85": _pct(lead, 0.85)},
            "aging": aging[:10], "stuck": [a["id"] for a in aging if a["stuck"]], "cfd": cfd, **({"_cyc": cyc, "_lead": lead} if raw else {})}


def scope_analytics(q):
    """The single-board analytics, rolled up over the boards in scope (bs=, tm=, none = all): flow, active sprints, OKR, SLA, agent stats."""
    days = max(1, min(qint(q, "days", 30) or 30, 365))
    scope = scope_of(q)
    fr, fp = _in("id", scope)
    bs = rows("SELECT id, name, method FROM boards WHERE 1=1" + fr + " ORDER BY id", fp)
    out = {"days": days, "boards": len(bs)}
    fl = [board_flow(b["id"], days, raw=True) for b in bs]
    if fl:
        cyc, lead = [x for f in fl for x in f["_cyc"]], [x for f in fl for x in f["_lead"]]
        avg = lambda l: round(sum(l) / len(l), 1) if l else None
        names = {b["id"]: b["name"] for b in bs}
        aging = sorted(({**a, "b": f["b"], "bn": names[f["b"]]} for f in fl for a in f["aging"]), key=lambda x: -x["age_h"])[:8]
        out["flow"] = {"days": days, "wip": {k: sum(f["wip"].get(k, 0) for f in fl) for k in ALL_STATUSES}, "done_n": sum(f["done_n"] for f in fl),
                       "over": ["%s: %s" % (names[f["b"]], ", ".join(f["over"])) for f in fl if f["over"]],
                       "per_week": [{"from": fl[0]["per_week"][i]["from"], "n": sum(f["per_week"][i]["n"] for f in fl)} for i in range(len(fl[0]["per_week"]))],
                       "cycle_h": {"avg": avg(cyc), "p50": _pct(cyc, 0.5), "p85": _pct(cyc, 0.85)}, "lead_h": {"avg": avg(lead), "p50": _pct(lead, 0.5), "p85": _pct(lead, 0.85)},
                       "aging": aging, "stuck": [i for f in fl for i in f["stuck"]],
                       "cfd": [{"d": fl[0]["cfd"][i]["d"], **{k: sum(f["cfd"][i][k] for f in fl) for k in ALL_STATUSES}} for i in range(len(fl[0]["cfd"]))]}
    sps, okr, sla = [], [], []
    for b in bs:
        sp = rows("SELECT id FROM sprints WHERE board_id=? AND state='active' ORDER BY id DESC LIMIT 1", (b["id"],))
        if sp:
            st = sprint_stats(sp[0]["id"])
            sps.append({"b": b["id"], "bn": b["name"], **{k: st[k] for k in ("id", "n", "from", "to", "health", "pct", "w", "days", "proj", "burn", "risks")}})
        mt = METHODS.get(b["method"] or "scrum", {})
        if mt.get("group") == "okr":
            o = board_okr(b["id"])
            okr.append({"b": b["id"], "bn": b["name"], "overall": o["overall"], "n": len(o["i"]), "objectives": [{"id": x["id"], "t": x["t"], "progress": x["progress"]} for x in o["i"][:4]]})
        if mt.get("sla"):
            s = board_sla(b["id"])
            sla.append({"b": b["id"], "bn": b["name"], "breached": len(s["breached"]), "at_risk": len(s["at_risk"]), "ok": s["ok"], "items": [{**x, "bn": b["name"]} for x in s["breached"][:3]]})
    out.update({"sprints": sps, "okr": okr, "sla": sla, "agents": agent_stats(scope, days)})
    return out


# ---- velocity and retrospectives (scrum, shape up, scrumban)
def board_velocity(bid, n=8):
    rs = rows("SELECT * FROM sprints WHERE board_id=? ORDER BY id DESC LIMIT ?", (bid, n))
    out = []
    for sp in reversed(rs):
        ts = rows("SELECT status, points FROM tasks WHERE sprint_id=?", (sp["id"],))
        pts = any(t["points"] for t in ts)
        w = lambda t: (t["points"] or 1) if pts else 1
        out.append({"id": sp["id"], "n": sp["name"], "st": sp["state"], "from": sp["start"], "to": sp["end"], "committed": sp["committed"] or 0,
                    "done": sum(w(t) for t in ts if t["status"] == "done"), "scope": sum(w(t) for t in ts if t["status"] != "declined"), "pts": pts})
    closed = [x["done"] for x in out if x["st"] == "closed"]
    return {"b": bid, "i": out, "avg": round(sum(closed) / len(closed), 1) if closed else None}


def retro_get(sid):
    sp = rows("SELECT * FROM sprints WHERE id=?", (sid,))
    if not sp:
        raise ApiError("nf", 404)
    r = rows("SELECT * FROM retros WHERE sprint_id=?", (sid,))
    ts = rows("SELECT status, points FROM tasks WHERE sprint_id=?", (sid,))
    o = {"id": sid, "n": sp[0]["name"], "st": sp[0]["state"], "done": sum(1 for t in ts if t["status"] == "done"), "total": len(ts), "committed": sp[0]["committed"] or 0}
    if r:
        o.update({"well": r[0]["well"], "improve": r[0]["improve"], "actions": json.loads(r[0]["actions"] or "[]"), "by": r[0]["author"], "c": r[0]["created"]})
    return o


def retro_save(actor, sid, body):
    sp = rows("SELECT * FROM sprints WHERE id=?", (sid,))
    if not sp:
        raise ApiError("nf", 404)
    sp = sp[0]
    well, imp = str(body.get("well") or "").strip()[:4000], str(body.get("improve") or "").strip()[:4000]
    raw = body.get("actions") or []
    raw = [x for x in raw.splitlines()] if isinstance(raw, str) else list(raw)
    acts = [" ".join(str(x).split())[:200] for x in raw if str(x).strip()][:20]
    old = rows("SELECT actions FROM retros WHERE sprint_id=?", (sid,))
    prev = json.loads(old[0]["actions"]) if old else []
    done_texts = {a["t"]: a.get("task") for a in prev}
    items = []
    for a in acts:
        tk = done_texts.get(a)
        if not tk and body.get("make_tasks", True):
            res = crud(actor, "POST", "tasks", None, {"board_id": sp["board_id"], "title": "Retro action: " + a, "status": "backlog", "labels": "retro", "priority": 2, "channel": "retro"}, {}, False)
            tk = res["id"]
        items.append({"t": a, **({"task": tk} if tk else {})})
    with lock:
        db.execute("INSERT INTO retros(sprint_id,well,improve,actions,author) VALUES(?,?,?,?,?) ON CONFLICT(sprint_id) DO UPDATE SET well=excluded.well, improve=excluded.improve, actions=excluded.actions, author=excluded.author",
                   (sid, well, imp, json.dumps(items, separators=(",", ":")), actor))
        emit(actor, "sprint.updated", sid, sp["board_id"], {"retro": 1})
    return retro_get(sid)


# ---- move a task (and an epic's children) to another board, keeping everything consistent
def move_task(actor, tid, body):
    r = rows("SELECT * FROM tasks WHERE id=?", (tid,))
    if not r:
        raise ApiError("nf", 404)
    t = r[0]
    try:
        dest = int(body.get("board") or body.get("b") or body.get("board_id") or 0)
    except (TypeError, ValueError):
        raise ApiError("bad", 400, "b")
    if not dest:
        raise ApiError("missing", 400, "b")
    dest_b = rows("SELECT * FROM boards WHERE id=?", (dest,))
    if not dest_b:
        raise ApiError("nf", 404, "b")
    src = t["board_id"]
    if dest == src:
        raise ApiError("bad", 400, "b")
    src_b = rows("SELECT * FROM boards WHERE id=?", (src,))[0]
    sprint = body.get("sprint", body.get("s"))
    if sprint not in (None, ""):
        sprint = int(sprint)
        if not rows("SELECT id FROM sprints WHERE id=? AND board_id=?", (sprint, dest)):
            raise ApiError("bad", 400, "s")
    else:
        sprint = None
    kids = body.get("children", True) is not False
    force = bool(body.get("force")) and _priv(getattr(_ctx, "role", "agent"))
    warns = []
    with lock:
        ids = [tid]
        if t["type"] in ("epic", "story"):
            ch = [x[0] for x in descendants(tid) if rows("SELECT id FROM tasks WHERE id=? AND board_id=?", (x[0], src))]
            if kids:
                ids += ch
            elif ch:
                db.execute("UPDATE tasks SET parent=NULL WHERE parent=?", (tid,))
        moving = rows("SELECT * FROM tasks WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)
        meth, cfg = board_cfg(dest)
        if METHODS.get(meth, {}).get("wip"):         # the destination's work-in-progress limits apply to everything arriving
            arrive = {}
            for m_ in moving:
                arrive[m_["status"]] = arrive.get(m_["status"], 0) + 1
            for st_, n_ in arrive.items():
                lim = (cfg.get("wip") or {}).get(st_)
                if lim:
                    cur = db.execute("SELECT COUNT(*) FROM tasks WHERE board_id=? AND status=?", (dest, st_)).fetchone()[0]
                    if cur + n_ > lim:
                        if cfg.get("wip_mode") == "block" and not force:
                            raise ApiError("wip", 409, st_)
                        warns.append("WIP limit for %s will be exceeded on the new board (%d of %d)" % (st_, cur + n_, lim))
        act = rows("SELECT id FROM sprints WHERE board_id=? AND state='active' ORDER BY id DESC LIMIT 1", (dest,)) if board_has_sprints(dest) else []
        same_dod = (src_b["dod"] or "") == (dest_b[0]["dod"] or "")
        main_sprint = None
        for m_ in moving:
            ns = None if m_["status"] in ("done", "declined") else (sprint if sprint is not None else (act[0]["id"] if act else None))
            if m_["id"] == tid:
                main_sprint = ns
            parent = m_["parent"] if m_["id"] != tid and t["type"] in ("epic", "story") else None
            db.execute("UPDATE tasks SET board_id=?, sprint_id=?, parent=?, dod_done=?, nudge='', updated=CURRENT_TIMESTAMP, version=COALESCE(version,1)+1 WHERE id=?",
                       (dest, ns, parent, m_["dod_done"] if same_dod else "", m_["id"]))
            db.execute("UPDATE docs SET board_id=? WHERE scope='task' AND ref_id=?", (dest, m_["id"]))
            db.execute("UPDATE asks SET board_id=? WHERE task_id=?", (dest, m_["id"]))
            db.execute("INSERT INTO comments(task_id,actor,body) VALUES(?,?,?)", (m_["id"], actor, "Moved from '%s' to '%s'" % (src_b["name"], dest_b[0]["name"])))
            emit(actor, "task.moved", m_["id"], src, {"b": dest})
            emit(actor, "task.moved", m_["id"], dest, cdict("tasks", rows("SELECT * FROM tasks WHERE id=?", (m_["id"],))[0], False))
            if m_["assignee"]:
                notify(m_["assignee"], "task.moved", actor, m_["id"], dest, "#%d '%s' moved from '%s' to '%s' by %s" % (m_["id"], m_["title"][:60], src_b["name"], dest_b[0]["name"], actor), {"b": dest})
    out = {"id": tid, "b": dest, "from": src, "s": main_sprint, "moved": ids[1:], "w": warns}
    return {k: v for k, v in out.items() if v not in (None, [], "")}


# ---- stage-gate (waterfall), OKR progress, support SLAs
def ensure_phases(bid):
    """A waterfall board without phases gets its default ones (the first is started)."""
    m, cfg = board_cfg(bid)
    if m != "waterfall" or db.execute("SELECT 1 FROM sprints WHERE board_id=? LIMIT 1", (bid,)).fetchone():
        return
    names = cfg.get("phases") or METHODS["waterfall"]["phases"]
    ids = []
    for n in names:
        sid = db.execute("INSERT INTO sprints(board_id,name,goal,state) VALUES(?,?,?,'planned')", (bid, n, "")).lastrowid
        emit("system", "sprint.created", sid, bid, {"n": n})
        ids.append(sid)
    sprint_start("system", ids[0])


def close_phase_aware(actor, sid, to=None, force=False):
    """Close a sprint. On waterfall boards the gate must be open (all work done) unless forced, and the next phase starts automatically."""
    r = sprint_close(actor, sid, move_to=to, force=force)
    sp = rows("SELECT board_id FROM sprints WHERE id=?", (sid,))[0]
    if board_cfg(sp["board_id"])[0] == "waterfall" and not rows("SELECT id FROM sprints WHERE board_id=? AND state='active'", (sp["board_id"],)):
        nx = rows("SELECT id, name FROM sprints WHERE board_id=? AND state='planned' ORDER BY id LIMIT 1", (sp["board_id"],))
        if nx:
            sprint_start(actor, nx[0]["id"])
            r["next"] = {"id": nx[0]["id"], "n": nx[0]["name"]}
    return r


def board_okr(bid):
    ts = rows("SELECT id, title, status, assignee, parent, type, target, current FROM tasks WHERE board_id=? AND status!='declined' ORDER BY id", (bid,))

    def kr(t):
        tg, cur = t["target"] or 0, t["current"] or 0
        pct = min(1.0, cur / tg) if tg > 0 else (1.0 if t["status"] == "done" else 0.0)
        return {k: v for k, v in {"id": t["id"], "t": t["title"], "as": t["assignee"], "st": t["status"], "target": t["target"], "current": cur, "pct": round(pct, 2)}.items() if v not in (None, "")}
    out = []
    for o in (t for t in ts if t["type"] == "epic"):
        krs = [kr(t) for t in ts if t["parent"] == o["id"]]
        out.append({"id": o["id"], "t": o["title"], "as": o["assignee"], "st": o["status"], "progress": round(sum(k["pct"] for k in krs) / len(krs), 2) if krs else 0.0, "krs": krs})
    return {"b": bid, "i": out, "loose": [kr(t) for t in ts if t["type"] != "epic" and not t["parent"]],
            "overall": round(sum(o["progress"] for o in out) / len(out), 2) if out else 0.0}


def sla_hours(cfg):
    base = {"1": 4, "2": 24, "3": 72, "4": 168}
    base.update({str(k): int(v) for k, v in (cfg.get("sla_hours") or {}).items()})
    return base


def board_sla(bid):
    m, cfg = board_cfg(bid)
    sl = sla_hours(cfg)
    now = _datetime.datetime.now(_datetime.timezone.utc).replace(tzinfo=None)
    out = {"breached": [], "at_risk": [], "ok": 0}
    for t in rows("SELECT id, title, priority, created, assignee FROM tasks WHERE board_id=? AND status NOT IN ('done','declined')", (bid,)):
        age = (now - _tsd(t["created"])).total_seconds() / 3600
        lim = sl.get(str(t["priority"]), 72)
        it = {"id": t["id"], "t": t["title"], "pr": t["priority"], "as": t["assignee"], "age_h": round(age, 1), "sla_h": lim}
        if age > lim:
            out["breached"].append(it)
        elif age > 0.75 * lim:
            out["at_risk"].append(it)
        else:
            out["ok"] += 1
    out["breached"].sort(key=lambda x: -(x["age_h"] - x["sla_h"]))
    return {"b": bid, "mt": m, "sla_h": sl, **out}


def sla_tick(actor="system"):
    """Support boards: tell the assignee and the supervisors once when a request passes its response-time target."""
    for b in rows("SELECT id, name FROM boards WHERE method='support'"):
        for it in board_sla(b["id"])["breached"]:
            key = "sla:%d" % it["id"]
            if get_setting(key):
                continue
            set_setting(key, "1")
            notify(it["as"], "sla.breach", actor, it["id"], b["id"], "#%d '%s' has passed its %dh response target (open %gh)" % (it["id"], it["t"][:60], it["sla_h"], it["age_h"]), {"sla": it["sla_h"]})
            escalate("sla-breach", actor, it["id"], b["id"], "#%d '%s' on '%s' breached its %dh SLA (p%d)" % (it["id"], it["t"][:60], b["name"], it["sla_h"], it["pr"]), skip=(it["as"],))


# ---- cadence helpers used by the periodic tick (caller holds `lock`)
def rollover_tick(actor="system"):
    """auto_rollover boards: when the active sprint has ended, close it (unfinished work carries over) and start the next one."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    for sp in rows("SELECT s.*, b.cfg FROM sprints s JOIN boards b ON b.id=s.board_id WHERE s.state='active' AND s.\"end\"!='' AND s.\"end\"<?", (today,)):
        try:
            cfg = json.loads(sp["cfg"] or "{}")
        except ValueError:
            continue
        if not cfg.get("auto_rollover") or not board_has_sprints(sp["board_id"]) or board_cfg(sp["board_id"])[0] == "waterfall":
            continue
        nxt = rows("SELECT id FROM sprints WHERE board_id=? AND state='planned' ORDER BY id LIMIT 1", (sp["board_id"],))
        if nxt:
            nid = nxt[0]["id"]
        else:
            n = db.execute("SELECT COUNT(*) FROM sprints WHERE board_id=?", (sp["board_id"],)).fetchone()[0] + 1
            nid = db.execute("INSERT INTO sprints(board_id,name,goal,state) VALUES(?,?,?,'planned')", (sp["board_id"], "%s %d" % (METHODS[board_cfg(sp["board_id"])[0]]["term"] or "Sprint", n), "")).lastrowid
            emit(actor, "sprint.created", nid, sp["board_id"], {"auto": 1})
        sprint_close(actor, sp["id"], move_to=nid)
        sprint_start(actor, nid)


def replenish_tick(actor="system"):
    """scrumban: when the ready column runs low, tell the supervisors once."""
    for b in rows("SELECT id, name, method, cfg FROM boards WHERE method='scrumban'"):
        try:
            n = json.loads(b["cfg"] or "{}").get("replenish_below")
        except ValueError:
            continue
        if not n:
            continue
        ready = db.execute("SELECT COUNT(*) FROM tasks WHERE board_id=? AND status='todo'", (b["id"],)).fetchone()[0]
        key = "replenish:%d" % b["id"]
        if ready < n and not get_setting(key):
            set_setting(key, "1")
            escalate("replenish", actor, None, b["id"], "'%s' is running low on ready work (%d, wanted at least %d) - replenish the Ready column" % (b["name"], ready, n))
        elif ready >= n and get_setting(key):
            set_setting(key, "")


# ---------------------------------------------------------------- token-saving calls: briefing, next, finish, batch, guides, usage meter
def _first(s_, n):
    s_ = " ".join(str(s_ or "").split())
    return s_ if len(s_) <= n else s_[:n - 1] + "…"


def task_since(tid, since):
    """Only what changed on a task after event `since` (the `ev` of an earlier read): {same:1} when nothing did."""
    r = rows("SELECT * FROM tasks WHERE id=?", (tid,))
    if not r:
        raise ApiError("nf", 404)
    last, ch, cc = last_event(), {}, []
    for e in rows("SELECT id, type, payload FROM events WHERE id>? AND type LIKE 'task.%' AND payload LIKE ? ORDER BY id", (since, '{"id":%d,"b":%%' % tid)):
        try:
            d = json.loads(e["payload"]).get("d") or {}
        except ValueError:
            continue
        if e["type"] == "task.commented" and d.get("cm_id"):
            c = rows("SELECT actor, body FROM comments WHERE id=?", (d["cm_id"],))
            if c:
                cc.append({"by": c[0]["actor"], "c": _first(c[0]["body"], 220)})
        else:
            ch.update({k: (_first(v, 200) if k == "d" and isinstance(v, str) else v) for k, v in d.items() if k in TASK_KEYS or k == "d"})
    if not ch and not cc:
        return {"id": tid, "same": 1, "ev": last}
    return {"id": tid, "ev": last, **({"ch": ch} if ch else {}), **({"cc": cc[-5:]} if cc else {})}


def decisions_get(tid):
    if not rows("SELECT id FROM tasks WHERE id=?", (tid,)):
        raise ApiError("nf", 404)
    return {"i": [{"id": d["id"], "by": d["actor"], "ts": d["ts"], "c": d["body"]} for d in rows("SELECT * FROM decisions WHERE task_id=? ORDER BY id", (tid,))]}


def decision_add(actor, tid, body):
    """A short, settled decision on a story or epic. Everything below it sees the latest ones in its brief."""
    text = " ".join(str(body.get("c") or body.get("text") or "").split())[:300]
    if not text:
        raise ApiError("missing", 400, "c")
    with lock:
        t = rows("SELECT board_id FROM tasks WHERE id=?", (tid,))
        if not t:
            raise ApiError("nf", 404)
        did = db.execute("INSERT INTO decisions(task_id, body, actor) VALUES(?,?,?)", (tid, text, actor)).lastrowid
        emit(actor, "task.decision", tid, t[0]["board_id"], {"dc": did})
    return {"id": did, "tk": tid}


def decisions_for(tid, n=5):
    """The latest decisions on this task and the epic/story above it: [{t: task, c: text}]."""
    out, seen, cur = [], set(), tid
    while cur and cur not in seen and len(out) < n:
        seen.add(cur)
        for d in rows("SELECT body FROM decisions WHERE task_id=? ORDER BY id DESC LIMIT ?", (cur, n - len(out))):
            out.append({"t": cur, "c": _first(d["body"], 160)})
        up = rows("SELECT parent FROM tasks WHERE id=?", (cur,))
        cur = up[0]["parent"] if up else None
    return out


SCAFFOLDS = {"standard": [("Build", "task"), ("Test", "task"), ("Docs", "chore"), ("Review", "task")],
             "bugfix": [("Reproduce", "spike"), ("Fix", "bug"), ("Regression test", "task"), ("Release note", "chore")],
             "research": [("Question and plan", "spike"), ("Findings", "spike"), ("Decision", "task")]}


def task_scaffold(actor, role, tid, body):
    """Create the usual child tasks under a story or epic in one call: preset (standard, bugfix, research) or titles=[...] (strings or {t, ty, as});
    chain=true makes each depend on the one before."""
    r = rows("SELECT * FROM tasks WHERE id=?", (tid,))
    if not r:
        raise ApiError("nf", 404)
    p = r[0]
    if p["type"] not in ("epic", "story"):
        raise ApiError("bad", 400, "ty")
    items = body.get("titles")
    if not items:
        items = SCAFFOLDS.get(str(body.get("preset") or "standard").lower())
        if not items:
            raise ApiError("bad", 400, "preset")
    if not isinstance(items, list) or len(items) > 12:
        raise ApiError("bad", 400, "titles")
    made, prev = [], None
    for it in items:
        if isinstance(it, dict):
            t, ty, who = it.get("t"), it.get("ty"), it.get("as")
        elif isinstance(it, (list, tuple)):
            t, ty, who = (list(it) + [None, None])[:3]
        else:
            t, ty, who = it, None, None
        if not str(t or "").strip() or (ty and ty not in TASK_TYPES):
            raise ApiError("bad", 400, "titles")
        ty = ty or ("story" if p["type"] == "epic" else "task")
        res = crud(actor, "POST", "tasks", None, {"b": p["board_id"], "t": ("%s: %s" % (str(t).strip()[:50], p["title"][:50])).strip(), "ty": ty, "pa": tid, "ch": p["channel"] or "scaffold",
                                                   "pr": p["priority"], **({"as": who} if who else {}), **({"dp": [prev]} if (body.get("chain") and prev) else {}), "st": "backlog"}, {}, False)
        prev = res["id"]
        made.append(prev)
    return {"id": tid, "made": made}


def task_brief(tid, deep=False):
    """Everything needed to start on a task in ~60 tokens (deep: ~250): title, status, owner, open deps, one-line description, latest comment."""
    r = rows("SELECT * FROM tasks WHERE id=?", (tid,))
    if not r:
        raise ApiError("nf", 404)
    t = r[0]
    o = {"id": tid, "t": t["title"], "st": t["status"], "pr": t["priority"], "as": t["assignee"], "pt": t["points"] or None, "ty": t["type"] if t["type"] != "task" else None,
         "sum": _first(t["description"], 1500 if deep else 140) or None}
    od = open_dep_ids(t["depends"])
    if od:
        o["dpo"] = od
    if t["accept"]:
        o["ac"] = t["accept"].splitlines()[:12]
    if t["play"]:
        o["po"] = _first(t["play"], 600 if deep else 240)
    if deep:
        if t["needs"]:
            o["nd"] = t["needs"].splitlines()[:8]
        if t["makes"]:
            o["mk"] = t["makes"].splitlines()[:8]
        dec = decisions_for(tid)
        if dec:
            o["dec"] = dec
        rl = sorted(task_rules(t))
        if rl:
            o["rules"] = rl
    o["ev"] = last_event()
    cm = rows("SELECT actor, body FROM comments WHERE task_id=? ORDER BY id DESC LIMIT ?", (tid, 3 if deep else 1))
    if cm:
        o["cc"] = [{"by": c["actor"], "c": _first(c["body"], 220 if deep else 100)} for c in reversed(cm)]
    if deep:
        dc = [d["title"] for d in rows("SELECT title FROM docs WHERE (scope='task' AND ref_id=?) OR (scope='board' AND ref_id=?) ORDER BY id LIMIT 8", (tid, t["board_id"]))]
        if dc:
            o["rules"] = dc
        n_at = db.execute("SELECT COUNT(*) FROM attachments WHERE task_id=?", (tid,)).fetchone()[0]
        if n_at:
            o["att"] = n_at
    return {k: v for k, v in o.items() if v not in (None, "", [])}


def _avail(me, since, scope):
    base = "status='todo' AND (claimed_by='' OR claim_until<?) AND (assignee='' OR assignee=? COLLATE NOCASE) AND " + READY_SQL
    args = [now_s(), me]
    fr, fa = _in("board_id", scope)
    if since is not None:
        ids = set()
        for e in rows("SELECT payload FROM events WHERE id>? AND type LIKE 'task.%'", (since,)):
            try:
                ids.add(json.loads(e["payload"]).get("id"))
            except ValueError:
                pass
        ids = [i for i in ids if isinstance(i, int)]
        if not ids:
            return 0
        base += " AND id IN (%s)" % ",".join("?" * len(ids))
        args += ids
    return db.execute("SELECT COUNT(*) FROM tasks WHERE " + base + fr, args + fa).fetchone()[0]


def _briefing_once(actor, q):
    """One call instead of inbox + dashboard + risks. With `since=<c from the last call>` it returns only what changed, or just {"c":N} when nothing did."""
    me = _qv(q, "as", "agent") or (actor if actor and actor.lower() not in GENERIC_ACTORS else "")
    if not me:
        raise ApiError("missing", 400, "as")
    since = qint(q, "since", None)
    scope = scope_of(q)
    cur = last_event()
    out = {"c": cur}
    with lock:
        items = rows("SELECT * FROM notifications WHERE agent=? AND read=0 ORDER BY id LIMIT 30", (me,))
        if items:
            out["in"] = [ndict(i) for i in items]
            if q.get("ack", ["1"])[0] not in ("0", ""):
                db.execute("UPDATE notifications SET read=1 WHERE id IN (%s)" % ",".join("?" * len(items)), [i["id"] for i in items])
    nw = _avail(me, since, scope)
    if nw:
        out["nw"] = nw
    if since is None or q.get("full", ["0"])[0] not in ("0", ""):
        fr, fa = _in("board_id", scope)
        work = rows("SELECT * FROM tasks WHERE assignee=? COLLATE NOCASE AND status NOT IN ('done','declined')" + fr + " ORDER BY CASE status WHEN 'in_progress' THEN 0 WHEN 'review' THEN 1 WHEN 'todo' THEN 2 WHEN 'blocked' THEN 3 ELSE 4 END, priority, id LIMIT 15", [me] + fa)
        if work:
            out["work"] = [{k: v for k, v in cdict("tasks", t, False, ("description",)).items() if k in ("id", "t", "st", "pr", "b", "cu", "dpo")} for t in work]
        att = attention(scope)[:5]
        if att:
            out["att"] = [{"k": a["k"], "sev": a["sev"], "m": a["m"]} for a in att]
        rk = [{"n": r["n"], "h": r["health"]} for r in active_risks(scope) if r["health"] != "green"]
        if rk:
            out["rk"] = rk
    return out


def briefing(actor, q):
    """One briefing call. With wait=<seconds, max 30> and a since cursor the request is held open until something changes: a cheap alternative to polling."""
    res = _briefing_once(actor, q)
    wait = max(0, min(qint(q, "wait", 0) or 0, 30))
    if wait and qint(q, "since", None) is not None and set(res) == {"c"}:
        end = time.time() + wait
        while time.time() < end:
            with inbox_cv:
                inbox_cv.wait(min(1.0, max(0.05, end - time.time())))
            res = _briefing_once(actor, q)
            if set(res) != {"c"}:
                break
    return res


def next_task(actor, body):
    """claim_next + the context to start working, in one call."""
    res = claim_next(actor, body)
    if res.get("none"):
        return res
    return {**res, "task": task_brief(res["id"], deep=True)}


def task_finish(actor, role, tid, body):
    """Attach outputs, comment, record PR/sha, then close, send to review, or hand off - one call."""
    check_holder(actor, role, tid)
    check_move(role, tid, {"st": body.get("st") or ("done" if not (body.get("review") or body.get("to")) else None), "force": body.get("force")})
    t = rows("SELECT * FROM tasks WHERE id=?", (tid,))
    if not t:
        raise ApiError("nf", 404)
    t = t[0]
    summary = str(body.get("summary") or "").strip()[:2000]
    if not summary:
        raise ApiError("missing", 400, "summary")
    review, to = str(body.get("review") or "").strip(), str(body.get("to") or "").strip()
    st = body.get("st") or "done"
    if st not in STATUSES:
        raise ApiError("bad", 400, "st")
    patch = {}
    for k_, src in (("pu", "pr"), ("pu", "pu"), ("sha", "sha"), ("dn", "dn")):
        if body.get(src) not in (None, "", []):
            patch[k_] = body[src]
    patch["po"] = summary[:600]
    closing = not review and not to
    if closing and st == "done" and t["status"] != "done":
        check_done_gate(tid, t["board_id"], ",".join(str(i) for i in patch["dn"]) if isinstance(patch.get("dn"), list) else patch.get("dn", t["dod_done"]))   # fail before any side effect
    did = []
    if isinstance(body.get("cost"), dict):
        cost_log(actor, {**body["cost"], "task": tid, "as": body.get("as")})
        did.append("cost")
    for a in (body.get("attach") or []):
        att_add(actor, tid, a)
        did.append("attach")
    comment_add(actor, tid, {"c": summary}, notify_assignee=False)
    did.append("comment")
    if closing:
        patch["st"] = st
    if patch:
        crud(actor, "PATCH", "tasks", tid, patch, {}, False)
        did.append("update")
    if review:
        review_request(actor, tid, {"reviewer": review, "note": summary[:300], "as": body.get("as")})
        did.append("review")
    elif to:
        task_handoff(actor, tid, {"to": to, "summary": summary, "next": body.get("next", ""), "st": body.get("st")})
        did.append("handoff")
    return {"id": tid, "did": did, "st": rows("SELECT status FROM tasks WHERE id=?", (tid,))[0]["status"]}


GUIDES = {
    "agent": "Work loop: briefing (no args after the first call: it returns only changes, or just {c}) -> next (claims the best task and returns it with context) -> work -> finish {id,summary,pr,sha,to|review}. "
             "Reply to inbox items with their `do` hint. heartbeat with one line while busy. lock shared things (env:test). Prefer tasks fields=… fmt=t for lists, task brief=1 for one task, help(op) before an unfamiliar tool.",
    "coder": "1 briefing. 2 next (read task.sum, rules, dpo). 3 lock repo/env if shared. 4 commit with '#<id>' in the message. 5 finish {id,summary,pr,sha,review:<reviewer>} (or no review for done). "
             "Blocked? set_task st=blocked bl=why bb=[ids]; ask {to,q,due} when you need an answer; handoff when someone else continues. Tick dn items before done if the board has a definition of done.",
    "reviewer": "Check finished work against the card's acceptance criteria (task brief=1 shows ac) and record it with verify {id,ok,note}; you cannot verify work you did. briefing shows review.requested items with do='decide_review id=N'. Read the task with task brief=1 and its attachments, then decide_review {id,decision:approve|changes,note}. changes needs a note and sends the task back. Do not set done yourself while a review is open.",
    "supervisor": "briefing returns att (needs attention) and rk (sprints at risk). Use portfolio tm=<type> for several boards, attention for the list, risks, agent_stats for load. Escalations arrive as esc.* items. Unblock by reassigning (set_task as=…), answering asks, or freeze_env/unfreeze_env. Force closes only when the gate is wrong.",
    "planner": "Shape work as epic > story > task: ty (epic/story/task/bug/spike/chore), pa (parent: a story or epic; stories sit under epics), pt (points), dp (dependencies), lb. Use specs for decisions (search first, append instead of rewrite). board_flow / board_velocity / okr / board_sla show how the method is going; set_board changes method and limits. Use as='auto' with a skill label to route.",
}


def guide_get(q):
    role = (_qv(q, "role") or "agent").lower()
    if role not in GUIDES:
        raise ApiError("bad", 400, "role")
    return {"role": role, "roles": sorted(GUIDES), "g": GUIDES[role]}


# ---- usage meter: approximate tokens (bytes/4) spent per agent and per route since the server started
USAGE = {}
USAGE_SINCE = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
_us_lock = threading.Lock()


def route_label(path):
    return re.sub(r"/\d+", "/{id}", path.replace("/api", "", 1)) or "/"


def usage_hit(actor, route, out_b, in_b):
    with _us_lock:
        e = USAGE.setdefault((str(actor), route), [0, 0, 0])
        e[0] += 1
        e[1] += out_b
        e[2] += in_b


def usage_report(top=8):
    ag, rt = {}, {}
    with _us_lock:
        for (a, r), (n, ob, ib) in USAGE.items():
            x = ag.setdefault(a, {"n": a, "calls": 0, "out": 0, "in": 0, "routes": []})
            x["calls"] += n
            x["out"] += ob // 4
            x["in"] += ib // 4
            x["routes"].append({"r": r, "calls": n, "t": (ob + ib) // 4})
            y = rt.setdefault(r, {"r": r, "calls": 0, "t": 0})
            y["calls"] += n
            y["t"] += (ob + ib) // 4
    for x in ag.values():
        x["routes"] = sorted(x["routes"], key=lambda z: -z["t"])[:5]
        x["t"] = x["out"] + x["in"]
    return {"since": USAGE_SINCE, "unit": "tokens (bytes/4, responses 'out' and requests 'in')",
            "agents": sorted(ag.values(), key=lambda z: -z["t"])[:top], "routes": sorted(rt.values(), key=lambda z: -z["t"])[:top]}


# ---- spec views: outline and one section instead of the whole body
def spec_view(o, q):
    if "outline" not in q and not _qv(q, "section"):
        return o
    body = o.get("d", "")
    heads, pos = [], 0
    lines = body.split("\n")
    marks = []
    for i, ln in enumerate(lines):
        m = re.match(r"^(#{1,4})\s+(.*)", ln)
        if m:
            marks.append((i, len(m.group(1)), m.group(2).strip()))
    sections = []
    for k, (i, lvl, h) in enumerate(marks):
        end = marks[k + 1][0] if k + 1 < len(marks) else len(lines)
        sections.append({"i": k, "h": h, "lvl": lvl, "text": "\n".join(lines[i:end]).strip()})
    if "outline" in q:
        return {"id": o["id"], "t": o["t"], "rv": o.get("rv"), "len": len(body), "outline": [{"i": x["i"], "h": x["h"], "lvl": x["lvl"], "len": len(x["text"])} for x in sections]}
    want = _qv(q, "section")
    for x in sections:
        if str(x["i"]) == want or x["h"].lower() == want.lower():
            return {"id": o["id"], "t": o["t"], "rv": o.get("rv"), "h": x["h"], "d": x["text"]}
    raise ApiError("nf", 404, "section")


# ---------------------------------------------------------------- sprint lifecycle
def sprint_close(actor, sid, move_to=None, force=False):
    """Close a sprint; unfinished tasks go to `move_to` (sprint id | 'backlog'); default = next planned sprint, else backlog."""
    s = rows("SELECT * FROM sprints WHERE id=?", (sid,))
    if not s:
        raise ApiError("nf", 404)
    s = s[0]
    if board_cfg(s["board_id"])[0] == "waterfall" and not force:
        open_n = db.execute("SELECT COUNT(*) FROM tasks WHERE sprint_id=? AND status NOT IN ('done','declined')", (sid,)).fetchone()[0]
        if open_n:
            raise ApiError("gate", 409, "open")
    if move_to in (None, ""):
        nx = rows("SELECT id FROM sprints WHERE board_id=? AND state='planned' AND id!=? ORDER BY id LIMIT 1", (s["board_id"], sid))
        target = nx[0]["id"] if nx else None
    elif str(move_to).lower() in ("backlog", "none", "0"):
        target = None
    else:
        target = int(move_to)
        if not rows("SELECT id FROM sprints WHERE id=? AND board_id=?", (target, s["board_id"])):
            raise ApiError("bad", 400, "to")
    ids = [r["id"] for r in rows("SELECT id FROM tasks WHERE sprint_id=? AND status NOT IN ('done','declined')", (sid,))]
    for i in ids:
        db.execute("UPDATE tasks SET sprint_id=?, updated=CURRENT_TIMESTAMP WHERE id=?", (target, i))
        emit(actor, "task.updated", i, s["board_id"], {"s": target})
    db.execute("UPDATE sprints SET state='closed' WHERE id=?", (sid,))
    emit(actor, "sprint.updated", sid, s["board_id"], {"st": "closed"})
    chat_event("sprint", "'%s' closed on %s" % (s["name"], _board_name(s["board_id"])), None, s["board_id"])
    allts = rows("SELECT assignee, status FROM tasks WHERE sprint_id=? OR id IN (%s)" % ",".join("?" * len(ids)) if ids else "SELECT assignee, status FROM tasks WHERE sprint_id=?", ([sid] + ids) if ids else [sid])
    done_n = sum(1 for t in rows("SELECT status FROM tasks WHERE sprint_id=?", (sid,)) if t["status"] == "done")
    for a in {t["assignee"] for t in allts if t["assignee"]}:
        notify(a, "sprint.closed", actor, None, s["board_id"], "'%s' closed: %d done, %d unfinished moved on. Add your notes to the retrospective." % (s["name"], done_n, len(ids)), {"sprint": sid})
    return {"id": sid, "st": "closed", "mv": len(ids), "to": target}


def close_other_active(actor, sid, bid):
    for p in rows("SELECT id FROM sprints WHERE board_id=? AND state='active' AND id!=?", (bid, sid)):
        sprint_close(actor, p["id"], move_to=sid)


def sprint_start(actor, sid):
    s = rows("SELECT * FROM sprints WHERE id=?", (sid,))
    if not s:
        raise ApiError("nf", 404)
    s = s[0]
    close_other_active(actor, sid, s["board_id"])
    st = s["start"] or time.strftime("%Y-%m-%d")
    en = s["end"] or time.strftime("%Y-%m-%d", time.gmtime(time.time() + sprint_days_for(s["board_id"]) * 86400))
    db.execute("UPDATE sprints SET state='active', start=?, \"end\"=?, committed=? WHERE id=?", (st, en, sprint_weight(sid), sid))
    emit(actor, "sprint.updated", sid, s["board_id"], {"st": "active", "from": st, "to": en})
    chat_event("sprint", "'%s' started on %s (%s to %s)" % (s["name"], _board_name(s["board_id"]), st, en), None, s["board_id"])
    return {"id": sid, "st": "active", "from": st, "to": en}


# ---------------------------------------------------------------- sprint statistics + risks
import datetime as _datetime

SEV = {"high": 3, "med": 2, "low": 1}


def _d(v, default=None):
    try:
        return _datetime.datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except Exception:
        return default


def sprint_stats(sid):
    """Progress, pace, projection, burndown and risk list for one sprint (read-only)."""
    sr = rows("SELECT * FROM sprints WHERE id=?", (sid,))
    if not sr:
        raise ApiError("nf", 404)
    sp = sr[0]
    ts = rows("SELECT id, title, status, priority, assignee, labels, nudge, created, updated, points, depends FROM tasks WHERE sprint_id=?", (sid,))
    today = _datetime.datetime.now(_datetime.timezone.utc).replace(tzinfo=None).date()
    start = _d(sp["start"]) or _d(sp["created"], today)
    end = _d(sp["end"]) or (start + _datetime.timedelta(days=sprint_days_for(sp["board_id"])))
    active = sp["state"] == "active"
    by = {}
    for t in ts:
        by[t["status"]] = by.get(t["status"], 0) + 1
    declined = by.get("declined", 0)
    use_pts = any(t["points"] for t in ts)
    wt = (lambda t: t["points"] or 1) if use_pts else (lambda t: 1)
    scope = sum(wt(t) for t in ts if t["status"] != "declined")
    done = sum(wt(t) for t in ts if t["status"] == "done")
    open_ = [t for t in ts if t["status"] not in ("done", "declined")]
    open_w = sum(wt(t) for t in open_)
    span = max((end - start).days, 1)
    elapsed = min(max((today - start).days, 0), span)
    left = (end - today).days
    time_pct = round(elapsed / span, 2)
    done_pct = round(done / scope, 2) if scope else 0.0
    pace = round(done / elapsed, 2) if elapsed > 0 else 0.0
    proj = None
    if scope and not open_:
        proj = today.isoformat()
    elif pace > 0:
        proj = (today + _datetime.timedelta(days=int(round(open_w / pace)))).isoformat()
    # burndown: remaining open scope per day (actual from done events, ideal linear)
    ids = {t["id"] for t in ts if t["status"] == "done"}
    wmap = {t["id"]: wt(t) for t in ts}
    doned = {t["id"]: _d(t["updated"], today) for t in ts if t["id"] in ids}   # fallback: last update
    if ids:
        for r in rows("SELECT ts, payload FROM events WHERE type='task.updated' AND payload LIKE '%\"st\":\"done\"%' ORDER BY id"):
            try:
                pj = json.loads(r["payload"])
            except Exception:
                continue
            if pj.get("id") in ids and (pj.get("d") or {}).get("st") == "done":
                doned[pj["id"]] = _d(r["ts"], today)
    burn = []
    for i in range(0, min(span, 90) + 1):
        day = start + _datetime.timedelta(days=i)
        ideal = round(scope * (1 - i / span), 1)
        actual = None
        if day <= today:
            actual = scope - sum(wmap[tid] for tid in ids if (doned.get(tid) or today) <= day)
        burn.append([day.isoformat(), ideal, actual])
    added = sum(1 for t in ts if _d(t["created"], start) > start)
    risks = []

    def risk(k, sev, msg, tids=()):
        risks.append({"k": k, "sev": sev, "m": msg, "ids": sorted(tids)[:20]})
    prio = lambda t: t["priority"] <= 2
    if active:
        if left < 0 and open_:
            risk("overdue", "high", "Sprint ended %d day(s) ago with %d open task(s)" % (-left, len(open_)), [t["id"] for t in open_])
        elif scope == 0:
            risk("empty", "med", "Active sprint has no tasks")
        elif elapsed >= 1 and time_pct - done_pct >= 0.25:
            gap = time_pct - done_pct
            risk("behind", "high" if gap >= 0.4 else "med",
                 "%d%% of the time used but only %d%% done%s" % (round(time_pct * 100), round(done_pct * 100),
                                                               "; projected finish %s (ends %s)" % (proj, end.isoformat()) if proj else "; no tasks finished yet"),
                 [t["id"] for t in open_])
        late = [t["id"] for t in open_ if prio(t) and t["status"] in ("backlog", "todo")]
        if late and time_pct >= 0.5:
            risk("late-start", "high", "%d high-priority task(s) not started with %d%% of the sprint gone" % (len(late), round(time_pct * 100)), late)
    blk = [t for t in open_ if t["status"] == "blocked"]
    if blk:
        risk("blocked", "high" if any(prio(t) for t in blk) else "med", "%d blocked task(s)" % len(blk), [t["id"] for t in blk])
    owner = [t["id"] for t in open_ if is_owner_assignee(t["assignee"])]
    if owner:
        risk("human-wait", "med", "%d task(s) waiting on a human" % len(owner), owner)
    stale = [t["id"] for t in open_ if t["nudge"] or "nudge" in _lbl_set(t["labels"])]
    if stale:
        risk("stale", "med", "%d task(s) with no update for %gh" % (len(stale), NUDGE_HOURS), stale)
    unas = [t["id"] for t in open_ if not t["assignee"]]
    if unas and (active or sp["state"] == "planned"):
        risk("unassigned", "med" if time_pct >= 0.3 and active else "low", "%d open task(s) without an assignee" % len(unas), unas)
    load = {}
    for t in open_:
        if t["assignee"]:
            load.setdefault(t["assignee"], []).append(t["id"])
    for a, l in load.items():
        if len(l) >= 6:
            risk("overload", "med", "%s has %d open tasks" % (a, len(l)), l)
    caps = {r["name"].lower(): r["capacity"] for r in rows("SELECT name, capacity FROM agents WHERE capacity>0")}
    for a in load:
        mine = sum(wt(t) for t in open_ if t["assignee"] == a)
        if caps.get(a.lower()) and mine > caps[a.lower()]:
            risk("capacity", "med", "%s has %d %s open vs capacity %d" % (a, mine, "points" if use_pts else "tasks", caps[a.lower()]), load[a])
    sil = silent_agents()
    sl = [t["id"] for t in open_ if t["status"] == "in_progress" and (t["assignee"] or "").lower() in sil]
    if sl:
        risk("silent-agent", "med", "%d task(s) in progress belong to agents who have gone quiet" % len(sl), sl)
    depw = [t["id"] for t in open_ if t["status"] in ("in_progress", "review") and open_dep_ids(t["depends"])]
    if depw:
        risk("dep-start", "med", "%d task(s) in flight while their dependencies are still open" % len(depw), depw)
    rv = [t["id"] for t in open_ if t["status"] == "review"]
    if len(rv) >= 3:
        risk("review-queue", "low", "%d task(s) waiting in review" % len(rv), rv)
    if scope and added >= 3 and added / scope >= 0.3:
        risk("scope-creep", "low", "%d task(s) added after the sprint started (%d%% of scope)" % (added, round(100 * added / scope)))
    risks.sort(key=lambda r: -SEV[r["sev"]])
    health = "red" if any(r["sev"] == "high" for r in risks) else "amber" if any(r["sev"] == "med" for r in risks) else "green"
    return {"id": sid, "b": sp["board_id"], "n": sp["name"], "g": sp["goal"], "st": sp["state"], "from": start.isoformat(), "to": end.isoformat(),
            "days": {"total": span, "elapsed": elapsed, "left": left},
            "w": {"scope": scope, "done": done, "open": len(open_), "open_w": open_w, "pts": use_pts, "declined": declined, "by": by},
            "pct": {"time": time_pct, "done": done_pct}, "pace": pace, "proj": proj, "added": added,
            "load": {a: len(l) for a, l in sorted(load.items())}, "health": health, "risks": risks, "burn": burn}


def _in(col, bid):
    """SQL fragment for a board scope: None/0 = all boards, int = one board, list = those boards."""
    if bid is None or bid == 0:
        return "", []
    ids = [bid] if isinstance(bid, int) else [int(x) for x in bid]
    if not ids:
        return " AND 1=0", []
    return " AND %s IN (%s)" % (col, ",".join("?" * len(ids))), ids


def scope_ids(q):
    """Cross-board scope from the query: bs=1,2,3 (board ids) and/or tm=<team label>. None = no scope given (all boards)."""
    ids = None
    if _qv(q, "bs"):
        ids = [int(x) for x in re.findall(r"\d+", _qv(q, "bs"))]
    if _qv(q, "tm", "team"):
        tids = [r["id"] for r in rows("SELECT id FROM boards WHERE team=? COLLATE NOCASE", (_qv(q, "tm", "team"),))]
        ids = tids if ids is None else [i for i in ids if i in tids]
    return _clip(ids)


def scope_of(q):
    b = qint(q, "b") or qint(q, "board_id")
    return _clip([b]) if b else scope_ids(q)


def active_risks(bid=None):
    out = []
    fr, fp = _in("board_id", bid)
    for sp in rows("SELECT id FROM sprints WHERE state='active'" + fr, fp):
        st = sprint_stats(sp["id"])
        out.append({k: st[k] for k in ("id", "b", "n", "to", "health", "pct", "proj", "risks")})
    return out


def risk_alerts(actor="system"):
    """Notify everyone with open work in an active sprint when its risk picture changes (once per change)."""
    for sp in rows("SELECT id FROM sprints WHERE state='active'"):
        st = sprint_stats(sp["id"])
        sig = st["health"] + ":" + ",".join("%s%s" % (r["k"], len(r["ids"])) for r in st["risks"])
        old = db.execute("SELECT sig FROM risk_state WHERE sprint_id=?", (sp["id"],)).fetchone()
        if old and old["sig"] == sig:
            continue
        db.execute("INSERT OR REPLACE INTO risk_state(sprint_id, sig) VALUES(?,?)", (sp["id"], sig))
        if st["health"] == "green" and not old:
            continue
        who = {r["assignee"] for r in rows("SELECT assignee FROM tasks WHERE sprint_id=? AND status NOT IN ('done','declined') AND assignee!=''", (sp["id"],))}
        if st["health"] == "green":
            msg = "Sprint '%s' is back on track (%d%% done, %d%% of time used)" % (st["n"], round(st["pct"]["done"] * 100), round(st["pct"]["time"] * 100))
        else:
            msg = "Sprint '%s' is %s: %s" % (st["n"], "AT RISK" if st["health"] == "red" else "watch", "; ".join(r["m"] for r in st["risks"][:3]))
        for a in who:
            notify(a, "sprint.risk", actor, None, st["b"], msg, {"health": st["health"], "sprint": sp["id"]})
        if st["health"] == "red":
            escalate("sprint-red", actor, None, st["b"], msg, {"sprint": sp["id"]}, skip=who)
            post_async(get_setting("alert_url"), msg)


# ---------------------------------------------------------------- tokens, settings, backups, digests, export/import
def lookup_token(t):
    """-> (name, role, boards) or None. Env tokens first, then DB tokens (stored as sha256). boards = the set of board ids the token is limited to, None = all."""
    if t in TOKENS:
        return (*TOKENS[t], None)
    if not t:
        return None
    r = db.execute("SELECT id, name, role, boards FROM tokens WHERE hash=?", (hashlib.sha256(t.encode()).hexdigest(),)).fetchone()
    if r:
        db.execute("UPDATE tokens SET used=CURRENT_TIMESTAMP WHERE id=?", (r["id"],))
        return (r["name"], r["role"], {int(x) for x in re.findall(r"\d+", r["boards"] or "")} or None)
    return None


def auth_open():
    return not TOKENS and not db.execute("SELECT 1 FROM tokens LIMIT 1").fetchone()


def _board_ids(v):
    ids = [int(x) for x in re.findall(r"\d+", ",".join(str(i) for i in v) if isinstance(v, list) else str(v or ""))]
    if ids and len(rows("SELECT id FROM boards WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)) != len(set(ids)):
        raise ApiError("nf", 404, "boards")
    return sorted(set(ids))


def token_create(body):
    """Create a token, or (with id) change which boards an existing one may touch. boards = [ids]; empty = all boards (admin tokens cannot be limited)."""
    has_b = "boards" in body or "bs" in body
    ids = _board_ids(body.get("boards", body.get("bs"))) if has_b else []
    if body.get("id"):
        with lock:
            r = db.execute("SELECT role FROM tokens WHERE id=?", (int(body["id"]),)).fetchone()
            if not r:
                raise ApiError("nf", 404)
            if ids and r["role"] == "admin":
                raise ApiError("bad", 400, "boards")
            db.execute("UPDATE tokens SET boards=? WHERE id=?", (",".join(str(i) for i in ids), int(body["id"])))
        return {"id": int(body["id"]), "bs": ids}
    name = " ".join(str(body.get("n") or body.get("name") or "").split())[:60]
    role = body.get("role") or "agent"
    if not name:
        raise ApiError("missing", 400, "n")
    if role not in ROLES:
        raise ApiError("bad", 400, "role")
    if ids and role == "admin":
        raise ApiError("bad", 400, "boards")
    tk = secrets.token_urlsafe(24)
    with lock:
        db.execute("INSERT INTO tokens(name,hash,role,boards) VALUES(?,?,?,?)", (name, hashlib.sha256(tk.encode()).hexdigest(), role, ",".join(str(i) for i in ids)))
    return {"n": name, "role": role, "token": tk, **({"bs": ids} if ids else {}), "note": "shown once - store it now"}


def tokens_list():
    items = [{"n": n, "role": r, "env": 1} for n, r in TOKENS.values()]
    items += [{"id": r["id"], "n": r["name"], "role": r["role"], "u": r["used"] or None, "c": r["created"], "bs": [int(x) for x in re.findall(r"\d+", r["boards"] or "")] or None} for r in rows("SELECT * FROM tokens ORDER BY id")]
    return {"n": len(items), "i": [{k: v for k, v in o.items() if v is not None} for o in items]}


def token_delete(tid):
    with lock:
        db.execute("DELETE FROM tokens WHERE id=?", (tid,))
    return {"id": tid, "x": 1}


SETTING_KEYS = ("digest_url", "digest_hour", "alert_url", "backup_keep", "supervisors", "git_secret", "git_autodone", "base_url", "wake_after_min", "prices", "budget_usd", "agent_limits", "run_lost_min", "holder_only", "strict_moves", "auto_verify", "policy_default", "media_hosts")


def get_setting(k, default=""):
    r = db.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r["v"] if r and r["v"] != "" else default


def set_setting(k, v):
    db.execute("INSERT OR REPLACE INTO settings(k,v) VALUES(?,?)", (k, str(v)))


def settings_get():
    return {**{k: get_setting(k) for k in SETTING_KEYS}, "digest_last": get_setting("digest_last"), "backup_last": get_setting("backup_last")}


def settings_set(body):
    with lock:
        for k in SETTING_KEYS:
            if k in body:
                v = str(body[k] or "").strip()
                if k.endswith("_url") and k != "base_url" and v:           # base_url is only ever printed in messages, never requested
                    try:
                        check_public_url(v)
                    except ApiError as e:
                        raise ApiError(e.code, 400, k)
                if k == "policy_default" and v not in ("", "loose", "standard", "strict"):
                    raise ApiError("bad", 400, k)
                if k in ("digest_hour", "backup_keep", "wake_after_min", "run_lost_min") and v:
                    try:
                        assert 0 <= int(v) <= (23 if k == "digest_hour" else 1440 if k in ("wake_after_min", "run_lost_min") else 365)
                    except Exception:
                        raise ApiError("bad", 400, k)
                if k == "budget_usd" and v:
                    try:
                        assert float(v) >= 0
                    except Exception:
                        raise ApiError("bad", 400, k)
                if k == "agent_limits" and v:
                    try:
                        lj = json.loads(v)
                        assert isinstance(lj, dict) and all(isinstance(x, dict) and all(kk in ("jobs", "usd", "secs") and isinstance(n, (int, float)) and n >= 0 for kk, n in x.items()) for x in lj.values())
                    except Exception:
                        raise ApiError("bad", 400, k)
                if k == "media_hosts" and v and not all(re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d+)?", h.strip()) for h in v.split(",") if h.strip()):
                    raise ApiError("bad", 400, k)
                if k == "base_url" and v and not re.match(r"https?://", v):
                    raise ApiError("bad", 400, k)
                if k == "prices" and v:
                    try:
                        pj = json.loads(v)
                        assert isinstance(pj, dict) and all(isinstance(x, list) and len(x) == 2 and all(isinstance(n, (int, float)) for n in x) for x in pj.values())
                    except Exception:
                        raise ApiError("bad", 400, k)
                set_setting(k, v)
    return settings_get()


def post_async(url, text):
    """Slack/Teams/Discord-style incoming webhook: POST {"text": ...}. Fire and forget."""
    if not url:
        return

    def run():
        try:
            data = json.dumps({"text": text, "content": text}).encode()
            check_public_url(url)
            _opener.open(urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST"), timeout=8).read()
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


def digest_text():
    lines = ["Valnivo Taskboard daily digest - " + time.strftime("%Y-%m-%d", time.gmtime())]
    for r in active_risks():
        st = sprint_stats(r["id"])
        w = st["w"]
        lines.append("%s %s: %d/%d done, %d day(s) left, %d open" % ({"green": "OK", "amber": "WATCH", "red": "RISK"}[st["health"]], st["n"], w["done"], w["scope"], st["days"]["left"], w["open"]))
        for k in st["risks"][:3]:
            lines.append("   - [%s] %s" % (k["sev"], k["m"]))
    bl = rows("SELECT COUNT(*) n FROM tasks WHERE status='blocked'")[0]["n"]
    nu = rows("SELECT COUNT(*) n FROM tasks WHERE nudge!='' AND status NOT IN ('done','declined')")[0]["n"]
    lines.append("Blocked: %d, stale: %d" % (bl, nu))
    return "\n".join(lines)


def maybe_digest(force=False):
    url = get_setting("digest_url")
    if not url:
        return {"sent": False, "why": "no digest_url"}
    today = time.strftime("%Y-%m-%d", time.gmtime())
    hour = int(get_setting("digest_hour", "7"))
    if not force and (get_setting("digest_last") == today or time.gmtime().tm_hour < hour):
        return {"sent": False}
    post_async(url, digest_text())
    with lock:
        set_setting("digest_last", today)
    return {"sent": True}


def do_backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = "taskboard-%s.db" % time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    path = os.path.join(BACKUP_DIR, name)
    dst = sqlite3.connect(path)
    with lock:
        db.backup(dst)
    dst.close()
    chk = sqlite3.connect(path)
    try:
        ok = chk.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        counts = {t: chk.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in ("boards", "sprints", "tasks", "docs", "specs", "comments")}
    finally:
        chk.close()
    src = {t: db.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in counts}
    res = {"file": name, "size": os.path.getsize(path), "ok": bool(ok and all(counts[t] <= src[t] for t in counts)), "counts": counts,
           "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())}
    keep = int(get_setting("backup_keep", "14"))
    olds = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith("taskboard-") and f.endswith(".db"))
    for f in (olds[:-keep] if keep else []):
        os.remove(os.path.join(BACKUP_DIR, f))
    with lock:
        set_setting("backup_last", json.dumps(res, separators=(",", ":")))
    return res


def backups_list():
    fs = sorted((f for f in os.listdir(BACKUP_DIR) if f.endswith(".db")), reverse=True) if os.path.isdir(BACKUP_DIR) else []
    last = get_setting("backup_last")
    return {"n": len(fs), "dir": BACKUP_DIR, "i": [{"file": f, "size": os.path.getsize(os.path.join(BACKUP_DIR, f))} for f in fs],
            "last": json.loads(last) if last else None}


def maybe_backup():
    last = get_setting("backup_last")
    try:
        age = time.time() - calendar.timegm(time.strptime(json.loads(last)["ts"], "%Y-%m-%d %H:%M:%S")) if last else 1e9
    except Exception:
        age = 1e9
    if age >= 86400:
        return do_backup()


EXPORT_TABLES = ("boards", "sprints", "tasks", "docs", "specs", "comments")


def export_data(bid=None):
    """Full JSON snapshot (raw column names) of one board or everything."""
    out = {"v": 1, "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())}
    ba = (bid,) if bid else ()
    out["boards"] = rows("SELECT * FROM boards" + (" WHERE id=?" if bid else ""), ba)
    out["sprints"] = rows("SELECT * FROM sprints" + (" WHERE board_id=?" if bid else ""), ba)
    out["tasks"] = rows("SELECT * FROM tasks" + (" WHERE board_id=?" if bid else ""), ba)
    tids = {t["id"] for t in out["tasks"]}
    out["docs"] = rows("SELECT * FROM docs" + (" WHERE board_id=?" if bid else ""), ba)
    out["specs"] = rows("SELECT * FROM specs" + (" WHERE board_id=? OR board_id IS NULL" if bid else ""), ba)
    out["comments"] = [c for c in rows("SELECT * FROM comments") if not bid or c["task_id"] in tids]
    return out


def import_data(actor, data):
    """Create COPIES of the exported boards/sprints/tasks/docs/specs/comments with remapped ids (never overwrites)."""
    if not isinstance(data, dict) or data.get("v") != 1:
        raise ApiError("bad", 400, "v")
    bmap, smap, tmap = {}, {}, {}
    n = {t: 0 for t in EXPORT_TABLES}

    def ins(table, r, cols):
        cur = db.execute("INSERT INTO %s(%s) VALUES(%s)" % (table, ",".join(cols), ",".join("?" * len(cols))), [r.get(c) for c in cols])
        n[table] += 1
        return cur.lastrowid
    with lock:
        db.execute("BEGIN")
        try:
            for b in data.get("boards", []):
                bmap[b["id"]] = ins("boards", b, ["name", "description", "team", "dod", "method", "cfg"])
            for sp in data.get("sprints", []):
                if sp["board_id"] in bmap:
                    smap[sp["id"]] = ins("sprints", {**sp, "board_id": bmap[sp["board_id"]]}, ["board_id", "name", "goal", "start", "end", "state"])
            tcols = [c for c in FIELDS["tasks"] if c not in ("board_id", "sprint_id")]
            for t in data.get("tasks", []):
                if t["board_id"] in bmap:
                    r = {**t, "board_id": bmap[t["board_id"]], "sprint_id": smap.get(t.get("sprint_id")), "blocked_by": "", "depends": "", "parent": None}
                    tmap[t["id"]] = ins("tasks", r, ["board_id", "sprint_id"] + tcols)
            for t in data.get("tasks", []):          # second pass: remap links between tasks
                if t["id"] in tmap:
                    for col in ("blocked_by", "depends"):
                        if t.get(col):
                            ids = ",".join(str(tmap[int(i)]) for i in re.findall(r"\d+", t[col]) if int(i) in tmap)
                            db.execute("UPDATE tasks SET %s=? WHERE id=?" % col, (ids, tmap[t["id"]]))
                    if t.get("parent") in tmap:
                        db.execute("UPDATE tasks SET parent=? WHERE id=?", (tmap[t["parent"]], tmap[t["id"]]))
            for d in data.get("docs", []):
                sc, ref = d["scope"], d["ref_id"]
                new = {"board": bmap, "sprint": smap, "task": tmap}.get(sc, {}).get(ref)
                if new:
                    bid = db.execute("SELECT %s FROM %s WHERE id=?" % ("id" if sc == "board" else "board_id", DOC_SCOPES[sc]), (new,)).fetchone()[0]
                    ins("docs", {**d, "ref_id": new, "board_id": bid}, ["scope", "ref_id", "title", "body", "url", "board_id"])
            for sp in data.get("specs", []):
                nb = bmap.get(sp.get("board_id")) if sp.get("board_id") else None
                if sp.get("board_id") is None or nb:
                    sid = ins("specs", {**sp, "board_id": nb, "author": sp.get("author") or actor, "updated_by": actor}, ["board_id", "title", "body", "tags", "status", "author", "updated_by", "rev", "tasks"])
                    db.execute("INSERT INTO spec_revs(spec_id,rev,title,body,actor,note) VALUES(?,?,?,?,?,?)", (sid, sp.get("rev") or 1, sp["title"], sp.get("body", ""), actor, "imported"))
            for c in data.get("comments", []):
                if c["task_id"] in tmap:
                    ins("comments", {**c, "task_id": tmap[c["task_id"]]}, ["task_id", "actor", "body"])
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise ApiError("bad", 400, "data")
    return {"ok": 1, "n": n, "boards": bmap}


CSV_COLS = ["id", "board_id", "sprint_id", "title", "description", "status", "priority", "assignee", "kind", "channel", "labels", "points", "depends", "created", "updated"]


def export_csv(bid=None):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLS)
    for t in rows("SELECT * FROM tasks" + (" WHERE board_id=?" if bid else "") + " ORDER BY id", (bid,) if bid else ()):
        w.writerow([t.get(c, "") for c in CSV_COLS])
    return buf.getvalue()


def import_csv(actor, bid, text):
    """CSV with header (title required; description,status,priority,assignee,kind,channel,labels,points,sprint_id). No notifications."""
    if not rows("SELECT id FROM boards WHERE id=?", (bid,)):
        raise ApiError("nf", 404, "b")
    made, bad = 0, []
    with lock:
        for i, r in enumerate(csv.DictReader(io.StringIO(text)), 2):
            r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
            body = {k: v for k, v in r.items() if v and k in ("title", "description", "status", "priority", "assignee", "kind", "channel", "labels", "points", "sprint_id")}
            body["board_id"] = bid
            body.setdefault("channel", "Import")
            try:
                d = validate("tasks", body, False)
                db.execute("INSERT INTO tasks(%s) VALUES(%s)" % (",".join(d), ",".join("?" * len(d))), list(d.values()))
                made += 1
            except ApiError as e:
                bad.append({"row": i, "e": e.code, "f": e.field})
        emit(actor, "board.updated", bid, bid, {"import": made})
    return {"ok": 1, "made": made, "bad": bad[:50]}


# ---------------------------------------------------------------- dashboard
def dashboard(q):
    bid = qint(q, "b") or qint(q, "board_id")
    scope = _clip([bid]) if bid else scope_ids(q)
    try:
        nudge_tick(bid)
    except Exception:
        pass
    fr, fp = _in("id", scope)
    bs = rows("SELECT * FROM boards WHERE 1=1" + fr + " ORDER BY id", fp)
    boards = []
    for b in bs:
        by = {r["status"]: r["n"] for r in rows("SELECT status, COUNT(*) n FROM tasks WHERE board_id=? GROUP BY status", (b["id"],))}
        o = {"id": b["id"], "n": b["name"], "by": by, "mt": b["method"] or "scrum"}
        if b["team"]:
            o["tm"] = b["team"]
        if METHODS.get(o["mt"], {}).get("wip"):
            fl = board_flow(b["id"], 14)
            o["fl"] = {"wip": fl["wip"], "over": fl["over"], "done14": fl["done_n"], "cycle_h": fl["cycle_h"]["avg"], "stuck": fl["stuck"]}
        sp = rows("SELECT * FROM sprints WHERE board_id=? AND state='active' ORDER BY id DESC LIMIT 1", (b["id"],))
        if sp:
            o["sp"] = sprint_row(sp[0], False)
            stt = sprint_stats(sp[0]["id"])
            o["sp"].update({"hl": stt["health"], "tp": stt["pct"]["time"], "rk": len(stt["risks"]), "proj": stt["proj"]})
        bl = [r["id"] for r in rows("SELECT id FROM tasks WHERE board_id=? AND status='blocked'", (b["id"],))]
        nu = [r["id"] for r in rows("SELECT id FROM tasks WHERE board_id=? AND nudge!='' AND status NOT IN ('done','declined')", (b["id"],))]
        if bl:
            o["bl"] = bl
        if nu:
            o["nu"] = nu
        boards.append(o)
    wh, ar = _in("board_id", scope)
    load = {}
    for r in rows("SELECT assignee, status, COUNT(*) n FROM tasks WHERE assignee!='' AND status NOT IN ('done','declined')" + wh + " GROUP BY assignee COLLATE NOCASE, status", ar):
        load.setdefault(r["assignee"], {})[r["status"]] = r["n"]
    unread = {r["agent"].lower(): r["n"] for r in rows("SELECT agent, COUNT(*) n FROM notifications WHERE read=0 GROUP BY agent COLLATE NOCASE")}
    tot = {"boards": len(boards), "open": 0, "in_progress": 0, "review": 0, "blocked": 0, "done": 0, "red_sprints": 0}
    for o in boards:
        for k in ("in_progress", "review", "blocked", "done"):
            tot[k] += o["by"].get(k, 0)
        tot["open"] += sum(v for k, v in o["by"].items() if k not in ("done", "declined"))
        tot["red_sprints"] += 1 if (o.get("sp") or {}).get("hl") == "red" else 0
    out = {"totals": tot, "scope": {"tm": _qv(q, "tm", "team"), "bs": scope}, "attention": attention(scope)[:8], "risks": [r for r in active_risks(scope) if r["risks"]], "boards": boards, "agents": [{"as": a, "by": v, "inbox": unread.get(a.lower(), 0)} for a, v in sorted(load.items())]}
    me = _qv(q, "as", "agent")
    if me:
        work = rows("SELECT * FROM tasks WHERE assignee=? COLLATE NOCASE AND status NOT IN ('done','declined')" + wh +
                    " ORDER BY CASE status WHEN 'in_progress' THEN 0 WHEN 'review' THEN 1 WHEN 'todo' THEN 2 WHEN 'blocked' THEN 3 ELSE 4 END, priority, id LIMIT 30", [me] + ar)
        out["me"] = {"as": me, "inbox": unread.get(me.lower(), 0), "work": [cdict("tasks", t, False, ("description",)) for t in work]}
    return out



_media = {"t": 0, "v": ""}


def media_hosts():
    """Extra origins allowed to show images, video and audio on task pages (setting media_hosts), cached for a few seconds."""
    if time.time() - _media["t"] > 10:
        _media["t"], _media["v"] = time.time(), " ".join(h.strip() for h in get_setting("media_hosts").split(",") if h.strip())
    return _media["v"]


SEC_HEADERS = (
    ("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"), ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"), ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
    ("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
                                "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
)
_rl = collections.defaultdict(collections.deque)
_rl_lock = threading.Lock()


def rate_hit(key, limit, window=60, count=True):
    """Sliding-window limiter. Returns True when the caller is over `limit`. `count=False` only checks."""
    if not limit:
        return False
    now = time.time()
    with _rl_lock:
        q = _rl[key]
        while q and q[0] < now - window:
            q.popleft()
        over = len(q) >= limit
        if count and not over:
            q.append(now)
        if len(_rl) > 5000:
            for k in [k for k, v in _rl.items() if not v or v[-1] < now - window]:
                _rl.pop(k, None)
        return over


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ValnivoTaskboard/0.3"

    def log_message(self, *a):
        pass

    def send_json(self, code, obj, etag=None):
        b = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()
        if getattr(self, "_rt", None):
            usage_hit(getattr(self, "_act", "?"), self._rt, len(b), getattr(self, "_inb", 0))
            self._rt = None
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-cache")
        if etag:
            self.send_header("ETag", etag)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b)

    def err(self, code, status, field=None):
        self.send_json(status, {"e": code, **({"f": field} if field else {})})

    def actor(self, query):
        self.role = "admin"
        self.allowed = None
        if auth_open():
            return "anon"
        t = ""
        a = self.headers.get("Authorization", "")
        if a.lower().startswith("bearer "):
            t = a[7:].strip()
        if not t and self.path.split("?")[0] in ("/api/events", "/api/mcp"):    # EventSource cannot set headers; no other endpoint takes a URL token
            t = query.get("token", [""])[0] or query.get("key", [""])[0]
        r = lookup_token(t)
        if not r:
            return None
        self.role = r[1]
        self.allowed = r[2]
        self.tok = t
        touch_presence(r[0])
        return r[0]

    def send_raw(self, code, body, ctype, filename=None):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-cache")
        if filename:
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b)

    def end_headers(self):
        for k, v in SEC_HEADERS:
            if k == "Content-Security-Policy":
                mh = media_hosts()
                if mh:
                    v = v.replace("img-src 'self' data:", "img-src 'self' data: " + mh).replace("object-src", "media-src 'self' %s; object-src" % mh)
            self.send_header(k, v)
        if TRUST_PROXY and self.headers.get("X-Forwarded-Proto") == "https":
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        super().end_headers()

    def client_ip(self):
        if TRUST_PROXY:
            xf = self.headers.get("X-Forwarded-For", "")
            if xf:
                return xf.split(",")[0].strip()
        return self.client_address[0]

    def run_batch(self, body):
        ops = body.get("ops")
        if not isinstance(ops, list) or not ops or len(ops) > 20:
            raise ApiError("bad", 400, "ops")
        host = "127.0.0.1" if HOST in ("0.0.0.0", "", "::") else HOST
        port, auth = self.server.server_address[1], self.headers.get("Authorization", "")
        out = []
        for op in ops:
            m, pth = str(op.get("m") or "GET").upper(), str(op.get("p") or "")
            if m not in ("GET", "POST", "PATCH", "PUT", "DELETE") or not pth.startswith("/") or ".." in pth or pth.startswith(("/batch", "/api/batch")):
                raise ApiError("bad", 400, "op")
            data = json.dumps(op["b"]).encode() if isinstance(op.get("b"), (dict, list)) else None
            conn = http.client.HTTPConnection(host, port, timeout=30)
            try:
                conn.request(m, pth if pth.startswith("/api/") else "/api" + pth, data, {"Authorization": auth, "Content-Type": "application/json"})
                resp = conn.getresponse()
                raw = resp.read()
            finally:
                conn.close()
            try:
                r = json.loads(raw)
            except ValueError:
                r = raw.decode("utf-8", "replace")[:300]
            out.append({"s": resp.status, "r": r})
            if resp.status >= 400 and not body.get("cont"):
                break
        return {"n": len(out), "i": out}

    def scope_check(self, method, path, q):
        """A token limited to some boards may only name those boards, and tasks, sprints, docs, reviews and attachments on them."""
        if allowed_boards() is None:
            return
        for k in ("b", "board_id", "board"):
            v = (q.get(k) or [""])[0]
            if v.isdigit():
                need_board(int(v))
        m = re.match(r"/api/(boards|sprints|tasks|docs|reviews|attachments|templates|runs)/(\d+)", path)
        if m:
            kind, i = m.group(1), int(m.group(2))
            col = {"sprints": ("sprints", "board_id"), "docs": ("docs", "board_id"), "templates": ("templates", "board_id")}.get(kind)
            if kind == "boards":
                need_board(i)
            elif kind == "tasks":
                need_task(i)
            elif kind in ("reviews", "attachments", "runs"):
                r = db.execute("SELECT task_id FROM %s WHERE id=?" % kind, (i,)).fetchone()
                if r:
                    need_task(r[0])
            else:
                r = db.execute("SELECT %s FROM %s WHERE id=?" % (col[1], col[0]), (i,)).fetchone()
                if r and not (kind == "templates" and r[0] is None):
                    need_board(r[0])
        if method == "POST" and path == "/api/boards":
            raise ApiError("scope", 403, "b")
        if method in ("POST", "PATCH", "PUT", "DELETE") and not path.startswith("/api/import"):
            n = int(self.headers.get("Content-Length") or 0)
            if 0 < n <= 2 * 1024 * 1024:
                raw = self.rfile.read(n)
                self.rfile = io.BytesIO(raw)             # the route reads the body again
                try:
                    body = json.loads(raw)
                except ValueError:
                    return
                for b in (body if isinstance(body, list) else [body]):
                    if not isinstance(b, dict):
                        continue
                    for k in ("b", "board", "board_id"):
                        if str(b.get(k) or "").isdigit():
                            need_board(int(b[k]))
                    ids = list(b.get("ids") or []) if isinstance(b.get("ids"), list) else []
                    if path in ("/api/tasks", "/api/tasks/bulk") and str(b.get("id") or "").isdigit():
                        ids.append(b["id"])
                    if str(b.get("task") or "").isdigit():
                        ids.append(b["task"])
                    for i in ids:
                        if str(i).isdigit():
                            need_task(int(i))

    def mcp_http(self, actor, q, method):
        """Remote MCP over HTTP (streamable HTTP, JSON answers, stateless): the same tools as mcp_server.py, each call made with the caller's own token."""
        if method != "POST":
            return self.err("method", 405)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"null")
        except ValueError:
            return self.send_json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
        msgs = body if isinstance(body, list) else [body]
        if not msgs or len(msgs) > 20 or not all(isinstance(x, dict) for x in msgs):
            return self.send_json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}})
        import mcp_server as M
        tok = getattr(self, "tok", "") or "-"
        M._ctx.url, M._ctx.token, M._ctx.key = "http://127.0.0.1:%d" % self.server.server_address[1], tok, hashlib.sha256(tok.encode()).hexdigest()[:16]
        try:
            outs = [M.handle(x, _qv(q, "tools") or ("core,coord" if _priv(self.role) else "core")) for x in msgs]
        finally:
            M._ctx.url = M._ctx.token = M._ctx.key = None
        outs = [o for o in outs if o is not None]
        if not outs:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return self.send_json(200, outs if isinstance(body, list) else outs[0])

    def git_hook(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 5 * 1024 * 1024:
            self.close_connection = True
            return self.err("too_big", 413)
        raw = self.rfile.read(n) if n else b""
        try:
            git_verify(self.headers, raw)
            try:
                p = json.loads(raw or b"{}")
            except ValueError:
                raise ApiError("json", 400)
            if not isinstance(p, dict):
                raise ApiError("json", 400)
            _ctx.role = "agent"
            return self.send_json(200, git_event(self.headers, p))
        except ApiError as e:
            return self.err(e.code, e.status, e.field)

    def read_body(self, limit=None):
        n = int(self.headers.get("Content-Length") or 0)
        if n > (limit or MAX_BODY):
            raise ApiError("too_big", 413)
        if not n:
            return {}
        try:
            body = json.loads(self.rfile.read(n))
        except Exception:
            raise ApiError("json", 400)
        if not isinstance(body, dict):
            raise ApiError("json", 400)
        return body

    def handle_any(self, method):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path
        _ctx.allowed = None
        if path == "/api/health":
            return self.send_json(200, {"ok": True})
        if not path.startswith("/api/"):
            if method != "GET":
                return self.err("method", 405)
            return self.static(path)
        if rate_hit("authfail:" + self.client_ip(), AUTH_FAILS_PER_MIN, count=False):
            return self.err("rate", 429)
        if path == "/api/hooks/git" and method == "POST":
            return self.git_hook()
        actor = self.actor(q)
        if actor is None:
            rate_hit("authfail:" + self.client_ip(), AUTH_FAILS_PER_MIN)
            return self.err("auth", 401)
        if rate_hit("tok:" + str(actor), RATE_PER_MIN):
            return self.err("rate", 429)
        self._act, self._rt = actor, route_label(path)
        try:
            self._inb = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._inb = 0
        try:
            cl = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            cl = 0
        if cl > (MAX_IMPORT if path.startswith("/api/import") else MAX_BODY):
            self.close_connection = True
            return self.err("too_big", 413)
        _ctx.role = self.role
        _ctx.allowed = self.allowed
        full = (q.get("full", ["0"])[0] not in ("0", "")) or self.headers.get("X-TB-Full") == "1"
        try:
            if path == "/api/mcp":
                return self.mcp_http(actor, q, method)
            if path in ("/api/nudge", "/api/tasks/nudge/run") and method == "POST":
                bid = qint(q, "b") or qint(q, "board_id")
                res = nudge_tick(bid, actor=actor or "agent")
                return self.send_json(200, {"ok": True, **res, "h": NUDGE_HOURS})
            if self.role == "viewer" and method != "GET":
                return self.err("forbidden", 403)
            self.scope_check(method, path, q)
            if self.role != "admin" and (path.startswith(("/api/admin", "/api/tokens", "/api/settings", "/api/import", "/api/chats", "/api/export")) or
                                         (method == "DELETE" and (re.match(r"/api/boards/", path) or (re.match(r"/api/sprints/", path) and self.role != "supervisor")))):
                return self.err("forbidden", 403)
            if path == "/api/tokens":
                if method == "GET":
                    return self.send_json(200, tokens_list())
                if method == "POST":
                    return self.send_json(201, token_create(self.read_body()))
                if method == "DELETE":
                    return self.send_json(200, token_delete(qint(q, "id", 0)))
            if path == "/api/settings":
                return self.send_json(200, settings_set(self.read_body()) if method == "POST" else settings_get())
            if path == "/api/admin/backup" and method == "POST":
                return self.send_json(200, do_backup())
            if path == "/api/admin/backups" and method == "GET":
                return self.send_json(200, backups_list())
            if path == "/api/admin/digest" and method == "POST":
                return self.send_json(200, maybe_digest(force=True))
            if path == "/api/export" and method == "GET":
                return self.send_raw(200, json.dumps(export_data(qint(q, "b") or qint(q, "board_id")), separators=(",", ":"), default=str), "application/json", "taskboard-export.json")
            if path == "/api/export.csv" and method == "GET":
                return self.send_raw(200, export_csv(qint(q, "b") or qint(q, "board_id")), "text/csv; charset=utf-8", "taskboard-tasks.csv")
            if path == "/api/import" and method == "POST":
                return self.send_json(200, import_data(actor, self.read_body(MAX_IMPORT)))
            if path == "/api/import/csv" and method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_IMPORT:
                    raise ApiError("too_big", 413)
                return self.send_json(200, import_csv(actor, qint(q, "b") or qint(q, "board_id") or 0, self.rfile.read(n).decode("utf-8", "replace")))
            if path in ("/api/dashboard", "/api/portfolio") and method == "GET":
                return self.send_json(200, dashboard(q))
            if path == "/api/inbox" and method == "GET":
                return self.send_json(200, inbox_get(q, actor))
            if path == "/api/inbox/ack" and method == "POST":
                return self.send_json(200, inbox_ack(self.read_body()))
            if path == "/api/agents":
                if method == "GET":
                    return self.send_json(200, agents_list())
                if method == "POST":
                    return self.send_json(200, agent_set(self.read_body()))
                if method == "DELETE":
                    return self.send_json(200, agent_del(_qv(q, "n", "name", default="")))
            ms = re.fullmatch(r"/api/sprints/(\d+)/(start|close)", path)
            if ms and method == "POST":
                body = self.read_body()
                with lock:
                    res = sprint_start(actor, int(ms.group(1))) if ms.group(2) == "start" else close_phase_aware(actor, int(ms.group(1)), body.get("to"), bool(body.get("force")) and _priv(self.role))
                return self.send_json(200, res)
            if path == "/api/specs":
                if method == "GET":
                    return self.send_json(200, specs_list(q))
                if method == "POST":
                    return self.send_json(201, spec_create(actor, self.read_body()))
            msp = re.fullmatch(r"/api/specs/(\d+)(?:/(revs)(?:/(\d+))?)?", path)
            if msp:
                sid = int(msp.group(1))
                if msp.group(2) and method == "GET":
                    return self.send_json(200, spec_get(sid, int(msp.group(3))) if msp.group(3) else spec_revs(sid))
                if not msp.group(2):
                    if method == "GET":
                        return self.send_json(200, spec_view(spec_get(sid), q))
                    if method in ("PATCH", "PUT"):
                        return self.send_json(200, spec_update(actor, sid, self.read_body()))
                    if method == "DELETE":
                        return self.send_json(200, spec_delete(actor, sid))
            if path == "/api/templates":
                if method == "GET":
                    bb = qint(q, "b") or qint(q, "board_id")
                    rs = rows("SELECT * FROM templates" + (" WHERE board_id=? OR board_id IS NULL" if bb else "") + " ORDER BY id", (bb,) if bb else ())
                    return self.send_json(200, {"n": len(rs), "help": RECUR_HELP, "i": [tpl_out(r) for r in rs]})
                if method == "POST":
                    return self.send_json(201, tpl_save(self.read_body()))
            if path == "/api/templates/run" and method == "POST":
                return self.send_json(200, recur_tick(actor="recurring"))
            mtp = re.fullmatch(r"/api/templates/(\d+)(?:/(use))?", path)
            if mtp:
                tid = int(mtp.group(1))
                if mtp.group(2) and method == "POST":
                    return self.send_json(201, tpl_use(actor, tid, self.read_body()))
                if not mtp.group(2):
                    if method in ("PATCH", "PUT"):
                        return self.send_json(200, tpl_save(self.read_body(), tid))
                    if method == "DELETE":
                        with lock:
                            db.execute("DELETE FROM templates WHERE id=?", (tid,))
                        return self.send_json(200, {"id": tid, "x": 1})
                    if method == "GET":
                        r = rows("SELECT * FROM templates WHERE id=?", (tid,))
                        if not r:
                            raise ApiError("nf", 404)
                        return self.send_json(200, tpl_out(r[0]))
            if path == "/api/stats/agents" and method == "GET":
                return self.send_json(200, agent_stats(scope_of(q), max(1, min(qint(q, "days", 30), 365))))
            mcl = re.fullmatch(r"/api/tasks/(\d+)/(claim|release|review|attachments)", path)
            if mcl:
                tid, act = int(mcl.group(1)), mcl.group(2)
                if act == "attachments" and method == "GET":
                    return self.send_json(200, atts_get(tid))
                if method == "POST":
                    body = self.read_body()
                    if act == "claim":
                        return self.send_json(200, claim_task(actor, tid, body))
                    if act == "release":
                        return self.send_json(200, release_task(actor, self.role, tid, body))
                    if act == "review":
                        return self.send_json(201, review_request(actor, tid, body))
                    return self.send_json(201, att_add(actor, tid, body))
            if path == "/api/claim-next" and method == "POST":
                return self.send_json(200, claim_next(actor, self.read_body()))
            if path == "/api/locks":
                if method == "GET":
                    return self.send_json(200, locks_list())
                if method == "POST":
                    return self.send_json(200, lock_acquire(actor, self.read_body()))
                if method == "DELETE":
                    return self.send_json(200, lock_release(actor, self.role, _qv(q, "name", default=""), {"as": _qv(q, "as")} if _qv(q, "as") else {}))
            if path == "/api/asks":
                if method == "GET":
                    return self.send_json(200, asks_list(q, actor))
                if method == "POST":
                    return self.send_json(201, ask_create(actor, self.read_body()))
            mas = re.fullmatch(r"/api/asks/(\d+)/(answer|cancel)", path)
            if mas and method == "POST":
                fn = ask_answer if mas.group(2) == "answer" else ask_cancel
                return self.send_json(200, fn(actor, self.role, int(mas.group(1)), self.read_body()))
            if path == "/api/reviews" and method == "GET":
                return self.send_json(200, reviews_list(q))
            mrv = re.fullmatch(r"/api/reviews/(\d+)/decide", path)
            if mrv and method == "POST":
                return self.send_json(200, review_decide(actor, self.role, int(mrv.group(1)), self.read_body()))
            if path == "/api/presence" and method == "POST":
                return self.send_json(200, presence_set(actor, self.read_body()))
            if path == "/api/route" and method == "GET":
                return self.send_json(200, {"i": route_candidates(_qv(q, "skill", default=""), _qv(q, "lb", default=""), qint(q, "b"))})
            if path == "/api/envs":
                if method == "GET":
                    return self.send_json(200, envs_list())
                if method == "POST":
                    return self.send_json(200, env_deploy(actor, self.role, self.read_body()))
            mev = re.fullmatch(r"/api/envs/([A-Za-z0-9._-]+)/(freeze|unfreeze)", path)
            if mev and method == "POST":
                if self.role not in ("admin", "supervisor"):
                    return self.err("forbidden", 403)
                return self.send_json(200, env_freeze(actor, mev.group(1), self.read_body(), mev.group(2) == "freeze"))
            if path == "/api/watch":
                if method == "GET":
                    return self.send_json(200, watch_list(q, actor))
                if method == "POST":
                    return self.send_json(200, watch_add(actor, self.read_body()))
                if method == "DELETE":
                    return self.send_json(200, watch_del(actor, q))
            mat = re.fullmatch(r"/api/attachments/(\d+)", path)
            if mat and method == "DELETE":
                return self.send_json(200, att_del(int(mat.group(1))))
            if path == "/api/methods" and method == "GET":
                return self.send_json(200, {"i": [{"id": k, **v} for k, v in METHODS.items()], "types": list(TASK_TYPES)})
            mbf = re.fullmatch(r"/api/boards/(\d+)/(flow|velocity|okr|sla)", path)
            if mbf and method == "GET":
                bb, kind = int(mbf.group(1)), mbf.group(2)
                return self.send_json(200, board_flow(bb, max(7, min(qint(q, "days", 30), 180))) if kind == "flow" else board_velocity(bb, max(1, min(qint(q, "n", 8), 30))) if kind == "velocity" else board_okr(bb) if kind == "okr" else board_sla(bb))
            mrt = re.fullmatch(r"/api/sprints/(\d+)/retro", path)
            if mrt and method == "GET":
                return self.send_json(200, retro_get(int(mrt.group(1))))
            if mrt and method == "POST":
                return self.send_json(200, retro_save(actor, int(mrt.group(1)), self.read_body()))
            if path == "/api/admin/git-secret" and method == "POST":
                with lock:
                    set_setting("git_secret", secrets.token_urlsafe(24))
                return self.send_json(200, {"secret": get_setting("git_secret"), "hook": "/api/hooks/git", "events": "push, pull_request, pull_request_review, workflow_run (GitHub); Push, Merge Request, Pipeline (GitLab)"})
            if path == "/api/chats":
                if method == "GET":
                    return self.send_json(200, chats_list())
                if method == "POST":
                    b_ = self.read_body()
                    return self.send_json(200, chat_save(b_, int(b_["id"]) if b_.get("id") else None))
                if method == "DELETE":
                    return self.send_json(200, chat_del(qint(q, "id", 0)))
            mct = re.fullmatch(r"/api/chats/(\d+)/test", path)
            if mct and method == "POST":
                return self.send_json(200, chat_test(int(mct.group(1)), actor))
            if path == "/api/cost":
                if method == "POST":
                    return self.send_json(201, cost_log(actor, self.read_body()))
                if method == "GET":
                    return self.send_json(200, cost_report(q))
            mtc = re.fullmatch(r"/api/tasks/(\d+)/(cost|links|runs)", path)
            if mtc and method == "GET":
                if mtc.group(2) == "runs":
                    return self.send_json(200, runs_list({**q, "task": [mtc.group(1)]}))
                return self.send_json(200, (task_cost if mtc.group(2) == "cost" else task_links)(int(mtc.group(1))))
            map_ = re.fullmatch(r"/api/tasks/(\d+)/approval", path)
            if map_ and method == "POST":
                return self.send_json(200, approval_request(actor, int(map_.group(1)), self.read_body()))
            if path == "/api/runs":
                if method == "POST":
                    return self.send_json(201, run_start(actor, self.role, self.read_body()))
                if method == "GET":
                    return self.send_json(200, runs_list(q))
            mrn = re.fullmatch(r"/api/runs/(\d+)(?:/(end))?", path)
            if mrn and method == "POST" and mrn.group(2):
                return self.send_json(200, run_end(actor, self.role, int(mrn.group(1)), self.read_body()))
            if mrn and method == "GET":
                r_ = rows("SELECT * FROM runs WHERE id=?", (int(mrn.group(1)),))
                if not r_:
                    raise ApiError("nf", 404)
                return self.send_json(200, run_out(r_[0]))
            if path == "/api/audit" and method == "GET":
                return self.send_json(200, audit_get(q, self.role))
            if path == "/api/tasks/move" and method == "POST":
                return self.send_json(200, bulk_move(actor, self.read_body()))
            if path == "/api/agents/wake" and method == "POST":
                return self.send_json(200, wake_now(actor, self.role, self.read_body()))
            if path == "/api/views":
                if method == "GET":
                    return self.send_json(200, views_list())
                if method == "POST":
                    return self.send_json(200, view_save(actor, self.read_body()))
                if method == "DELETE":
                    return self.send_json(200, view_del(q))
            if path == "/api/messages":
                if method == "POST":
                    return self.send_json(201, say(actor, self.role, self.read_body()))
                if method == "GET":
                    return self.send_json(200, messages_get(actor, self.role, q))
            if path == "/api/overview" and method == "GET":
                return self.send_json(200, scope_analytics(q))
            if path == "/api/briefing" and method == "GET":
                return self.send_json(200, briefing(actor, q))
            if path == "/api/next" and method == "POST":
                return self.send_json(200, next_task(actor, self.read_body()))
            mmv = re.fullmatch(r"/api/tasks/(\d+)/move", path)
            if mmv and method == "POST":
                return self.send_json(200, move_task(actor, int(mmv.group(1)), self.read_body()))
            mfn = re.fullmatch(r"/api/tasks/(\d+)/finish", path)
            if mfn and method == "POST":
                return self.send_json(200, task_finish(actor, self.role, int(mfn.group(1)), self.read_body()))
            if path == "/api/batch" and method == "POST":
                return self.send_json(200, self.run_batch(self.read_body()))
            if path == "/api/guide" and method == "GET":
                g = guide_get(q)
                return self.send_raw(200, g["g"], "text/plain; charset=utf-8") if _qv(q, "fmt") == "text" else self.send_json(200, g)
            if path == "/api/admin/usage" and method == "GET":
                return self.send_json(200, usage_report())
            if path == "/api/attention" and method == "GET":
                return self.send_json(200, {"i": attention(scope_of(q))})
            mha = re.fullmatch(r"/api/tasks/(\d+)/(handoff|activity|timeline)", path)
            if mha and method == "POST" and mha.group(2) == "handoff":
                return self.send_json(200, task_handoff(actor, int(mha.group(1)), self.read_body()))
            if mha and method == "GET" and mha.group(2) == "activity":
                return self.send_json(200, task_activity(int(mha.group(1))))
            if mha and method == "GET" and mha.group(2) == "timeline":
                return self.send_json(200, task_timeline(int(mha.group(1))))
            if path == "/api/risks" and method == "GET":
                return self.send_json(200, {"i": active_risks(scope_of(q))})
            mst = re.fullmatch(r"/api/sprints/(\d+)/stats", path)
            if mst and method == "GET":
                return self.send_json(200, sprint_stats(int(mst.group(1))))
            msg_ = re.fullmatch(r"/api/tasks/(\d+)/(stage|history)", path)
            if msg_ and method == "POST" and msg_.group(2) == "stage":
                return self.send_json(200, task_stage(actor, self.role, int(msg_.group(1)), self.read_body()))
            if msg_ and method == "GET" and msg_.group(2) == "history":
                return self.send_json(200, task_history(int(msg_.group(1))))
            mdc = re.fullmatch(r"/api/tasks/(\d+)/(decisions|scaffold)", path)
            if mdc and method == "GET" and mdc.group(2) == "decisions":
                return self.send_json(200, decisions_get(int(mdc.group(1))))
            if mdc and method == "POST":
                fn_ = decision_add if mdc.group(2) == "decisions" else None
                if fn_:
                    return self.send_json(201, fn_(actor, int(mdc.group(1)), self.read_body()))
                return self.send_json(201, task_scaffold(actor, self.role, int(mdc.group(1)), self.read_body()))
            mvf = re.fullmatch(r"/api/tasks/(\d+)/verify", path)
            if mvf and method == "POST":
                return self.send_json(200, task_verify(actor, self.role, int(mvf.group(1)), self.read_body()))
            if path == "/api/policy" and method == "GET":
                return self.send_json(200, policy_view(q))
            mbp = re.fullmatch(r"/api/boards/(\d+)/policy", path)
            if mbp and method == "POST":
                return self.send_json(200, board_policy_set(actor, self.role, int(mbp.group(1)), self.read_body()))
            if path == "/api/verify/queue" and method == "GET":
                return self.send_json(200, verify_queue(q, actor, self.role))
            if path == "/api/faq" and method == "GET":
                return self.send_json(200, {"i": faq_search(_qv(q, "q") or "", qint(q, "b"), 5)})
            if path == "/api/fit" and method == "GET":
                return self.send_json(200, agent_fit(_qv(q, "lb", "labels") or ""))
            if path == "/api/nodes" and method == "POST":
                return self.send_json(200, node_report(actor, self.role, self.read_body()))
            if path == "/api/nodes" and method == "GET":
                return self.send_json(200, nodes_list())
            if path == "/api/quota" and method == "GET":
                return self.send_json(200, quota_view())
            if path == "/api/system" and method == "GET":
                return self.send_json(200, system_get(q, self.role))
            if path == "/api/audit/verify" and method == "GET":
                return self.send_json(200, audit_verify(self.role))
            mdr = re.fullmatch(r"/api/tasks/(\d+)/(decline|reopen)", path)
            if mdr and method == "POST":
                fn = task_decline if mdr.group(2) == "decline" else task_reopen
                return self.send_json(200, fn(actor, int(mdr.group(1)), self.read_body()))
            mm = re.fullmatch(r"/api/tasks/(\d+)/comments", path)
            if mm and method == "GET":
                return self.send_json(200, comments_get(int(mm.group(1))))
            if mm and method == "POST":
                return self.send_json(201, comment_add(actor, int(mm.group(1)), self.read_body()))
            if path == "/api/events":
                return self.sse(q)
            if path == "/api/changes":
                return self.changes(q)
            # POST /api/tasks/bulk → same as PATCH /api/tasks with ids
            if path == "/api/tasks/bulk" and method == "POST":
                body = {}
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    try:
                        body = json.loads(self.rfile.read(n))
                    except Exception:
                        raise ApiError("json", 400)
                    if not isinstance(body, dict):
                        raise ApiError("json", 400)
                with lock:
                    res = bulk_set_tasks(actor, body, full)
                return self.send_json(200, res)
            mc = re.fullmatch(r"/api/tasks/(?:(\d+)/)?commits", path)
            if mc and method == "POST":
                body = {}
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    try:
                        body = json.loads(self.rfile.read(n))
                    except Exception:
                        raise ApiError("json", 400)
                if not isinstance(body, (dict, list)):
                    raise ApiError("json", 400)
                if mc.group(1):
                    res = append_commit(actor, int(mc.group(1)), body)
                elif isinstance(body, dict):
                    res = append_commit_by_msg(actor, body)
                else:
                    raise ApiError("json", 400)
                return self.send_json(201, res)
            m = re.fullmatch(r"/api/(boards|sprints|tasks|docs)(?:/(\d+))?", path)
            if not m:
                raise ApiError("route", 404)
            body = {}
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                try:
                    body = json.loads(self.rfile.read(n))
                except Exception:
                    raise ApiError("json", 400)
                if not isinstance(body, dict):
                    raise ApiError("json", 400)
            rid = int(m.group(2)) if m.group(2) else None
            etag = None
            if method == "GET":
                key = hashlib.md5((u.path + "?" + "&".join(sorted(u.query.split("&"))) + str(full)).encode()).hexdigest()[:8]
                etag = 'W/"%d-%s"' % (last_event(), key)
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.end_headers()
                    return
            if m.group(1) == "boards" and rid and method in ("PATCH", "PUT") and isinstance(body, dict) and any(k in body for k in ("cf", "cfg")):
                _k = "cf" if "cf" in body else "cfg"
                try:
                    _c = json.loads(body[_k]) if isinstance(body[_k], str) and body[_k].strip() else (body[_k] if isinstance(body[_k], dict) else {})
                except ValueError:
                    _c = {}
                _stored = board_policy(rid)
                if _c.get("policy") is None or not _priv(self.role):
                    _c.pop("policy", None)
                    if _stored:
                        _c["policy"] = _stored
                body[_k] = _c
            if m.group(1) == "tasks" and rid and method in ("PATCH", "PUT", "DELETE"):
                check_holder(actor, self.role, rid)
                if method != "DELETE" and isinstance(body, dict):
                    check_move(self.role, rid, body)
                    _nr = body.get("rigor", body.get("rg"))
                    if _nr is not None and not _priv(self.role):
                        _cur = rows("SELECT rigor FROM tasks WHERE id=?", (rid,))
                        if _cur and RIGOR_RANK.get(str(_nr).strip().lower(), 0) < RIGOR_RANK.get(_cur[0]["rigor"] or "", 0):
                            raise ApiError("forbidden", 403, "rg")
            res = crud(actor, method, m.group(1), rid, body, q, full)
            self.send_json(201 if method == "POST" else 200, res, etag)
        except ApiError as e:
            self.err(e.code, e.status, e.field)
        except sqlite3.IntegrityError:
            self.err("bad", 400, "ref")
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            import traceback
            traceback.print_exc()
            try:
                self.err("server", 500)
            except Exception:
                self.close_connection = True

    def changes(self, q):
        """Deltas since event id -> {"c":last_id,"e":[[id,type,entity_id,board,delta],...]}"""
        since = qint(q, "since", 0)
        lim = max(1, min(qint(q, "limit", 200), 1000))
        board = q.get("board_id", [None])[0]
        out, last = [], since
        al = allowed_boards()
        for r in rows("SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?", (since, lim)):
            last = r["id"]
            p = json.loads(r["payload"])
            if al is not None and p.get("b") not in al:
                continue
            if board and str(p.get("b")) != board:
                continue
            out.append([r["id"], r["type"], p["id"], p.get("b"), p.get("d")])
        self.send_json(200, {"c": last, "e": out})

    def static(self, path):
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        # Agent playbook for humans (also linked from UI header)
        if name in ("AGENTS.md", "playbook.md"):
            full = os.path.join(HERE, "docs", "AGENTS.md")
            if not os.path.isfile(full):
                return self.err("nf", 404)
            with open(full, "rb") as f:
                b = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(b)
            return
        base = os.path.join(HERE, "static")
        full = os.path.normpath(os.path.join(base, name))
        if not full.startswith(base + os.sep) or not os.path.isfile(full):
            return self.err("nf", 404)
        ct = {"html": "text/html", "js": "text/javascript", "css": "text/css", "md": "text/markdown"}.get(full.rsplit(".", 1)[-1], "application/octet-stream")
        with open(full, "rb") as f:
            b = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ct + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b)

    def sse(self, q):
        board = q.get("board", [None])[0]
        al = allowed_boards()
        last = self.headers.get("Last-Event-ID") or q.get("last", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        if self.command == "HEAD":
            return
        qu = queue.Queue()
        with subs_lock:
            subs.add(qu)
        try:
            self.wfile.write(b"retry: 2000\n\n")
            if last and last.isdigit():
                for r in rows("SELECT * FROM events WHERE id>? ORDER BY id", (int(last),)):
                    qu.put({"i": r["id"], "t": r["type"], "a": r["actor"], **json.loads(r["payload"])})
            self.wfile.flush()
            while True:
                try:
                    ev = qu.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if al is not None and ev.get("b") not in al:
                    continue
                if board and ev.get("b") is not None and str(ev["b"]) != board:
                    continue
                ev = dict(ev)
                i = ev.pop("i")
                self.wfile.write(f"id: {i}\ndata: {json.dumps(ev, separators=(',', ':'))}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with subs_lock:
                subs.discard(qu)
            self.close_connection = True

    do_GET = lambda s: s.handle_any("GET")
    do_HEAD = lambda s: s.handle_any("GET")
    do_POST = lambda s: s.handle_any("POST")
    do_PATCH = lambda s: s.handle_any("PATCH")
    do_PUT = lambda s: s.handle_any("PUT")
    do_DELETE = lambda s: s.handle_any("DELETE")


class S(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 256          # the default (5) drops connections when a page loads many boards at once


def _nudge_loop():
    import time
    threading.Thread(target=_wake_loop, name="wake", daemon=True).start()
    while True:
        time.sleep(max(NUDGE_INTERVAL_SEC, 60))
        try:
            nudge_tick(actor="nudge-cron")
        except Exception:
            pass
        for fn in (maybe_digest, maybe_backup, recur_tick):
            try:
                fn()
            except Exception:
                pass


def bootstrap_admin(name="admin", quiet=False):
    """Create an admin token (stored hashed) and return it. Called on first start when no token exists."""
    r = token_create({"n": name, "role": "admin"})
    if not quiet:
        bar = "=" * 64
        print("\n%s\n First start: admin token created (shown once, store it now)\n\n   %s\n\n Open the web UI and paste it when asked, or send it as\n   Authorization: Bearer <token>\n Lost it later?  python3 server.py --new-admin-token\n%s\n" % (bar, r["token"], bar), flush=True)
    return r["token"]


def seed_demo():
    """Sample board so a fresh install is not empty (python3 server.py --demo)."""
    if rows("SELECT id FROM boards LIMIT 1"):
        return False
    b = crud("demo", "POST", "boards", None, {"name": "Demo project", "dod": "Tests pass\nChange reviewed"}, {}, False)["id"]
    sp = crud("demo", "POST", "sprints", None, {"board_id": b, "name": "Sprint 1", "goal": "Get the first release out", "state": "active"}, {}, False)["id"]
    for t, a, st, pr, pt in (("Design the public API", "planner-agent", "done", 2, 3), ("Implement task claiming", "coder-agent", "in_progress", 1, 5),
                             ("Write the quickstart", "docs-agent", "todo", 3, 2), ("Review the claiming PR", "reviewer-agent", "review", 2, 2),
                             ("Set up CI", "", "backlog", 3, 3)):
        crud("demo", "POST", "tasks", None, {"board_id": b, "sprint_id": sp, "title": t, "assignee": a, "status": st, "priority": pr, "points": pt, "kind": "agent" if a else "human"}, {}, False)
    spec_create("demo", {"t": "Claiming protocol", "tg": "protocol,agents", "st": "approved", "d": "# Goal\nTwo agents must never work the same task.\n\n## Rules\n- `claim` a task before starting it\n- claims expire; renew them while you work\n- use `lock` for shared resources such as environments\n"})
    return True


def main():
    if "--new-admin-token" in sys.argv:
        i = sys.argv.index("--new-admin-token")
        bootstrap_admin(sys.argv[i + 1] if len(sys.argv) > i + 1 and not sys.argv[i + 1].startswith("--") else "admin")
        return
    if auth_open():                      # never run an open server: create the first admin token
        bootstrap_admin()
    if "--demo" in sys.argv or os.environ.get("TASKBOARD_DEMO") in ("1", "true"):
        if seed_demo():
            print("demo project created", flush=True)
    if NUDGE_HOURS > 0 and NUDGE_INTERVAL_SEC > 0:
        threading.Thread(target=_nudge_loop, name="nudge", daemon=True).start()
    print(f"taskboard on http://{HOST}:{PORT} db={DB} nudge={NUDGE_HOURS}h/{NUDGE_INTERVAL_SEC}s", flush=True)
    S((HOST, PORT), H).serve_forever()


if __name__ == "__main__":
    main()
