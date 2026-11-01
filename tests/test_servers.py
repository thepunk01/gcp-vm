"""自有服务器（非 GCP）与服务监控告警。"""
import subprocess

import pytest

from app import manager as manager_mod
from app.checker import CheckResult, STATE_BLOCKED, STATE_OK
from app.manager import Manager, ManagerError
from app.remote import SSHKey, shell_command
from app.traffic import bootstrap_script
from tests.conftest import FAKE_KEY, FakePool


@pytest.fixture
def notes(monkeypatch):
    sent = []
    monkeypatch.setattr(manager_mod, "send_telegram", lambda settings, text: sent.append(text))
    return sent


class Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, host, script, key_path, on_output, **kw):
        self.calls.append({"host": host, "script": script, **kw})
        on_output("ok\n")
        return 0


def make(db, fake_gcp, tmp_path):
    db.add_account("main", FAKE_KEY)
    runner = Runner()
    mgr = Manager(db, FakePool(fake_gcp), ssh_key=SSHKey(tmp_path), ssh_runner=runner)
    mgr.sync_all()
    return mgr, runner


def wait(job):
    import time
    for _ in range(200):
        if job.status != "running":
            return job
        time.sleep(0.01)
    return job


def test_add_server_and_listing(db, fake_gcp, tmp_path):
    mgr, runner = make(db, fake_gcp, tmp_path)
    inst, job = mgr.add_server({"name": "香港小鸡1", "ip": "1.2.3.4", "ssh_port": 2222, "grp": "香港入口", "check_port": 443})
    assert job is None and inst["provider"] == "manual" and inst["external_ip"] == "1.2.3.4"
    assert inst["ssh_port"] == 2222 and inst["ssh_user"] == "root" and inst["grp"] == "香港入口"
    # GCP 账号列表不包含内置账号；同步不会删除自有服务器
    assert [a["name"] for a in db.list_accounts()] == ["main"]
    mgr.sync_all()
    assert {i["name"] for i in db.list_instances()} == {"vm1", "香港小鸡1"}
    with pytest.raises(ManagerError, match="同名"):
        mgr.add_server({"name": "香港小鸡1", "ip": "1.2.3.5"})
    for bad in ({"name": "", "ip": "1.2.3.4"}, {"name": "x", "ip": "abc"}, {"name": "x", "ip": "1.1.1.1", "ssh_port": 0},
                {"name": "x", "ip": "1.1.1.1", "ssh_user": "Root;rm"}):
        with pytest.raises(ManagerError):
            mgr.add_server(bad)
    # 云厂商操作不可用
    for call in (lambda: mgr.power(inst["id"], "stop"), lambda: mgr.change_ip(inst["id"]), lambda: mgr.delete_instance(inst["id"])):
        with pytest.raises(ManagerError, match="自有服务器"):
            call()
    # 更新 IP / 移除
    up = mgr.update_server(inst["id"], {"ip": "5.6.7.8", "ssh_port": 22})
    assert up["external_ip"] == "5.6.7.8" and up["ssh_port"] == 22
    mgr.remove_server(inst["id"])
    assert {i["name"] for i in db.list_instances()} == {"vm1"}


def test_add_server_with_password_bootstraps(db, fake_gcp, tmp_path):
    mgr, runner = make(db, fake_gcp, tmp_path)
    inst, job = mgr.add_server({"name": "hk2", "ip": "9.9.9.9", "password": "secret"}, "https://p.example.com")
    wait(job)
    assert job.status == "ok"
    call = runner.calls[0]
    assert call["password"] == "secret" and call["user"] == "root" and call["port"] == 22
    assert "gcpvm-panel" in call["script"] and "gcpvm-traffic" in call["script"] and 'SVC="nyanpass"' in call["script"]
    assert "secret" not in str(db.get_instance(inst["id"]))  # 密码不保存
    # 之后执行命令用面板密钥、root、不再需要密码
    job2 = wait(mgr.start_exec(inst["id"], "uptime"))
    assert job2.status == "ok" and runner.calls[1]["password"] is None and runner.calls[1]["user"] == "root"
    # 一键接入脚本
    token = db.get_instance(inst["id"])["traffic_token"]
    script = mgr.bootstrap_by_token(token, "https://p.example.com")
    assert subprocess.run(["bash", "-n"], input=script, text=True).returncode == 0
    assert mgr.bootstrap_by_token("bad", "https://p.example.com") is None


def test_shell_command_root_without_sudo():
    assert not shell_command("root").startswith("sudo")
    assert shell_command("gcpvm").startswith("sudo -n")
    with pytest.raises(ValueError):
        bootstrap_script("https://p.example.com", "a" * 32, "ssh-rsa AAA x")


def test_manual_blocked_notifies_once(db, fake_gcp, tmp_path, notes):
    mgr, _ = make(db, fake_gcp, tmp_path)
    inst, _ = mgr.add_server({"name": "hk", "ip": "1.2.3.4"})
    state = {"s": STATE_BLOCKED}

    class Checker:
        def __init__(self, settings):
            pass

        def check(self, host, port):
            return CheckResult(state["s"], detail="国内可达率 0%，疑似被墙")

    mgr.checker_cls = Checker
    settings = db.get_settings() | {"fail_threshold": 2}
    for _ in range(4):
        mgr.process_instance(db.get_instance(inst["id"]), settings)
    assert sum("疑似被墙" in n for n in notes) == 1  # 达到阈值只通知一次
    assert db.get_instance(inst["id"])["external_ip"] == "1.2.3.4"  # 不会尝试换 IP
    state["s"] = STATE_OK
    mgr.process_instance(db.get_instance(inst["id"]), settings)
    assert any("恢复可达" in n for n in notes)


def test_service_alerts(db, fake_gcp, tmp_path, notes):
    mgr, _ = make(db, fake_gcp, tmp_path)
    inst, _ = mgr.add_server({"name": "hk", "ip": "1.2.3.4"})
    mgr.traffic_agent_script(inst["id"], "https://p.example.com")
    token = db.get_instance(inst["id"])["traffic_token"]
    t0 = mgr._started + 1000
    # 正常上报
    mgr.traffic.report(inst["id"], 0, 0, now=t0, svc="active")
    mgr.check_alerts(now=t0 + 1)
    assert notes == []
    # nyanpass 停止：未满 60 秒不报，满了报一次
    mgr.traffic.report(inst["id"], 10, 10, now=t0 + 5, svc="inactive")
    mgr.check_alerts(now=t0 + 6)
    mgr.traffic.report(inst["id"], 20, 20, now=t0 + 60, svc="inactive")
    mgr.check_alerts(now=t0 + 61)
    assert notes == []
    mgr.traffic.report(inst["id"], 30, 30, now=t0 + 70, svc="inactive")
    mgr.check_alerts(now=t0 + 71)
    mgr.check_alerts(now=t0 + 72)
    assert len(notes) == 1 and "nyanpass 已停止" in notes[0]
    # 恢复
    mgr.traffic.report(inst["id"], 40, 40, now=t0 + 80, svc="active")
    mgr.check_alerts(now=t0 + 81)
    assert "已恢复正常" in notes[-1]
    # 失联：超过 60 秒没上报
    mgr.check_alerts(now=t0 + 200)
    assert "失联" in notes[-1]
    mgr.check_alerts(now=t0 + 260)
    assert sum("失联" in n for n in notes) == 1
    # 上报恢复
    assert mgr.traffic_report(token, 50, 50, "active")
    mgr.check_alerts()
    assert "已恢复正常" in notes[-1]
    # 非法服务状态值被忽略
    assert mgr.traffic_report(token, 60, 60, "bad state!")
