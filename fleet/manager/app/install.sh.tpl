#!/usr/bin/env bash
# Fleet node installer. Served by the manager with a one-time token baked in.
# Installs: node_exporter (metrics on :__EXPORTER_PORT__) + fleet-agent (heartbeat / command runner).
set -euo pipefail

MANAGER_URL="__MANAGER_URL__"
TOKEN="__TOKEN__"
NODE_EXPORTER_VERSION="1.8.2"

[ "$(id -u)" -eq 0 ] || { echo "run as root (use sudo)"; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
command -v systemctl >/dev/null || { echo "systemd is required"; exit 1; }

case "$(uname -m)" in
  x86_64)  ARCH=amd64 ;;
  aarch64) ARCH=arm64 ;;
  *) echo "unsupported architecture $(uname -m)"; exit 1 ;;
esac

echo "==> node_exporter ${NODE_EXPORTER_VERSION}"
id node_exporter &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin node_exporter
if [ ! -x /usr/local/bin/node_exporter ]; then
  TMP="$(mktemp -d)"
  curl -fsSL "https://github.com/prometheus/node_exporter/releases/download/v${NODE_EXPORTER_VERSION}/node_exporter-${NODE_EXPORTER_VERSION}.linux-${ARCH}.tar.gz" | tar -xz -C "$TMP"
  install -m 0755 "$TMP"/node_exporter-*/node_exporter /usr/local/bin/node_exporter
  rm -rf "$TMP"
fi
cat > /etc/systemd/system/node_exporter.service <<EOF
[Unit]
Description=Prometheus node_exporter
After=network-online.target
Wants=network-online.target

[Service]
User=node_exporter
ExecStart=/usr/local/bin/node_exporter --web.listen-address=:__EXPORTER_PORT__
Restart=always
NoNewPrivileges=true
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF

echo "==> fleet-agent"
id fleet-agent &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin fleet-agent
getent group systemd-journal >/dev/null && usermod -aG systemd-journal fleet-agent || true
install -d -m 0755 /opt/fleet-agent
install -d -m 0750 -o root -g fleet-agent /etc/fleet-agent
curl -fsSL "${MANAGER_URL}/agent/agent.py" -o /opt/fleet-agent/agent.py
chmod 0644 /opt/fleet-agent/agent.py
cat > /etc/fleet-agent/config.json <<EOF
{"manager_url": "${MANAGER_URL}", "token": "${TOKEN}"}
EOF
chown root:fleet-agent /etc/fleet-agent/config.json
chmod 0640 /etc/fleet-agent/config.json
rm -rf /var/lib/fleet-agent   # fresh registration with the new token

cat > /etc/systemd/system/fleet-agent.service <<EOF
[Unit]
Description=Fleet agent
After=network-online.target
Wants=network-online.target

[Service]
User=fleet-agent
Group=fleet-agent
ExecStart=/usr/bin/python3 /opt/fleet-agent/agent.py
Restart=always
RestartSec=5
StateDirectory=fleet-agent
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now node_exporter fleet-agent
sleep 2
systemctl --no-pager --lines=3 status fleet-agent node_exporter || true
echo "==> done. This node should appear in the Fleet Manager UI within a few seconds."
