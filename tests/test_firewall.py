import pytest

from app.firewall import build_body, parse_rules, summarize
from app.manager import Manager, ManagerError
from tests.conftest import FAKE_KEY, FakePool


def test_parse_rules():
    assert parse_rules("all") == [{"IPProtocol": "all"}]
    assert parse_rules("ALL") == [{"IPProtocol": "all"}]
    assert parse_rules("tcp:80,443,8000-9000 udp:443") == [
        {"IPProtocol": "tcp", "ports": ["80", "443", "8000-9000"]},
        {"IPProtocol": "udp", "ports": ["443"]},
    ]
    assert parse_rules("tcp udp") == [{"IPProtocol": "tcp"}, {"IPProtocol": "udp"}]
    assert parse_rules("icmp; tcp:22") == [{"IPProtocol": "icmp"}, {"IPProtocol": "tcp", "ports": ["22"]}]
    # 同协议既写全部又写端口 -> 全部
    assert parse_rules("tcp:22 tcp") == [{"IPProtocol": "tcp"}]
    for bad in ("", "443", "tcp:0", "tcp:70000", "tcp:9-1", "icmp:8", "foo:1", "tcp:a"):
        with pytest.raises(ValueError):
            parse_rules(bad)


def test_build_body_and_summary():
    b = build_body({"name": "allow-all", "rules": "all", "ranges": "0.0.0.0/0, 1.2.3.0/24"})
    assert b["network"] == "global/networks/default" and b["direction"] == "INGRESS"
    assert b["allowed"] == [{"IPProtocol": "all"}]
    assert b["sourceRanges"] == ["0.0.0.0/0", "1.2.3.0/24"] and "targetTags" not in b
    s = summarize(b)
    assert s["rules"] == "全部" and s["targets"] == "所有实例" and s["action"] == "allow"

    e = build_body({"name": "block-out", "direction": "egress", "action": "deny", "rules": "tcp:25", "target_tags": "a b"})
    assert e["denied"] == [{"IPProtocol": "tcp", "ports": ["25"]}]
    assert e["destinationRanges"] == ["0.0.0.0/0"] and e["targetTags"] == ["a", "b"]
    assert summarize(e)["rules"] == "tcp:25" and summarize(e)["action"] == "deny"

    for bad in ({"name": "Bad_Name", "rules": "all"}, {"name": "ok", "rules": "all", "ranges": "1.2.3"},
                {"name": "ok", "rules": "all", "priority": "99999"}, {"name": "ok", "rules": "all", "action": "x"}):
        with pytest.raises(ValueError):
            build_body(bad)


def test_manager_firewall_crud(db, fake_gcp):
    acc = db.add_account("main", FAKE_KEY)
    mgr = Manager(db, FakePool(fake_gcp))
    r = mgr.list_firewalls(acc["id"])
    assert r["networks"] == ["default"] and r["rules"][0]["name"] == "default-allow-ssh"
    assert r["rules"][0]["rules"] == "tcp:22"

    mgr.create_firewall(acc["id"], {"name": "allow-all-ingress", "rules": "all"})
    names = [x["name"] for x in mgr.list_firewalls(acc["id"])["rules"]]
    assert names == ["allow-all-ingress", "default-allow-ssh"]  # 按优先级排序
    with pytest.raises(ManagerError, match="已存在"):
        mgr.create_firewall(acc["id"], {"name": "allow-all-ingress", "rules": "all"})

    mgr.set_firewall_disabled(acc["id"], "allow-all-ingress", True)
    assert mgr.list_firewalls(acc["id"])["rules"][0]["disabled"] is True
    mgr.delete_firewall(acc["id"], "allow-all-ingress")
    assert len(mgr.list_firewalls(acc["id"])["rules"]) == 1
    assert any(e["type"] == "firewall" for e in db.list_events())
    with pytest.raises(ManagerError):
        mgr.list_firewalls(999)
