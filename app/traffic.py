"""实例实时流量：VM 上报脚本、速率计算、按组汇总。

每台实例上运行一个很小的 bash 脚本（systemd 服务），每 5 秒把默认出口网卡的
累计收发字节数上报到面板；面板用相邻两次的差值计算速率（bit/s）。
"""

from __future__ import annotations

import re
import threading
import time

REPORT_INTERVAL = 5
ONLINE_SECONDS = 20
URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-]+(:\d{1,5})?$|^https?://\[[0-9A-Fa-f:]+\](:\d{1,5})?$")
TOKEN_RE = re.compile(r"^[0-9a-f]{32,64}$")

AGENT_LOOP = r'''#!/bin/bash
# gcpvm 流量上报：每 __INTERVAL__ 秒上报默认出口网卡的累计收发字节数
URL="__URL__/api/traffic-agent/report"
TOKEN="__TOKEN__"
while true; do
  IF=$(ip -o route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')
  [ -n "$IF" ] || IF=$(awk -F: 'NR>2 {gsub(/ /,"",$1); if ($1!="lo") {print $1; exit}}' /proc/net/dev)
  set -- $(sed 's/:/ /' /proc/net/dev | awk -v i="$IF" '$1==i {print $2, $10}')
  if [ -n "$1" ] && [ -n "$2" ]; then
    curl -fsS -m 4 --connect-timeout 3 -o /dev/null -X POST "$URL" \
      -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
      -d "{\"rx\":$1,\"tx\":$2,\"iface\":\"$IF\"}" || true
  fi
  sleep __INTERVAL__
done
'''

INSTALL = r'''set -e
command -v curl >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq curl; } 2>/dev/null || yum install -y -q curl
cat > /usr/local/bin/gcpvm-traffic.sh <<'GCPVM_EOF'
__LOOP__GCPVM_EOF
chmod 755 /usr/local/bin/gcpvm-traffic.sh
cat > /etc/systemd/system/gcpvm-traffic.service <<'GCPVM_EOF'
[Unit]
Description=GCP-VM traffic reporter
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/local/bin/gcpvm-traffic.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
GCPVM_EOF
systemctl daemon-reload
systemctl enable gcpvm-traffic >/dev/null 2>&1
systemctl restart gcpvm-traffic
sleep 2
if systemctl is-active --quiet gcpvm-traffic; then
  echo "gcpvm: 流量上报已启动，上报到 __URL__"
else
  echo "gcpvm: 流量上报启动失败"; systemctl status gcpvm-traffic --no-pager | tail -5; exit 1
fi
'''

UNINSTALL = '''systemctl disable --now gcpvm-traffic 2>/dev/null || true
rm -f /etc/systemd/system/gcpvm-traffic.service /usr/local/bin/gcpvm-traffic.sh
systemctl daemon-reload
echo "gcpvm: 已卸载流量上报"
'''


def normalize_panel_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not URL_RE.match(url):
        raise ValueError("面板地址格式不正确，应为 http(s)://域名或IP[:端口]，不带路径")
    return url


def agent_install_script(panel_url: str, token: str) -> str:
    url = normalize_panel_url(panel_url)
    if not TOKEN_RE.match(token):
        raise ValueError("上报令牌格式不正确")
    loop = AGENT_LOOP.replace("__URL__", url).replace("__TOKEN__", token).replace(
        "__INTERVAL__", str(REPORT_INTERVAL)
    )
    return INSTALL.replace("__LOOP__", loop).replace("__URL__", url)


class TrafficHub:
    """保存每台实例最近一次上报与速率（内存中，面板重启后几秒内恢复）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._samples: dict[int, tuple[float, int, int]] = {}
        self._rates: dict[int, tuple[float, float]] = {}
        self._seen: dict[int, float] = {}

    def report(self, instance_id: int, rx: int, tx: int, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            prev = self._samples.get(instance_id)
            self._samples[instance_id] = (now, rx, tx)
            self._seen[instance_id] = now
            if not prev:
                return
            dt = now - prev[0]
            if dt < 0.5 or dt > 60 or rx < prev[1] or tx < prev[2]:
                return  # 间隔异常或计数器重置（重启），丢弃这一次
            self._rates[instance_id] = ((rx - prev[1]) * 8 / dt, (tx - prev[2]) * 8 / dt)

    def instance_rate(self, instance_id: int, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        seen = self._seen.get(instance_id, 0.0)
        online = now - seen < ONLINE_SECONDS
        rx, tx = self._rates.get(instance_id, (0.0, 0.0)) if online else (0.0, 0.0)
        return {"online": online, "rx_bps": round(rx), "tx_bps": round(tx), "last_seen": seen}

    def all_rates(self, instances: list[dict], now: float | None = None) -> dict[int, dict]:
        return {i["id"]: self.instance_rate(i["id"], now) for i in instances}

    def groups(self, instances: list[dict], now: float | None = None) -> list[dict]:
        """按 grp 汇总，只包含设置了分组的实例，不含任何 IP / 实例名。"""
        out: dict[str, dict] = {}
        for inst in instances:
            name = (inst.get("grp") or "").strip()
            if not name:
                continue
            g = out.setdefault(name, {"name": name, "total": 0, "online": 0, "rx_bps": 0, "tx_bps": 0})
            g["total"] += 1
            r = self.instance_rate(inst["id"], now)
            if r["online"]:
                g["online"] += 1
                g["rx_bps"] += r["rx_bps"]
                g["tx_bps"] += r["tx_bps"]
        return sorted(out.values(), key=lambda g: g["name"])
