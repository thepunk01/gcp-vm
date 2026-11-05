from datetime import datetime

import pytest

from app.report import TZ, Reporter, parse_times
from app.traffic import TrafficHub
from tests.conftest import FAKE_KEY, FakeGCP, FakePool
from app.manager import Manager


@pytest.fixture
def rep(db):
    fake = FakeGCP()
    db.add_account("a", FAKE_KEY)
    mgr = Manager(db, FakePool(fake))
    mgr.sync_all()
    insts = db.list_instances()
    db.update_instance_config(insts[0]["id"], {"grp": "台湾入口"})
    db.update_settings({"report_enabled": True, "report_times": "09:00", "telegram_bot_token": "t", "telegram_chat_id": "1",
                        "report_chat_id": "-100777"})
    hub = TrafficHub()
    sent = []
    r = Reporter(db, hub, sender=lambda s, text: sent.append((s["telegram_chat_id"], text)) or True)
    return r, hub, db, insts, sent


def ts(h, m, s=0):
    return datetime(2026, 11, 5, h, m, s, tzinfo=TZ).timestamp()


def test_parse_times():
    assert parse_times("9:00， 21:30 9:00") == ["09:00", "21:30"]
    with pytest.raises(ValueError):
        parse_times("25:00")


def test_peaks_counts_and_scheduled_send(rep):
    r, hub, db, insts, sent = rep
    iid = insts[0]["id"]
    t0 = ts(8, 0)
    r.since = t0
    # 两次上报：10 秒内 rx 增加 125 MB = 100 Mbps，tx 62.5 MB = 50 Mbps
    hub.report(iid, 0, 0, now=t0)
    hub.report(iid, 125_000_000, 62_500_000, now=t0 + 10)
    r.sample(t0 + 11)
    hub.report(iid, 130_000_000, 63_000_000, now=t0 + 20)  # 之后速率下降，峰值保留
    r.sample(t0 + 21)
    db.add_event("change_ip", "IP 已更换（自动-被墙）：1.1.1.1 -> 2.2.2.2", iid, "warn")
    db.add_event("change_ip", "IP 已更换（手动）：2.2.2.2 -> 3.3.3.3", iid, "warn")
    db.add_event("change_ip", "更换 IP 失败（自动）：xx", iid, "error")
    db.add_event("replace_new", "自动替补：...", iid, "warn")
    with db.tx() as c:
        c.execute("UPDATE events SET ts = ?", (t0 + 30,))
    assert r.tick(ts(8, 59)) is False and not sent
    assert r.tick(ts(9, 0, 1)) is True
    assert r.tick(ts(9, 0, 30)) is False  # 同一个时间点只发一次
    chat, text = sent[0]
    assert chat == "-100777"
    assert "台湾入口" in text and "↓ 100 Mbps" in text and "↑ 50.0 Mbps" in text
    assert "更换 IP：2 次，替补 1 次" in text and "（自动 1 次）" in text
    assert all(v == {"rx": 0, "tx": 0} for v in r.peaks.values()) and r.since == ts(9, 0, 1)  # 新周期
    # 状态持久化，重启后恢复
    r2 = Reporter(db, hub)
    assert r2.since == r.since and r2.last_slot == r.last_slot


def test_disabled_and_preview(rep):
    r, hub, db, insts, sent = rep
    db.update_settings({"report_enabled": False})
    assert r.tick(ts(9, 0)) is False and not sent
    r._bump("台湾入口", 5e6, 5e6)
    assert r.send(reset=False)
    assert r.peaks  # 预览不清空
    assert sent[0][0] == "-100777"
