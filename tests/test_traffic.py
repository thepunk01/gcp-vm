import sqlite3
import subprocess

import pytest

from app.db import Database
from app.manager import Manager, ManagerError
from app.traffic import TrafficHub, agent_install_script, normalize_panel_url
from tests.conftest import FAKE_KEY, FakePool


def test_rates_and_groups():
    hub = TrafficHub()
    hub.report(1, 1000, 2000, now=100)
    assert hub.instance_rate(1, now=101)["rx_bps"] == 0  # 第一次上报没有速率
    hub.report(1, 1000 + 125_000 * 5, 2000 + 250_000 * 5, now=105)
    r = hub.instance_rate(1, now=106)
    assert r["online"] and r["rx_bps"] == 1_000_000 and r["tx_bps"] == 2_000_000
    # 计数器重置（实例重启）时丢弃这一次，保留上次速率
    hub.report(1, 10, 10, now=110)
    assert hub.instance_rate(1, now=111)["rx_bps"] == 1_000_000
    # 超过 20 秒未上报视为离线
    assert hub.instance_rate(1, now=200) == {"online": False, "rx_bps": 0, "tx_bps": 0, "last_seen": 110}

    hub.report(2, 0, 0, now=100)
    hub.report(2, 625_000, 0, now=105)  # 1 Mbps
    insts = [{"id": 1, "grp": "香港"}, {"id": 2, "grp": "香港"}, {"id": 3, "grp": "东京"}, {"id": 4, "grp": ""}]
    hub.report(1, 10 + 125_000 * 5, 10, now=115)  # 1 Mbps rx
    groups = hub.groups(insts, now=130)
    assert [g["name"] for g in groups] == ["东京", "香港"]
    hk = groups[1]
    assert hk == {"name": "香港", "total": 2, "online": 1, "rx_bps": 1_000_000, "tx_bps": 0}  # 2 号已超时
    assert groups[0]["online"] == 0
    assert all("ip" not in str(g) for g in groups)


def test_agent_script():
    script = agent_install_script("https://gcpvm.example.com/", "a" * 32)
    assert 'URL="https://gcpvm.example.com/api/traffic-agent/report"' in script
    assert 'TOKEN="' + "a" * 32 + '"' in script
    assert subprocess.run(["bash", "-n"], input=script, text=True).returncode == 0
    assert normalize_panel_url("http://1.2.3.4:8080") == "http://1.2.3.4:8080"
    for bad in ("ftp://x", "https://x.com/path", 'https://x.com";rm -rf /', ""):
        with pytest.raises(ValueError):
            normalize_panel_url(bad)
    with pytest.raises(ValueError):
        agent_install_script("https://x.com", "bad token")


def test_agent_loop_reads_proc_net_dev(tmp_path):
    """在本机实际跑一遍采集逻辑：能从 /proc/net/dev 取到 lo 的数字。"""
    out = subprocess.run(
        ["bash", "-c", "set -- $(sed 's/:/ /' /proc/net/dev | awk -v i=lo '$1==i {print $2, $10}'); echo \"$1 $2\""],
        capture_output=True, text=True,
    ).stdout.split()
    assert len(out) == 2 and all(x.isdigit() for x in out)


def test_manager_report_and_migration(tmp_path, fake_gcp):
    # 旧数据库没有 grp / traffic_token 列，打开时自动补上
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE instances (id INTEGER PRIMARY KEY, account_id INTEGER, project TEXT, zone TEXT, name TEXT);")
    con.close()
    Database(path)
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(instances)")}
    assert {"grp", "traffic_token"} <= cols

    db = Database(":memory:")
    db.add_account("main", FAKE_KEY)
    mgr = Manager(db, FakePool(fake_gcp))
    mgr.sync_all()
    iid = db.list_instances()[0]["id"]
    db.update_instance_config(iid, {"grp": "  香港入口  "})
    assert db.get_instance(iid)["grp"] == "香港入口"
    script = mgr.traffic_agent_script(iid, "https://p.example.com")
    token = db.get_instance(iid)["traffic_token"]
    assert token in script and mgr.traffic_agent_script(iid, "https://p.example.com") == script  # 令牌复用
    assert mgr.traffic_report("wrong", 1, 1) is False
    assert mgr.traffic_report(token, 100, 200) is True
    with pytest.raises(ManagerError):
        mgr.traffic_report(token, -1, 2)
