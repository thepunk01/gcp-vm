"""定时 Telegram 报告：各分组的峰值带宽、换 IP 次数、替补次数。

每 2 秒采样一次各分组的实时总速率，记录本周期内的峰值；到了设定的时间点（北京时间）
把本周期的汇总发到 Telegram 群组，然后开始新周期。周期状态每分钟存一次库，面板重启不丢。
"""

from __future__ import annotations

import html
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from .integrations import send_telegram

log = logging.getLogger(__name__)

TZ = timezone(timedelta(hours=8))  # 北京时间
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
TOTAL = "__total__"
esc = html.escape


def fmt_bps(bps: float) -> str:
    for unit, n in (("Gbps", 1e9), ("Mbps", 1e6), ("Kbps", 1e3)):
        if bps >= n:
            v = bps / n
            return f"{v:.0f} {unit}" if v >= 100 else f"{v:.1f} {unit}" if v >= 10 else f"{v:.2f} {unit}"
    return f"{bps:.0f} bps"


def parse_times(value: str) -> list[str]:
    """'9:00, 21:00' -> ['09:00', '21:00']；格式错误抛 ValueError。"""
    out = []
    for part in re.split(r"[,，\s]+", str(value or "").strip()):
        if not part:
            continue
        m = TIME_RE.match(part)
        if not m:
            raise ValueError(f"推送时间格式不正确：{part}（例如 09:00,21:00）")
        out.append(f"{int(m.group(1)):02d}:{m.group(2)}")
    return sorted(set(out))


class Reporter:
    def __init__(self, db, traffic, sender=send_telegram):
        self.db = db
        self.traffic = traffic
        self.sender = sender
        self._lock = threading.Lock()
        st = db.get_settings().get("report_state") or {}
        self.since: float = float(st.get("since") or time.time())
        self.peaks: dict[str, dict] = st.get("peaks") or {}
        self.last_slot: str = st.get("last_slot") or ""
        self._saved_at = 0.0

    # ---------------- 采样 ----------------
    def sample(self, now: float | None = None) -> None:
        now = now or time.time()
        insts = self.db.list_instances()
        with self._lock:
            for g in self.traffic.groups(insts, now):
                self._bump(g["name"], g["rx_bps"], g["tx_bps"])
            rx = tx = 0.0
            for inst in insts:
                r = self.traffic.instance_rate(inst["id"], now)
                if r["online"]:
                    rx += r["rx_bps"]
                    tx += r["tx_bps"]
            self._bump(TOTAL, rx, tx)

    def _bump(self, name: str, rx: float, tx: float) -> None:
        p = self.peaks.setdefault(name, {"rx": 0.0, "tx": 0.0})
        p["rx"] = max(p["rx"], rx)
        p["tx"] = max(p["tx"], tx)

    def _save(self) -> None:
        with self._lock:
            state = {"since": self.since, "peaks": self.peaks, "last_slot": self.last_slot}
        self.db.update_settings({"report_state": state})
        self._saved_at = time.time()

    # ---------------- 定时 ----------------
    def tick(self, now: float | None = None) -> bool:
        """每 2 秒调用：采样、定期存盘、到点发送。返回本次是否发送了报告。"""
        now = now or time.time()
        settings = self.db.get_settings()
        self.sample(now)  # 不论是否开启都持续采样，开启后第一份报告也有数据
        if not settings.get("report_enabled"):
            if now - self._saved_at >= 60:
                self._save()
            return False
        sent = False
        local = datetime.fromtimestamp(now, TZ)
        hm = local.strftime("%H:%M")
        slot = local.strftime("%Y-%m-%d ") + hm
        try:
            times = parse_times(settings.get("report_times", ""))
        except ValueError:
            times = []
        if hm in times and slot != self.last_slot:
            self.last_slot = slot
            sent = self.send(settings, now, reset=True)
        if sent or now - self._saved_at >= 60:
            self._save()
        return sent

    # ---------------- 内容 ----------------
    def _counts(self, since: float, now: float) -> dict[str, dict]:
        """按分组统计换 IP（区分自动/手动）和替补次数。"""
        grp = {i["id"]: (i.get("grp") or "").strip() for i in self.db.list_instances()}
        out: dict[str, dict] = {}

        def row(name):
            return out.setdefault(name, {"ip": 0, "ip_auto": 0, "replace": 0})

        for e in self.db.list_events(limit=5000):
            if not since <= e["ts"] <= now:
                continue
            name = grp.get(e.get("instance_id"), "")
            if e["type"] == "change_ip" and e["message"].startswith("IP 已更换"):
                auto = "手动" not in e["message"][:12]
                for k in (name, TOTAL):
                    r = row(k)
                    r["ip"] += 1
                    r["ip_auto"] += int(auto)
            elif e["type"] == "replace_new":
                for k in (name, TOTAL):
                    row(k)["replace"] += 1
        return out

    @staticmethod
    def _span(since: float, now: float) -> str:
        a, b = datetime.fromtimestamp(since, TZ), datetime.fromtimestamp(now, TZ)
        right = f"{b:%H:%M}" if a.date() == b.date() else f"{b:%m-%d %H:%M}"
        mins = int((now - since) // 60)
        dur = "不到 1 分钟" if mins < 1 else f"{mins} 分钟" if mins < 60 else (
            f"{mins // 60} 小时" + (f" {mins % 60} 分" if mins % 60 else ""))
        return f"{a:%m-%d %H:%M} → {right}　共 {dur}"

    def build(self, settings: dict, now: float | None = None) -> str:
        """Telegram HTML 卡片。峰值至少取发送这一刻的实时速率，避免刚开始统计时全是 0。"""
        now = now or time.time()
        self.sample(now)
        with self._lock:
            peaks = {k: dict(v) for k, v in self.peaks.items()}
            since = self.since
        counts = self._counts(since, now)
        groups = self.traffic.groups(self.db.list_instances(), now)
        show_peak = settings.get("report_peaks", True)
        show_ip = settings.get("report_ip_changes", True)
        title = (settings.get("report_title") or "").strip() or "线路运行报告"
        zero = {"ip": 0, "ip_auto": 0, "replace": 0}
        out = [f"<b>📊 {esc(title)}</b>", f"<i>{self._span(since, now)}</i>", ""]
        for g in groups:
            p = peaks.get(g["name"], {"rx": 0, "tx": 0})
            c = counts.get(g["name"], zero)
            dot = "🟢" if g["online"] == g["total"] else "🟡" if g["online"] else "🔴"
            head, _, sub = g["name"].partition(" ")
            out.append(f"{dot} <b>{esc(head)}</b>　<code>在线 {g['online']}/{g['total']}</code>")
            if sub.strip():
                out.append(f"<i>{esc(sub.strip())}</i>")
            if show_peak:
                out.append(f"├ 峰值下行　<code>{fmt_bps(p['rx'])}</code>")
                out.append(f"{'├' if show_ip else '└'} 峰值上行　<code>{fmt_bps(p['tx'])}</code>")
            if show_ip:
                extra = f"　替补 <code>{c['replace']}</code> 次" if c["replace"] else ""
                out.append(f"└ 更换 IP　<code>{c['ip']} 次</code>{extra}")
            out.append("")
        return "\n".join(out).strip()

    def send(self, settings: dict | None = None, now: float | None = None, reset: bool = False) -> bool:
        settings = settings or self.db.get_settings()
        now = now or time.time()
        text = self.build(settings, now)
        chat = (settings.get("report_chat_id") or "").strip() or settings.get("telegram_chat_id")
        ok = self.sender(settings | {"telegram_chat_id": chat}, text, True)
        if reset and ok:
            with self._lock:
                self.since = now
                self.peaks = {}
            self.db.add_event("report", "已推送定时报告到 Telegram")
        elif reset:
            self.db.add_event("report", "定时报告推送失败，请检查 Bot Token / 群组 Chat ID", level="error")
        return ok
