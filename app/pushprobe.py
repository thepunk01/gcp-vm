"""主动上报探针（push 模式）。

适用于没有公网 IP 的国内设备（家用路由器、NAS 等）：探针定时主动连接管理端，
领取需要检测的 IP:端口列表，在本地做 TCP 探测后把结果上报。管理端缓存最新结果，
检测时直接读取；结果过期或是新目标时登记为「待测」，等待探针下一轮上报。
"""

from __future__ import annotations

import hmac
import threading
import time
from typing import Callable

from .checker import ProbeResult

POLL_INTERVAL = 10  # 探针领取任务的间隔（秒），越短新 IP 验证越快
FRESH_SECONDS = 90  # 结果在此时间内视为有效
OFFLINE_SECONDS = 120  # 超过此时间未上报视为离线
# 新目标等待探针上报的最长时间：最坏情况 = 领取间隔 + 上一轮探测（被墙目标 5 秒 × 3 次）+ 本轮探测，留足余量
WAIT_SECONDS = 90
STARTUP_GRACE = 150  # 面板刚启动时探针还没重连，这段时间内不判离线，等它连上
PENDING_TTL = 120  # 临时目标保留时间
MAX_TARGETS = 200


class PushProbeHub:
    def __init__(self):
        self._cond = threading.Condition()
        self._results: dict[tuple[str, str, int], tuple[bool, float | None, str, float]] = {}
        self._last_seen: dict[str, float] = {}
        self._pending: dict[tuple[str, int], float] = {}
        self._started = time.time()
        self.settings_fn: Callable[[], dict] = lambda: {}
        self.targets_fn: Callable[[], list[tuple[str, int]]] = lambda: []

    def configure(self, settings_fn: Callable[[], dict], targets_fn: Callable[[], list[tuple[str, int]]]):
        self.settings_fn = settings_fn
        self.targets_fn = targets_fn

    # ---------------- 探针侧 ----------------
    def authenticate(self, token: str) -> str | None:
        """按令牌找到探针名称，找不到返回 None。"""
        if not token:
            return None
        for p in self.settings_fn().get("push_probes") or []:
            expected = str(p.get("token", ""))
            if expected and hmac.compare_digest(token.encode(), expected.encode()):
                return str(p.get("name") or "push")
        return None

    def tasks(self, name: str) -> dict:
        now = time.time()
        settings = self.settings_fn()
        with self._cond:
            self._last_seen[name] = now
            self._pending = {k: exp for k, exp in self._pending.items() if exp > now}
            pending = list(self._pending)
        targets: list[tuple[str, int]] = []
        for host, port in pending + list(self.targets_fn()):
            key = (host, int(port))
            if host and key not in targets:
                targets.append(key)
        return {
            "targets": [{"host": h, "port": p} for h, p in targets[:MAX_TARGETS]],
            "interval": POLL_INTERVAL,
            "timeout": float(settings.get("tcp_timeout", 5)),
            "tries": int(settings.get("tcp_tries", 3)),
        }

    def report(self, name: str, results: list) -> int:
        now = time.time()
        n = 0
        with self._cond:
            self._last_seen[name] = now
            for r in results[:MAX_TARGETS]:
                if not isinstance(r, dict):
                    continue
                try:
                    host, port = str(r["host"]), int(r["port"])
                except (KeyError, ValueError, TypeError):
                    continue
                ok = r.get("ok")
                if ok is not None:
                    ok = bool(ok)
                latency = r.get("latency_ms")
                self._results[(name, host, port)] = (
                    ok, float(latency) if isinstance(latency, (int, float)) else None,
                    str(r.get("error", ""))[:200], now,
                )
                n += 1
            # 清理很久没更新的结果
            cutoff = now - 3600
            self._results = {k: v for k, v in self._results.items() if v[3] >= cutoff}
            self._cond.notify_all()
        return n

    # ---------------- 管理端侧 ----------------
    def status(self) -> list[dict]:
        now = time.time()
        out = []
        for p in self.settings_fn().get("push_probes") or []:
            name = str(p.get("name") or "push")
            seen = self._last_seen.get(name, 0.0)
            out.append({"name": name, "last_seen": seen, "online": now - seen < OFFLINE_SECONDS})
        return out

    def result(self, name: str, host: str, port: int, wait: float = WAIT_SECONDS) -> ProbeResult:
        """读取某个探针对 host:port 的最新结果，必要时等待探针上报。"""
        key = (name, host, int(port))
        now = time.time()
        with self._cond:
            cached = self._results.get(key)
            if cached and now - cached[3] <= FRESH_SECONDS:
                return ProbeResult(name, cached[0], cached[1], cached[2])
            seen = self._last_seen.get(name, 0.0)
            starting = not seen and now - self._started < STARTUP_GRACE
            if now - seen > OFFLINE_SECONDS and not starting:
                return ProbeResult(name, None, error="主动上报探针离线（超过 2 分钟未连接面板）")
            self._pending[(host, int(port))] = now + PENDING_TTL
            requested = now
            deadline = now + wait
            while True:
                cached = self._results.get(key)
                if cached and cached[3] >= requested:
                    return ProbeResult(name, cached[0], cached[1], cached[2])
                left = deadline - time.time()
                if left <= 0:
                    if not self._last_seen.get(name):
                        return ProbeResult(name, None, error="面板重启后探针还没重新连上")
                    return ProbeResult(name, None, error=f"等待探针上报超时（{int(wait)} 秒）")
                self._cond.wait(left)


HUB = PushProbeHub()
