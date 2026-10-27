"""Fleet Manager: node onboarding, heartbeat-based availability, controlled remote commands.

Design notes
------------
* Pull model: agents call the manager. The manager never opens a connection to a node,
  so nodes need no inbound SSH or agent port (only node_exporter :9100 for Prometheus).
* Availability is a state machine driven by heartbeat age:
      ONLINE -> DEGRADED (no heartbeat for DEGRADED_AFTER s) -> OFFLINE (OFFLINE_AFTER s)
  and it recovers automatically on the next heartbeat.
* Prometheus discovers nodes through file_sd: this service rewrites targets/nodes.json
  whenever the fleet changes, so prometheus.yml never needs editing.
* Remote commands come from a fixed allow-list. The manager validates, the agent validates
  again (defence in depth). Nothing is ever executed through a shell.
"""
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

# ----------------------------------------------------------------- config
DB_PATH = os.getenv("DB_PATH", "/data/fleet.db")
TARGETS_FILE = os.getenv("TARGETS_FILE", "/targets/nodes.json")
ADMIN_KEY = os.getenv("ADMIN_KEY", "changeme")
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
DEGRADED_AFTER = int(os.getenv("DEGRADED_AFTER", "30"))
OFFLINE_AFTER = int(os.getenv("OFFLINE_AFTER", "60"))
TOKEN_TTL = int(os.getenv("TOKEN_TTL", "3600"))
COMMAND_TTL = int(os.getenv("COMMAND_TTL", "120"))  # queued commands expire after this
EXPORTER_PORT = int(os.getenv("EXPORTER_PORT", "9100"))
BASE = Path(__file__).parent

# ------------------------------------------------------- command allow-list
SERVICE_RE = re.compile(r"^[A-Za-z0-9@._-]{1,64}$")

# key -> (argv template, needs_arg, description). {arg} is validated before substitution.
COMMANDS = {
    "uptime": (["uptime"], False, "System uptime and load"),
    "df": (["df", "-h"], False, "Disk usage"),
    "free": (["free", "-m"], False, "Memory usage (MiB)"),
    "uname": (["uname", "-a"], False, "Kernel and host info"),
    "ip_addr": (["ip", "-br", "addr"], False, "Network interfaces"),
    "listening": (["ss", "-tuln"], False, "Listening sockets"),
    "top_cpu": (["ps", "aux", "--sort=-%cpu"], False, "Processes by CPU"),
    "top_mem": (["ps", "aux", "--sort=-%mem"], False, "Processes by memory"),
    "loadavg": (["cat", "/proc/loadavg"], False, "Raw /proc/loadavg"),
    "systemctl_status": (["systemctl", "status", "{arg}", "--no-pager"], True, "Status of a systemd unit"),
    "journal": (["journalctl", "-u", "{arg}", "-n", "50", "--no-pager"], True, "Last 50 log lines of a unit"),
}
CMD_TIMEOUT = 15
OUTPUT_CAP = 16 * 1024

# ------------------------------------------------------------- database
if DB_PATH != ":memory:":
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.row_factory = sqlite3.Row
_lock = threading.RLock()


def init_db():
    with _lock:
        _db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS tokens(
                token TEXT PRIMARY KEY, label TEXT, created REAL, expires REAL, used_by TEXT);
            CREATE TABLE IF NOT EXISTS nodes(
                id TEXT PRIMARY KEY, hostname TEXT, address TEXT, os TEXT, kernel TEXT,
                key_hash TEXT, registered REAL, last_seen REAL, state TEXT, state_since REAL,
                load1 REAL, mem_avail_pct REAL, uptime_s REAL);
            CREATE TABLE IF NOT EXISTS commands(
                id INTEGER PRIMARY KEY AUTOINCREMENT, node_id TEXT, cmd TEXT, arg TEXT,
                status TEXT, created REAL, started REAL, finished REAL,
                exit_code INTEGER, output TEXT, requested_by TEXT);
            CREATE TABLE IF NOT EXISTS audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, actor TEXT, event TEXT,
                node_id TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS alerts(
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, status TEXT, name TEXT,
                severity TEXT, instance TEXT, summary TEXT);
            """
        )
        _db.commit()


def q(sql, args=(), one=False):
    with _lock:
        cur = _db.execute(sql, args)
        rows = cur.fetchall()
        _db.commit()
    return (rows[0] if rows else None) if one else rows


def insert(sql, args=()):
    """Run an INSERT and return the new row id."""
    with _lock:
        cur = _db.execute(sql, args)
        _db.commit()
        return cur.lastrowid


def audit(actor, event, node_id=None, detail=""):
    q("INSERT INTO audit(ts,actor,event,node_id,detail) VALUES(?,?,?,?,?)",
      (time.time(), actor, event, node_id, detail[:500]))


# ------------------------------------------------------ targets (file_sd)
_targets_lock = threading.Lock()


def write_targets():
    """Atomically rewrite the Prometheus file_sd file from the node registry."""
    rows = q("SELECT id,hostname,address FROM nodes ORDER BY hostname")
    doc = [
        {"targets": [f"{r['address']}:{EXPORTER_PORT}"],
         "labels": {"node_id": r["id"], "hostname": r["hostname"]}}
        for r in rows
    ]
    with _targets_lock:
        Path(TARGETS_FILE).parent.mkdir(parents=True, exist_ok=True)
        tmp = TARGETS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(doc, f, indent=2)
        os.replace(tmp, TARGETS_FILE)


# --------------------------------------------------------- state machine
def compute_state(last_seen: float, now: float) -> str:
    age = now - last_seen
    if age >= OFFLINE_AFTER:
        return "OFFLINE"
    if age >= DEGRADED_AFTER:
        return "DEGRADED"
    return "ONLINE"


def monitor_loop():
    while True:
        try:
            now = time.time()
            for n in q("SELECT id,last_seen,state FROM nodes"):
                new = compute_state(n["last_seen"], now)
                if new != n["state"]:
                    q("UPDATE nodes SET state=?, state_since=? WHERE id=?", (new, now, n["id"]))
                    audit("system", "state_change", n["id"], f"{n['state']} -> {new}")
            # graceful degradation of the command queue
            q("UPDATE commands SET status='expired', finished=? WHERE status='queued' AND created < ?",
              (now, now - COMMAND_TTL))
            q("UPDATE commands SET status='failed', finished=?, output='agent lost while running' "
              "WHERE status='running' AND started < ?", (now, now - CMD_TIMEOUT - 30))
            # commands queued for nodes that went OFFLINE are failed straight away
            q("UPDATE commands SET status='failed', finished=?, output='node offline' "
              "WHERE status='queued' AND node_id IN (SELECT id FROM nodes WHERE state='OFFLINE')", (now,))
        except Exception as e:  # never let the monitor die
            print("monitor error:", e, flush=True)
        time.sleep(5)


# ------------------------------------------------------------------ app
app = FastAPI(title="Fleet Manager")


@app.on_event("startup")
def startup():
    init_db()
    write_targets()
    threading.Thread(target=monitor_loop, daemon=True).start()


def admin(x_admin_key: str = Header(default="")):
    if not secrets.compare_digest(x_admin_key, ADMIN_KEY):
        raise HTTPException(401, "bad admin key")
    return "admin"


def agent_auth(x_node_id: str = Header(default=""), x_agent_key: str = Header(default="")):
    n = q("SELECT * FROM nodes WHERE id=?", (x_node_id,), one=True)
    if not n or not secrets.compare_digest(n["key_hash"], hashlib.sha256(x_agent_key.encode()).hexdigest()):
        raise HTTPException(401, "bad agent credentials")
    return n


def base_url(request: Request) -> str:
    return PUBLIC_URL or str(request.base_url).rstrip("/")


# ---------------------------------------------------------- onboarding
class TokenReq(BaseModel):
    label: str = ""


@app.post("/api/tokens")
def create_token(req: TokenReq, request: Request, who=Depends(admin)):
    token = secrets.token_urlsafe(18)
    now = time.time()
    q("INSERT INTO tokens(token,label,created,expires) VALUES(?,?,?,?)",
      (token, req.label[:60], now, now + TOKEN_TTL))
    audit(who, "token_created", None, req.label)
    url = base_url(request)
    return {"token": token, "expires_in": TOKEN_TTL,
            "install_command": f"curl -fsSL '{url}/install.sh?token={token}' | sudo bash"}


class RegisterReq(BaseModel):
    token: str
    hostname: str
    address: str = ""
    os: str = ""
    kernel: str = ""


@app.post("/api/agent/register")
def register(req: RegisterReq, request: Request):
    now = time.time()
    t = q("SELECT * FROM tokens WHERE token=?", (req.token,), one=True)
    if not t or t["used_by"] or t["expires"] < now:
        audit("agent", "register_rejected", None, f"{req.hostname}: invalid/used/expired token")
        raise HTTPException(403, "invalid, used or expired token")
    node_id = "n-" + secrets.token_hex(4)
    key = secrets.token_urlsafe(32)
    host = re.sub(r"[^A-Za-z0-9_.-]", "-", req.hostname)[:63] or node_id
    addr = req.address or request.client.host
    q("INSERT INTO nodes(id,hostname,address,os,kernel,key_hash,registered,last_seen,state,state_since)"
      " VALUES(?,?,?,?,?,?,?,?,?,?)",
      (node_id, host, addr, req.os[:80], req.kernel[:80],
       hashlib.sha256(key.encode()).hexdigest(), now, now, "ONLINE", now))
    q("UPDATE tokens SET used_by=? WHERE token=?", (node_id, req.token))
    audit("agent", "node_registered", node_id, f"{host} {addr}")
    write_targets()
    return {"node_id": node_id, "agent_key": key, "heartbeat_interval": 10, "exporter_port": EXPORTER_PORT}


# ----------------------------------------------------------- agent API
class Heartbeat(BaseModel):
    load1: Optional[float] = None
    mem_avail_pct: Optional[float] = None
    uptime_s: Optional[float] = None


@app.post("/api/agent/heartbeat")
def heartbeat(hb: Heartbeat, node=Depends(agent_auth)):
    now = time.time()
    if node["state"] != "ONLINE":
        audit("system", "state_change", node["id"], f"{node['state']} -> ONLINE (heartbeat resumed)")
    q("UPDATE nodes SET last_seen=?, state='ONLINE', state_since=CASE WHEN state!='ONLINE' THEN ? ELSE state_since END,"
      " load1=?, mem_avail_pct=?, uptime_s=? WHERE id=?",
      (now, now, hb.load1, hb.mem_avail_pct, hb.uptime_s, node["id"]))
    return {"ok": True}


@app.get("/api/agent/jobs")
def poll_job(node=Depends(agent_auth)):
    """Agent long-polls here. Hands out at most one job and marks it running."""
    with _lock:
        row = q("SELECT * FROM commands WHERE node_id=? AND status='queued' ORDER BY id LIMIT 1",
                (node["id"],), one=True)
        if not row:
            return {"job": None}
        q("UPDATE commands SET status='running', started=? WHERE id=?", (time.time(), row["id"]))
    return {"job": {"id": row["id"], "cmd": row["cmd"], "arg": row["arg"],
                    "timeout": CMD_TIMEOUT, "output_cap": OUTPUT_CAP}}


class JobResult(BaseModel):
    exit_code: int
    output: str


@app.post("/api/agent/jobs/{job_id}/result")
def job_result(job_id: int, res: JobResult, node=Depends(agent_auth)):
    row = q("SELECT * FROM commands WHERE id=? AND node_id=?", (job_id, node["id"]), one=True)
    if not row or row["status"] != "running":
        raise HTTPException(404, "no such running job")
    status = "done" if res.exit_code == 0 else "failed"
    q("UPDATE commands SET status=?, finished=?, exit_code=?, output=? WHERE id=?",
      (status, time.time(), res.exit_code, res.output[:OUTPUT_CAP], job_id))
    audit("agent", "command_finished", node["id"], f"#{job_id} {row['cmd']} exit={res.exit_code}")
    return {"ok": True}


# ----------------------------------------------------------- admin API
@app.get("/api/nodes")
def list_nodes(_=Depends(admin)):
    now = time.time()
    out = []
    for n in q("SELECT * FROM nodes ORDER BY hostname"):
        d = {k: n[k] for k in n.keys() if k != "key_hash"}
        d["heartbeat_age"] = round(now - n["last_seen"], 1)
        out.append(d)
    return out


@app.delete("/api/nodes/{node_id}")
def delete_node(node_id: str, who=Depends(admin)):
    if not q("SELECT 1 FROM nodes WHERE id=?", (node_id,), one=True):
        raise HTTPException(404, "no such node")
    q("DELETE FROM nodes WHERE id=?", (node_id,))
    q("UPDATE commands SET status='failed', output='node removed' WHERE node_id=? AND status IN ('queued','running')", (node_id,))
    audit(who, "node_removed", node_id)
    write_targets()
    return {"ok": True}


@app.get("/api/commands/allowed")
def allowed(_=Depends(admin)):
    return [{"key": k, "needs_arg": v[1], "description": v[2], "argv": " ".join(v[0])}
            for k, v in COMMANDS.items()]


class CmdReq(BaseModel):
    cmd: str
    arg: str = ""


@app.post("/api/nodes/{node_id}/commands")
def run_command(node_id: str, req: CmdReq, who=Depends(admin)):
    node = q("SELECT * FROM nodes WHERE id=?", (node_id,), one=True)
    if not node:
        raise HTTPException(404, "no such node")
    if req.cmd not in COMMANDS:
        audit(who, "command_rejected", node_id, f"not in allow-list: {req.cmd}")
        raise HTTPException(400, f"command '{req.cmd}' is not in the allow-list")
    _, needs_arg, _ = COMMANDS[req.cmd]
    if needs_arg and not SERVICE_RE.match(req.arg):
        audit(who, "command_rejected", node_id, f"bad argument for {req.cmd}: {req.arg!r}")
        raise HTTPException(400, "invalid argument (allowed: letters, digits, @ . _ -)")
    if not needs_arg and req.arg:
        raise HTTPException(400, f"command '{req.cmd}' takes no argument")
    # graceful degradation: refuse work for a node we know is gone
    if node["state"] == "OFFLINE":
        audit(who, "command_rejected", node_id, f"node offline: {req.cmd}")
        raise HTTPException(409, "node is OFFLINE; command not queued")
    cid = insert("INSERT INTO commands(node_id,cmd,arg,status,created,requested_by) VALUES(?,?,?,?,?,?)",
                 (node_id, req.cmd, req.arg, "queued", time.time(), who))
    audit(who, "command_queued", node_id, f"#{cid} {req.cmd} {req.arg}".strip())
    return {"id": cid, "status": "queued",
            "note": "node is DEGRADED; command may wait or expire" if node["state"] == "DEGRADED" else ""}


@app.get("/api/commands")
def list_commands(limit: int = 30, _=Depends(admin)):
    return [dict(r) for r in q(
        "SELECT c.*, n.hostname FROM commands c LEFT JOIN nodes n ON n.id=c.node_id "
        "ORDER BY c.id DESC LIMIT ?", (min(limit, 200),))]


@app.get("/api/audit")
def list_audit(limit: int = 50, _=Depends(admin)):
    return [dict(r) for r in q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (min(limit, 500),))]


@app.get("/api/alerts")
def list_alerts(limit: int = 30, _=Depends(admin)):
    return [dict(r) for r in q("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (min(limit, 200),))]


@app.post("/api/alerts/webhook")
async def alert_webhook(request: Request):
    """Alertmanager webhook receiver (internal docker network only)."""
    body = await request.json()
    for a in body.get("alerts", []):
        lab, ann = a.get("labels", {}), a.get("annotations", {})
        q("INSERT INTO alerts(ts,status,name,severity,instance,summary) VALUES(?,?,?,?,?,?)",
          (time.time(), a.get("status", ""), lab.get("alertname", ""), lab.get("severity", ""),
           lab.get("hostname") or lab.get("instance", ""), ann.get("summary", "")))
    return {"ok": True}


# ------------------------------------------------------- install assets
@app.get("/install.sh", response_class=PlainTextResponse)
def install_sh(token: str, request: Request):
    tpl = (BASE / "install.sh.tpl").read_text()
    return tpl.replace("__MANAGER_URL__", base_url(request)).replace("__TOKEN__", re.sub(r"[^A-Za-z0-9_-]", "", token)) \
              .replace("__EXPORTER_PORT__", str(EXPORTER_PORT))


@app.get("/agent/agent.py", response_class=PlainTextResponse)
def agent_source():
    return (BASE / "agent.py").read_text()


# ------------------------------------------------- Prometheus /metrics
def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    now = time.time()
    lines = ["# HELP fleet_node_state_code 0=ONLINE 1=DEGRADED 2=OFFLINE", "# TYPE fleet_node_state_code gauge"]
    nodes = q("SELECT * FROM nodes")
    code = {"ONLINE": 0, "DEGRADED": 1, "OFFLINE": 2}
    for n in nodes:
        lab = f'node_id="{_esc(n["id"])}",hostname="{_esc(n["hostname"])}"'
        lines.append(f"fleet_node_state_code{{{lab}}} {code.get(n['state'], 2)}")
    lines += ["# HELP fleet_node_heartbeat_age_seconds Seconds since last heartbeat", "# TYPE fleet_node_heartbeat_age_seconds gauge"]
    for n in nodes:
        lab = f'node_id="{_esc(n["id"])}",hostname="{_esc(n["hostname"])}"'
        lines.append(f"fleet_node_heartbeat_age_seconds{{{lab}}} {now - n['last_seen']:.1f}")
    lines += ["# HELP fleet_nodes Number of nodes by state", "# TYPE fleet_nodes gauge"]
    for s in code:
        lines.append(f'fleet_nodes{{state="{s.lower()}"}} {sum(1 for n in nodes if n["state"] == s)}')
    lines += ["# HELP fleet_commands Commands by status", "# TYPE fleet_commands gauge"]
    for r in q("SELECT status, COUNT(*) c FROM commands GROUP BY status"):
        lines.append(f'fleet_commands{{status="{_esc(r["status"])}"}} {r["c"]}')
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------- UI
@app.get("/")
def ui():
    return FileResponse(BASE / "static" / "index.html")
