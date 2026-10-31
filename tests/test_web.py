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


def test_exec_api(db, fake_gcp, monkeypatch, tmp_path):
    import time as _t

    from app.remote import SSHKey

    def runner(host, script, key_path, on_output, **kw):
        on_output("out-" + script)
        return 0

    monkeypatch.setattr(web, "GCPClient", lambda key: fake_gcp)
    mgr = Manager(db, FakePool(fake_gcp), ssh_key=SSHKey(tmp_path), ssh_runner=runner)
    app = web.create_app(db=db, manager=mgr, password="pw123", enable_monitor=False)
    with TestClient(app) as c:
        h = login(c)
        c.post("/api/accounts", json={"name": "main", "key": FAKE_KEY}, headers=h)
        iid = c.get("/api/instances", headers=h).json()[0]["id"]

        assert c.post(f"/api/instances/{iid}/exec", json={"script": ""}, headers=h).status_code == 400
        job = c.post(f"/api/instances/{iid}/exec", json={"script": "ls"}, headers=h).json()
        for _ in range(100):
            r = c.get(f"/api/jobs/{job['id']}?offset=0", headers=h).json()
            if r["status"] != "running":
                break
            _t.sleep(0.02)
        assert r["status"] == "ok" and "out-ls" in r["output"]
        tail = c.get(f"/api/jobs/{job['id']}?offset={r['offset']}", headers=h).json()
        assert tail["output"] == ""
        assert c.get(f"/api/jobs?instance_id={iid}", headers=h).json()[0]["id"] == job["id"]
        assert c.get("/api/jobs/999", headers=h).status_code == 404

        r = c.put(f"/api/instances/{iid}/startup-script", json={"script": "echo hi"}, headers=h)
        assert r.status_code == 200, r.text
        assert c.get(f"/api/instances/{iid}/startup-script", headers=h).json()["script"] == "echo hi"
        assert c.delete(f"/api/instances/{iid}/startup-script", headers=h).status_code == 200
        assert c.get(f"/api/instances/{iid}/serial-log", headers=h).status_code == 200
        assert c.get("/api/ssh-key", headers=h).json()["public_key"].startswith("ssh-ed25519")

        presets = [{"name": "a", "script": "ls"}, {"name": "", "script": "x"}]
        s = c.put("/api/settings", json={"script_presets": presets}, headers=h).json()
        assert s["script_presets"] == [{"name": "a", "script": "ls"}]


def test_firewall_api(client):
    h = login(client)
    aid = client.post("/api/accounts", json={"name": "main", "key": FAKE_KEY}, headers=h).json()["id"]
    r = client.get(f"/api/accounts/{aid}/firewalls", headers=h).json()
    assert r["rules"][0]["name"] == "default-allow-ssh"
    assert client.post(f"/api/accounts/{aid}/firewalls", json={"name": "x", "rules": "tcp:0"}, headers=h).status_code == 400
    assert client.post(f"/api/accounts/{aid}/firewalls", json={"name": "allow-all", "rules": "all"}, headers=h).status_code == 200
    assert client.patch(f"/api/accounts/{aid}/firewalls/allow-all", json={"disabled": True}, headers=h).status_code == 200
    assert client.delete(f"/api/accounts/{aid}/firewalls/allow-all", headers=h).status_code == 200
    assert len(client.get(f"/api/accounts/{aid}/firewalls", headers=h).json()["rules"]) == 1


def test_create_delete_instance_api(client, fake_gcp):
    h = login(client)
    aid = client.post("/api/accounts", json={"name": "main", "key": FAKE_KEY}, headers=h).json()["id"]
    assert client.get(f"/api/accounts/{aid}/create-options", headers=h).json()["zones"][0]["name"] == "asia-east1-b"
    assert client.get(f"/api/accounts/{aid}/machine-types?zone=asia-east1-b", headers=h).status_code == 200
    r = client.post(f"/api/accounts/{aid}/instances", json={"name": "vm-new", "zone": "asia-east1-b", "machine_type": "e2-small"}, headers=h)
    assert r.status_code == 200, r.text
    iid = r.json()["instance"]["id"]
    assert client.request("DELETE", f"/api/instances/{iid}", json={"confirm_name": "nope"}, headers=h).status_code == 400
    assert client.request("DELETE", f"/api/instances/{iid}", json={"confirm_name": "vm-new"}, headers=h).status_code == 200
    assert len(client.get("/api/instances", headers=h).json()) == 1


def test_traffic_api(client, db):
    h = login(client)
    client.post("/api/accounts", json={"name": "main", "key": FAKE_KEY}, headers=h)
    iid = client.get("/api/instances", headers=h).json()[0]["id"]
    client.patch(f"/api/instances/{iid}", json={"grp": "香港"}, headers=h)
    r = client.post(f"/api/instances/{iid}/traffic-agent", json={"panel_url": "https://p.example.com"}, headers=h)
    assert r.status_code == 200 and "gcpvm-traffic" in r.json()["script"]
    assert client.post(f"/api/instances/{iid}/traffic-agent", json={"panel_url": "bad"}, headers=h).status_code == 400
    token = db.get_instance(iid)["traffic_token"]
    assert client.post("/api/traffic-agent/report", json={"rx": 1, "tx": 1}, headers={"Authorization": "Bearer x"}).status_code == 401
    assert client.post("/api/traffic-agent/report", json={"rx": 1, "tx": 1}, headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert client.get("/api/traffic", headers=h).json()[str(iid)]["online"] is True

    assert client.get("/api/public/traffic?key=x").status_code == 404  # 未开启
    assert client.put("/api/settings", json={"status_page_enabled": True, "status_page_key": "short"}, headers=h).status_code == 400
    key = "k" * 24
    assert client.put("/api/settings", json={"status_page_enabled": True, "status_page_key": key}, headers=h).status_code == 200
    assert client.get("/api/public/traffic?key=wrong").status_code == 404
    data = client.get(f"/api/public/traffic?key={key}").json()
    assert data["groups"][0]["name"] == "香港" and data["groups"][0]["online"] == 1
    assert "34.0.0.1" not in str(data) and "vm1" not in str(data)
    assert client.get("/status").status_code == 200
