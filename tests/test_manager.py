import time

import pytest

from app.checker import CheckResult
from app.manager import Manager, ManagerError
from tests.conftest import FAKE_KEY, FakePool


def make_checker(state):
    class C:
        def __init__(self, settings):
            pass

        def check(self, host, port):
            return CheckResult(state[0], detail="test")
    return C


@pytest.fixture
def env(db, fake_gcp):
    state = ["blocked"]
    mgr = Manager(db, FakePool(fake_gcp), checker_cls=make_checker(state))
    acc = db.add_account("a1", FAKE_KEY)
    mgr.sync_account(acc["id"])
    inst = db.list_instances()[0]
    return mgr, db, fake_gcp, inst, state


def test_sync(env):
    mgr, db, gcp, inst, _ = env
    assert inst["name"] == "vm1" and inst["external_ip"] == "34.0.0.1"
    gcp.instances.clear()
    mgr.sync_account(inst["account_id"])
    assert db.list_instances() == []


def test_power_actions(env):
    mgr, db, gcp, inst, _ = env
    assert mgr.power(inst["id"], "stop")["status"] == "TERMINATED"
    assert mgr.power(inst["id"], "start")["status"] == "RUNNING"
    with pytest.raises(ManagerError):
        mgr.power(inst["id"], "destroy")


def test_change_ip_ephemeral(env):
    mgr, db, gcp, inst, _ = env
    out = mgr.change_ip(inst["id"])
    assert out["external_ip"] == "35.1.0.1"
    names = [c[0] for c in gcp.calls]
    # 先占住旧 IP，再删除 / 新建 access config，最后释放占位地址
    assert names == ["reserve", "delete_ac", "add_ac", "release"]
    assert gcp.calls[0][1].startswith("gcpvm-hold-vm1-") and gcp.addresses == {}


def test_change_ip_skips_previously_blocked(env):
    mgr, db, gcp, inst, _ = env
    db.add_blocked_ip("35.1.0.1", inst["id"])
    out = mgr.change_ip(inst["id"])
    assert out["external_ip"] == "35.1.0.2"


def test_change_ip_failure_restores_ip(env):
    mgr, db, gcp, inst, _ = env
    gcp.fail_add = True
    with pytest.raises(ManagerError):
        mgr.change_ip(inst["id"])
    # 失败后自动补回临时 IP
    assert gcp.instances[("asia-east1-b", "vm1")]["ip"]


def test_change_ip_static_releases_only_own(env):
    mgr, db, gcp, inst, _ = env
    db.update_instance_config(inst["id"], {"ip_mode": "static"})
    # 当前 IP 是用户自己预留的静态 IP，不应被释放
    gcp.addresses["user-ip"] = {"address": "34.0.0.1", "region": "asia-east1"}
    out = mgr.change_ip(inst["id"])
    first = out["external_ip"]
    assert "user-ip" in gcp.addresses
    assert ("release", "user-ip") not in gcp.calls
    # 再换一次：上一次由本系统创建的地址应被释放
    time.sleep(1.1)  # 地址名带秒级时间戳
    out = mgr.change_ip(inst["id"])
    assert out["external_ip"] != first
    assert not any(a["address"] == first for a in gcp.addresses.values())


def test_auto_change_after_threshold(env):
    mgr, db, gcp, inst, state = env
    db.update_instance_config(inst["id"], {"auto_ip": True})
    s = db.get_settings()
    for n in (1, 2):
        mgr.process_instance(db.get_instance(inst["id"]), s)
        cur = db.get_instance(inst["id"])
        assert cur["fail_count"] == n and cur["external_ip"] == "34.0.0.1"
    mgr.process_instance(db.get_instance(inst["id"]), s)
    cur = db.get_instance(inst["id"])
    assert cur["external_ip"] == "35.1.0.1"
    assert cur["fail_count"] == 0
    assert "34.0.0.1" in db.recent_blocked_ips()


def test_cooldown_prevents_repeat(env):
    mgr, db, gcp, inst, state = env
    db.update_instance_config(inst["id"], {"auto_ip": True})
    s = db.get_settings() | {"fail_threshold": 1}
    mgr.process_instance(db.get_instance(inst["id"]), s)
    assert db.get_instance(inst["id"])["external_ip"] == "35.1.0.1"
    mgr.process_instance(db.get_instance(inst["id"]), s)  # 冷却期内
    assert db.get_instance(inst["id"])["external_ip"] == "35.1.0.1"


def test_daily_limit(env):
    mgr, db, gcp, inst, state = env
    db.update_instance_config(inst["id"], {"auto_ip": True})
    s = db.get_settings() | {"fail_threshold": 1, "cooldown_minutes": 0, "max_changes_per_day": 2}
    for _ in range(4):
        mgr.process_instance(db.get_instance(inst["id"]), s)
    assert db.count_events_since(inst["id"], "change_ip_auto", 0) == 2


def test_down_and_ok_reset_counter(env):
    mgr, db, gcp, inst, state = env
    db.update_instance_config(inst["id"], {"auto_ip": True})
    s = db.get_settings()
    mgr.process_instance(db.get_instance(inst["id"]), s)
    assert db.get_instance(inst["id"])["fail_count"] == 1
    state[0] = "down"
    mgr.process_instance(db.get_instance(inst["id"]), s)
    assert db.get_instance(inst["id"])["cn_state"] == "down"
    assert db.get_instance(inst["id"])["external_ip"] == "34.0.0.1"
    state[0] = "ok"
    mgr.process_instance(db.get_instance(inst["id"]), s)
    cur = db.get_instance(inst["id"])
    assert cur["fail_count"] == 0 and cur["cn_state"] == "ok"


def test_auto_start(env):
    mgr, db, gcp, inst, state = env
    db.update_instance_config(inst["id"], {"auto_start": True})
    gcp.instances[("asia-east1-b", "vm1")]["status"] = "TERMINATED"
    mgr.process_instance(db.get_instance(inst["id"]), db.get_settings())
    assert ("start", "vm1") in gcp.calls
    assert db.get_instance(inst["id"])["status"] == "RUNNING"


def test_unmanaged_instance_untouched(env):
    mgr, db, gcp, inst, state = env
    mgr.run_cycle()
    assert gcp.calls == []


# ---------------- 换 IP：不再分回被墙 IP ----------------
def _mgr_two_vms(db, fake_gcp):
    from app.manager import Manager
    from tests.conftest import FAKE_KEY, FakePool
    fake_gcp.instances[("asia-east1-b", "vm2")] = {"status": "RUNNING", "ip": "34.0.0.2", "ac": "External NAT", "tier": "PREMIUM"}
    db.add_account("main", FAKE_KEY)
    mgr = Manager(db, FakePool(fake_gcp))
    mgr.sync_all()
    ids = {i["name"]: i["id"] for i in db.list_instances()}
    return mgr, ids


def test_change_ip_never_returns_same_or_blocked_ip(db, fake_gcp):
    mgr, ids = _mgr_two_vms(db, fake_gcp)
    # vm1 被墙换 IP：旧 IP 34.0.0.1 被占住，不会被分回来
    inst = mgr.change_ip(ids["vm1"], reason="自动-被墙")
    assert inst["external_ip"] not in ("34.0.0.1", "")
    # 占位地址已释放，34.0.0.1 回到地址池（地址池会优先分配它）
    assert fake_gcp.addresses == {} and fake_gcp.released[-1] == "34.0.0.1"
    # vm2 接着换 IP：地址池先给的是 vm1 刚释放的被墙 IP 34.0.0.1，必须拒绝
    inst2 = mgr.change_ip(ids["vm2"], reason="自动-被墙")
    assert inst2["external_ip"] not in ("34.0.0.1", "34.0.0.2", inst["external_ip"])
    assert fake_gcp.addresses == {}
    assert {"34.0.0.1", "34.0.0.2"} <= db.recent_blocked_ips()


def test_change_ip_gives_up_instead_of_accepting_blocked_ip(db, fake_gcp):
    from app.manager import ManagerError
    mgr, ids = _mgr_two_vms(db, fake_gcp)
    # 让地址池只会分配已知被墙的 IP
    for i in range(1, 200):
        db.add_blocked_ip(f"35.1.0.{i}", None)
    with pytest.raises(ManagerError, match="已知被墙"):
        mgr.change_ip(ids["vm1"], reason="自动-被墙")
    assert fake_gcp.addresses == {}  # 占位地址全部释放
    ev = db.list_events()
    assert not any("IP 已更换" in e["message"] for e in ev)
    assert any("更换 IP 失败" in e["message"] for e in ev)
    assert fake_gcp.get_instance("asia-east1-b", "vm1")["external_ip"]  # 实例仍有外网 IP


def test_change_ip_reports_hold_failure_reason(db, fake_gcp):
    from app.gcp import GCPError
    from app.manager import ManagerError
    mgr, ids = _mgr_two_vms(db, fake_gcp)

    def no_reserve(*a, **k):
        raise GCPError("Quota 'STATIC_ADDRESSES' exceeded. Limit: 0.0")

    fake_gcp.reserve_address = no_reserve
    mgr._sleep = lambda s: None
    with pytest.raises(ManagerError, match="STATIC_ADDRESSES"):
        mgr.change_ip(ids["vm1"], reason="自动-被墙")


def test_change_ip_waits_when_cannot_hold(db, fake_gcp):
    from app.gcp import GCPError
    mgr, ids = _mgr_two_vms(db, fake_gcp)
    waits = []
    mgr._sleep = waits.append

    def no_reserve(*a, **k):
        raise GCPError("Quota 'STATIC_ADDRESSES' exceeded. Limit: 0.0")

    fake_gcp.reserve_address = no_reserve
    # 模拟地址池：前两次把刚释放的被墙 IP 还回来，等待后才轮换到新 IP
    real_add = fake_gcp.add_access_config
    count = {"n": 0}

    def add(zone, name, nic, ac_name="External NAT", nat_ip=None, network_tier=None):
        count["n"] += 1
        if count["n"] > 2:
            fake_gcp.released.clear()
        return real_add(zone, name, nic, ac_name, nat_ip, network_tier)

    fake_gcp.add_access_config = add
    inst = mgr.change_ip(ids["vm1"], reason="自动-被墙")
    assert inst["external_ip"] not in ("34.0.0.1", "")
    assert waits == [10, 20, 30]  # 每次占不住都等待后再申请，等待时间递增
