"""IP 被墙检测。

判断逻辑：
  * 国内视角（国内探针，或本机部署在国内时直接检测）对 ip:port 发起 TCP 连接；
  * 海外视角（本机）做对照检测；
  * 国内不通 + 海外通  => 疑似被墙（blocked）
  * 国内不通 + 海外也不通 => 实例/服务故障（down），不换 IP
  * 国内通 => 正常（ok）
  * 探针自身都不可用 => 无法判断（unknown），不计数
"""

from __future__ import annotations

import logging
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import requests

log = logging.getLogger(__name__)

STATE_OK = "ok"
STATE_BLOCKED = "blocked"
STATE_DOWN = "down"
STATE_UNKNOWN = "unknown"


def tcp_probe(host: str, port: int, timeout: float = 5, tries: int = 3) -> tuple[bool, float | None, str]:
    """TCP 连接探测，任意一次成功即视为可达。返回 (ok, latency_ms, error)。"""
    last_err = ""
    for i in range(max(1, tries)):
        start = time.monotonic()
        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                return True, round((time.monotonic() - start) * 1000, 1), ""
        except OSError as e:
            last_err = str(e) or e.__class__.__name__
        if i + 1 < tries:
            time.sleep(0.5)
    return False, None, last_err


@dataclass
class ProbeResult:
    source: str
    ok: bool | None  # None 表示探针本身不可用
    latency_ms: float | None = None
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "ok": self.ok,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }


@dataclass
class CheckResult:
    state: str
    cn: list[ProbeResult] = field(default_factory=list)
    control: ProbeResult | None = None
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "cn": [p.to_dict() for p in self.cn],
            "control": self.control.to_dict() if self.control else None,
            "detail": self.detail,
        }


def probe_via_agent(agent: dict, host: str, port: int, timeout: float, tries: int) -> ProbeResult:
    """调用部署在国内的探针（probe/probe.py）。"""
    name = agent.get("name") or agent.get("url", "probe")
    url = agent.get("url", "").rstrip("/") + "/probe"
    try:
        resp = requests.post(
            url,
            json={"host": host, "port": int(port), "timeout": timeout, "tries": tries},
            headers={"Authorization": f"Bearer {agent.get('token', '')}"},
            timeout=timeout * tries + 15,
        )
        if resp.status_code != 200:
            return ProbeResult(name, None, error=f"探针返回 HTTP {resp.status_code}")
        data = resp.json()
        return ProbeResult(name, bool(data.get("ok")), data.get("latency_ms"), data.get("error", ""))
    except (requests.RequestException, ValueError) as e:
        return ProbeResult(name, None, error=f"探针不可用：{e}")


class BlockChecker:
    def __init__(self, settings: dict, agent_fn=probe_via_agent, tcp_fn=tcp_probe, push_hub=None):
        self.settings = settings
        self.agent_fn = agent_fn
        self.tcp_fn = tcp_fn
        if push_hub is None:
            from .pushprobe import HUB as push_hub  # 延迟导入，避免循环依赖
        self.push_hub = push_hub

    def _local(self, source: str, host: str, port: int) -> ProbeResult:
        ok, latency, err = self.tcp_fn(
            host, port, float(self.settings["tcp_timeout"]), int(self.settings["tcp_tries"])
        )
        return ProbeResult(source, ok, latency, err)

    def probe_cn(self, host: str, port: int) -> list[ProbeResult]:
        if self.settings.get("detect_mode") == "local":
            return [self._local("本机(国内)", host, port)]
        agents = [a for a in self.settings.get("probes") or [] if a.get("url")]
        pushes = [p for p in self.settings.get("push_probes") or [] if p.get("token")]
        if not agents and not pushes:
            return []
        timeout = float(self.settings["tcp_timeout"])
        tries = int(self.settings["tcp_tries"])
        with ThreadPoolExecutor(max_workers=min(8, len(agents) + len(pushes))) as pool:
            futures = [pool.submit(self.agent_fn, a, host, port, timeout, tries) for a in agents]
            futures += [
                pool.submit(self.push_hub.result, str(p.get("name") or "push"), host, port)
                for p in pushes
            ]
            return [f.result() for f in futures]

    def check(self, host: str, port: int) -> CheckResult:
        if not host:
            return CheckResult(STATE_UNKNOWN, detail="实例没有外网 IP")

        cn = self.probe_cn(host, port)
        usable = [p for p in cn if p.ok is not None]
        if not usable:
            detail = "未配置国内探针" if not cn else "所有国内探针均不可用"
            return CheckResult(STATE_UNKNOWN, cn=cn, detail=detail)

        success = sum(1 for p in usable if p.ok) / len(usable)
        if success >= float(self.settings.get("cn_success_ratio", 0.5)):
            return CheckResult(STATE_OK, cn=cn, detail=f"国内可达率 {success:.0%}")

        control = None
        if self.settings.get("control_check", True) and self.settings.get("detect_mode") != "local":
            control = self._local("本机(海外对照)", host, port)
            if not control.ok:
                return CheckResult(
                    STATE_DOWN, cn=cn, control=control,
                    detail="国内外均不可达，判定为实例或服务故障，不更换 IP",
                )
        return CheckResult(
            STATE_BLOCKED, cn=cn, control=control, detail=f"国内可达率 {success:.0%}，疑似被墙"
        )
