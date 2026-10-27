#!/usr/bin/env python3
"""Fleet agent: runs on every managed Linux node. Standard library only.

* registers once with a one-time token, then stores node id + key in the state dir
* sends a heartbeat (with a few /proc stats) every HEARTBEAT seconds
* polls the manager for jobs; executes ONLY commands from the local allow-list below
  (the manager sends a command *name* and an optional validated argument, never an argv)
* never uses a shell; every command has a timeout and an output cap
"""
import json
import os
import platform
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG = Path(os.getenv("FLEET_CONFIG", "/etc/fleet-agent/config.json"))
STATE_DIR = Path(os.getenv("FLEET_STATE_DIR", "/var/lib/fleet-agent"))
HEARTBEAT = 10
JOB_POLL = 3

SERVICE_RE = re.compile(r"^[A-Za-z0-9@._-]{1,64}$")
ALLOWED = {  # name -> (argv template, takes_arg)
    "uptime": (["uptime"], False),
    "df": (["df", "-h"], False),
    "free": (["free", "-m"], False),
    "uname": (["uname", "-a"], False),
    "ip_addr": (["ip", "-br", "addr"], False),
    "listening": (["ss", "-tuln"], False),
    "top_cpu": (["ps", "aux", "--sort=-%cpu"], False),
    "top_mem": (["ps", "aux", "--sort=-%mem"], False),
    "loadavg": (["cat", "/proc/loadavg"], False),
    "systemctl_status": (["systemctl", "status", "{arg}", "--no-pager"], True),
    "journal": (["journalctl", "-u", "{arg}", "-n", "50", "--no-pager"], True),
}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class Api:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.headers = {"Content-Type": "application/json"}

    def call(self, method, path, body=None, timeout=10):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=self.headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")


def local_address(manager_url):
    """The IP this host uses to reach the manager (what Prometheus should scrape)."""
    u = urllib.parse.urlparse(manager_url)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((u.hostname, u.port or 80))  # UDP connect sends nothing; just picks a route
        return s.getsockname()[0]
    finally:
        s.close()


def os_name():
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip('"')
    except OSError:
        pass
    return platform.system()


def read_stats():
    """Tiny health summary straight from /proc."""
    out = {}
    try:
        out["load1"] = float(Path("/proc/loadavg").read_text().split()[0])
        mi = {l.split(":")[0]: int(l.split()[1]) for l in Path("/proc/meminfo").read_text().splitlines()}
        out["mem_avail_pct"] = round(100.0 * mi["MemAvailable"] / mi["MemTotal"], 1)
        out["uptime_s"] = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        pass
    return out


def register(api, cfg):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_file = STATE_DIR / "state.json"
    if state_file.exists():
        return json.loads(state_file.read_text())
    body = {"token": cfg["token"], "hostname": socket.gethostname(),
            "address": cfg.get("address") or local_address(cfg["manager_url"]),
            "os": os_name(), "kernel": platform.release()}
    while True:
        try:
            st = api.call("POST", "/api/agent/register", body)
            state_file.write_text(json.dumps(st))
            os.chmod(state_file, 0o600)
            log("registered as", st["node_id"])
            return st
        except urllib.error.HTTPError as e:
            if e.code == 403:
                log("registration refused (token invalid/used/expired); exiting")
                sys.exit(2)
            log("register HTTP error", e.code)
        except Exception as e:
            log("register failed, retrying:", e)
        time.sleep(5)


def heartbeat_loop(api):
    fails = 0
    while True:
        try:
            api.call("POST", "/api/agent/heartbeat", read_stats())
            fails = 0
        except urllib.error.HTTPError as e:
            log("heartbeat rejected:", e.code, "(node removed from manager?)")
            fails += 1
        except Exception as e:
            fails += 1
            if fails in (1, 5) or fails % 30 == 0:
                log("manager unreachable (will keep retrying):", e)
        time.sleep(HEARTBEAT)


def run_job(job):
    spec = ALLOWED.get(job["cmd"])
    if not spec:
        return 126, f"refused: '{job['cmd']}' is not in the agent allow-list"
    argv_t, takes_arg = spec
    arg = job.get("arg") or ""
    if takes_arg and not SERVICE_RE.match(arg):
        return 126, "refused: invalid argument"
    if not takes_arg and arg:
        return 126, "refused: command takes no argument"
    argv = [a.replace("{arg}", arg) for a in argv_t]
    timeout = min(int(job.get("timeout", 15)), 30)
    cap = min(int(job.get("output_cap", 16384)), 65536)
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "SYSTEMD_COLORS": "0"})
        out = (p.stdout + p.stderr)
        code = p.returncode
    except subprocess.TimeoutExpired:
        out, code = f"timed out after {timeout}s", 124
    except FileNotFoundError:
        out, code = f"{argv[0]}: not installed on this node", 127
    if len(out) > cap:
        out = out[:cap] + "\n...[output truncated]"
    return code, out


def job_loop(api):
    while True:
        try:
            job = api.call("GET", "/api/agent/jobs").get("job")
            if job:
                log("running job", job["id"], job["cmd"], job.get("arg", ""))
                code, out = run_job(job)
                api.call("POST", f"/api/agent/jobs/{job['id']}/result", {"exit_code": code, "output": out})
                continue  # check for another job immediately
        except Exception as e:
            log("job poll error:", e)
            time.sleep(JOB_POLL * 2)
        time.sleep(JOB_POLL)


def main():
    cfg = json.loads(CONFIG.read_text())
    api = Api(cfg["manager_url"])
    st = register(api, cfg)
    api.headers.update({"X-Node-Id": st["node_id"], "X-Agent-Key": st["agent_key"]})
    threading.Thread(target=heartbeat_loop, args=(api,), daemon=True).start()
    log("agent running; manager =", cfg["manager_url"])
    job_loop(api)


if __name__ == "__main__":
    main()
