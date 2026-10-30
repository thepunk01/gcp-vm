import json

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.gcp import GCPClient, GCPError, region_of, validate_service_account_key

PRIV = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = PRIV.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
KEY = {
    "type": "service_account", "project_id": "p1", "private_key_id": "k1",
    "private_key": PEM, "client_email": "sa@p1.iam.gserviceaccount.com",
    "token_uri": "https://oauth2.googleapis.com/token",
}


class Resp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.text = json.dumps(data)
        self.content = self.text.encode()

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, routes):
        self.routes = routes  # list of (method, url-suffix, response)
        self.log = []

    def post(self, url, data=None, timeout=None):
        claims = jwt.decode(data["assertion"], PRIV.public_key(), algorithms=["RS256"], audience=KEY["token_uri"])
        assert claims["iss"] == KEY["client_email"]
        assert "compute" in claims["scope"]
        self.log.append(("TOKEN", url))
        return Resp(200, {"access_token": "tok", "expires_in": 3600})

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        assert headers["Authorization"] == "Bearer tok"
        self.log.append((method, url, params, json))
        for m, suffix, resp in self.routes:
            if m == method and url.endswith(suffix):
                return resp
        raise AssertionError(f"unexpected {method} {url}")


INST = {
    "name": "vm1", "zone": "https://x/zones/asia-east1-b", "status": "RUNNING",
    "machineType": "https://x/machineTypes/e2-small",
    "networkInterfaces": [{"name": "nic0", "networkIP": "10.0.0.2",
        "accessConfigs": [{"name": "external-nat", "natIP": "34.1.1.1", "networkTier": "STANDARD"}]}],
    "scheduling": {"provisioningModel": "SPOT"},
}
OP_DONE = {"status": "DONE", "selfLink": "https://compute.googleapis.com/compute/v1/projects/p1/zones/z/operations/op1"}


def test_validate_key():
    validate_service_account_key(KEY)
    with pytest.raises(ValueError):
        validate_service_account_key({"type": "authorized_user"})
    assert region_of("asia-east1-b") == "asia-east1"


def test_list_instances_and_token_cache():
    s = FakeSession([("GET", "/aggregated/instances", Resp(200, {"items": {
        "zones/asia-east1-b": {"instances": [INST]}, "zones/us-east1-b": {"warning": {}}}}))])
    c = GCPClient(KEY, session=s)
    items = c.list_instances()
    c.list_instances()
    assert len(items) == 1
    i = items[0]
    assert (i["zone"], i["external_ip"], i["network_tier"], i["access_config_name"], i["preemptible"]) == (
        "asia-east1-b", "34.1.1.1", "STANDARD", "external-nat", True)
    assert sum(1 for x in s.log if x[0] == "TOKEN") == 1


def test_operation_wait_and_error():
    pending = dict(OP_DONE, status="RUNNING")
    s = FakeSession([
        ("POST", "/instances/vm1/start", Resp(200, pending)),
        ("POST", "/operations/op1/wait", Resp(200, OP_DONE)),
    ])
    GCPClient(KEY, session=s).start("asia-east1-b", "vm1")
    assert s.log[-1][1].endswith("/wait")

    bad = dict(OP_DONE, error={"errors": [{"message": "ZONE_RESOURCE_POOL_EXHAUSTED"}]})
    s = FakeSession([("POST", "/instances/vm1/start", Resp(200, bad))])
    with pytest.raises(GCPError, match="EXHAUSTED"):
        GCPClient(KEY, session=s).start("asia-east1-b", "vm1")


def test_http_error():
    s = FakeSession([("GET", "/instances/vm1", Resp(403, {"error": {"message": "Permission denied"}}))])
    with pytest.raises(GCPError, match="Permission denied"):
        GCPClient(KEY, session=s).get_instance("asia-east1-b", "vm1")


def test_access_config_calls():
    s = FakeSession([
        ("POST", "/deleteAccessConfig", Resp(200, OP_DONE)),
        ("POST", "/addAccessConfig", Resp(200, OP_DONE)),
    ])
    c = GCPClient(KEY, session=s)
    c.delete_access_config("asia-east1-b", "vm1", "nic0", "external-nat")
    c.add_access_config("asia-east1-b", "vm1", "nic0", "external-nat", None, "STANDARD")
    _, _, params, _ = s.log[1]
    assert params == {"networkInterface": "nic0", "accessConfig": "external-nat"}
    _, _, params, body = s.log[2]
    assert body == {"name": "external-nat", "type": "ONE_TO_ONE_NAT", "networkTier": "STANDARD"}


def test_set_metadata_merges_and_retries_on_412():
    inst = dict(INST, metadata={"fingerprint": "fp1", "items": [
        {"key": "ssh-keys", "value": "alice:ssh-rsa A alice"}, {"key": "startup-script", "value": "old"}]})

    class Session(FakeSession):
        attempts = 0

        def request(self, method, url, params=None, json=None, headers=None, timeout=None):
            if url.endswith("/setMetadata"):
                Session.attempts += 1
                self.log.append((method, url, params, json))
                if Session.attempts == 1:
                    return Resp(412, {"error": {"message": "fingerprint mismatch"}})
                return Resp(200, OP_DONE)
            return super().request(method, url, params, json, headers, timeout)

    s = Session([("GET", "/instances/vm1", Resp(200, inst))])
    c = GCPClient(KEY, session=s)
    c.set_metadata("asia-east1-b", "vm1", {"startup-script": None, "ssh-keys": "x:y z"})
    bodies = [e[3] for e in s.log if e[0] == "POST"]
    assert len(bodies) == 2  # 412 后重读重试
    assert bodies[-1] == {"fingerprint": "fp1", "items": [{"key": "ssh-keys", "value": "x:y z"}]}
    assert c.get_metadata("asia-east1-b", "vm1")["startup-script"] == "old"
