import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from probe import probe


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(probe, "TOKEN", "t" * 20)
    monkeypatch.setattr(probe, "ALLOW_PRIVATE", True)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), probe.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def call(url, body, token="t" * 20):
    req = urllib.request.Request(
        url + "/probe", data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_probe_agent(server):
    target = socket.socket()
    target.bind(("127.0.0.1", 0))
    target.listen()
    port = target.getsockname()[1]
    code, data = call(server, {"host": "127.0.0.1", "port": port, "timeout": 2, "tries": 1})
    assert code == 200 and data["ok"] is True
    target.close()
    code, data = call(server, {"host": "127.0.0.1", "port": port, "timeout": 1, "tries": 1})
    assert code == 200 and data["ok"] is False
    assert call(server, {"host": "127.0.0.1", "port": port}, token="wrong")[0] == 401
    assert call(server, {"host": "example.com", "port": 22})[0] == 403


def test_private_targets_blocked_by_default(monkeypatch):
    monkeypatch.setattr(probe, "ALLOW_PRIVATE", False)
    assert not probe.target_allowed("10.0.0.1")
    assert not probe.target_allowed("127.0.0.1")
    assert probe.target_allowed("8.8.8.8")
