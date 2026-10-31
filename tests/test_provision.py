import pytest

from app.manager import Manager, ManagerError
from app.provision import build_instance_body, filter_machine_types, sort_zones
from app.remote import SSHKey
from tests.conftest import FAKE_KEY, FakePool


def test_sort_zones_and_machine_types(fake_gcp):
    zones = sort_zones(fake_gcp.list_zones())
    assert [z["name"] for z in zones] == ["asia-east1-b", "asia-east2-a", "us-central1-a"]  # 常用区域在前，DOWN 的去掉
    assert zones[0]["label"] == "台湾 · asia-east1-b"
    mts = filter_machine_types(fake_gcp.list_machine_types("asia-east1-b"))
    assert [m["name"] for m in mts] == ["e2-micro", "e2-small", "n1-standard-1"]  # 过滤超大/非常用/已弃用
    assert mts[0]["label"] == "e2-micro · 2 vCPU（共享） · 1 GB"


def test_build_instance_body():
    zone, b = build_instance_body(
        {"name": "vm-1", "zone": "asia-east1-b", "machine_type": "e2-small", "image": "ubuntu-2404",
         "disk_size_gb": 30, "spot": True, "network_tier": "standard", "tags": "a, b"},
        "gcpvm:ssh-ed25519 AAA gcpvm", "echo hi",
    )
    assert zone == "asia-east1-b"
    assert b["machineType"] == "zones/asia-east1-b/machineTypes/e2-small"
    init = b["disks"][0]["initializeParams"]
    assert init["sourceImage"].endswith("ubuntu-2404-lts-amd64") and init["diskSizeGb"] == "30"
    assert b["networkInterfaces"][0]["accessConfigs"][0]["networkTier"] == "STANDARD"
    assert b["scheduling"]["provisioningModel"] == "SPOT" and b["tags"] == {"items": ["a", "b"]}
    meta = {i["key"]: i["value"] for i in b["metadata"]["items"]}
    assert meta == {"ssh-keys": "gcpvm:ssh-ed25519 AAA gcpvm", "enable-oslogin": "FALSE", "startup-script": "echo hi"}
    assert "serviceAccounts" not in b

    _, plain = build_instance_body({"name": "vm-2", "zone": "us-central1-a", "machine_type": "e2-micro"}, "k")
    assert "scheduling" not in plain and "tags" not in plain
    assert plain["disks"][0]["initializeParams"]["sourceImage"].endswith("debian-12")

    base = {"name": "vm-3", "zone": "us-central1-a", "machine_type": "e2-micro"}
    for bad in ({"name": "VM_1"}, {"zone": "bad"}, {"machine_type": ""}, {"image": "windows"},
                {"disk_size_gb": 5}, {"disk_type": "hyperdisk"}, {"network_tier": "gold"}, {"tags": "Bad_Tag"}):
        with pytest.raises(ValueError):
            build_instance_body(base | bad, "k")


def make(db, fake_gcp, tmp_path):
    acc = db.add_account("main", FAKE_KEY)
    mgr = Manager(db, FakePool(fake_gcp), ssh_key=SSHKey(tmp_path))
    mgr.sync_all()
    return acc["id"], mgr


def test_create_instance(db, fake_gcp, tmp_path):
    aid, mgr = make(db, fake_gcp, tmp_path)
    opts = mgr.create_options(aid)
    assert opts["zones"][0]["name"] == "asia-east1-b" and opts["images"][0]["id"] == "debian-12"
    assert mgr.machine_types(aid, "asia-east1-b")[0]["name"] == "e2-micro"

    inst = mgr.create_instance(aid, {
        "name": "vm-new", "zone": "asia-east2-a", "machine_type": "e2-small", "spot": True,
        "check_port": 443, "startup_script": "apt-get install -y curl\r\n", "note": "测试",
    })
    assert inst["name"] == "vm-new" and inst["external_ip"].startswith("35.1.0.")
    assert inst["auto_ip"] == 1 and inst["auto_start"] == 1 and inst["check_port"] == 443 and inst["note"] == "测试"
    meta = fake_gcp.metadata[("asia-east2-a", "vm-new")]
    assert meta["ssh-keys"].startswith("gcpvm:ssh-ed25519 ")
    assert "run-once wrapper" in meta["startup-script"]
    assert len(db.list_instances()) == 2
    assert any(e["type"] == "create" for e in db.list_events())

    with pytest.raises(ManagerError, match="名称已存在"):
        mgr.create_instance(aid, {"name": "vm-new", "zone": "asia-east2-a", "machine_type": "e2-small"})
    with pytest.raises(ValueError):
        mgr.create_instance(aid, {"name": "x", "zone": "asia-east2-a", "machine_type": "e2-small", "disk_size_gb": 1})


def test_delete_instance_releases_own_static_ip(db, fake_gcp, tmp_path):
    aid, mgr = make(db, fake_gcp, tmp_path)
    iid = db.list_instances()[0]["id"]
    db.update_instance_config(iid, {"ip_mode": "static"})
    mgr.change_ip(iid)  # 预留 gcpvm- 静态 IP
    assert len(fake_gcp.addresses) == 1
    mgr.delete_instance(iid)
    assert ("delete", "vm1") in fake_gcp.calls
    assert fake_gcp.addresses == {} and db.list_instances() == []
    ev = [e for e in db.list_events() if e["type"] == "delete"][0]
    assert "释放静态 IP" in ev["message"]
    with pytest.raises(ManagerError):
        mgr.delete_instance(iid)
