#!/usr/bin/env python3
"""Agent parity: everything the web UI can do through the REST API must also be doable through an MCP tool.
Reads the UI's API calls from static/app.js and the MCP tools' calls from mcp_server.py and fails when the screen can do something no tool can.
Usage: python3 test_parity.py"""
import os, re, sys
HERE = os.path.dirname(os.path.abspath(__file__))
ui_js = open(os.path.join(HERE, "static", "app.js")).read()
mcp_py = open(os.path.join(HERE, "mcp_server.py")).read()


def norm(p):
    p = p.split("?")[0]
    p = re.sub(r"\$\{[^}]*\}|\{[^}]*\}|%s|%d", "{id}", p)
    p = re.sub(r"/\d+(?=/|$)", "/{id}", p)
    return tuple(x for x in p.strip("/").split("/") if x)


def ui_calls():
    out = set()
    for m in re.finditer(r'(?:api|rawPost|download)\(\s*(?:"([A-Z]+)",\s*)?([`"])(/[^`"]*)\2(\s*\+)?', ui_js):
        meth = m.group(1) or ("GET" if "download" in m.group(0)[:9] else "POST")
        path = m.group(3)
        if m.group(4):                       # "/tasks/" + id: a path with one more segment
            path = path.rstrip("/") + "/{id}"
        out.add((meth, norm(path)))
    return out


def mcp_calls():
    out = set()
    for m in re.finditer(r'api(?:_text)?\(\s*"([A-Z]+)",\s*f?"(/[^"]*)"', mcp_py):
        out.add((m.group(1), norm(m.group(2))))
    return out


# what only a browser does (events stream, static documents) or has another route-shaped spelling in the tools
EXEMPT = {("GET", ("events",)), ("GET", ("AGENTS.md",)), ("GET", ("playbook.md",)), ("GET", ("methods",))}
ui, mcp = ui_calls(), mcp_calls()
# a tool for a family covers the sub-actions that are the same call with a different verb: PATCH/DELETE /x/{id} are covered by the set_/del_ tools
missing = []
for meth, path in sorted(ui):
    if (meth, path) in EXEMPT or (meth, path) in mcp:
        continue
    if (len(path) == 1 or path[-1] == "{id}") and any(m == meth and p[:1] == path[:1] and len(p) <= 2 for m, p in mcp):
        continue                             # e.g. DELETE /tasks/{id} spelled "/tasks/" + id in the UI
    if len(path) == 3 and path[2] == "{id}" and any(m == meth and len(p) == 3 and p[:2] == path[:2] and p[2] != "{id}" for m, p in mcp):
        continue                             # /sprints/{id}/${act}: the verb is a variable in the UI, spelled out in the tools (start_sprint, close_sprint, ...)
    missing.append((meth, "/" + "/".join(path)))
assert not missing, "the screen can do these but no MCP tool can: %s" % missing
print("parity OK: %d UI calls, %d covered by tools" % (len(ui), len(ui) - len(EXEMPT & ui)))
