"""Home agent (doc 146, owner 07.11.2026): runs on the home PC with no Claude and no tokens.

Every ~20 s it takes the oldest queued row of public.home_jobs (Supabase, project kept-quiet-publisher), runs ONE action
from a closed list, and writes the result back. Every minute it updates public.home_heartbeat ("I am alive"): disk space,
the two Flow MCP servers and the Codex service. Flow is driven through its own MCP servers (flow, flow-b) over stdio with
the official MCP Python client, exactly as Claude used to call them. Actions that cost Flow credits run only when the row
carries approved_credits (the owner's approval), passed on as max_credits. No free shell commands.

Run:  python tools\\home_agent\\agent.py            (install.ps1 registers it to start at logon and restart if it stops)
Needs: pip install mcp requests ; user environment variable KQ_SUPABASE_SERVICE_KEY
"""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import traceback
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path

import requests

VERSION = "2026-11-07.3"
SB = "https://stvjtutkgpswmyrqyvec.supabase.co/rest/v1"
KEY = os.environ.get("KQ_SUPABASE_SERVICE_KEY", "")
REPO = Path(os.environ.get("KQ_REPO", r"D:\kq\youtube-ai-channel"))
BRANCH = os.environ.get("KQ_BRANCH", "claude/eager-cori-w528pc")
FLOW_OUT = Path(os.environ.get("KQ_FLOW_OUT", r"D:\kq\flow-mcp-out"))
POLL, BEAT, FLOW_BEAT = 20, 60, 300
LOG = Path(os.environ.get("KQ_AGENT_LOG", r"D:\kq\home_agent.log"))

# the two Flow MCP servers, as configured in C:\Users\User\.claude.json (docs/flow_mcp_setup.md)
SERVERS = {
    "flow": {"args": [r"C:\Users\User\flow-mcp\dist\index.js"], "cdp": 9333,
             "env": {"FLOW_MCP_OUTPUT": str(FLOW_OUT)}},
    "flow-b": {"args": [r"C:/Users/User/flow-mcp-2/dist/index.js"], "cdp": 9334,
               "env": {"FLOW_MCP_HOME": "C:/Users/User/.flow-mcp-b", "FLOW_MCP_CDP_PORT": "9334", "FLOW_MCP_PORT": "8788",
                       "FLOW_MCP_OUTPUT": str(FLOW_OUT), "FLOW_MCP_PAUSE_MIN_S": "8", "FLOW_MCP_PAUSE_MAX_S": "20"}},
}
PAID = {"flow_clips", "flow_continue"}
NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}   # no console windows popping up and closing          # cost credits: need approved_credits on the row


def log(*a):
    line = datetime.now().strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in a)
    try:
        print(line, flush=True)   # pythonw (started at logon) has no console
    except Exception:  # noqa: BLE001
        pass
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def now():
    return datetime.now(timezone.utc).isoformat()


def sb(path, method="GET", body=None, prefer=None):
    h = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    if prefer:
        h["Prefer"] = prefer
    r = requests.request(method, f"{SB}/{path}", headers=h, data=json.dumps(body) if body is not None else None, timeout=30)
    r.raise_for_status()
    return r.json() if r.text else None


# ------------------------------------------------------------------ Flow MCP sessions (kept open between jobs)
class Flow:
    def __init__(self, name):
        self.name, self.stack, self.session = name, None, None

    async def open(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        cfg = SERVERS[self.name]
        # the child does not inherit our environment by default: pass all of it plus the server's own variables
        params = StdioServerParameters(command=shutil.which("node") or "node", args=cfg["args"], env={**os.environ, **cfg["env"]})
        self.stack = AsyncExitStack()
        read, write = await self.stack.enter_async_context(stdio_client(params))
        self.session = await self.stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        log(f"{self.name}: connected")

    async def close(self):
        if self.stack:
            try:
                await self.stack.aclose()
            except Exception as e:  # noqa: BLE001
                log(f"{self.name}: close error {e}")
        self.stack = self.session = None

    async def call(self, tool, args, timeout=1200):
        if not self.session:
            await self.open()
        res = await asyncio.wait_for(self.session.call_tool(tool, arguments=args), timeout)
        texts = [getattr(c, "text", "") for c in (res.content or []) if getattr(c, "text", None)]
        structured = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
        is_err = getattr(res, "is_error", None) or getattr(res, "isError", False)
        out = {"text": "\n".join(texts)[:20000], "structured": structured}
        if is_err:
            raise RuntimeError(f"{tool} error: {out['text'][:2000]}")
        return out


FLOWS = {n: Flow(n) for n in SERVERS}
RESTART = []


def kill_chrome(cdp_port):
    """Ends the Chrome that a Flow server started (found by its --remote-debugging-port)."""
    ps = (f"Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | Where-Object {{ $_.CommandLine -like "
          f"'*--remote-debugging-port={cdp_port}*' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=60, **NOWIN)


async def restart_flow(name):
    await FLOWS[name].close()
    kill_chrome(SERVERS[name]["cdp"])
    await asyncio.sleep(3)
    await FLOWS[name].open()
    return {"restarted": name}


# ------------------------------------------------------------------ results back to git
def git(*a, check=True):
    return subprocess.run(["git", "-C", str(REPO), *a], capture_output=True, text=True, timeout=600, check=check, **NOWIN)


def push_results(job_id, files):
    """Copies the job's new files into flow_jobs/results/home_<id>/ in the repo and pushes them."""
    if not files:
        return []
    dest = REPO / "flow_jobs" / "results" / f"home_{job_id}"
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for f in files:
        shutil.copy2(f, dest / f.name)
        out.append(f"flow_jobs/results/home_{job_id}/{f.name}")
    for attempt in range(3):
        try:
            git("add", "-f", str(dest))
            git("commit", "-q", "-m", f"home agent: results of job {job_id}", check=False)
            git("pull", "-q", "--rebase", "origin", BRANCH)
            git("push", "-q", "origin", f"HEAD:{BRANCH}")
            return out
        except subprocess.CalledProcessError as e:
            log("push failed", attempt, e.stderr[-400:])
            time.sleep(10)
    raise RuntimeError("git push failed 3 times; files are in " + str(dest))


def _json(text):
    try:
        i = text.find("{")
        return json.loads(text[i:]) if i >= 0 else {}
    except ValueError:
        return {}


async def wait_done(srv, first, limit_s=7200):
    """Flow answers within ~45 s with the jobs still running (FLOW_MCP_PATIENCE_MS); call flow_wait until they finish.
    Returns (last wait answer, output_dir, files listed by Flow)."""
    j = _json(first["text"])
    out_dir = j.get("output_dir") or ""
    ids = [x["id"] for x in j.get("jobs", []) if x.get("id")]
    last, t0 = first, time.time()
    while time.time() - t0 < limit_s:
        jobs = _json(last["text"]).get("jobs", [])
        if _json(last["text"]).get("finished") is True or (jobs and all(x.get("status") not in ("running", "queued", "pending") for x in jobs)):
            break
        last = await FLOWS[srv].call("flow_wait", {"job_ids": ids, "timeout_seconds": 900} if ids else {"timeout_seconds": 900}, 1000)
    files = [f for x in _json(last["text"]).get("jobs", []) for f in (x.get("files") or [])]
    return last, out_dir, files


def collect(out_dir, listed, project, since):
    found = [Path(f) for f in listed if Path(f).is_file()]
    for d in [Path(out_dir)] if out_dir else []:
        if d.exists():
            found += [p for p in d.rglob("*") if p.is_file() and p.stat().st_mtime >= since - 2
                      and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".wav", ".mp3")]
    found += new_files(project, since)
    seen, out = set(), []
    for f in found:
        k = str(f.resolve()).lower()
        if k not in seen:
            seen.add(k); out.append(f)
    return out


def new_files(project, since):
    d = FLOW_OUT / project
    if not d.exists():
        return []
    return sorted(p for p in d.rglob("*") if p.is_file() and p.stat().st_mtime >= since - 2
                  and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".wav", ".mp3"))


# ------------------------------------------------------------------ the closed list of actions
async def run_action(job):
    a, args = job["action"], job.get("args") or {}
    srv = args.get("server", "flow")
    if srv not in FLOWS:
        raise ValueError(f"unknown server {srv}")
    if a in PAID and not isinstance(job.get("approved_credits"), int):
        raise PermissionError(f"{a} costs Flow credits: the row needs approved_credits (the owner's approval)")
    t0 = time.time()
    if a == "status":
        return {"heartbeat": heartbeat_row(with_flow=True)}
    if a == "flow_status":
        return await FLOWS[srv].call("flow_status", {"detailed": True}, 120)
    if a == "flow_images":                      # free: Flow's Agent mode, up to 50 images, 0 credits
        keep = {k: args[k] for k in ("project", "scenes", "aspect_ratio", "references", "scene_references") if k in args}
        r = await FLOWS[srv].call("flow_agent_images", keep, 3600)
        w, out_dir, listed = await wait_done(srv, r)
        return {"start": r["text"][:3000], "wait": w["text"][-3000:],
                "files": push_results(job["id"], collect(out_dir, listed, args["project"], t0))}
    if a == "flow_clips":                       # paid
        r = await FLOWS[srv].call("flow_generate", {"project": args["project"], "scenes": args["scenes"],
                                                    "max_credits": job["approved_credits"]}, 600)
        w, out_dir, listed = await wait_done(srv, r)
        return {"start": r["text"][:3000], "wait": w["text"][-3000:],
                "files": push_results(job["id"], collect(out_dir, listed, args["project"], t0))}
    if a == "flow_continue":                    # paid
        keep = {k: args[k] for k in ("project", "from_asset", "prompt", "attach", "mode", "force") if k in args}
        r = await FLOWS[srv].call("flow_continue", {**keep, "acknowledge_cost": True}, 1800)
        w, out_dir, listed = await wait_done(srv, r)
        return {"start": r["text"][:3000], "wait": w["text"][-3000:],
                "files": push_results(job["id"], collect(out_dir, listed, args["project"], t0))}
    if a in ("flow_download", "flow_retry", "flow_wait", "flow_assets"):   # free
        r = await FLOWS[srv].call(a, {k: v for k, v in args.items() if k != "server"}, 1800)
        if a == "flow_download" and args.get("project"):
            w, out_dir, listed = await wait_done(srv, r)
            r["files"] = push_results(job["id"], collect(out_dir, listed, args["project"], t0))
        elif a == "flow_wait":
            j = _json(r["text"])
            files = [Path(f) for x in j.get("jobs", []) for f in (x.get("files") or []) if Path(f).is_file()]
            if args.get("push"):
                r["files"] = push_results(job["id"], files)
        return r
    if a == "codex_batch":                      # hands a batch to codex_jobs\watch.ps1 (it already runs without Claude)
        name = "".join(c for c in args["name"] if c.isalnum() or c in "_-")
        q = REPO / "codex_jobs" / "queue" / f"{name}.json"
        q.write_text(json.dumps(args["batch"], ensure_ascii=False, indent=1), encoding="utf-8")
        return {"queued": str(q), "watcher": codex_alive()}
    if a == "restart_flow":
        return await restart_flow(srv)
    if a == "restart_codex_watch":
        return restart_codex()
    if a == "self_update":                      # new agent.py from the media repo, then exit: the scheduled task restarts it
        src = "https://raw.githubusercontent.com/kobimazuz/kq-media/main/studio/home_agent.py"
        code = requests.get(src, timeout=60).text
        compile(code, "agent.py", "exec")       # never replace a working agent with a broken file
        Path(__file__).write_text(code, encoding="utf-8")
        RESTART.append(True)
        return {"updated": True, "bytes": len(code)}
    if a == "git_pull":
        return {"out": git("pull", "-q", "origin", BRANCH, check=False).stdout[-1000:]}
    raise ValueError(f"unknown action {a}")


# ------------------------------------------------------------------ Codex service and heartbeat
def codex_alive():
    ps = ("(Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | Where-Object { $_.CommandLine -like "
          "'*codex_jobs*watch.ps1*' } | Measure-Object).Count")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=60, **NOWIN)
    return r.stdout.strip() not in ("", "0")


def restart_codex():
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | Where-Object { $_.CommandLine -like "
          "'*codex_jobs*watch.ps1*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=60, **NOWIN)
    env = {**os.environ, "CODEX_HOME": os.environ.get("CODEX_HOME", r"D:\kq\codex-home")}
    subprocess.Popen(["powershell.exe", "-NoExit", "-ExecutionPolicy", "Bypass", "-File", r"codex_jobs\watch.ps1"],
                     cwd=str(REPO), env=env, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
    time.sleep(5)
    return {"codex_watch": codex_alive()}


FLOW_STATE = {}


def heartbeat_row(with_flow=False):
    disk = {}
    for d in ("C:\\", "D:\\"):
        try:
            u = shutil.disk_usage(d)
            disk[d[0]] = {"free_gb": round(u.free / 1e9, 1), "total_gb": round(u.total / 1e9, 1)}
        except OSError:
            pass
    return {"id": "home", "at": now(), "host": socket.gethostname(), "disk": disk, "flow": FLOW_STATE,
            "codex": {"watch_running": codex_alive()}, "agent_version": VERSION}


async def refresh_flow_state():
    for n, fl in FLOWS.items():
        try:
            r = await fl.call("flow_status", {"detailed": False}, 120)
            FLOW_STATE[n] = {"ok": True, "at": now(), "status": r["text"][:1500]}
        except Exception as e:  # noqa: BLE001
            FLOW_STATE[n] = {"ok": False, "at": now(), "error": str(e)[:500]}
            await fl.close()


def beat(running=None):
    row = heartbeat_row()
    row["running_job"] = running
    sb("home_heartbeat?on_conflict=id", "POST", row, prefer="resolution=merge-duplicates")


# ------------------------------------------------------------------ main loop
async def handle(job):
    log(f"job {job['id']} {job['action']} start")
    err = None
    for attempt in range(3):          # one retry, then restart Flow and one more try (doc 146)
        try:
            res = await run_action(job)
            sb(f"home_jobs?id=eq.{job['id']}", "PATCH", {"status": "done", "result": res, "finished_at": now(), "attempts": attempt + 1})
            log(f"job {job['id']} done")
            return
        except PermissionError as e:
            err = str(e)
            break
        except Exception as e:  # noqa: BLE001
            err = f"{e}\n{traceback.format_exc()[-3000:]}"
            log(f"job {job['id']} attempt {attempt + 1} failed: {e}")
            srv = (job.get("args") or {}).get("server", "flow")
            if job["action"].startswith("flow") and srv in FLOWS:
                if attempt == 1:
                    try:
                        await restart_flow(srv)
                    except Exception as e2:  # noqa: BLE001
                        log("restart failed", e2)
                else:
                    await FLOWS[srv].close()
            if job["action"] in PAID:   # never run a paid action twice by itself: report and stop
                break
    sb(f"home_jobs?id=eq.{job['id']}", "PATCH", {"status": "failed", "log": (err or "")[-8000:], "finished_at": now()})
    log(f"job {job['id']} failed")


async def main():
    if not KEY:
        sys.exit("KQ_SUPABASE_SERVICE_KEY is not set (run install.ps1)")
    log(f"home agent {VERSION} on {socket.gethostname()}")
    last_beat = last_flow = 0
    while True:
        try:
            if time.time() - last_flow > FLOW_BEAT:
                await refresh_flow_state()
                last_flow = time.time()
            if time.time() - last_beat > BEAT:
                beat()
                last_beat = time.time()
            rows = sb("home_jobs?status=eq.queued&order=id.asc&limit=1")
            if rows:
                job = rows[0]
                # claim it only if it is still queued (another agent instance cannot take it twice)
                got = sb(f"home_jobs?id=eq.{job['id']}&status=eq.queued", "PATCH",
                         {"status": "running", "started_at": now()}, prefer="return=representation")
                if got:
                    beat(running=job["id"])
                    await handle(got[0])
                    beat()
                    last_beat = time.time()
                    if RESTART:
                        log("updated, restarting")
                        for fl in FLOWS.values():
                            await fl.close()
                        os._exit(3)           # the scheduled task starts the new version within a minute
                    continue
        except Exception as e:  # noqa: BLE001
            log("loop error:", e)
        await asyncio.sleep(POLL)


if __name__ == "__main__":
    asyncio.run(main())
