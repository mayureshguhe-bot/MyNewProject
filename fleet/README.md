# Distributed Linux Fleet Management (SWFW_5S_12)

A web-based Fleet Manager that onboards Linux nodes, tracks their availability, collects metrics with
Prometheus, visualises them in Grafana and runs allow-listed remote commands.

## Architecture

```
                         +--------------------- Manager EC2 (docker compose) ---------------------+
 browser --:8000-------> | fleet-manager (FastAPI + SQLite)                                        |
 browser --:3000-------> | grafana  --queries--> prometheus --alerts--> alertmanager --webhook-+   |
                         |                          ^   ^                                      |   |
                         |   writes targets/nodes.json  | file_sd (10s)       POST /api/alerts/webhook
                         |   (fleet-manager) -----------+                                      v   |
                         +--------------------------^-------------------------------------------+
                                                    | scrape :9100 (pull)
        heartbeat / job poll / result (HTTP, agent -> manager, pull)
                                                    |
                         +--------- Node EC2 (x N) -+----------+
                         | node_exporter :9100                 |
                         | fleet-agent (systemd, user fleet-agent, no shell)
                         +-------------------------------------+
```

Key ideas
- **Pull everywhere.** Agents call the manager; Prometheus scrapes the nodes. The manager never connects
  into a node, so nodes need no inbound SSH or agent port (only :9100 for Prometheus).
- **Onboarding.** UI generates a single-use, 1-hour token and a one-line installer. The installer sets up
  node_exporter and the agent as systemd services. The agent registers, the manager stores the node and
  rewrites `prometheus/targets/nodes.json`; Prometheus picks it up through `file_sd`. No config edits.
- **Availability / graceful degradation.** Heartbeat every 10 s. No heartbeat for 30 s = `DEGRADED`,
  60 s = `OFFLINE`; the next heartbeat restores `ONLINE`. While OFFLINE the node keeps its last-known data
  in the UI, new commands are rejected (HTTP 409), and queued commands are failed instead of piling up.
  Queued commands also expire after 120 s. Prometheus alerts (`FleetNodeDegraded`, `FleetNodeOffline`,
  `NodeExporterDown`) cross-check from the metrics side and show up in the UI via Alertmanager.
- **Controlled remote execution.** The manager sends a command *name* plus an optional validated argument
  (`^[A-Za-z0-9@._-]{1,64}$`). The agent maps the name to a fixed argv from its own allow-list (defence in
  depth), runs it without a shell, with a 15 s timeout and 16 KiB output cap, as an unprivileged user under
  systemd hardening (`NoNewPrivileges`, `ProtectSystem=strict`). Every request, rejection and result is
  written to the audit log.

Allow-list: `uptime`, `df`, `free`, `uname`, `ip_addr`, `listening`, `top_cpu`, `top_mem`, `loadavg`,
`systemctl_status <unit>`, `journal <unit>`.

## Deploy on AWS (3 x Ubuntu 22.04/24.04, t3.small, same VPC)

Security groups
| Group | Inbound |
|---|---|
| manager-sg | 22, 3000, 9090, 9093 from **your IP**; 8000 from your IP **and node-sg** |
| node-sg | 22 from your IP; **9100 from manager-sg** |

If you also install the agent on the manager host, add 9100 from manager-sg to manager-sg itself.

1. **Manager instance**
   ```bash
   curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker $USER && newgrp docker
   # copy this folder to the instance (scp -r fleet ubuntu@<manager-public-ip>:~/) then:
   cd fleet
   cat > .env <<EOF
   ADMIN_KEY=choose-a-strong-key
   PUBLIC_URL=http://<MANAGER-PRIVATE-IP>:8000
   GRAFANA_PASSWORD=choose-another
   EOF
   docker compose up -d --build
   docker compose ps
   ```
   `PUBLIC_URL` must be the manager's **private** IP: it is baked into install commands and nodes use it.
2. Open `http://<manager-public-ip>:8000`, enter the admin key in the header box.
3. **Add a node:** click "Generate install command", paste it on each node instance (needs sudo, python3,
   internet access for the node_exporter download). The node appears within seconds.
4. Grafana: `http://<manager-public-ip>:3000` (admin / your GRAFANA_PASSWORD), dashboard **Fleet > Fleet Overview**.

## Demo script (about 5 minutes)

1. UI shows 2 nodes ONLINE; Grafana shows CPU, memory, disk, network per host.
2. Run `uptime`, `df`, then `systemctl_status ssh` from the UI; expand the output.
3. Try `systemctl_status` with argument `ssh; reboot`: rejected (400) and visible in the audit log.
4. On a node: `sudo systemctl stop fleet-agent`.
   - after ~30 s the node turns **DEGRADED** (UI + `FleetNodeDegraded` alert);
   - after ~60 s it is **OFFLINE**; running a command is refused with 409; Grafana still shows metrics
     (node_exporter is up), proving agent failure and metrics failure are independent signals.
5. `sudo systemctl start fleet-agent`: node returns to ONLINE on the next heartbeat.
6. `sudo systemctl stop node_exporter`: `NodeExporterDown` fires after 1 min; Grafana "Scrape up" drops to 0.
7. Load test: `sudo apt install -y stress-ng && stress-ng --cpu 2 --timeout 400s` and watch CPU in Grafana.

## Files

| Path | Purpose |
|---|---|
| `manager/app/main.py` | API, state machine, command queue, audit, file_sd writer, `/metrics` |
| `manager/app/agent.py` | node agent (stdlib only), served to nodes at `/agent/agent.py` |
| `manager/app/install.sh.tpl` | node installer served at `/install.sh?token=...` |
| `manager/app/static/index.html` | minimal admin UI |
| `prometheus/prometheus.yml`, `alert.rules.yml` | file_sd scrape config and alert rules |
| `alertmanager/alertmanager.yml` | routes alerts to the manager webhook |
| `grafana/` | provisioned datasource and Fleet Overview dashboard |

## Known limitations (good to state in the report)
- Single manager with SQLite; no HA. Plain HTTP and one shared admin key: put it behind TLS/an ALB and real
  auth for production. The agent key is stored hashed on the manager, and the agent state file is mode 0600.
- A node removed from the UI keeps its agent running but is rejected (401); re-onboard with a fresh token.
- Metrics history lives in Prometheus (7-day retention); the manager stores only state, commands and audit.
