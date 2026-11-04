import socket
import threading
import time

from app.checker import STATE_BLOCKED, STATE_OK, STATE_UNKNOWN, BlockChecker
from app.config import DEFAULT_SETTINGS
from app.pushprobe import PushProbeHub
from probe import probe

TOKEN = "tok-" + "x" * 20


def make_hub(targets=(("1.2.3.4", 22),)):
    settings = dict(DEFAULT_SETTINGS, push_probes=[{"name": "home", "token": TOKEN}], probes=[])
    hub = PushProbeHub()
    hub.configure(lambda: settings, lambda: list(targets))
    return hub, settings


def test_auth_and_tasks():
    hub, _ = make_hub()
    assert hub.authenticate("bad") is None and hub.authenticate("") is None
    assert hub.authenticate(TOKEN) == "home"
    t = hub.tasks("home")
    assert t["targets"] == [{"host": "1.2.3.4", "port": 22}]
    assert t["timeout"] == 5 and t["tries"] == 3
    assert hub.status()[0]["online"]


def test_offline_probe_returns_unknown_immediately():
    hub, _ = make_hub()
    hub._started -= 1000  # 已过启动宽限期
    start = time.time()
    r = hub.result("home", "1.2.3.4", 22)
    assert r.ok is None and "离线" in r.error and time.time() - start < 1


def test_cached_and_waited_results():
    hub, settings = make_hub()
    hub.tasks("home")  # 探针上线
    hub.report("home", [{"host": "1.2.3.4", "port": 22, "ok": False, "error": "timed out"}])
    r = hub.result("home", "1.2.3.4", 22)
    assert r.ok is False and r.error == "timed out"

    # 新目标：登记为待测，探针下一轮领取后上报
    def fake_probe_round():
        time.sleep(0.2)
        targets = hub.tasks("home")["targets"]
        assert {"host": "5.6.7.8", "port": 443} in targets
        hub.report("home", [{"host": "5.6.7.8", "port": 443, "ok": True, "latency_ms": 31.5}])

    threading.Thread(target=fake_probe_round).start()
    r = hub.result("home", "5.6.7.8", 443, wait=5)
    assert r.ok is True and r.latency_ms == 31.5

    # 等待超时
    assert hub.result("home", "9.9.9.9", 80, wait=0.2).error.startswith("等待探针上报超时")


def test_startup_grace_waits_for_reconnect():
    hub, _ = make_hub()  # 面板刚启动，探针还没连上

    def reconnect():
        time.sleep(0.2)
        hub.tasks("home")
        hub.report("home", [{"host": "1.2.3.4", "port": 22, "ok": True, "latency_ms": 12}])

    threading.Thread(target=reconnect).start()
    r = hub.result("home", "1.2.3.4", 22, wait=5)
    assert r.ok is True
    assert hub.result("home", "7.7.7.7", 22, wait=0.1).ok is None


def test_checker_uses_push_probe():
    hub, settings = make_hub()
    hub.tasks("home")
    hub.report("home", [{"host": "1.2.3.4", "port": 22, "ok": False}])
    blocked = BlockChecker(settings, tcp_fn=lambda *a: (True, 10.0, ""), push_hub=hub).check("1.2.3.4", 22)
    assert blocked.state == STATE_BLOCKED and blocked.cn[0].source == "home"
    hub.report("home", [{"host": "1.2.3.4", "port": 22, "ok": True, "latency_ms": 40}])
    assert BlockChecker(settings, push_hub=hub).check("1.2.3.4", 22).state == STATE_OK
    # 没有任何探针
    empty = dict(settings, push_probes=[])
    assert BlockChecker(empty, push_hub=hub).check("1.2.3.4", 22).state == STATE_UNKNOWN


def test_probe_client_targets(monkeypatch):
    monkeypatch.setattr(probe, "ALLOW_PRIVATE", True)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port = srv.getsockname()[1]
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    dead = closed.getsockname()[1]
    closed.close()
    res = probe.probe_targets(
        [{"host": "127.0.0.1", "port": port}, {"host": "127.0.0.1", "port": dead}, {"host": "example.com", "port": 1}],
        timeout=1, tries=1,
    )
    srv.close()
    assert res[0]["ok"] is True and res[1]["ok"] is False
    assert res[2]["ok"] is None  # 域名不接受
