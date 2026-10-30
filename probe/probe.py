#!/usr/bin/env python3
"""国内探针：部署在中国大陆的机器上（任意一台国内 VPS / 家用机），
供管理端判断 GCP 实例 IP 在国内是否可达。仅依赖 Python 3 标准库。

用法：
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
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("PROBE_TOKEN", "")
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


def main():
    if len(TOKEN) < 16:
        sys.exit("请设置 PROBE_TOKEN 环境变量（至少 16 位随机字符）")
    httpd = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"gcpvm probe listening on {BIND}:{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
