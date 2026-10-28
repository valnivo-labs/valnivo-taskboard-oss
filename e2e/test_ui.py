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


def click_until(page, click_sel, expect_sel, tries=4):
    """Click, and click again if the page re-rendered under the pointer (live updates redraw lists) and nothing happened."""
    for i in range(tries):
        page.click(click_sel)
        try:
            page.wait_for_selector(expect_sel, timeout=3000)
            return
        except Exception:
            if i == tries - 1:
                raise


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
    page.wait_for_selector("#v-overview h2:has-text('Agent spend')")
    assert "$1.5" in page.inner_text("#v-overview")


def test_deep_link_opens_the_task(page):
    b, _ = mkboard("Deep %d" % time.time())
    tid = mktask(b, "deep linked task")
    page.goto("%s/?token=%s#t/%d" % (URL, ADMIN, tid))
    page.wait_for_selector("#dlg[open]")
    assert "deep linked task" in page.inner_text("#dlg")


def test_admin_chat_channel_and_git_secret(page):
    goto(page, view="admin")
    page.click("[data-admsec=chat]")
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
    page.click("[data-admsec=git]")
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
    page.click("#v-agents [data-msg='Mailbox Agent']")
    page.fill("#f [name=c]", "please pick up the release notes")
    page.click("#dsave")
    assert "Sent to 1" in toast_text(page)
    got = [n for n in api("GET", "/inbox?as=Mailbox%20Agent")["i"] if n["ty"] == "msg.direct"]
    assert got and "release notes" in got[0]["m"] and got[0]["do"].startswith("say to="), got
    page.wait_for_selector("#v-agents .msg:has-text('release notes')")


def test_today_is_the_start_screen(page):
    b, _ = mkboard("Today %d" % time.time())
    t = mktask(b, "stuck on something", st="blocked", pr=1, bl="waiting for a key")
    page.goto("%s/?token=%s" % (URL, ADMIN))                                         # a fresh browser lands on Today
    page.wait_for_selector("#v-today .needrow")
    assert "blocked" in page.inner_text("#v-today").lower() and page.locator("#v-today .tile").count() >= 4
    click_until(page, "#v-today .needrow [data-open]", "#dlg.drawer[open]")          # tasks open in a side drawer
    assert page.locator("#dlg").bounding_box()["x"] > 400


def test_command_bar(page):
    b, _ = mkboard("Palette %d" % time.time())
    mktask(b, "find me by the palette")
    goto(page, view="board", scope="b:%d" % b)
    page.keyboard.press("Control+k")
    page.wait_for_selector("#cmdk[open]")
    page.fill("#cmdq", "by the palette")
    page.wait_for_selector("#cmdlist .cmditem:has-text('find me by the palette')")
    page.keyboard.press("Enter")
    page.wait_for_selector("#dlg.drawer[open]")
    assert "find me by the palette" in page.inner_text("#dlg")
    page.keyboard.press("Escape")
    page.keyboard.press("Control+k")
    page.fill("#cmdq", "agents")
    page.wait_for_selector("#cmdlist .cmditem.on:has-text('Agents')")
    page.keyboard.press("Enter")
    page.wait_for_selector("#v-agents:not([hidden])")
    page.click("#cmdbtn")                                                            # the sidebar button opens it too
    page.wait_for_selector("#cmdk[open]")


def test_admin_is_split_into_sections(page):
    goto(page, view="admin")
    page.wait_for_selector(".admnav")
    assert page.locator("[data-sec=access]").is_visible() and not page.locator("[data-sec=chat]").is_visible()
    page.click("[data-admsec=chat]")
    assert page.locator("[data-sec=chat]").is_visible() and not page.locator("[data-sec=access]").is_visible()
    secs = page.evaluate("[...document.querySelectorAll('#v-admin .admnav [data-admsec]')].map(b => b.dataset.admsec)")
    assert "roles" in secs and set(secs) == set(page.evaluate("[...document.querySelectorAll('#v-admin .admbody>[data-sec]')].map(p => p.dataset.sec)")), secs
    for sec in secs:                                                                 # every nav entry must reveal a non-empty panel
        page.click("[data-admsec=%s]" % sec)
        assert page.locator("[data-sec=%s]" % sec).is_visible(), sec
        shown = page.evaluate("[...document.querySelectorAll('#v-admin [data-sec]')].filter(p => p.offsetParent).map(p => p.dataset.sec)")
        assert shown == [sec], shown                                                 # exactly one section at a time
        assert page.locator("[data-sec=%s]" % sec).bounding_box()["width"] > 200, sec
        over = page.evaluate("[...document.querySelectorAll('#v-admin .panel')].filter(p => p.offsetParent && p.scrollWidth > p.clientWidth + 1).length")
        assert over == 0, (sec, over)                                                # nothing spills out of its panel


def test_agents_show_state_and_filter(page):
    b, _ = mkboard("States %d" % time.time())
    mktask(b, "busy work", st="in_progress", **{"as": "State Agent"})
    goto(page, view="agents", scope="b:%d" % b)
    page.wait_for_selector(".agent:has-text('State Agent') .badge:has-text('Working')")
    page.click("[data-agf=blocked]")
    page.wait_for_selector(".agent:has-text('State Agent')", state="detached")       # filtered out
    page.click("[data-agf=working]")
    page.wait_for_selector(".agent:has-text('State Agent')")


def test_phone_layout(page):
    page.set_viewport_size({"width": 390, "height": 844})
    goto(page, view="today")
    box = page.locator("#tabs").bounding_box()
    assert box["y"] + box["height"] >= 840 and box["width"] >= 380, box              # tab bar at the bottom
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    page.click('#tabs [data-view="agents"]')
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")


def test_saved_views_are_shared_with_agents(page):
    b, _ = mkboard("Views %d" % time.time())
    mktask(b, "a blocked one", st="blocked", pr=1)
    api("POST", "/views", {"n": "Made by an agent", "f": {"preset": "blocked"}})
    goto(page, view="board", scope="b:%d" % b)
    page.wait_for_selector("#views option:has-text('Made by an agent')", state="attached")        # a view an agent saved shows up on the screen
    page.click("#morefil summary")
    page.click("#presets [data-preset=open]")
    page.fill("#search", "blocked")
    page.click("#morefil summary")
    page.click("#saveview")
    page.fill("#f [name=name]", "Saved on screen")
    page.click("#dsave")
    assert "View saved" in toast_text(page)
    saved = [v for v in api("GET", "/views")["i"] if v["n"] == "Saved on screen"]
    assert saved and saved[0]["f"].get("q") == "blocked" and saved[0]["f"].get("preset") == "open", saved       # and one saved on screen is there for agents


def test_today_can_be_clicked_while_the_boards_still_load(page):
    b, _ = mkboard("Slow %d" % time.time())
    mktask(b, "blocked while loading", st="blocked", pr=1, bl="slow")
    page.add_init_script("const of = window.fetch; window.fetch = (u, o) => /\\/api\\/boards\\/\\d+/.test(String(u)) ? new Promise(r => setTimeout(r, 2500)).then(() => of(u, o)) : of(u, o);")   # a slow network for the board snapshots
    page.goto("%s/?token=%s" % (URL, ADMIN))
    page.wait_for_selector("#v-today .needrow [data-open]")
    assert page.evaluate("B === null"), "the boards must still be loading for this test to mean anything"
    page.click("#v-today .needrow [data-open]")                                       # clicked before B exists: the app waits for it, then opens the task
    page.wait_for_selector("#dlg.drawer[open]", timeout=60000)      # every earlier test left a board, and the browser loads only a few snapshots at a time


def test_drawer_shows_due_runs_and_approvals(page):
    b, _ = mkboard("Runs %d" % time.time())
    tid = mktask(b, "task with history", st="in_progress", **{"as": "Runner Two"})
    rid = api("POST", "/runs", {"as": "Runner Two", "task": tid, "runtime": "agent-cli", "job": "j9"})["id"]
    api("POST", "/runs/%d/end" % rid, {"as": "Runner Two", "st": "failed", "out": "tests did not pass"})
    api("POST", "/tasks/%d/approval" % tid, {"as": "Runner Two", "action": "deploy to production", "to": "admin"})
    goto(page, view="board", scope="b:%d" % b)
    page.click(".card[data-id='%d'] .t" % tid)
    page.wait_for_selector("#runsbox:not([hidden]) .chip:has-text('failed')")
    assert "tests did not pass" in page.inner_text("#runsbox")
    page.fill("#f [name=due]", "2020-01-01")
    page.click("#dsave")
    page.wait_for_selector(".card[data-id='%d'] .chip.late" % tid)                   # overdue shows in red on the card
    assert api("GET", "/tasks/%d" % tid)["du"] == "2020-01-01"
    page.click(".card[data-id='%d'] .t" % tid)
    page.click("#f [data-tab=review]")
    page.wait_for_selector("#rvbox .chip:has-text('approval')")


def test_audit_log_section(page):
    b, _ = mkboard("Audit %d" % time.time())
    mktask(b, "audited change")
    goto(page, view="admin")
    page.click("[data-admsec=audit]")
    page.wait_for_selector("#auditbox table tr:has-text('task.created')")
    page.fill("#auditf [name=type]", "run.")
    page.click("#auditf button[type=submit]")
    page.wait_for_selector("#auditbox:has-text('Nothing matches'), #auditbox tr:has-text('run.')")


def test_agent_quota_is_visible_and_clearable(page):
    api("POST", "/agents", {"n": "Quota Agent", "quota_min": 45, "quota_note": "5h limit"})
    goto(page, view="agents")
    page.wait_for_selector("#v-agents .agent:has-text('Quota Agent') .badge:has-text('Quota')")
    page.click("#v-agents .agent:has-text('Quota Agent') [data-clearq]")
    page.wait_for_selector("#v-agents .agent:has-text('Quota Agent') .badge:has-text('Quota')", state="detached")


def test_system_overview_shows_machines_and_limits(page):
    api("POST", "/nodes", {"name": "e2e-box", "machine": "test", "agents": ["claude"], "quota": [{"source": "claude", "label": "E2E plan", "used_pct": 55}]})
    goto(page, view="system")
    page.wait_for_selector("#v-system .node:has-text('e2e-box')")
    assert "Up" in page.inner_text("#v-system .node:has-text('e2e-box')")
    page.wait_for_selector("#v-system:has-text('E2E plan')")
    page.click("#v-system [data-days='30']")
    page.wait_for_selector("#v-system .sbar >> nth=29")


def test_task_timeline_and_independent_check(page):
    b, _ = mkboard("Timeline %d" % time.time())
    t = mktask(b, "timeline subject", ac="Works\nDocumented", **{"as": "Worker"})
    api("PATCH", "/tasks/%d" % t, {"st": "review"})
    goto(page, scope="b:%d" % b)
    page.click(".card[data-id='%d'] .t" % t)
    page.wait_for_selector("#f [name=accept]")
    assert "Works" in page.input_value("#f [name=accept]")
    page.fill("#vfn", "checked by the e2e")
    page.click("#vfok")
    page.wait_for_selector("#toast:has-text('Verified')")
    assert api("GET", "/tasks/%d" % t)["vb"] == "admin"
    page.click(".card[data-id='%d'] .t" % t)
    page.click(".dtab[data-tab=activity]")
    page.wait_for_selector("#actl .tl")
    assert "who held it" in page.inner_text("#actl").lower()


def test_pipeline_baton_and_strict_move_message(page):
    b, _ = mkboard("Pipeline %d" % time.time())
    t = mktask(b, "pipeline subject", pl="Build | admin | auto\nCheck | someone-else | review")
    api("POST", "/tasks/%d/stage" % t, {"action": "start"})
    goto(page, scope="b:%d" % b)
    page.click(".card[data-id='%d'] .t" % t)
    page.wait_for_selector("#pgd")
    page.click("#pgd")
    page.wait_for_selector("#toast:has-text('Done')")
    task = api("GET", "/tasks/%d" % t)
    assert task["sg"] == 2 and task["as"] == "someone-else", task
    api("POST", "/settings", {"strict_moves": "1"})
    try:
        goto(page, scope="b:%d" % b)
        page.click(".card[data-id='%d'] .t" % t)
        page.select_option("#f [name=status]", "done")
        page.click("#dsave")
        page.wait_for_selector("#toast:has-text('not allowed')")
    finally:
        api("POST", "/settings", {"strict_moves": "0"})


def test_story_holds_tasks_and_shows_progress(page):
    b, _ = mkboard("Tree %d" % time.time())
    ep = mktask(b, "tree epic", ty="epic")
    sy = mktask(b, "tree story", ty="story", pa=ep)
    done = mktask(b, "finished piece", pa=sy)
    api("PATCH", "/tasks/%d" % done, {"st": "done"})
    goto(page, scope="b:%d" % b)
    page.wait_for_selector(".card[data-id='%d']:has-text('1/1 done')" % sy)
    page.click(".card[data-id='%d'] .t" % sy)
    page.click("#addkid")
    page.wait_for_selector("#f [name=title]")
    assert page.input_value("#f [name=parent]") == str(sy)
    page.fill("#f [name=title]", "new piece under the story")
    page.click("#dsave")
    page.wait_for_selector(".card:has-text('new piece under the story')")
    kid = [x for x in api("GET", "/tasks?b=%d&limit=50" % b)["i"] if x["t"] == "new piece under the story"][0]
    assert kid["pa"] == sy and api("GET", "/tasks/%d?ctx=1" % sy)["prog"] == [1, 2]


def test_decisions_scaffold_and_state_of_play(page):
    b, _ = mkboard("Collab %d" % time.time())
    sy = mktask(b, "collab story", ty="story")
    goto(page, scope="b:%d" % b)
    page.click(".card[data-id='%d'] .t" % sy)
    page.wait_for_selector("#decin")
    page.fill("#decin", "Reset links expire after 30 minutes")
    page.click("#decgo")
    page.wait_for_selector("#decbox:has-text('expire after 30 minutes')")
    page.select_option("#scp", "bugfix")
    page.click("#scgo")
    page.wait_for_selector("#toast:has-text('Tasks added')")
    kids = [x for x in api("GET", "/tasks?b=%d&limit=50" % b)["i"] if x.get("pa") == sy]
    assert len(kids) == 4 and {k["ty"] for k in kids} >= {"bug", "spike"}, kids
    page.click(".card[data-id='%d'] .t" % kids[0]["id"])
    page.wait_for_selector("#f [name=play]")
    page.fill("#f [name=play]", "Reproduced on staging; fix is next")
    page.click("#dsave")
    page.wait_for_selector("#toast")
    assert api("GET", "/tasks/%d?brief=1" % kids[0]["id"])["po"] == "Reproduced on staging; fix is next"


def test_board_way_of_working_can_be_chosen(page):
    b, _ = mkboard("Rigor %d" % time.time())
    goto(page, scope="b:%d" % b)
    page.click("#bset")
    page.wait_for_selector(".pcard")
    page.click(".pcard:has-text('Strict')")
    page.wait_for_function("document.querySelector('[name=rule_require_parent]').checked")
    page.click("#dsave")
    page.wait_for_selector("#toast:has-text('Board settings saved')")
    pv = api("GET", "/policy?b=%d" % b)
    assert pv["board"]["preset"] == "strict" and "require_parent" in pv["board"]["rules"], pv
    r = api("POST", "/tasks", {"b": b, "t": "no parent", "ch": "Core"})
    assert r.get("e") == "parent", r
    api("POST", "/boards/%d/policy" % b, {"preset": "loose"})


def test_today_decides_proposals_and_shows_waits(page):
    b, _ = mkboard("Proposals %d" % time.time())
    pid = api("POST", "/proposals", {"b": b, "t": "Propose dark mode", "ty": "story", "why": "asked twice"}, tok=WORKER)["id"]
    t = mktask(b, "waiting subject", **{"as": "Worker"})
    api("POST", "/tasks/%d/wait" % t, {"kind": "human", "owner": "admin", "reason": "Which colour?"}, tok=WORKER)
    goto(page, view="today")
    page.wait_for_selector("[data-prop='%d:accept']" % pid)
    assert page.is_visible("text=Waiting for someone")
    page.click("[data-prop='%d:accept']" % pid)
    page.wait_for_selector("#toast:has-text('Accepted as')")
    assert api("GET", "/proposals?st=accepted")["i"][0]["id"] == pid


def test_proof_can_be_added_to_a_criterion(page):
    b, _ = mkboard("Proof %d" % time.time())
    t = mktask(b, "needs proof", ac="First point\nSecond point\ncheck: pr_linked", st="review", **{"as": "Worker"})
    goto(page, scope="b:%d" % b)
    page.click(".card[data-id='%d'] .t" % t)
    page.wait_for_selector("#evform:not([hidden])")
    page.fill("#evnote", "ran it twice")
    page.click("#evgo")
    page.wait_for_selector("#acl li.ok")
    ev = api("GET", "/tasks/%d/evidence" % t)
    assert ev["i"][0]["note"] == "ran it twice" and len(ev["need"]) == 2, ev


def test_a_new_machine_waits_for_admission(page):
    api("POST", "/settings", {"node_admission": "manual"})
    try:
        api("POST", "/nodes", {"name": "pending-box", "machine": "test"}, tok=WORKER)
        goto(page, view="system")
        page.wait_for_selector("#v-system .node:has-text('pending-box') [data-nodeact='admit']")
        page.click("#v-system .node:has-text('pending-box') [data-nodeact='admit']")
        page.wait_for_selector("#v-system .node:has-text('pending-box'):has-text('Up')")
    finally:
        api("POST", "/settings", {"node_admission": "auto"})


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
            browser = pw.chromium.launch(channel=os.environ.get("E2E_CHANNEL") or None, headless=True, args=["--disable-dev-shm-usage"])
            for fn in [t for t in TESTS if not os.environ.get("E2E_ONLY") or os.environ["E2E_ONLY"] in t.__name__]:
                for attempt in (1, 2):                          # a slow runner can time a step out once: a real bug fails both times
                    ctx = browser.new_context(viewport={"width": 1360, "height": 900})
                    page = ctx.new_page()
                    page.set_default_timeout(10000)
                    errs, logs = [], []
                    page.on("pageerror", lambda e: errs.append(str(e)))
                    page.on("console", lambda m: logs.append("%s: %s" % (m.type, m.text[:200])) if m.type in ("error", "warning") else None)
                    try:
                        fn(page)
                        assert not errs, "page errors: %s" % errs[:2]
                        print("ok   ", fn.__name__, "(second try)" if attempt == 2 else "")
                        ctx.close()
                        break
                    except Exception:
                        print("FAIL ", fn.__name__, "(first try, retrying)" if attempt == 1 else "")
                        traceback.print_exc()
                        print("      page errors:", errs[:5], "console:", logs[:6])
                        try:
                            print("      url:", page.url, "| view:", page.evaluate("localStorage.tb_view"), "| dialog open:", page.evaluate("document.getElementById('dlg').open"), "| toast:", page.evaluate("(document.getElementById('toast') || {}).textContent"))
                        except Exception:
                            pass
                        try:
                            page.screenshot(path=os.path.join(tmp, fn.__name__ + ".png"))
                            print("      screenshot:", os.path.join(tmp, fn.__name__ + ".png"))
                        except Exception:
                            pass
                        if attempt == 2:
                            failed.append(fn.__name__)
                        ctx.close()
            browser.close()
    finally:
        srv.terminate()
    print("\n%d passed, %d failed" % (len(TESTS) - len(failed), len(failed)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
