import pytest

from app.checker import CheckResult
from app.manager import Manager, ManagerError
from tests.conftest import FAKE_KEY, FakeGCP, FakePool


class SeqChecker:
    """按 IP 返回预设状态，未设置的为 ok。"""
    states: dict = {}

    def __init__(self, settings):
        pass

    def check(self, host, port):
        return CheckResult(self.states.get(host, "ok"), detail="test")


@pytest.fixture
def xenv(db):
    fake = FakeGCP()
    db.add_account("a", FAKE_KEY)
    SeqChecker.states = {}
    db.update_settings({"verify_new_ip": True, "fail_threshold": 2, "cooldown_minutes": 0})
    mgr = Manager(db, FakePool(fake), checker_cls=SeqChecker)
    mgr._sleep = lambda s: None
    mgr.sync_all()
    inst = db.list_instances()[0]
    db.update_instance_config(inst["id"], {"auto_ip": True})
    return mgr, db, fake, inst


def test_add_change_remove(xenv):
    mgr, db, fake, inst = xenv
    ex = mgr.add_extra_ip(inst["id"])
    assert ex["ip"] == "35.1.0.1" and ex["cn_state"] == "ok"
    assert fake.rules[ex["res_name"]]["ip"] == "35.1.0.1" and len(fake.targets) == 1
    ex2 = mgr.add_extra_ip(inst["id"])
    assert ex2["ip"] == "35.1.0.2"
    new = mgr.change_extra_ip(ex["id"])
    assert new["ip"] == "35.1.0.3" and ex["res_name"] not in fake.rules and ex["res_name"] not in fake.addresses
    assert any("IP 已更换（手动·附加 IP）：35.1.0.1 -> 35.1.0.3" in e["message"] for e in db.list_events())
    mgr.remove_extra_ip(new["id"])
    assert len(fake.targets) == 1  # 还有一个附加 IP，目标实例保留
    mgr.remove_extra_ip(ex2["id"])
    assert fake.targets == {} and fake.rules == {} and db.list_extra_ips() == []


def test_add_skips_blocked_candidates(xenv):
    mgr, db, fake, inst = xenv
    db.add_blocked_ip("35.1.0.1", None)  # 已知被墙
    SeqChecker.states = {"35.1.0.2": "blocked"}  # 验证不通过
    ex = mgr.add_extra_ip(inst["id"])
    assert ex["ip"] == "35.1.0.3"
    assert list(fake.rules) == [ex["res_name"]] and list(fake.addresses) == [ex["res_name"]]  # 被拒绝的都已释放
    assert "35.1.0.2" in db.recent_blocked_ips()


def test_auto_change_blocked_extra(xenv):
    mgr, db, fake, inst = xenv
    ex = mgr.add_extra_ip(inst["id"])
    SeqChecker.states = {ex["ip"]: "blocked"}
    settings = db.get_settings()
    mgr.process_extras(inst["id"], settings)
    assert db.get_extra_ip(ex["id"])["fail_count"] == 1
    mgr.process_extras(inst["id"], settings)
    x = db.get_extra_ip(ex["id"])
    assert x["ip"] != ex["ip"] and x["fail_count"] == 0
    assert db.get_instance(inst["id"])["external_ip"] == "34.0.0.1"  # 主 IP 不受影响


def test_delete_instance_cleans_extras_first(xenv):
    mgr, db, fake, inst = xenv
    mgr.add_extra_ip(inst["id"])
    mgr.delete_instance(inst["id"])
    assert fake.rules == {} and fake.targets == {} and fake.addresses == {}
    assert db.list_extra_ips() == []


def test_limit_and_hub_targets(xenv):
    mgr, db, fake, inst = xenv
    from app import manager as m
    old = m.MAX_EXTRA_IPS
    m.MAX_EXTRA_IPS = 1
    try:
        mgr.add_extra_ip(inst["id"])
        with pytest.raises(ManagerError):
            mgr.add_extra_ip(inst["id"])
    finally:
        m.MAX_EXTRA_IPS = old


def test_change_under_quota_releases_old_first(xenv):
    mgr, db, fake, inst = xenv
    ex = mgr.add_extra_ip(inst["id"])
    fake.static_quota = len(fake.addresses)  # 配额用满：拿不到新地址，只能先释放旧的
    with pytest.raises(ManagerError) as ei:
        mgr.change_extra_ip(ex["id"], reason="自动-被墙")
    assert "配额" in str(ei.value) or "被墙" in str(ei.value)
    fake.static_quota = len(fake.addresses) + 2  # 再空出位置：释放旧的后，被分回的旧 IP 占住 1 个，新 IP 用 1 个
    ex = db.get_extra_ip(ex["id"])
    new = mgr.change_extra_ip(ex["id"], reason="自动-被墙")
    assert new["ip"] and new["ip"] != "35.1.0.1"
    assert any("配额已满，先释放旧附加 IP" in e["message"] for e in db.list_events())


def test_quota_releases_held_rejects(xenv):
    mgr, db, fake, inst = xenv
    ex = mgr.add_extra_ip(inst["id"])             # 35.1.0.1
    db.add_blocked_ip("35.1.0.2", None)           # 下一个候选被墙，会被占住
    fake.static_quota = len(fake.addresses) + 2   # 旧的 + 1 个占住 + 1 个新的 = 刚好
    new = mgr.change_extra_ip(ex["id"])
    assert new["ip"] == "35.1.0.3"


def test_cleanup_extra_leftovers(xenv):
    mgr, db, fake, inst = xenv
    fake.addresses["gcpvmx-old-01010101010abc-1"] = {"address": "9.9.9.9", "region": "asia-east1"}
    mgr.add_extra_ip(inst["id"])
    assert "gcpvmx-old-01010101010abc-1" not in fake.addresses


def test_extra_without_ip_is_reallocated(xenv):
    mgr, db, fake, inst = xenv
    ex = mgr.add_extra_ip(inst["id"])
    fake.static_quota = 0
    try:
        mgr.change_extra_ip(ex["id"], reason="自动-被墙")
    except ManagerError:
        pass
    assert db.get_extra_ip(ex["id"])["ip"] == ""
    fake.static_quota = None
    mgr.process_extras(inst["id"], db.get_settings())
    assert db.get_extra_ip(ex["id"])["ip"]
