import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("GCPVM_SECRET_KEY", "test-secret-key-0123456789abcdef0123456789")
os.environ.setdefault("GCPVM_ENABLE_MONITOR", "0")

import pytest  # noqa: E402

from app.db import Database  # noqa: E402

FAKE_KEY = {
    "type": "service_account",
    "project_id": "proj-1",
    "private_key_id": "kid1",
    "private_key": "dummy",
    "client_email": "sa@proj-1.iam.gserviceaccount.com",
}


class FakeGCP:
    """内存版 GCPClient，模拟实例与 IP 分配。"""

    def __init__(self, key=None):
        self.project = "proj-1"
        self.ip_seq = iter([f"35.1.0.{i}" for i in range(1, 200)])
        self.instances = {
            ("asia-east1-b", "vm1"): {
                "status": "RUNNING", "ip": "34.0.0.1", "ac": "External NAT", "tier": "PREMIUM",
            }
        }
        self.addresses = {}  # name -> {address, region}
        self.metadata = {}  # (zone, name) -> {key: value}
        self.project_metadata = {}
        self.serial = ""
        self.firewalls = {
            "default-allow-ssh": {
                "name": "default-allow-ssh", "network": "https://x/global/networks/default",
                "direction": "INGRESS", "priority": 65534, "sourceRanges": ["0.0.0.0/0"],
                "allowed": [{"IPProtocol": "tcp", "ports": ["22"]}],
            }
        }
        self.calls = []
        self.fail_add = False

    def _sum(self, zone, name):
        i = self.instances[(zone, name)]
        return {
            "project": self.project, "zone": zone, "name": name, "machine_type": "e2-micro",
            "status": i["status"], "external_ip": i["ip"], "internal_ip": "10.0.0.2",
            "network_tier": i["tier"] if i["ac"] else "", "access_config_name": i["ac"],
            "nic_name": "nic0", "preemptible": False,
        }

    def list_instances(self):
        return [self._sum(z, n) for (z, n) in self.instances]

    def get_instance(self, zone, name):
        return self._sum(zone, name)

    def start(self, zone, name):
        self.calls.append(("start", name))
        self.instances[(zone, name)]["status"] = "RUNNING"

    def stop(self, zone, name):
        self.calls.append(("stop", name))
        self.instances[(zone, name)]["status"] = "TERMINATED"

    def reset(self, zone, name):
        self.calls.append(("reset", name))

    def delete_access_config(self, zone, name, nic, ac_name):
        self.calls.append(("delete_ac", ac_name))
        i = self.instances[(zone, name)]
        assert i["ac"] == ac_name
        i["ac"], i["ip"] = "", ""

    def add_access_config(self, zone, name, nic, ac_name="External NAT", nat_ip=None, network_tier=None):
        from app.gcp import GCPError
        self.calls.append(("add_ac", nat_ip))
        if self.fail_add:
            self.fail_add = False
            raise GCPError("quota exceeded")
        i = self.instances[(zone, name)]
        assert not i["ac"], "access config already exists"
        i["ac"], i["ip"] = ac_name, nat_ip or next(self.ip_seq)

    def get_metadata(self, zone, name):
        return dict(self.metadata.get((zone, name), {}))

    def set_metadata(self, zone, name, updates):
        self.calls.append(("set_metadata", tuple(sorted(updates))))
        md = self.metadata.setdefault((zone, name), {})
        for k, v in updates.items():
            if v is None:
                md.pop(k, None)
            else:
                md[k] = v

    def get_project_metadata(self):
        return dict(self.project_metadata)

    def get_serial_output(self, zone, name, start=None):
        return {"contents": self.serial, "start": 0, "next": len(self.serial)}

    def list_zones(self):
        return [
            {"name": "us-central1-a", "region": "us-central1", "status": "UP"},
            {"name": "asia-east1-b", "region": "asia-east1", "status": "UP"},
            {"name": "asia-east1-c", "region": "asia-east1", "status": "DOWN"},
            {"name": "asia-east2-a", "region": "asia-east2", "status": "UP"},
        ]

    def list_machine_types(self, zone):
        return [
            {"name": "e2-small", "cpus": 2, "memory_mb": 2048, "shared": True, "deprecated": False},
            {"name": "e2-micro", "cpus": 2, "memory_mb": 1024, "shared": True, "deprecated": False},
            {"name": "e2-standard-64", "cpus": 64, "memory_mb": 262144, "shared": False, "deprecated": False},
            {"name": "m1-ultramem-40", "cpus": 40, "memory_mb": 983040, "shared": False, "deprecated": False},
            {"name": "n1-standard-1", "cpus": 1, "memory_mb": 3840, "shared": False, "deprecated": False},
            {"name": "f1-micro", "cpus": 1, "memory_mb": 614, "shared": True, "deprecated": True},
        ]

    def insert_instance(self, zone, body):
        from app.gcp import GCPError
        if (zone, body["name"]) in self.instances:
            raise GCPError("POST instances 失败：HTTP 409 The resource 'x' already exists")
        self.calls.append(("insert", body["name"]))
        self.inserted = body
        tier = body["networkInterfaces"][0]["accessConfigs"][0]["networkTier"]
        self.instances[(zone, body["name"])] = {"status": "RUNNING", "ip": next(self.ip_seq), "ac": "External NAT", "tier": tier}
        self.metadata[(zone, body["name"])] = {i["key"]: i["value"] for i in body["metadata"]["items"]}

    def delete_instance(self, zone, name):
        self.calls.append(("delete", name))
        del self.instances[(zone, name)]

    def list_firewalls(self):
        return list(self.firewalls.values())

    def list_networks(self):
        return ["default"]

    def create_firewall(self, body):
        from app.gcp import GCPError
        if body["name"] in self.firewalls:
            raise GCPError("POST firewalls 失败：HTTP 409 The resource 'x' already exists")
        self.calls.append(("create_firewall", body["name"]))
        self.firewalls[body["name"]] = dict(body)

    def patch_firewall(self, name, body):
        self.firewalls[name].update(body)

    def delete_firewall(self, name):
        self.calls.append(("delete_firewall", name))
        del self.firewalls[name]

    def find_address_by_ip(self, region, ip):
        for n, a in self.addresses.items():
            if a["address"] == ip:
                return {"name": n, "address": ip}
        return None

    def reserve_address(self, region, name, network_tier=None):
        ip = next(self.ip_seq)
        self.addresses[name] = {"address": ip, "region": region}
        self.calls.append(("reserve", name))
        return ip

    def release_address(self, region, name):
        self.calls.append(("release", name))
        del self.addresses[name]


class FakePool:
    def __init__(self, client):
        self.client = client

    def get(self, account):
        return self.client

    def drop(self, account_id):
        pass


@pytest.fixture
def db():
    return Database(":memory:")


@pytest.fixture
def fake_gcp():
    return FakeGCP()
