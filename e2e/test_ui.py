#!/usr/bin/env python3
"""Browser end-to-end tests: starts the server on a throwaway database and drives the real web UI with Playwright.

    pip install playwright && playwright install chromium     # once
    python3 e2e/test_ui.py                                    # E2E_CHANNEL=chrome uses an installed Chrome instead

Each test seeds its own data through the API, so they can run in any order. Exit code 1 if anything fails.
"""
import http.server, json, os, socket, subprocess, sys, tempfile, threading, time, traceback, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ADMIN, WORKER = "e2e-admin-token", "e2e-worker-token"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORT, SINK_PORT = free_port(), free_port()
URL = "http://127.0.0.1:%d" % PORT
sink = []


class Sink(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        sink.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


def api(method, path, body=None, tok=ADMIN):
    req = urllib.request.Request(URL + "/api" + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}")


def mkboard(name):
    b = api("POST", "/boards", {"n": name})["id"]
    sp = api("POST", "/sprints", {"b": b, "st": "active"})["id"]
    return b, sp


def mktask(b, title, **kw):
    return api("POST", "/tasks", {"b": b, "t": title, "ch": "Core", "st": "todo", **kw})["id"]


def goto(page, hash_="", view="board", scope=None):
    page.goto("%s/?token=%s%s" % (URL, ADMIN, hash_))
    page.wait_for_selector("#board option", state="attached")
    if scope:
        page.select_option("#board", scope)
    page.click('#tabs [data-view="%s"]' % view)


def toast_text(page):
    page.wait_for_selector("#toast:not([hidden])")
    return page.inner_text("#toast")


# ---------------------------------------------------------------- tests
def test_reload_storm_keeps_the_data(page):
    b, _ = mkboard("Storm %d" % time.time())
    ids = [mktask(b, "storm task %d" % i) for i in range(4)]
    goto(page, scope="b:%d" % b)
    for _ in range(6):                                   # reload again and again without waiting: the board must still come up every time
        page.reload(wait_until="commit")
    page.wait_for_selector(".card[data-id='%d']" % ids[0], timeout=15000)
    assert page.locator("#loadfail").count() == 0
    assert page.locator(".card").count() >= 4


def test_new_task_lands_in_the_active_sprint(page):
    b, sp = mkboard("Create %d" % time.time())
    goto(page, scope="b:%d" % b)
    page.click("#nt")
    page.fill("#f [name=title]", "created in the browser")
    if page.locator("#f [name=channel]").count():
        page.fill("#f [name=channel]", "Core")
    page.click("#dsave")
    page.wait_for_selector(".card:has-text('created in the browser')")
    t = [x for x in api("GET", "/tasks?b=%d&limit=50" % b)["i"] if x["t"] == "created in the browser"][0]
    assert t.get("s") == sp, t


def test_move_a_task_to_another_board(page):
    a, _ = mkboard("Move from %d" % time.time())
    b, sb = mkboard("Move to %d" % time.time())
    tid = mktask(a, "move me")
    goto(page, scope="b:%d" % a)
    page.click(".card[data-id='%d'] .t" % tid)
    page.select_option("#f [name=move_board]", str(b))
    assert page.is_visible("#movehint")
    page.click("#dsave")
    assert "moved" in toast_text(page)
    page.wait_for_selector(".card[data-id='%d']" % tid, state="detached")        # the card leaves this board live
    t = api("GET", "/tasks/%d" % tid)
    assert t["b"] == b and t.get("s") == sb, t


def test_bulk_move(page):
    a, _ = mkboard("Bulk from %d" % time.time())
    b, _ = mkboard("Bulk to %d" % time.time())
    ids = [mktask(a, "bulk %d" % i) for i in range(2)]
    goto(page, scope="b:%d" % a)
    for i in ids:
        page.check(".card[data-id='%d'] input.sel" % i, force=True)
    page.wait_for_selector("#bulkbar:not([hidden])")
    page.select_option("#bmv", str(b))
    page.click("#bapply")
    assert "Moved 2" in toast_text(page)
    assert all(api("GET", "/tasks/%d" % i)["b"] == b for i in ids)


def test_dialog_shows_links_code_and_cost(page):
    a, _ = mkboard("Links A %d" % time.time())
    b, _ = mkboard("Links B %d" % time.time())
    up = mktask(a, "the upstream task")
    tid = mktask(b, "waits on upstream", dp=[up], pu="https://github.com/o/r/pull/9")
    api("POST", "/tasks/%d/commits" % tid, {"h": "abc1234def", "m": "wire it up", "u": "https://github.com/o/r/commit/abc1234def"})
    api("POST", "/cost", {"task": tid, "in": 1200, "out": 300, "usd": 0.42, "as": "Worker"})
    goto(page, scope="b:%d" % b)
    assert page.locator(".card[data-id='%d'] .chip.cost" % tid).inner_text().startswith("$0.42")
    page.click(".card[data-id='%d'] .t" % tid)
    page.wait_for_selector("#linksbox .lk")
    assert "Links A" in page.inner_text("#linksbox"), page.inner_text("#linksbox")             # the other board is named
    assert "wire it up" in page.inner_text(".codebox") and "$0.42" in page.inner_text(".codebox")
    page.click("#costlog")
    page.wait_for_selector("#costbox .cbrow")


def test_overview_shows_agent_spend(page):
    b, _ = mkboard("Spend %d" % time.time())
    tid = mktask(b, "spender")
    api("POST", "/cost", {"task": tid, "in": 5000, "out": 1000, "usd": 1.5, "as": "Worker"})
    goto(page, view="overview", scope="b:%d" % b)
    page.wait_for_selector("text=Agent spend")
    assert "$1.5" in page.inner_text("#v-overview")


def test_deep_link_opens_the_task(page):
    b, _ = mkboard("Deep %d" % time.time())
    tid = mktask(b, "deep linked task")
    page.goto("%s/?token=%s#t/%d" % (URL, ADMIN, tid))
    page.wait_for_selector("#dlg[open]")
    assert "deep linked task" in page.inner_text("#dlg")


def test_admin_chat_channel_and_git_secret(page):
    goto(page, view="admin")
    page.wait_for_selector("#newchat")
    page.click("#newchat")
    page.fill("#f [name=n]", "E2E channel")
    page.select_option("#f [name=kind]", "generic")
    page.fill("#f [name=url]", "http://127.0.0.1:%d/hook" % SINK_PORT)
    page.click("#dsave")
    page.wait_for_selector("#v-admin tr:has-text('E2E channel')")
    assert "127.0.0.1" in page.inner_text("#v-admin tr:has-text('E2E channel')")             # only the host is shown, never the full address
    sink.clear()
    page.click("#v-admin tr:has-text('E2E channel') [data-chat^='test']")
    assert "Test message sent" in toast_text(page)
    assert sink and "Test message" in sink[0]["text"]
    page.once("dialog", lambda d: d.accept())
    page.click("#gitsecret")
    page.wait_for_selector("#gsec")
    assert len(page.input_value("#gsec")) >= 20


def test_all_boards_overview_has_the_analytics(page):
    a, _ = mkboard("Roll A %d" % time.time())
    b, _ = mkboard("Roll B %d" % time.time())
    for bid in (a, b):
        t = mktask(bid, "finished work", st="in_progress")
        api("PATCH", "/tasks/%d" % t, {"st": "done"})
    mktask(a, "open and assigned", st="in_progress", **{"as": "Roller"})              # the agent table lists agents with open work
    goto(page, view="overview", scope="all")
    page.wait_for_selector("#v-overview h2:has-text('Active sprints')")
    assert page.locator(".sprintmini").count() >= 2                                 # one card per active sprint, with its burndown
    assert page.locator(".sprintmini svg.burn").count() >= 2
    assert page.locator("#v-overview h2:has-text('Flow')").count() == 1             # flow rolled up across the boards
    page.wait_for_selector("#v-overview th:has-text('Done 30d')")


def test_token_limited_to_a_board(page):
    a, _ = mkboard("Walled in %d" % time.time())
    b, _ = mkboard("Walled out %d" % time.time())
    goto(page, view="admin")
    page.wait_for_selector("#newtoken")
    page.click("#newtoken")
    page.fill("#f [name=n]", "Boxed Agent")
    page.check("#f [name=tb_%d]" % a)
    page.click("#dsave")
    page.wait_for_selector("#tkv")
    tok = page.input_value("#tkv")
    seen = [x["id"] for x in api("GET", "/boards", tok=tok)["i"]]
    assert seen == [a], seen                                                         # the other boards do not exist for this token
    assert api("GET", "/boards/%d" % b, tok=tok).get("e") == "scope"
    page.click("#dsave")                                                             # "Copy & close"
    page.wait_for_selector("#v-admin tr:has-text('Boxed Agent') :text('Walled in')")


def test_message_an_agent_from_the_board(page):
    api("POST", "/agents", {"n": "Mailbox Agent"})
    goto(page, view="agents")
    page.click("[data-msg='Mailbox Agent']")
    page.fill("#f [name=c]", "please pick up the release notes")
    page.click("#dsave")
    assert "Sent to 1" in toast_text(page)
    got = [n for n in api("GET", "/inbox?as=Mailbox%20Agent")["i"] if n["ty"] == "msg.direct"]
    assert got and "release notes" in got[0]["m"] and got[0]["do"].startswith("say to="), got
    page.wait_for_selector("#v-agents .msg:has-text('release notes')")


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    from playwright.sync_api import sync_playwright
    tmp = tempfile.mkdtemp(prefix="tb-e2e-")
    env = {**os.environ, "TASKBOARD_DB": os.path.join(tmp, "e2e.db"), "PORT": str(PORT), "HOST": "127.0.0.1", "TASKBOARD_NUDGE_HOURS": "0",
           "TASKBOARD_TOKENS": "admin:%s:admin,Worker:%s:agent" % (ADMIN, WORKER), "TASKBOARD_ALLOW_PRIVATE_WEBHOOKS": "1"}
    srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")], env=env, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    threading.Thread(target=http.server.ThreadingHTTPServer(("127.0.0.1", SINK_PORT), Sink).serve_forever, daemon=True).start()
    for _ in range(100):
        try:
            urllib.request.urlopen(URL + "/api/health", timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    else:
        srv.kill()
        sys.exit("server did not start")
    failed = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel=os.environ.get("E2E_CHANNEL") or None, headless=True)
            for fn in TESTS:
                ctx = browser.new_context(viewport={"width": 1360, "height": 900})
                page = ctx.new_page()
                page.set_default_timeout(10000)
                errs = []
                page.on("pageerror", lambda e: errs.append(str(e)))
                try:
                    fn(page)
                    assert not errs, "page errors: %s" % errs[:2]
                    print("ok   ", fn.__name__)
                except Exception:
                    failed.append(fn.__name__)
                    print("FAIL ", fn.__name__)
                    traceback.print_exc()
                    try:
                        page.screenshot(path=os.path.join(tmp, fn.__name__ + ".png"))
                        print("      screenshot:", os.path.join(tmp, fn.__name__ + ".png"))
                    except Exception:
                        pass
                ctx.close()
            browser.close()
    finally:
        srv.terminate()
    print("\n%d passed, %d failed" % (len(TESTS) - len(failed), len(failed)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
