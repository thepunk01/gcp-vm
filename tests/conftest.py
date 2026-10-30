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
