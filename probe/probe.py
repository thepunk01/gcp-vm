#!/usr/bin/env python3
"""国内探针：部署在中国大陆的机器上（任意一台国内 VPS / 家用机），
供管理端判断 GCP 实例 IP 在国内是否可达。仅依赖 Python 3 标准库。

两种模式：

1. 主动上报模式（推荐，探针所在设备不需要公网 IP，如家用路由器 / NAS）：
    PROBE_PANEL=http://面板地址:8080 PROBE_TOKEN=面板里生成的令牌 python3 probe.py
   探针每隔约 20 秒主动连接面板领取检测任务并上报结果。

2. 监听模式（探针有公网 IP，由面板来调用）：
    PROBE_TOKEN=换成一个长随机串 python3 probe.py            # 默认监听 0.0.0.0:9800
    PROBE_TOKEN=xxx PROBE_PORT=9800 PROBE_BIND=0.0.0.0 python3 probe.py

接口：
    POST /probe   Authorization: Bearer <token>
        {"host": "1.2.3.4", "port": 22, "timeout": 5, "tries": 3}
    ->  {"ok": true, "latency_ms": 38.2, "error": ""}
    GET  /health  -> {"ok": true}
"""

import hmac
import ipaddress
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

try:  # 主动上报模式用不到 http.server，精简版 Python 缺少时也能运行
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
except ImportError:  # pragma: no cover
    BaseHTTPRequestHandler, ThreadingHTTPServer = object, None

TOKEN = os.environ.get("PROBE_TOKEN", "")
PANEL = os.environ.get("PROBE_PANEL", "").strip().rstrip("/")
VERSION = "1.1"
BIND = os.environ.get("PROBE_BIND", "0.0.0.0")
PORT = int(os.environ.get("PROBE_PORT", "9800"))
# 默认禁止探测内网/保留地址，防止探针被滥用为内网扫描器
ALLOW_PRIVATE = os.environ.get("PROBE_ALLOW_PRIVATE", "0") == "1"


def tcp_probe(host, port, timeout, tries):
    last_err = ""
    for i in range(tries):
        start = time.monotonic()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True, round((time.monotonic() - start) * 1000, 1), ""
        except OSError as e:
            last_err = str(e) or e.__class__.__name__
        if i + 1 < tries:
            time.sleep(0.5)
    return False, None, last_err


def target_allowed(host):
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # 只接受 IP，避免 DNS 污染干扰判断
    if ALLOW_PRIVATE:
        return True
    return ip.is_global


class Handler(BaseHTTPRequestHandler):
    server_version = "gcpvm-probe/1.0"

    def _send(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/probe":
            return self._send(404, {"error": "not found"})
        auth = self.headers.get("Authorization", "")
        given = auth[7:] if auth.startswith("Bearer ") else ""
        if not hmac.compare_digest(given.encode(), TOKEN.encode()):
            return self._send(401, {"error": "unauthorized"})
        try:
            length = min(int(self.headers.get("Content-Length", 0)), 4096)
            req = json.loads(self.rfile.read(length) or b"{}")
            host = str(req["host"]).strip()
            port = int(req.get("port", 22))
            timeout = min(max(float(req.get("timeout", 5)), 1), 15)
            tries = min(max(int(req.get("tries", 3)), 1), 5)
            if not (1 <= port <= 65535):
                raise ValueError("bad port")
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            return self._send(400, {"error": "bad request"})
        if not target_allowed(host):
            return self._send(403, {"error": "target not allowed"})
        ok, latency, err = tcp_probe(host, port, timeout, tries)
        self._send(200, {"ok": ok, "latency_ms": latency, "error": err})

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


# ---------------- 主动上报模式 ----------------
def _panel_request(path, body=None, timeout=20):
    req = urllib.request.Request(
        PANEL + path,
        data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": f"gcpvm-probe/{VERSION}",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def probe_targets(targets, timeout, tries, max_workers=16):
    """并发探测目标列表，返回上报用的结果列表。"""
    results = [None] * len(targets)

    def work(i, t):
        host, port = str(t.get("host", "")), int(t.get("port", 22))
        if not target_allowed(host) or not (1 <= port <= 65535):
            results[i] = {"host": host, "port": port, "ok": None, "error": "target not allowed"}
            return
        ok, latency, err = tcp_probe(host, port, timeout, tries)
        results[i] = {"host": host, "port": port, "ok": ok, "latency_ms": latency, "error": err}

    pending = list(enumerate(targets))
    while pending:
        batch, pending = pending[:max_workers], pending[max_workers:]
        threads = [threading.Thread(target=work, args=it, daemon=True) for it in batch]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
    return [r for r in results if r]


def push_once():
    task = _panel_request("/api/probe-agent/tasks")
    targets = [t for t in task.get("targets") or [] if isinstance(t, dict)][:200]
    timeout = min(max(float(task.get("timeout", 5)), 1), 15)
    tries = min(max(int(task.get("tries", 3)), 1), 5)
    results = probe_targets(targets, timeout, tries)
    if results:
        _panel_request("/api/probe-agent/report", {"results": results})
    interval = min(max(int(task.get("interval", 20)), 5), 300)
    return len(results), interval


def push_loop():
    print(f"gcpvm probe (push mode) -> {PANEL}", flush=True)
    connected = False
    while True:
        try:
            n, interval = push_once()
            if not connected:
                print(f"gcpvm: 已连接面板，本轮检测 {n} 个目标", flush=True)
                connected = True
        except urllib.error.HTTPError as e:
            msg = "令牌无效，请核对面板「检测设置」里的令牌" if e.code == 401 else f"HTTP {e.code}"
            print(f"gcpvm: 连接面板失败：{msg}", file=sys.stderr, flush=True)
            connected, interval = False, 60
        except (OSError, ValueError) as e:
            print(f"gcpvm: 连接面板失败：{e}", file=sys.stderr, flush=True)
            connected, interval = False, 30
        time.sleep(interval)


def main():
    if len(TOKEN) < 16:
        sys.exit("请设置 PROBE_TOKEN 环境变量（至少 16 位随机字符）")
    if PANEL:
        if not PANEL.startswith(("http://", "https://")):
            sys.exit("PROBE_PANEL 需要以 http:// 或 https:// 开头")
        push_loop()
        return
    if ThreadingHTTPServer is None:
        sys.exit("当前 Python 缺少 http.server，请改用主动上报模式（设置 PROBE_PANEL）")
    httpd = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"gcpvm probe listening on {BIND}:{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
