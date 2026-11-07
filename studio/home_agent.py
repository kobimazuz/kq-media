"""Home agent (doc 146, owner 07.11.2026): runs on the home PC with no Claude and no tokens.

Every ~20 s it takes the oldest queued row of public.home_jobs (Supabase, project kept-quiet-publisher), runs ONE action
from a closed list, and writes the result back. Every minute it updates public.home_heartbeat ("I am alive"): disk space,
the Flow MCP server and the Codex service. Flow (ONE account, server "flow") is driven through its MCP server over stdio with
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

VERSION = "2026-11-07.9"
SB = "https://stvjtutkgpswmyrqyvec.supabase.co/rest/v1"
KEY = os.environ.get("KQ_SUPABASE_SERVICE_KEY", "")
REPO = Path(os.environ.get("KQ_REPO", r"D:\kq\youtube-ai-channel"))
BRANCH = os.environ.get("KQ_BRANCH", "claude/eager-cori-w528pc")
FLOW_OUT = Path(os.environ.get("KQ_FLOW_OUT", r"D:\kq\flow-mcp-out"))
POLL, BEAT, FLOW_BEAT = 20, 60, 300
LOG = Path(os.environ.get("KQ_AGENT_LOG", r"D:\kq\home_agent.log"))

# the agent's OWN two Flow MCP servers (owner 07.11.2026: "two separate, clean ones, not what was already open"): own
# Chrome profiles, CDP and studio ports, so they never touch the Chrome/studio of a Claude session at home (9333/9334,
# 8787/8788). Each profile needs one Google sign-in in the Chrome window it opens the first time. Output stays on C, the
# drive Flow downloads to (C -> D gave "EXDEV: cross-device link not permitted"); the agent then copies to the repo.
SERVERS = {   # ONE Flow account on this PC: account A in its existing profile ~\.flow-mcp\chrome-profile, server "flow"
    # (flow_jobs/AGENT.md 02.11.2026: account B got an unusual-activity warning, flow-b / flow-mcp-2 are disabled; owner
    # 07.11.2026: two accounts on one PC collide). No new profile, no automatic sign-in. One instance, one job at a time:
    # while the agent runs, no Claude session at home should drive Flow (same profile, same Chrome on port 9333).
    "flow": {"args": [r"C:\Users\User\flow-mcp\dist\index.js"], "cdp": 9333, "studio": 8787,
             "env": {"FLOW_MCP_OUTPUT": r"C:\Users\User\flow-agent-out"}},
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
    """One Flow MCP server. Its stdio connection lives inside ONE worker task, which opens it, serves calls from a queue
    and closes it, so opening and closing always happen in the same task. (07.11.2026: closing it from another task gave
    anyio "Attempted to exit cancel scope in a different task than it was entered in" and took the whole agent down.)"""

    def __init__(self, name):
        self.name, self.task, self.q, self.session = name, None, None, None

    async def _worker(self, ready):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        cfg = SERVERS[self.name]
        # the child does not inherit our environment by default: pass all of it plus the server's own variables
        params = StdioServerParameters(command=shutil.which("node") or "node", args=cfg["args"], env={**os.environ, **cfg["env"]})
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self.session = session
                    log(f"{self.name}: connected (account A profile, studio http://127.0.0.1:{cfg['studio']})")
                    ready.set_result(True)
                    while True:
                        item = await self.q.get()
                        if item is None:
                            break
                        tool, args, fut = item
                        try:
                            res = await session.call_tool(tool, arguments=args)
                            if not fut.done():
                                fut.set_result(res)
                        except Exception as e:  # noqa: BLE001
                            if not fut.done():
                                fut.set_exception(e)
        except BaseException as e:  # noqa: BLE001
            if not ready.done():
                ready.set_exception(e if isinstance(e, Exception) else RuntimeError(str(e)))
            else:
                log(f"{self.name}: connection ended: {e}")
        finally:
            self.session = None
            while self.q and not self.q.empty():          # anyone still waiting gets an error, not a hang
                item = self.q.get_nowait()
                if item and not item[2].done():
                    item[2].set_exception(RuntimeError("Flow connection closed"))

    async def open(self):
        self.q = asyncio.Queue()
        ready = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(self._worker(ready))
        await asyncio.wait_for(ready, 180)

    async def close(self):
        if self.task and not self.task.done():
            self.q.put_nowait(None)
            try:
                await asyncio.wait_for(asyncio.shield(self.task), 20)
            except Exception as e:  # noqa: BLE001
                log(f"{self.name}: close timeout ({e}), cancelling")
                self.task.cancel()
        self.task = None

    async def call(self, tool, args, timeout=1200):
        if not self.task or self.task.done():
            await self.open()
        fut = asyncio.get_running_loop().create_future()
        self.q.put_nowait((tool, args, fut))
        res = await asyncio.wait_for(fut, timeout)
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
            git("pull", "-q", "--rebase", "--autostash", "origin", BRANCH)   # other local edits (watch.ps1 etc.) must not block it
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
    ds = [Path(c["env"]["FLOW_MCP_OUTPUT"]) / project for c in SERVERS.values()] + [FLOW_OUT / project]
    d = next((x for x in ds if x.exists()), None)
    if d is None:
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
    if a == "flow_images_single":               # one image per scene, Flow's own generator, exact prompt, 0 credits
        scenes = [{"prompt": s, "type": "image", "max_credits": 0, **({"aspect_ratio": args["aspect_ratio"]} if "aspect_ratio" in args else {})}
                  for s in args["scenes"]]
        r = await FLOWS[srv].call("flow_generate", {"project": args["project"], "scenes": scenes}, 600)
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
        r = git("pull", "-q", "origin", BRANCH, check=False)
        return {"code": r.returncode, "out": (r.stdout + r.stderr)[-1500:]}
    if a == "git_sync":                         # pull (keeping local edits aside) and push what is committed but not pushed
        r1 = git("pull", "-q", "--rebase", "--autostash", "origin", BRANCH, check=False)
        r2 = git("push", "-q", "origin", f"HEAD:{BRANCH}", check=False)
        st = git("status", "-sb", check=False)
        return {"pull": [r1.returncode, (r1.stdout + r1.stderr)[-1200:]], "push": [r2.returncode, (r2.stdout + r2.stderr)[-1200:]],
                "status": st.stdout[-1500:]}
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


def lane(job):
    """Jobs run in parallel lanes, one job per lane: each Flow server is a lane, everything else is the local lane
    (owner 07.11.2026: the Codex batch waited behind a long Flow job)."""
    a, args = job["action"], job.get("args") or {}
    return args.get("server", "flow") if a.startswith("flow") or a == "restart_flow" else "local"


async def main():
    if not KEY:
        sys.exit("KQ_SUPABASE_SERVICE_KEY is not set (run install.ps1)")
    log(f"home agent {VERSION} on {socket.gethostname()}")
    last_beat = last_flow = 0
    busy = {}                                   # lane -> asyncio task

    async def run(job, ln):
        try:
            await handle(job)
        finally:
            busy.pop(ln, None)
            try:
                beat()
            except Exception:  # noqa: BLE001
                pass

    while True:
        try:
            if time.time() - last_flow > FLOW_BEAT and not any(k in busy for k in FLOWS):
                await refresh_flow_state()
                last_flow = time.time()
            if time.time() - last_beat > BEAT:
                beat(running=",".join(str(k) for k in busy) or None)
                last_beat = time.time()
            for job in sb("home_jobs?status=eq.queued&order=id.asc&limit=20") or []:
                ln = lane(job)
                if ln in busy:
                    continue
                # claim it only if it is still queued (another agent instance cannot take it twice)
                got = sb(f"home_jobs?id=eq.{job['id']}&status=eq.queued", "PATCH",
                         {"status": "running", "started_at": now()}, prefer="return=representation")
                if got:
                    busy[ln] = asyncio.create_task(run(got[0], ln))
            if RESTART and not busy:
                log("updated, restarting")
                for fl in FLOWS.values():
                    await fl.close()
                os._exit(3)                     # the window starts the new version after 5 seconds
        except Exception as e:  # noqa: BLE001
            log("loop error:", e)
        await asyncio.sleep(5 if busy else POLL)


if __name__ == "__main__":
    asyncio.run(main())

