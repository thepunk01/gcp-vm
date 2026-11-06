import time
import tempfile

import pytest

from app import azure as az
from app.checker import CheckResult
from app.gcp import GCPError
from app.manager import Manager, ManagerError
from app.remote import SSHKey
from tests.conftest import FAKE_KEY, FakeGCP, FakePool

AZ_KEY = {
    "appId": "11111111-1111-1111-1111-111111111111", "password": "secret-xyz",
    "tenant": "22222222-2222-2222-2222-222222222222", "subscription_id": "33333333-3333-3333-3333-333333333333",
}


class FakeAzure:
    def __init__(self):
        self.ip_seq = iter([f"20.0.0.{i}" for i in range(1, 200)])
        self.pips = {"pip-old": {"ip": "20.9.9.9", "attached": True}}
        self.vms = {("rg1", "az1"): {"status": "RUNNING", "pip": "pip-old", "loc": "japaneast"}}
        self.calls = []
        self.run_out = "gcpvm-key-ok"

    def _sum(self, rg, name):
        v = self.vms[(rg, name)]
        return {
            "project": rg, "zone": v["loc"], "name": name, "machine_type": "Standard_B2s", "status": v["status"],
            "external_ip": self.pips[v["pip"]]["ip"] if v["pip"] else "", "internal_ip": "10.10.0.4",
            "network_tier": "Standard", "preemptible": False, "nic_id": f"/nic/{name}", "ipconfig_name": "ipconfig1",
            "pip_id": v["pip"] or "", "pip_name": v["pip"] or "", "pip_zones": [],
        }

    def list_instances(self):
        return [self._sum(rg, n) for (rg, n) in self.vms]

    def get_instance(self, rg, name):
        return self._sum(rg, name)

    def start(self, rg, name):
        self.calls.append(("start", name)); self.vms[(rg, name)]["status"] = "RUNNING"

    def stop(self, rg, name):
        self.calls.append(("stop", name)); self.vms[(rg, name)]["status"] = "TERMINATED"

    def reset(self, rg, name):
        self.calls.append(("reset", name))

    def create_public_ip(self, rg, loc, name, zones=None):
        ip = next(self.ip_seq)
        self.pips[name] = {"ip": ip, "attached": False}
        return name, ip

    def set_nic_public_ip(self, nic_id, ipconfig, pid):
        vm = next(v for (rg, n), v in self.vms.items() if nic_id == f"/nic/{n}")
        if vm["pip"]:
            self.pips[vm["pip"]]["attached"] = False
        vm["pip"] = pid
        self.pips[pid]["attached"] = True

    def list_public_ips(self, rg):
        return [{"id": k, "name": k, "address": v["ip"], "attached": v["attached"]} for k, v in self.pips.items()]

    def delete_resource(self, rid, api=None):
        self.calls.append(("delete_res", rid))
        assert not self.pips[rid]["attached"]
        del self.pips[rid]

    def run_command(self, rg, name, script, timeout=600):
        self.calls.append(("run_command", name))
        return self.run_out

    def list_zones(self):
        return [{"name": "japaneast", "displayName": "Japan East", "metadata": {"regionType": "Physical", "geographyGroup": "Asia Pacific"}},
                {"name": "brazilsouth", "displayName": "Brazil South", "metadata": {"regionType": "Physical", "geographyGroup": "South America"}},
                {"name": "asia", "displayName": "Asia", "metadata": {"regionType": "Logical"}}]

    def list_machine_types(self, loc):
        def sku(name, cpu, mem, restrictions=()):
            return {"resourceType": "virtualMachines", "name": name, "locations": [loc], "restrictions": list(restrictions),
                    "capabilities": [{"name": "vCPUs", "value": str(cpu)}, {"name": "MemoryGB", "value": str(mem)}]}
        return [sku("Standard_B2s", 2, 4), sku("Standard_F4s_v2", 4, 8),
                sku("Standard_B1s", 1, 1, [{"reasonCode": "NotAvailableForSubscription"}]), sku("Standard_M128", 128, 2000)]

    def ensure_network(self, rg, loc):
        self.calls.append(("ensure_network", rg))
        return "subnet-id", "nsg-id"

    def insert_instance(self, rg, name, body):
        self.calls.append(("insert", name))
        self.body = body
        pid = f"{name}-ip"
        self.pips[pid] = {"ip": next(self.ip_seq), "attached": True}
        self.vms[(rg, name)] = {"status": "RUNNING", "pip": pid, "loc": body["location"]}

    def delete_instance(self, rg, name):
        self.calls.append(("delete", name))
        del self.vms[(rg, name)]
        return ["nic", "pip", "disk"]


def make_checker(state):
    class C:
        def __init__(self, settings):
            pass

        def check(self, host, port):
            return CheckResult(state[0], detail="test")
    return C


def ok_runner(host, script, key_path, on_output, **kw):
    on_output("ran\n")
    return 0


@pytest.fixture
def azenv(db):
    fake_gcp, fake_az = FakeGCP(), FakeAzure()
    db.update_settings({"verify_new_ip": False})
    mgr = Manager(db, FakePool(fake_gcp, azure=fake_az), checker_cls=make_checker(["ok"]),
                  ssh_key=SSHKey(tempfile.mkdtemp()), ssh_runner=ok_runner)
    acc = db.add_account("az", az.validate_azure_key(AZ_KEY), "azure")
    mgr.sync_account(acc["id"])
    inst = next(i for i in db.list_instances() if i["provider"] == "azure")
    return mgr, db, fake_az, inst, acc


def test_validate_azure_key():
    k = az.validate_azure_key(AZ_KEY)
    assert k["client_id"] == AZ_KEY["appId"] and k["tenant_id"] == AZ_KEY["tenant"] and k["client_secret"] == "secret-xyz"
    with pytest.raises(ValueError):
        az.validate_azure_key(AZ_KEY | {"subscription_id": "nope"})


def test_summarize_and_helpers():
    vm = {"id": "/subscriptions/s/resourceGroups/RG1/providers/Microsoft.Compute/virtualMachines/v1", "name": "v1",
          "location": "japaneast", "properties": {"hardwareProfile": {"vmSize": "Standard_B2s"}, "priority": "Spot",
          "networkProfile": {"networkInterfaces": [{"id": "/nic/1", "properties": {"primary": True}}]}}}
    nics = {"/nic/1": {"properties": {"ipConfigurations": [{"name": "ipc", "properties": {
        "privateIPAddress": "10.0.0.4", "publicIPAddress": {"id": "/PIP/1"}}}]}}}
    pips = {"/pip/1": {"name": "p1", "sku": {"name": "Standard"}, "properties": {"ipAddress": "1.2.3.4"}}}
    s = az.summarize_vm(vm, [{"code": "ProvisioningState/succeeded"}, {"code": "PowerState/deallocated"}], nics, pips)
    assert s["project"] == "RG1" and s["status"] == "TERMINATED" and s["external_ip"] == "1.2.3.4"
    assert s["preemptible"] and s["ipconfig_name"] == "ipc" and s["internal_ip"] == "10.0.0.4"
    sizes = az.filter_sizes(FakeAzure().list_machine_types("japaneast"), "japaneast")
    assert [m["name"] for m in sizes] == ["Standard_B2s", "Standard_F4s_v2"]
    locs = az.sort_locations(FakeAzure().list_zones())
    assert [l["name"] for l in locs] == ["japaneast", "brazilsouth"] and locs[0]["region"] == "常用"


def test_build_vm_body():
    loc, body = az.build_vm_body({"name": "vm-a", "zone": "japaneast", "machine_type": "Standard_B2s", "spot": True},
                                 "ssh-ed25519 AAA", "sub", "nsg", "#!/bin/bash\necho hi")
    p = body["properties"]
    assert loc == "japaneast" and p["priority"] == "Spot" and p["osProfile"]["customData"]
    nic = p["networkProfile"]["networkInterfaceConfigurations"][0]["properties"]
    assert nic["deleteOption"] == "Delete" and nic["networkSecurityGroup"]["id"] == "nsg"
    with pytest.raises(ValueError):
        az.build_vm_body({"name": "Bad_Name", "zone": "japaneast", "machine_type": "x1"}, "k", "s", "n")


def test_azure_sync_power_and_change_ip(azenv):
    mgr, db, fake, inst, _ = azenv
    assert inst["project"] == "rg1" and inst["zone"] == "japaneast" and inst["external_ip"] == "20.9.9.9"
    assert mgr.power(inst["id"], "stop")["status"] == "TERMINATED"
    assert mgr.power(inst["id"], "start")["status"] == "RUNNING"
    new = mgr.change_ip(inst["id"])
    assert new["external_ip"] == "20.0.0.1"
    assert "pip-old" not in fake.pips  # 旧 IP 已删除
    db.add_blocked_ip("20.0.0.2", None)  # 下一个分配的是已知被墙 IP，应跳过并删除
    new = mgr.change_ip(inst["id"], reason="自动")
    assert new["external_ip"] == "20.0.0.3"
    assert set(fake.pips) == {next(k for k, v in fake.pips.items() if v["ip"] == "20.0.0.3")}


def test_azure_change_ip_verify_rejects(azenv, db):
    mgr, db, fake, inst, _ = azenv
    db.update_settings({"verify_new_ip": True})
    seq = iter(["blocked", "ok"])

    class C:
        def __init__(self, s):
            pass

        def check(self, host, port):
            return CheckResult(next(seq), detail="v")
    mgr.checker_cls = C
    new = mgr.change_ip(inst["id"])
    assert new["external_ip"] == "20.0.0.2" and new["cn_state"] == "ok"
    assert len(fake.pips) == 1
    assert "20.0.0.1" in db.recent_blocked_ips()


def test_azure_exec_installs_key(azenv):
    import time
    mgr, db, fake, inst, _ = azenv
    job = mgr.start_exec(inst["id"], "ls")
    for _ in range(100):
        if job.status != "running":
            break
        time.sleep(0.02)
    assert job.status == "ok", job.output
    assert ("run_command", "az1") in fake.calls
    assert db.get_instance(inst["id"])["ssh_user"] == "root"
    with pytest.raises(ManagerError):
        mgr.get_startup_script(inst["id"])


def test_azure_create_and_delete(azenv):
    mgr, db, fake, _, acc = azenv
    opts = mgr.create_options(acc["id"])
    assert opts["provider"] == "azure" and opts["disk_types"][0] == "StandardSSD_LRS"
    assert mgr.machine_types(acc["id"], "japaneast")[0]["name"] == "Standard_B2s"
    inst = mgr.create_instance(acc["id"], {"name": "vm-new", "zone": "japaneast", "machine_type": "Standard_B2s",
                                           "disk_size_gb": 30, "startup_script": "echo hi"})
    assert inst["project"] == "gcpvm-japaneast" and inst["external_ip"] and inst["ssh_user"] == "root"
    import base64
    custom = base64.b64decode(fake.body["properties"]["osProfile"]["customData"]).decode()
    assert "gcpvm-panel" in custom and "echo hi" in custom
    mgr.delete_instance(inst["id"])
    assert db.get_instance(inst["id"]) is None and ("delete", "vm-new") in fake.calls
    with pytest.raises(ManagerError):
        mgr.list_firewalls(acc["id"])


# ---------------- 自动替补 ----------------
@pytest.fixture
def renv(db):
    a, b, c = FakeGCP(), FakeGCP(), FakeGCP()
    b.instances, c.instances = {}, {}
    b.ip_seq = iter([f"35.2.0.{i}" for i in range(1, 50)])
    c.ip_seq = iter([f"35.3.0.{i}" for i in range(1, 50)])
    db.update_settings({
        "verify_new_ip": False, "replace_enabled": True, "replace_max_per_account": 1,
        "panel_url": "https://panel.example.com",
        "replace_template": {"zone": "asia-east1-b", "machine_type": "e2-highcpu-4", "name_prefix": "rp",
                             "startup_script": "echo nyanpass", "install_agent": True, "ip_mode": "static",
                             "follow_original": False},
    })
    accs = [db.add_account(n, FAKE_KEY) for n in ("a", "b", "c")]
    pool = FakePool(a, by_account={accs[0]["id"]: a, accs[1]["id"]: b, accs[2]["id"]: c})
    mgr = Manager(db, pool, checker_cls=make_checker(["ok"]))
    for acc in accs:
        mgr.sync_account(acc["id"])
    inst = db.list_instances()[0]
    db.update_instance_config(inst["id"], {"auto_replace": True, "grp": "台湾入口", "check_port": 443})
    return mgr, db, (a, b, c), accs


def test_replace_when_instance_deleted(renv):
    mgr, db, (a, b, c), accs = renv
    del a.instances[("asia-east1-b", "vm1")]
    mgr.replace_cycle()
    rows = db.list_instances()
    assert len(rows) == 1
    new = rows[0]
    assert new["account_id"] == accs[1]["id"] and new["name"].startswith("rp-")
    assert new["grp"] == "台湾入口" and new["check_port"] == 443 and new["auto_replace"] == 1 and new["traffic_token"]
    script = b.metadata[("asia-east1-b", new["name"])]["startup-script"]
    import base64, re
    inner = base64.b64decode(re.search(r"echo '([^']+)' \| base64 -d", script).group(1)).decode()
    assert "echo nyanpass" in inner and new["traffic_token"] in inner and "panel.example.com" in inner
    assert b.inserted["machineType"].endswith("e2-highcpu-4")
    assert any(e["type"] == "replace_new" for e in db.list_events())


def test_replace_when_account_banned(renv):
    mgr, db, (a, b, c), accs = renv

    def boom():
        raise GCPError("GET instances 失败：HTTP 403 Project proj-1 has been suspended")
    a.list_instances = boom
    mgr.replace_cycle()  # 第 1 次：只记录
    assert db.get_account(accs[0]["id"])["status"] == "ok"
    assert len(db.list_instances()) == 1
    mgr.replace_cycle()  # 第 2 次：判定被封并替补
    assert db.get_account(accs[0]["id"])["status"] == "banned"
    rows = db.list_instances()
    assert len(rows) == 1 and rows[0]["account_id"] == accs[1]["id"]
    # 账号 b 已满（上限 1），下次替补应使用 c
    del b.instances[("asia-east1-b", rows[0]["name"])]
    mgr.replace_cycle()
    rows = db.list_instances()
    assert len(rows) == 1 and rows[0]["account_id"] == accs[2]["id"]
    # c 上的再丢一台：b 已空出位置，替补回 b
    del c.instances[("asia-east1-b", rows[0]["name"])]
    mgr.replace_cycle()
    rows = db.list_instances()
    assert len(rows) == 1 and rows[0]["account_id"] == accs[1]["id"]
    # c 也被封，再丢一台：已无账号可用，保留 LOST 记录并告警
    db.set_account_status(accs[2]["id"], "banned")
    del b.instances[("asia-east1-b", rows[0]["name"])]
    mgr.replace_cycle()
    rows = db.list_instances()
    assert len(rows) == 1 and rows[0]["status"] == "LOST"
    assert any("没有可用的 GCP 账号" in e["message"] for e in db.list_events())
    mgr.reset_account_status(accs[0]["id"])
    assert db.get_account(accs[0]["id"])["status"] == "ok"


def test_network_error_does_not_ban(renv):
    mgr, db, (a, b, c), accs = renv

    def boom():
        raise GCPError("GET instances 失败：HTTP 503 backend error")
    a.list_instances = boom
    for _ in range(3):
        mgr.replace_cycle()
    assert db.get_account(accs[0]["id"])["status"] == "ok"
    assert len(db.list_instances()) == 1


def test_replace_disabled_removes_missing(renv):
    mgr, db, (a, b, c), accs = renv
    db.update_settings({"replace_enabled": False})
    del a.instances[("asia-east1-b", "vm1")]
    mgr.sync_account(accs[0]["id"])
    mgr.replace_cycle()
    assert db.list_instances() == []


def test_create_from_template_and_day_limit(renv):
    mgr, db, (a, b, c), accs = renv
    inst = mgr.create_from_template()
    assert inst["account_id"] == accs[1]["id"]
    inst = mgr.create_from_template()
    assert inst["account_id"] == accs[2]["id"]
    with pytest.raises(ManagerError):
        mgr.create_from_template()  # 全部开满
    db.update_settings({"replace_max_per_account": 3, "replace_max_per_day": 1})
    for (z, n) in list(b.instances):
        del b.instances[(z, n)]
    for i in db.list_instances():
        if i["account_id"] == accs[1]["id"]:
            db.update_instance_config(i["id"], {"auto_replace": True})
    del a.instances[("asia-east1-b", "vm1")]
    mgr.replace_cycle()
    lost = [i for i in db.list_instances() if i["status"] == "LOST"]
    assert len(lost) == 1  # 两台需要替补，每天上限 1 台
    assert any("达到上限" in e["message"] for e in db.list_events())


class _Resp:
    def __init__(self, status=200, data=None, headers=None):
        import json as _j
        self.status_code, self._d, self.headers = status, data, headers or {}
        self.content = _j.dumps(data).encode() if data is not None else b""
        self.text = self.content.decode()

    def json(self):
        return self._d


class _Session:
    def __init__(self, routes):
        self.routes, self.log = routes, []

    def post(self, url, data=None, timeout=None):
        assert "oauth2/v2.0/token" in url and data["grant_type"] == "client_credentials"
        return _Resp(200, {"access_token": "tok", "expires_in": 3600})

    def request(self, method, url, params=None, json=None, timeout=None, headers=None):
        assert headers["Authorization"] == "Bearer tok"
        self.log.append((method, url, dict(params or {})))
        for (m, frag), fn in self.routes.items():
            if m == method and frag in url and (frag != "/virtualMachines" or url.endswith(frag)):
                return fn(params or {}, json)
        raise AssertionError(f"unexpected {method} {url}")


def test_azure_client_http():
    sub = AZ_KEY["subscription_id"]
    vm_id = f"/subscriptions/{sub}/resourceGroups/rg1/providers/Microsoft.Compute/virtualMachines/v1"
    vm = {"id": vm_id, "name": "v1", "location": "japaneast", "properties": {
        "hardwareProfile": {"vmSize": "Standard_B2s"},
        "networkProfile": {"networkInterfaces": [{"id": "/x/nic1"}]}}}
    polls = iter([_Resp(200, {"status": "InProgress"}), _Resp(200, {"status": "Succeeded"})])
    routes = {
        ("GET", "/virtualMachines"): lambda p, b: _Resp(200, {"value": [
            vm | ({"properties": {"instanceView": {"statuses": [{"code": "PowerState/running"}]}}} if p.get("statusOnly") else {})]}),
        ("GET", "/networkInterfaces"): lambda p, b: _Resp(200, {"value": [{"id": "/x/nic1", "properties": {"ipConfigurations": [
            {"name": "ipconfig1", "properties": {"privateIPAddress": "10.0.0.4", "publicIPAddress": {"id": "/x/pip1"}}}]}}]}),
        ("GET", "/publicIPAddresses"): lambda p, b: _Resp(200, {"value": [{"id": "/x/pip1", "name": "pip1", "properties": {"ipAddress": "4.4.4.4"}}],
                                                               "nextLink": None}),
        ("POST", "/start"): lambda p, b: _Resp(202, None, {"Azure-AsyncOperation": "https://management.azure.com/op/1", "Retry-After": "0"}),
        ("GET", "/op/1"): lambda p, b: next(polls),
    }
    sess = _Session(routes)
    c = az.AzureClient(AZ_KEY, session=sess)
    c._sleep = lambda s: None
    items = c.list_instances()
    assert items[0]["status"] == "RUNNING" and items[0]["external_ip"] == "4.4.4.4" and items[0]["project"] == "rg1"
    c.start("rg1", "v1")
    assert sum(1 for m, u, _ in sess.log if "/op/1" in u) == 2
    assert all(p.get("api-version") for m, u, p in sess.log if "/op/" not in u)


def _inner_script(fake, name):
    import base64, re
    script = fake.metadata[("asia-east1-b", name)]["startup-script"]
    return base64.b64decode(re.search(r"echo '([^']+)' \| base64 -d", script).group(1)).decode()


def test_replace_uses_group_template_and_boot_scripts(renv):
    mgr, db, (a, b, c), accs = renv
    db.update_settings({
        "script_presets": [{"name": "装 nyanpass", "script": "echo install-ny"}, {"name": "开启 BBR", "script": "echo bbr"}],
        "replace_templates": {"台湾入口": {"zone": "asia-east1-b", "machine_type": "e2-standard-2", "name_prefix": "tw",
                                         "startup_script": "echo tw-group", "install_agent": False,
                                         "follow_original": False}},
    })
    old = db.list_instances()[0]
    db.update_instance_config(old["id"], {"boot_scripts": ["开启 BBR", "装 nyanpass", "已删除的脚本"]})
    del a.instances[("asia-east1-b", "vm1")]
    mgr.replace_cycle()
    new = db.list_instances()[0]
    assert new["name"].startswith("tw-") and b.inserted["machineType"].endswith("e2-standard-2")
    inner = _inner_script(b, new["name"])
    assert inner.index("echo tw-group") < inner.index("echo bbr") < inner.index("echo install-ny")
    assert "panel.example.com" not in inner  # 分组模板关闭了上报安装
    import json
    assert json.loads(new["boot_scripts"]) == ["开启 BBR", "装 nyanpass", "已删除的脚本"] and new["grp"] == "台湾入口"
    assert any("已跳过：已删除的脚本" in e["message"] for e in db.list_events())


def test_group_without_template_uses_default_and_manual_group_create(renv):
    mgr, db, (a, b, c), accs = renv
    db.update_settings({"replace_templates": {"香港": {"name_prefix": "hk", "zone": "asia-east1-b", "machine_type": "e2-micro"}}})
    inst = mgr.create_from_template("香港")
    assert inst["name"].startswith("hk-") and inst["grp"] == "香港"
    inst = mgr.create_from_template("")
    assert inst["name"].startswith("rp-")


def test_replace_follows_original_then_falls_back(renv):
    from app.gcp import GCPError
    mgr, db, (a, b, c), accs = renv
    tpl = db.get_settings()["replace_template"] | {"follow_original": True}
    db.update_settings({"replace_template": tpl})
    # 原实例 vm1 是 asia-east1-b / e2-micro：沿用
    del a.instances[("asia-east1-b", "vm1")]
    mgr.replace_cycle()
    assert b.inserted["machineType"].endswith("e2-micro")
    # 原规格在新账号开不出来：同账号改用模板（e2-highcpu-4）
    first = db.list_instances()[0]
    orig_insert = c.insert_instance

    def picky(zone, body):
        if body["machineType"].endswith("e2-micro"):
            raise GCPError("POST instances 失败：HTTP 503 ZONE_RESOURCE_POOL_EXHAUSTED")
        return orig_insert(zone, body)
    c.insert_instance = picky
    del b.instances[("asia-east1-b", first["name"])]
    mgr.replace_cycle()
    new = db.list_instances()[0]
    assert new["account_id"] == accs[2]["id"] and c.inserted["machineType"].endswith("e2-highcpu-4")
    assert any("改用模板的可用区和机型重试" in e["message"] for e in db.list_events())


def _stop_vm1(mgr, db, a, accs, minutes=0.0):
    a.instances[("asia-east1-b", "vm1")]["status"] = "TERMINATED"
    db.update_settings({"replace_stopped_minutes": 1})
    mgr.sync_account(accs[0]["id"])
    iid = db.list_instances()[0]["id"]
    mgr._stopped_since[iid] = time.time() - 120  # 已停机 2 分钟
    return iid


def test_stopped_instance_is_restarted_first(renv):
    mgr, db, (a, b, c), accs = renv
    iid = _stop_vm1(mgr, db, a, accs)
    mgr.replace_cycle()
    assert db.get_instance(iid)["status"] == "RUNNING" and ("start", "vm1") in a.calls
    assert not b.instances


def test_stopped_billing_disabled_bans_account_and_replaces(renv):
    mgr, db, (a, b, c), accs = renv
    iid = _stop_vm1(mgr, db, a, accs)

    def no_billing(zone, name):
        raise GCPError("POST start 失败：HTTP 403 This API method requires billing to be enabled.")
    a.start = no_billing
    mgr.replace_cycle()
    assert db.get_account(accs[0]["id"])["status"] == "banned"
    rows = db.list_instances()
    assert len(rows) == 1 and rows[0]["account_id"] == accs[1]["id"]


def test_stopped_cannot_start_replaced_and_old_deleted(renv):
    mgr, db, (a, b, c), accs = renv
    iid = _stop_vm1(mgr, db, a, accs)

    def exhausted(zone, name):
        raise GCPError("POST start 失败：HTTP 503 ZONE_RESOURCE_POOL_EXHAUSTED")
    a.start = exhausted
    mgr.replace_cycle()
    assert db.get_account(accs[0]["id"])["status"] == "ok"
    assert ("asia-east1-b", "vm1") not in a.instances  # 旧实例已从云端删除
    rows = db.list_instances()
    assert len(rows) == 1 and rows[0]["account_id"] == accs[1]["id"]


def test_stopped_zero_minutes_disabled(renv):
    mgr, db, (a, b, c), accs = renv
    iid = _stop_vm1(mgr, db, a, accs)
    db.update_settings({"replace_stopped_minutes": 0})
    mgr.replace_cycle()
    assert db.get_instance(iid)["status"] == "TERMINATED" and not b.instances


def test_replace_recreates_extra_ips(renv):
    mgr, db, (a, b, c), accs = renv
    old = db.list_instances()[0]
    mgr.add_extra_ip(old["id"])
    mgr.add_extra_ip(old["id"])
    del a.instances[("asia-east1-b", "vm1")]
    mgr.replace_cycle()
    new = db.list_instances()[0]
    assert new["account_id"] == accs[1]["id"]
    assert len(db.list_extra_ips(new["id"])) == 2 and len(b.rules) == 2
    assert a.rules == {} and a.targets == {}  # 旧账号里的附加 IP 已清理
    assert any("附加 IP" in e["message"] and e["type"] == "replace_new" for e in db.list_events())


def test_change_ip_billing_disabled_bans_account_then_replaces(renv):
    mgr, db, (a, b, c), accs = renv
    inst = db.list_instances()[0]

    def no_billing(*args, **kw):
        raise GCPError("POST addAccessConfig 失败：HTTP 403 This API method requires billing to be enabled.")
    a.add_access_config = no_billing
    db.update_instance_config(inst["id"], {"ip_mode": "ephemeral"})
    with pytest.raises(ManagerError):
        mgr.change_ip(inst["id"], reason="自动-被墙")
    assert db.get_account(accs[0]["id"])["status"] == "banned"
    assert any("已判定为停用" in e["message"] for e in db.list_events())
    mgr.replace_cycle()
    rows = db.list_instances()
    assert len(rows) == 1 and rows[0]["account_id"] == accs[1]["id"]


def test_single_403_does_not_ban(renv):
    mgr, db, (a, b, c), accs = renv
    inst = db.list_instances()[0]

    def denied(*args, **kw):
        raise GCPError("POST start 失败：HTTP 403 Required 'compute.instances.start' permission")
    a.start = denied
    with pytest.raises(ManagerError):
        mgr.power(inst["id"], "start")
    assert db.get_account(accs[0]["id"])["status"] == "ok"
