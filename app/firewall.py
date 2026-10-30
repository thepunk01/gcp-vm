"""防火墙规则：解析端口写法、构造 GCP 请求体、整理展示字段。"""

from __future__ import annotations

import ipaddress
import re

NAME_RE = re.compile(r"^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$")
PROTOCOLS = {"tcp", "udp", "icmp", "esp", "ah", "sctp", "ipip", "all"}
PORT_RE = re.compile(r"^\d{1,5}(-\d{1,5})?$")


def parse_rules(spec: str) -> list[dict]:
    """把 "tcp:80,443,8000-9000; udp:443; icmp" 或 "all" 转成 GCP allowed 列表。

    支持分隔符：逗号、分号、空格、换行。不带协议的端口归到上一个协议。
    """
    text = (spec or "").strip().lower()
    if not text:
        raise ValueError("请填写协议和端口，如 tcp:443,udp:443 或 all")
    tokens = [t for t in re.split(r"[,;\s]+", text) if t]
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    current: str | None = None
    for tok in tokens:
        if ":" in tok:
            proto, _, port = tok.partition(":")
        elif tok in PROTOCOLS:
            proto, port = tok, ""
        else:
            if current is None:
                raise ValueError(f"端口 {tok} 前需要写明协议，如 tcp:{tok}")
            proto, port = current, tok
        if proto not in PROTOCOLS:
            raise ValueError(f"不支持的协议：{proto}")
        if port:
            if proto not in ("tcp", "udp", "sctp"):
                raise ValueError(f"{proto} 协议不能指定端口")
            if not PORT_RE.match(port):
                raise ValueError(f"端口写法不正确：{port}")
            nums = [int(x) for x in port.split("-")]
            if not all(1 <= n <= 65535 for n in nums) or (len(nums) == 2 and nums[0] > nums[1]):
                raise ValueError(f"端口范围不正确：{port}")
        if proto not in groups:
            groups[proto] = []
            order.append(proto)
        if port and port not in groups[proto]:
            groups[proto].append(port)
        elif not port:
            groups[proto] = ["*"]  # 该协议全部端口
        current = proto
    if "all" in groups:
        return [{"IPProtocol": "all"}]
    out = []
    for proto in order:
        ports = groups[proto]
        item: dict = {"IPProtocol": proto}
        if ports and "*" not in ports:
            item["ports"] = ports
        out.append(item)
    return out


def build_body(data: dict) -> dict:
    name = str(data.get("name", "")).strip()
    if not NAME_RE.match(name):
        raise ValueError("名称只能用小写字母、数字和短横线，以字母开头，最长 63 位")
    direction = str(data.get("direction", "INGRESS")).upper()
    if direction not in ("INGRESS", "EGRESS"):
        raise ValueError("方向只能是 INGRESS 或 EGRESS")
    action = str(data.get("action", "allow")).lower()
    if action not in ("allow", "deny"):
        raise ValueError("动作只能是 allow 或 deny")
    try:
        priority = int(data.get("priority", 1000))
    except (TypeError, ValueError):
        raise ValueError("优先级必须是数字") from None
    if not 0 <= priority <= 65535:
        raise ValueError("优先级范围 0-65535（数字越小越优先）")
    ranges = [r for r in re.split(r"[,\s]+", str(data.get("ranges") or "0.0.0.0/0")) if r]
    for r in ranges:
        try:
            ipaddress.ip_network(r, strict=False)
        except ValueError:
            raise ValueError(f"IP 范围不正确：{r}") from None
    tags = [t for t in re.split(r"[,\s]+", str(data.get("target_tags") or "")) if t]
    network = str(data.get("network") or "default").strip()
    body: dict = {
        "name": name,
        "network": f"global/networks/{network}",
        "direction": direction,
        "priority": priority,
        "description": str(data.get("description", ""))[:200],
        ("allowed" if action == "allow" else "denied"): parse_rules(str(data.get("rules", ""))),
        ("sourceRanges" if direction == "INGRESS" else "destinationRanges"): ranges,
    }
    if tags:
        body["targetTags"] = tags
    return body


def _fmt(items: list[dict]) -> str:
    parts = []
    for it in items or []:
        proto = it.get("IPProtocol", "")
        ports = it.get("ports")
        if proto == "all":
            parts.append("全部")
        elif ports:
            parts.append(f"{proto}:{','.join(ports)}")
        else:
            parts.append(f"{proto}:全部")
    return "; ".join(parts)


def summarize(rule: dict) -> dict:
    allowed, denied = rule.get("allowed"), rule.get("denied")
    direction = rule.get("direction", "INGRESS")
    ranges = rule.get("sourceRanges" if direction == "INGRESS" else "destinationRanges") or []
    if direction == "INGRESS" and rule.get("sourceTags"):
        ranges = ranges + [f"tag:{t}" for t in rule["sourceTags"]]
    return {
        "name": rule.get("name", ""),
        "network": (rule.get("network") or "").rsplit("/", 1)[-1],
        "direction": direction,
        "action": "allow" if allowed else "deny",
        "rules": _fmt(allowed or denied or []),
        "ranges": ", ".join(ranges),
        "targets": ", ".join(rule.get("targetTags") or []) or "所有实例",
        "priority": rule.get("priority", 1000),
        "disabled": bool(rule.get("disabled")),
        "description": rule.get("description", ""),
    }
