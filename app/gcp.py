"""Google Compute Engine REST 客户端。

直接调用 Compute Engine v1 REST API，用服务账号密钥签发 JWT 换取 OAuth 访问令牌，
不依赖体积庞大的 google-cloud-compute SDK。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any
from urllib.parse import quote

import jwt
import requests

log = logging.getLogger(__name__)

COMPUTE_BASE = "https://compute.googleapis.com/compute/v1"
SCOPE = "https://www.googleapis.com/auth/compute"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"


class GCPError(Exception):
    """GCP API 调用失败。"""


def validate_service_account_key(key: Any) -> dict:
    if not isinstance(key, dict):
        raise ValueError("服务账号密钥必须是 JSON 对象")
    if key.get("type") != "service_account":
        raise ValueError("不是服务账号密钥（type 应为 service_account）")
    for field in ("project_id", "client_email", "private_key"):
        if not key.get(field):
            raise ValueError(f"服务账号密钥缺少字段：{field}")
    return key


def region_of(zone: str) -> str:
    """us-central1-a -> us-central1"""
    return zone.rsplit("-", 1)[0]


def _last(url_or_name: str) -> str:
    return url_or_name.rsplit("/", 1)[-1] if url_or_name else ""


def summarize_instance(project: str, inst: dict) -> dict:
    """把 GCP instance 资源压缩成本系统使用的字段。"""
    nic = (inst.get("networkInterfaces") or [{}])[0]
    ac = (nic.get("accessConfigs") or [{}])[0]
    sched = inst.get("scheduling") or {}
    return {
        "project": project,
        "zone": _last(inst.get("zone", "")),
        "name": inst.get("name", ""),
        "machine_type": _last(inst.get("machineType", "")),
        "status": inst.get("status", ""),
        "external_ip": ac.get("natIP", "") or "",
        "internal_ip": nic.get("networkIP", "") or "",
        "network_tier": ac.get("networkTier", "") or "",
        "access_config_name": ac.get("name", "") or "",
        "nic_name": nic.get("name", "nic0") or "nic0",
        "preemptible": bool(sched.get("preemptible"))
        or sched.get("provisioningModel") == "SPOT",
    }


class GCPClient:
    """单个服务账号对应一个客户端，线程安全。"""

    def __init__(self, key: dict, session: requests.Session | None = None, timeout: int = 60):
        self.key = validate_service_account_key(key)
        self.project = key["project_id"]
        self.session = session or requests.Session()
        self.timeout = timeout
        self._token: str | None = None
        self._token_exp = 0.0
        self._lock = threading.Lock()

    # ---------------- auth ----------------
    def _access_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_exp - 120:
                return self._token
            now = int(time.time())
            token_uri = self.key.get("token_uri") or DEFAULT_TOKEN_URI
            assertion = jwt.encode(
                {
                    "iss": self.key["client_email"],
                    "scope": SCOPE,
                    "aud": token_uri,
                    "iat": now,
                    "exp": now + 3600,
                },
                self.key["private_key"],
                algorithm="RS256",
                headers={"kid": self.key.get("private_key_id", "")},
            )
            resp = self.session.post(
                token_uri,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                raise GCPError(f"获取访问令牌失败：HTTP {resp.status_code} {resp.text[:300]}")
            data = resp.json()
            self._token = data["access_token"]
            self._token_exp = time.time() + int(data.get("expires_in", 3600))
            return self._token

    # ---------------- http ----------------
    def _request(
        self, method: str, path: str, params: dict | None = None, body: dict | None = None
    ) -> dict:
        url = path if path.startswith("http") else f"{COMPUTE_BASE}/{path.lstrip('/')}"
        resp = self.session.request(
            method,
            url,
            params=params,
            json=body,
            headers={"Authorization": f"Bearer {self._access_token()}"},
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            try:
                err = resp.json().get("error", {})
                msg = err.get("message") or resp.text
            except ValueError:
                msg = resp.text
            raise GCPError(f"{method} {path} 失败：HTTP {resp.status_code} {msg[:500]}")
        return resp.json() if resp.content else {}

    def _wait(self, op: dict, timeout: int = 300) -> dict:
        """等待 zone/region/global operation 完成。"""
        deadline = time.time() + timeout
        self_link = op.get("selfLink", "")
        while op.get("status") != "DONE":
            if time.time() > deadline:
                raise GCPError(f"操作超时：{op.get('name')}")
            if not self_link:
                raise GCPError(f"无法跟踪操作状态：{op}")
            # operations.wait 最长阻塞约 2 分钟，返回时可能仍未完成，循环即可
            op = self._request("POST", f"{self_link}/wait")
        if op.get("error"):
            errors = op["error"].get("errors") or []
            msg = "; ".join(e.get("message", "") for e in errors) or str(op["error"])
            raise GCPError(f"操作失败：{msg}")
        return op

    # ---------------- instances ----------------
    def list_instances(self) -> list[dict]:
        result: list[dict] = []
        params: dict[str, Any] = {"maxResults": 500}
        while True:
            data = self._request(
                "GET", f"projects/{self.project}/aggregated/instances", params=params
            )
            for scope in (data.get("items") or {}).values():
                for inst in scope.get("instances") or []:
                    result.append(summarize_instance(self.project, inst))
            token = data.get("nextPageToken")
            if not token:
                break
            params["pageToken"] = token
        return result

    def get_instance_raw(self, zone: str, name: str) -> dict:
        return self._request("GET", f"projects/{self.project}/zones/{zone}/instances/{name}")

    def get_instance(self, zone: str, name: str) -> dict:
        return summarize_instance(self.project, self.get_instance_raw(zone, name))

    def _instance_action(self, zone: str, name: str, action: str) -> dict:
        op = self._request(
            "POST", f"projects/{self.project}/zones/{zone}/instances/{name}/{action}"
        )
        return self._wait(op)

    def start(self, zone: str, name: str) -> dict:
        return self._instance_action(zone, name, "start")

    def stop(self, zone: str, name: str) -> dict:
        return self._instance_action(zone, name, "stop")

    def reset(self, zone: str, name: str) -> dict:
        return self._instance_action(zone, name, "reset")

    # ---------------- metadata / serial ----------------
    @staticmethod
    def metadata_items(meta: dict | None) -> dict[str, str]:
        return {i["key"]: i.get("value", "") for i in (meta or {}).get("items") or []}

    def get_metadata(self, zone: str, name: str) -> dict[str, str]:
        return self.metadata_items(self.get_instance_raw(zone, name).get("metadata"))

    def set_metadata(self, zone: str, name: str, updates: dict[str, str | None]) -> None:
        """合并更新实例元数据（值为 None 表示删除该键），其余键保持不变。

        setMetadata 需要带上最新 fingerprint，并发修改导致 412 时重读后重试。
        """
        for attempt in range(3):
            meta = self.get_instance_raw(zone, name).get("metadata") or {}
            items = self.metadata_items(meta)
            for k, v in updates.items():
                if v is None:
                    items.pop(k, None)
                else:
                    items[k] = v
            body = {
                "fingerprint": meta.get("fingerprint"),
                "items": [{"key": k, "value": v} for k, v in items.items()],
            }
            try:
                op = self._request(
                    "POST",
                    f"projects/{self.project}/zones/{zone}/instances/{name}/setMetadata",
                    body=body,
                )
            except GCPError as e:
                if "HTTP 412" in str(e) and attempt < 2:
                    continue
                raise
            self._wait(op)
            return

    def get_project_metadata(self) -> dict[str, str]:
        data = self._request("GET", f"projects/{self.project}")
        return self.metadata_items(data.get("commonInstanceMetadata"))

    def get_serial_output(self, zone: str, name: str, start: int | None = None) -> dict:
        """串口 1 输出（启动脚本日志会写到这里）。返回 {contents, start, next}。"""
        params: dict[str, Any] = {"port": 1}
        if start is not None:
            params["start"] = start
        return self._request(
            "GET",
            f"projects/{self.project}/zones/{zone}/instances/{name}/serialPort",
            params=params,
        )

    # ---------------- access config / ip ----------------
    def delete_access_config(self, zone: str, name: str, nic: str, ac_name: str) -> None:
        op = self._request(
            "POST",
            f"projects/{self.project}/zones/{zone}/instances/{name}/deleteAccessConfig",
            params={"networkInterface": nic, "accessConfig": ac_name},
        )
        self._wait(op)

    def add_access_config(
        self,
        zone: str,
        name: str,
        nic: str,
        ac_name: str = "External NAT",
        nat_ip: str | None = None,
        network_tier: str | None = None,
    ) -> None:
        body: dict[str, Any] = {"name": ac_name or "External NAT", "type": "ONE_TO_ONE_NAT"}
        if nat_ip:
            body["natIP"] = nat_ip
        if network_tier:
            body["networkTier"] = network_tier
        op = self._request(
            "POST",
            f"projects/{self.project}/zones/{zone}/instances/{name}/addAccessConfig",
            params={"networkInterface": nic},
            body=body,
        )
        self._wait(op)

    # ---------------- firewall ----------------
    def list_firewalls(self) -> list[dict]:
        result: list[dict] = []
        params: dict[str, Any] = {"maxResults": 500}
        while True:
            data = self._request("GET", f"projects/{self.project}/global/firewalls", params=params)
            result.extend(data.get("items") or [])
            token = data.get("nextPageToken")
            if not token:
                return result
            params["pageToken"] = token

    def list_networks(self) -> list[str]:
        data = self._request("GET", f"projects/{self.project}/global/networks")
        return [n["name"] for n in data.get("items") or []]

    def create_firewall(self, body: dict) -> None:
        op = self._request("POST", f"projects/{self.project}/global/firewalls", body=body)
        self._wait(op)

    def patch_firewall(self, name: str, body: dict) -> None:
        op = self._request(
            "PATCH", f"projects/{self.project}/global/firewalls/{quote(name)}", body=body
        )
        self._wait(op)

    def delete_firewall(self, name: str) -> None:
        op = self._request("DELETE", f"projects/{self.project}/global/firewalls/{quote(name)}")
        self._wait(op)

    # ---------------- static addresses ----------------
    def find_address_by_ip(self, region: str, ip: str) -> dict | None:
        data = self._request(
            "GET",
            f"projects/{self.project}/regions/{region}/addresses",
            params={"filter": f'address="{ip}"'},
        )
        items = data.get("items") or []
        return items[0] if items else None

    def reserve_address(self, region: str, name: str, network_tier: str | None = None) -> str:
        body: dict[str, Any] = {"name": name, "addressType": "EXTERNAL"}
        if network_tier:
            body["networkTier"] = network_tier
        op = self._request(
            "POST", f"projects/{self.project}/regions/{region}/addresses", body=body
        )
        self._wait(op)
        addr = self._request(
            "GET", f"projects/{self.project}/regions/{region}/addresses/{quote(name)}"
        )
        return addr["address"]

    def release_address(self, region: str, name: str) -> None:
        op = self._request(
            "DELETE", f"projects/{self.project}/regions/{region}/addresses/{quote(name)}"
        )
        self._wait(op)


class ClientPool:
    """按账号 id 缓存 GCPClient（缓存访问令牌）。"""

    def __init__(self, factory=GCPClient):
        self._clients: dict[int, tuple[str, GCPClient]] = {}
        self._factory = factory
        self._lock = threading.Lock()

    def get(self, account: dict) -> GCPClient:
        fingerprint = account["key"].get("private_key_id", "") + account["key"]["client_email"]
        with self._lock:
            cached = self._clients.get(account["id"])
            if cached and cached[0] == fingerprint:
                return cached[1]
            client = self._factory(account["key"])
            self._clients[account["id"]] = (fingerprint, client)
            return client

    def drop(self, account_id: int) -> None:
        with self._lock:
            self._clients.pop(account_id, None)
