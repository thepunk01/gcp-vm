import socket
import threading

from app.checker import BlockChecker, ProbeResult, tcp_probe
from app.config import DEFAULT_SETTINGS


def settings(**kw):
    s = dict(DEFAULT_SETTINGS)
    s["probes"] = [{"name": "p1", "url": "http://p1"}, {"name": "p2", "url": "http://p2"}]
    s.update(kw)
    return s


def agents(*oks):
    it = iter(oks)

    def fn(agent, host, port, timeout, tries):
        return ProbeResult(agent["name"], next(it))
    return fn


def tcp(ok):
    return lambda host, port, timeout, tries: (ok, 10.0 if ok else None, "" if ok else "timeout")


def test_ok_when_cn_reachable():
    r = BlockChecker(settings(), agents(True, True), tcp(True)).check("1.2.3.4", 22)
    assert r.state == "ok"


def test_blocked_when_cn_fails_but_control_ok():
    r = BlockChecker(settings(), agents(False, False), tcp(True)).check("1.2.3.4", 22)
    assert r.state == "blocked"
    assert r.control.ok


def test_down_when_both_fail():
    r = BlockChecker(settings(), agents(False, False), tcp(False)).check("1.2.3.4", 22)
    assert r.state == "down"


def test_unknown_when_probes_unavailable():
    r = BlockChecker(settings(), agents(None, None), tcp(True)).check("1.2.3.4", 22)
    assert r.state == "unknown"
    r = BlockChecker(settings(probes=[]), agents(), tcp(True)).check("1.2.3.4", 22)
    assert r.state == "unknown"


def test_unavailable_probe_is_ignored_in_ratio():
    # 一个探针挂了、另一个能连通 => ok
    r = BlockChecker(settings(), agents(None, True), tcp(True)).check("1.2.3.4", 22)
    assert r.state == "ok"


def test_partial_success_below_ratio_is_blocked():
    s = settings(cn_success_ratio=0.6)
    r = BlockChecker(s, agents(True, False), tcp(True)).check("1.2.3.4", 22)
    assert r.state == "blocked"


def test_local_mode_uses_local_tcp_without_control():
    r = BlockChecker(settings(detect_mode="local"), agents(), tcp(False)).check("1.2.3.4", 22)
    assert r.state == "blocked"
    assert r.control is None


def test_no_ip():
    assert BlockChecker(settings()).check("", 22).state == "unknown"


def test_real_tcp_probe():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port = srv.getsockname()[1]
    threading.Thread(target=lambda: srv.accept(), daemon=True).start()
    ok, latency, _ = tcp_probe("127.0.0.1", port, timeout=2, tries=1)
    assert ok and latency is not None
    srv.close()
    ok, _, err = tcp_probe("127.0.0.1", port, timeout=1, tries=1)
    assert not ok and err
