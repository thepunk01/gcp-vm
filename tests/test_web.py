import pytest
from starlette.testclient import TestClient

from app import web
from app.manager import Manager
from tests.conftest import FAKE_KEY, FakePool


@pytest.fixture
def client(db, fake_gcp, monkeypatch):
    # 添加账号时会用真实 GCPClient 校验密钥，这里替换为假客户端
    monkeypatch.setattr(web, "GCPClient", lambda key: fake_gcp)
    mgr = Manager(db, FakePool(fake_gcp))
    app = web.create_app(db=db, manager=mgr, password="pw123", enable_monitor=False)
    with TestClient(app) as c:
        yield c


def login(c):
    r = c.post("/api/login", json={"password": "pw123"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_index_and_auth(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/api/instances").status_code == 401
    assert client.post("/api/login", json={"password": "bad"}).status_code == 401


def test_full_flow(client, fake_gcp):
    h = login(client)
    r = client.post("/api/accounts", json={"name": "main", "key": FAKE_KEY}, headers=h)
    assert r.status_code == 200, r.text
    insts = client.get("/api/instances", headers=h).json()
    assert len(insts) == 1
    iid = insts[0]["id"]

    r = client.patch(f"/api/instances/{iid}", json={"auto_ip": True, "check_port": 443}, headers=h)
    assert r.json()["auto_ip"] == 1 and r.json()["check_port"] == 443
    assert client.patch(f"/api/instances/{iid}", json={"check_port": 0}, headers=h).status_code == 400

    r = client.post(f"/api/instances/{iid}/action", json={"action": "stop"}, headers=h)
    assert r.json()["instance"]["status"] == "TERMINATED"
    r = client.post(f"/api/instances/{iid}/action", json={"action": "start"}, headers=h)
    assert r.json()["instance"]["status"] == "RUNNING"
    r = client.post(f"/api/instances/{iid}/action", json={"action": "change_ip"}, headers=h)
    assert r.json()["instance"]["external_ip"] == "35.1.0.1"

    ev = client.get("/api/events", headers=h).json()
    assert any(e["type"] == "change_ip" for e in ev)


def test_settings(client):
    h = login(client)
    r = client.put("/api/settings", json={"check_interval": 120, "probes": [{"name": "sh", "url": "http://1.1.1.1:9800", "token": "t"}, {"url": ""}]}, headers=h)
    assert r.status_code == 200
    s = client.get("/api/settings", headers=h).json()
    assert s["check_interval"] == 120 and len(s["probes"]) == 1
    assert client.put("/api/settings", json={"check_interval": 5}, headers=h).status_code == 400
    assert client.put("/api/settings", json={"detect_mode": "x"}, headers=h).status_code == 400


def test_bad_key(client):
    h = login(client)
    r = client.post("/api/accounts", json={"key": "{not json"}, headers=h)
    assert r.status_code == 400
    r = client.post("/api/accounts", json={"key": {"type": "x"}}, headers=h)
    assert r.status_code == 400
