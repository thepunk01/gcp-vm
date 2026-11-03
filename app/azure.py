"""Azure Resource Manager REST 客户端（服务主体 client secret 认证）。

与 GCPClient 一样直接调用 REST API，不依赖 azure SDK。
实例在本系统中的映射：project = 资源组，zone = 区域（location），name = 虚拟机名。
AzureError 继承 GCPError，管理层原有的错误处理可以直接复用。
"""

from __future__ import annotations

import base64
import logging
import re
import threading
import time
from typing import Any

import requests

from .gcp import GCPError

log = logging.getLogger(__name__)

ARM = "https://management.azure.com"
LOGIN = "https://login.microsoftonline.com"
API_COMPUTE = "2024-07-01"
API_NETWORK = "2024-05-01"
API_RESOURCES = "2021-04-01"
API_LOCATIONS = "2022-12-01"
API_SKUS = "2021-07-01"
API_DISKS = "2024-03-02"

GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
VM_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}[a-z0-9]$")
SIZE_RE = re.compile(r"^[A-Za-z0-9_]{3,64}$")
LOCATION_RE = re.compile(r"^[a-z0-9]{3,40}$")

PREFIX = "gcpvm-"
VNET = "gcpvm-vnet"
NSG = "gcpvm-nsg"
ADMIN_USER = "gcpvm"

IMAGES = [
    {"id": "ubuntu-2404", "label": "Ubuntu 24.04 LTS",
     "ref": {"publisher": "Canonical", "offer": "ubuntu-24_04-lts", "sku": "server", "version": "latest"}},
    {"id": "ubuntu-2204", "label": "Ubuntu 22.04 LTS",
     "ref": {"publisher": "Canonical", "offer": "0001-com-ubuntu-server-jammy", "sku": "22_04-lts-gen2", "version": "latest"}},
    {"id": "debian-12", "label": "Debian 12",
     "ref": {"publisher": "Debian", "offer": "debian-12", "sku": "12-gen2", "version": "latest"}},
]
IMAGE_BY_ID = {i["id"]: i for i in IMAGES}
DISK_TYPES = ("StandardSSD_LRS", "Standard_LRS", "Premium_LRS")

# 常用区域排前面，带中文名
LOCATION_NAMES = {
    "eastasia": "香港", "japaneast": "东京", "japanwest": "大阪", "koreacentral": "首尔",
    "southeastasia": "新加坡", "westus": "加州", "westus2": "华盛顿", "westus3": "亚利桑那",
    "eastus": "弗吉尼亚", "centralindia": "浦那", "australiaeast": "悉尼",
}
LOCATION_ORDER = list(LOCATION_NAMES)
SIZE_FAMILIES = ("B", "D", "F", "A")
POWER = {
    "running": "RUNNING", "deallocated": "TERMINATED", "stopped": "STOPPED", "starting": "STAGING",
    "stopping": "STOPPING", "deallocating": "STOPPING",
}


class AzureError(GCPError):
    """Azure API 调用失败。"""


def validate_azure_key(key: Any) -> dict:
    """接受面板表单字段，或 `az ad sp create-for-rbac` 的输出（appId / password / tenant）再加订阅 ID。"""
    if not isinstance(key, dict):
        raise ValueError("Azure 凭据必须是 JSON 对象")
    out = {
        "type": "azure",
        "tenant_id": str(key.get("tenant_id") or key.get("tenant") or key.get("tenantId") or "").strip(),
        "client_id": str(key.get("client_id") or key.get("appId") or key.get("clientId") or "").strip(),
        "client_secret": str(key.get("client_secret") or key.get("password") or key.get("clientSecret") or "").strip(),
        "subscription_id": str(key.get("subscription_id") or key.get("subscriptionId") or "").strip(),
    }
    labels = {"tenant_id": "租户 ID（tenant）", "client_id": "应用 ID（appId）", "subscription_id": "订阅 ID"}
    for f, label in labels.items():
        if not GUID_RE.match(out[f]):
            raise ValueError(f"{label} 格式不正确，应为 xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
    if len(out["client_secret"]) < 8:
        raise ValueError("请填写客户端密码（password / client secret）")
    return out


def parse_id(rid: str) -> dict[str, str]:
    """/subscriptions/s/resourceGroups/rg/providers/Microsoft.X/type/name -> {'resourcegroups': rg, 'type': name...}"""
    parts = [p for p in (rid or "").split("/") if p]
    out: dict[str, str] = {}
    for i in range(0, len(parts) - 1, 2):
        out[parts[i].lower()] = parts[i + 1]
    return out


def rg_of(rid: str) -> str:
    return parse_id(rid).get("resourcegroups", "")


def _power_state(statuses: list[dict]) -> str:
    for s in statuses or []:
        code = s.get("code", "")
        if code.startswith("PowerState/"):
            return POWER.get(code.split("/", 1)[1], code.split("/", 1)[1].upper())
    for s in statuses or []:
        if s.get("code", "") == "ProvisioningState/creating":
            return "PROVISIONING"
    return "UNKNOWN"


def summarize_vm(vm: dict, statuses: list[dict], nics: dict[str, dict], pips: dict[str, dict]) -> dict:
    props = vm.get("properties") or {}
    nic_refs = (props.get("networkProfile") or {}).get("networkInterfaces") or []
    nic_ref = next((n for n in nic_refs if (n.get("properties") or {}).get("primary")), nic_refs[0] if nic_refs else {})
    nic_id = nic_ref.get("id", "")
    nic = nics.get(nic_id.lower(), {})
    ipcs = (nic.get("properties") or {}).get("ipConfigurations") or []
    ipc = next((c for c in ipcs if (c.get("properties") or {}).get("primary")), ipcs[0] if ipcs else {})
    ipc_props = ipc.get("properties") or {}
    pip_id = (ipc_props.get("publicIPAddress") or {}).get("id", "")
    pip = pips.get(pip_id.lower(), {})
    return {
        "project": rg_of(vm.get("id", "")),
        "zone": vm.get("location", ""),
        "name": vm.get("name", ""),
        "machine_type": (props.get("hardwareProfile") or {}).get("vmSize", ""),
        "status": _power_state(statuses),
        "external_ip": (pip.get("properties") or {}).get("ipAddress", "") or "",
        "internal_ip": ipc_props.get("privateIPAddress", "") or "",
        "network_tier": ((pip.get("sku") or {}).get("name") or "") if pip else "",
        "preemptible": props.get("priority") == "Spot",
        "nic_id": nic_id,
        "ipconfig_name": ipc.get("name", ""),
        "pip_id": pip_id,
        "pip_name": pip.get("name", ""),
        "pip_zones": pip.get("zones") or [],
    }


def sort_locations(items: list[dict]) -> list[dict]:
    out = []
    for loc in items:
        meta = loc.get("metadata") or {}
        if meta.get("regionType", "Physical") != "Physical":
            continue
        name = loc["name"]
        cn = LOCATION_NAMES.get(name)
        out.append({
            "name": name,
            "region": meta.get("geographyGroup") or "其他",
            "label": f"{cn} · {loc.get('displayName', name)}" if cn else loc.get("displayName", name),
        })

    def key(z):
        n = z["name"]
        return (LOCATION_ORDER.index(n) if n in LOCATION_ORDER else len(LOCATION_ORDER), z["region"], n)

    out.sort(key=key)
    # 常用区域单独成组，排在最前
    for z in out:
        if z["name"] in LOCATION_NAMES:
            z["region"] = "常用"
    return out


def filter_sizes(skus: list[dict], location: str) -> list[dict]:
    out = []
    for s in skus:
        if s.get("resourceType") != "virtualMachines":
            continue
        if location and location.lower() not in [x.lower() for x in s.get("locations") or []]:
            continue
        if any(r.get("reasonCode") == "NotAvailableForSubscription" or r.get("type") == "Location"
               for r in s.get("restrictions") or []):
            continue
        caps = {c["name"]: c["value"] for c in s.get("capabilities") or []}
        try:
            cpus = int(float(caps.get("vCPUs", 0)))
            mem = float(caps.get("MemoryGB", 0))
        except ValueError:
            continue
        name = s.get("name", "")
        fam = name.split("_", 2)[1][:1] if name.startswith("Standard_") and "_" in name else ""
        if fam not in SIZE_FAMILIES or not 1 <= cpus <= 16:
            continue
        out.append({"name": name, "cpus": cpus, "memory_mb": int(mem * 1024),
                    "label": f"{name} · {cpus} vCPU · {mem:g} GB", "family": fam})
    seen: set[str] = set()
    uniq = []
    for m in sorted(out, key=lambda m: (SIZE_FAMILIES.index(m["family"]), m["cpus"], m["memory_mb"], m["name"])):
        if m["name"] not in seen:
            seen.add(m["name"])
            uniq.append(m)
    return uniq


def root_key_script(authorized_line: str) -> str:
    """把面板公钥写入 root（Azure 镜像默认允许 root 用密钥登录）。"""
    return f"""mkdir -p /root/.ssh && chmod 700 /root/.ssh
touch /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys
sed -i '/ gcpvm-panel$/d' /root/.ssh/authorized_keys
echo '{authorized_line}' >> /root/.ssh/authorized_keys
if grep -qE '^[[:space:]]*PermitRootLogin[[:space:]]+no' /etc/ssh/sshd_config 2>/dev/null; then
  sed -i -E 's/^[[:space:]]*PermitRootLogin[[:space:]]+no.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
  systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
fi
echo gcpvm-key-ok
"""


def build_vm_body(data: dict, ssh_pub: str, subnet_id: str, nsg_id: str, custom_data: str = "") -> tuple[str, dict]:
    """返回 (location, PUT virtualMachines 请求体)。网卡和公网 IP 随虚拟机一起创建，删除虚拟机时一起删除。"""
    name = str(data.get("name", "")).strip()
    if not VM_NAME_RE.match(name):
        raise ValueError("实例名称只能用小写字母、数字和短横线，以字母开头，最长 64 位")
    loc = str(data.get("zone", "")).strip()
    if not LOCATION_RE.match(loc):
        raise ValueError("请选择区域")
    size = str(data.get("machine_type", "")).strip()
    if not SIZE_RE.match(size):
        raise ValueError("请选择机型")
    image = IMAGE_BY_ID.get(str(data.get("image") or "ubuntu-2404"))
    if not image:
        raise ValueError("请选择系统镜像")
    try:
        disk_gb = int(data.get("disk_size_gb", 30))
    except (TypeError, ValueError):
        raise ValueError("磁盘大小必须是整数") from None
    if not 30 <= disk_gb <= 2000:
        raise ValueError("Azure 磁盘大小范围 30-2000 GB")
    disk_type = str(data.get("disk_type") or "StandardSSD_LRS")
    if disk_type not in DISK_TYPES:
        raise ValueError("不支持的磁盘类型")
    props: dict = {
        "hardwareProfile": {"vmSize": size},
        "storageProfile": {
            "imageReference": image["ref"],
            "osDisk": {
                "createOption": "FromImage", "deleteOption": "Delete", "diskSizeGB": disk_gb,
                "managedDisk": {"storageAccountType": disk_type},
            },
        },
        "osProfile": {
            "computerName": name[:64],
            "adminUsername": ADMIN_USER,
            "linuxConfiguration": {
                "disablePasswordAuthentication": True,
                "ssh": {"publicKeys": [{"path": f"/home/{ADMIN_USER}/.ssh/authorized_keys", "keyData": ssh_pub}]},
            },
        },
        "networkProfile": {
            "networkApiVersion": "2020-11-01",
            "networkInterfaceConfigurations": [{
                "name": f"{name}-nic",
                "properties": {
                    "primary": True,
                    "deleteOption": "Delete",
                    "networkSecurityGroup": {"id": nsg_id},
                    "ipConfigurations": [{
                        "name": "ipconfig1",
                        "properties": {
                            "subnet": {"id": subnet_id},
                            "publicIPAddressConfiguration": {
                                "name": f"{PREFIX}{name}-ip",
                                "sku": {"name": "Standard"},
                                "properties": {"publicIPAllocationMethod": "Static", "deleteOption": "Delete"},
                            },
                        },
                    }],
                },
            }],
        },
    }
    if custom_data:
        props["osProfile"]["customData"] = base64.b64encode(custom_data.encode()).decode()
    if data.get("spot"):
        props["priority"] = "Spot"
        props["evictionPolicy"] = "Deallocate"
        props["billingProfile"] = {"maxPrice": -1}
    return loc, {"location": loc, "tags": {"managed-by": "gcpvm"}, "properties": props}


class AzureClient:
    """单个服务主体 + 订阅对应一个客户端，线程安全。"""

    def __init__(self, key: dict, session: requests.Session | None = None, timeout: int = 60):
        self.key = validate_azure_key(key)
        self.sub = self.key["subscription_id"]
        self.project = self.sub
        self.session = session or requests.Session()
        self.timeout = timeout
        self._token: str | None = None
        self._token_exp = 0.0
        self._lock = threading.Lock()
        self._sleep = time.sleep

    # ---------------- auth / http ----------------
    def _access_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_exp - 120:
                return self._token
            resp = self.session.post(
                f"{LOGIN}/{self.key['tenant_id']}/oauth2/v2.0/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.key["client_id"],
                    "client_secret": self.key["client_secret"],
                    "scope": f"{ARM}/.default",
                },
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                try:
                    msg = resp.json().get("error_description", resp.text)
                except ValueError:
                    msg = resp.text
                raise AzureError(f"获取 Azure 访问令牌失败：HTTP {resp.status_code} {msg[:300]}")
            data = resp.json()
            self._token = data["access_token"]
            self._token_exp = time.time() + int(data.get("expires_in", 3600))
            return self._token

    def _raw(self, method: str, path: str, api: str | None, body: dict | None = None, params: dict | None = None):
        url = path if path.startswith("http") else f"{ARM}{path}"
        q = dict(params or {})
        if api:
            q["api-version"] = api
        resp = self.session.request(
            method, url, params=q, json=body, timeout=self.timeout,
            headers={"Authorization": f"Bearer {self._access_token()}"},
        )
        if resp.status_code >= 400:
            try:
                err = resp.json().get("error", {})
                msg = f"{err.get('code', '')}: {err.get('message', '')}".strip(": ") or resp.text
            except ValueError:
                msg = resp.text
            short = path.split("?")[0].rsplit("/providers/", 1)[-1]
            raise AzureError(f"{method} {short} 失败：HTTP {resp.status_code} {msg[:500]}")
        return resp

    def _call(self, method: str, path: str, api: str | None, body: dict | None = None,
              params: dict | None = None, wait: bool = True, timeout: int = 900) -> dict:
        resp = self._raw(method, path, api, body, params)
        data = resp.json() if resp.content else {}
        if wait and (resp.headers.get("Azure-AsyncOperation") or resp.status_code == 202):
            return self._wait_lro(resp, timeout) or data
        return data

    def _wait_lro(self, resp, timeout: int = 900) -> dict:
        """等待长时间运行的操作（Azure-AsyncOperation 或 Location 轮询）。"""
        deadline = time.time() + timeout
        async_url = resp.headers.get("Azure-AsyncOperation")
        loc_url = resp.headers.get("Location")
        while time.time() < deadline:
            self._sleep(min(int(resp.headers.get("Retry-After", 3) or 3), 10))
            if async_url:
                resp = self._raw("GET", async_url, None)
                data = resp.json() if resp.content else {}
                status = str(data.get("status", "")).lower()
                if status == "succeeded":
                    return data.get("properties") or data
                if status in ("failed", "canceled"):
                    err = data.get("error") or {}
                    raise AzureError(f"操作失败：{err.get('code', status)} {err.get('message', '')}".strip())
            elif loc_url:
                resp = self._raw("GET", loc_url, None)
                if resp.status_code != 202:
                    return resp.json() if resp.content else {}
            else:
                return {}
        raise AzureError("等待 Azure 操作超时")

    def _list(self, path: str, api: str, params: dict | None = None) -> list[dict]:
        out: list[dict] = []
        data = self._call("GET", path, api, params=params, wait=False)
        while True:
            out.extend(data.get("value") or [])
            nxt = data.get("nextLink")
            if not nxt:
                return out
            data = self._call("GET", nxt, None, wait=False)

    def _vm_path(self, rg: str, name: str) -> str:
        return f"/subscriptions/{self.sub}/resourceGroups/{rg}/providers/Microsoft.Compute/virtualMachines/{name}"

    # ---------------- instances ----------------
    def list_instances(self) -> list[dict]:
        base = f"/subscriptions/{self.sub}/providers"
        vms = self._list(f"{base}/Microsoft.Compute/virtualMachines", API_COMPUTE)
        if not vms:
            return []
        status = {
            v["id"].lower(): ((v.get("properties") or {}).get("instanceView") or {}).get("statuses") or []
            for v in self._list(f"{base}/Microsoft.Compute/virtualMachines", API_COMPUTE, {"statusOnly": "true"})
        }
        nics = {n["id"].lower(): n for n in self._list(f"{base}/Microsoft.Network/networkInterfaces", API_NETWORK)}
        pips = {p["id"].lower(): p for p in self._list(f"{base}/Microsoft.Network/publicIPAddresses", API_NETWORK)}
        return [summarize_vm(v, status.get(v["id"].lower(), []), nics, pips) for v in vms]

    def get_vm_raw(self, rg: str, name: str) -> dict:
        return self._call("GET", self._vm_path(rg, name), API_COMPUTE, params={"$expand": "instanceView"}, wait=False)

    def get_instance(self, rg: str, name: str) -> dict:
        vm = self.get_vm_raw(rg, name)
        statuses = ((vm.get("properties") or {}).get("instanceView") or {}).get("statuses") or []
        nics: dict[str, dict] = {}
        pips: dict[str, dict] = {}
        for ref in ((vm.get("properties") or {}).get("networkProfile") or {}).get("networkInterfaces") or []:
            nic = self._call("GET", ref["id"], API_NETWORK, wait=False)
            nics[ref["id"].lower()] = nic
            for c in (nic.get("properties") or {}).get("ipConfigurations") or []:
                pid = ((c.get("properties") or {}).get("publicIPAddress") or {}).get("id")
                if pid:
                    pips[pid.lower()] = self._call("GET", pid, API_NETWORK, wait=False)
        return summarize_vm(vm, statuses, nics, pips)

    def start(self, rg: str, name: str) -> None:
        self._call("POST", f"{self._vm_path(rg, name)}/start", API_COMPUTE)

    def stop(self, rg: str, name: str) -> None:
        # 解除分配：停止计费（静态公网 IP 保留）
        self._call("POST", f"{self._vm_path(rg, name)}/deallocate", API_COMPUTE)

    def reset(self, rg: str, name: str) -> None:
        self._call("POST", f"{self._vm_path(rg, name)}/restart", API_COMPUTE)

    def run_command(self, rg: str, name: str, script: str, timeout: int = 600) -> str:
        data = self._call(
            "POST", f"{self._vm_path(rg, name)}/runCommand", API_COMPUTE,
            body={"commandId": "RunShellScript", "script": script.splitlines()}, timeout=timeout,
        )
        values = (data.get("output") or data).get("value") or []
        return "\n".join(str(v.get("message", "")) for v in values)

    # ---------------- public ip ----------------
    def create_public_ip(self, rg: str, location: str, name: str, zones: list[str] | None = None) -> tuple[str, str]:
        path = f"/subscriptions/{self.sub}/resourceGroups/{rg}/providers/Microsoft.Network/publicIPAddresses/{name}"
        body: dict = {
            "location": location, "sku": {"name": "Standard"}, "tags": {"managed-by": "gcpvm"},
            "properties": {"publicIPAllocationMethod": "Static", "publicIPAddressVersion": "IPv4"},
        }
        if zones:
            body["zones"] = zones
        self._call("PUT", path, API_NETWORK, body)
        pip = self._call("GET", path, API_NETWORK, wait=False)
        return pip["id"], (pip.get("properties") or {}).get("ipAddress", "")

    def set_nic_public_ip(self, nic_id: str, ipconfig: str, pip_id: str | None) -> None:
        nic = self._call("GET", nic_id, API_NETWORK, wait=False)
        found = False
        for c in (nic.get("properties") or {}).get("ipConfigurations") or []:
            if c.get("name") == ipconfig or not ipconfig:
                props = c.setdefault("properties", {})
                if pip_id:
                    props["publicIPAddress"] = {"id": pip_id}
                else:
                    props.pop("publicIPAddress", None)
                found = True
                break
        if not found:
            raise AzureError(f"网卡上找不到 IP 配置 {ipconfig}")
        for k in ("etag",):
            nic.pop(k, None)
        self._call("PUT", nic_id, API_NETWORK, nic)

    def list_public_ips(self, rg: str) -> list[dict]:
        items = self._list(
            f"/subscriptions/{self.sub}/resourceGroups/{rg}/providers/Microsoft.Network/publicIPAddresses", API_NETWORK
        )
        return [{
            "id": p["id"], "name": p.get("name", ""),
            "address": (p.get("properties") or {}).get("ipAddress", ""),
            "attached": bool((p.get("properties") or {}).get("ipConfiguration")),
        } for p in items]

    def delete_resource(self, rid: str, api: str = API_NETWORK) -> None:
        try:
            self._call("DELETE", rid, api)
        except AzureError as e:
            if "HTTP 404" not in str(e):
                raise

    # ---------------- create / delete ----------------
    def list_zones(self) -> list[dict]:
        return self._list(f"/subscriptions/{self.sub}/locations", API_LOCATIONS)

    def list_machine_types(self, location: str) -> list[dict]:
        return self._list(
            f"/subscriptions/{self.sub}/providers/Microsoft.Compute/skus", API_SKUS,
            {"$filter": f"location eq '{location}'"},
        )

    def ensure_network(self, rg: str, location: str) -> tuple[str, str]:
        """确保资源组、虚拟网络、放行全部入站的安全组存在，返回 (子网 id, 安全组 id)。"""
        sub = f"/subscriptions/{self.sub}"
        self._call("PUT", f"{sub}/resourcegroups/{rg}", API_RESOURCES, {"location": location, "tags": {"managed-by": "gcpvm"}})
        net = f"{sub}/resourceGroups/{rg}/providers/Microsoft.Network"
        nsg_path = f"{net}/networkSecurityGroups/{NSG}"
        try:
            nsg = self._call("GET", nsg_path, API_NETWORK, wait=False)
        except AzureError as e:
            if "HTTP 404" not in str(e):
                raise
            rules = [
                {"name": f"allow-all-{p.lower()}", "properties": {
                    "priority": 100 + i, "direction": "Inbound", "access": "Allow", "protocol": p,
                    "sourceAddressPrefix": "*", "sourcePortRange": "*",
                    "destinationAddressPrefix": "*", "destinationPortRange": "*",
                }} for i, p in enumerate(("Tcp", "Udp", "Icmp"))
            ]
            self._call("PUT", nsg_path, API_NETWORK, {"location": location, "properties": {"securityRules": rules}})
            nsg = self._call("GET", nsg_path, API_NETWORK, wait=False)
        vnet_path = f"{net}/virtualNetworks/{VNET}"
        try:
            vnet = self._call("GET", vnet_path, API_NETWORK, wait=False)
        except AzureError as e:
            if "HTTP 404" not in str(e):
                raise
            self._call("PUT", vnet_path, API_NETWORK, {
                "location": location,
                "properties": {
                    "addressSpace": {"addressPrefixes": ["10.10.0.0/16"]},
                    "subnets": [{"name": "default", "properties": {"addressPrefix": "10.10.0.0/24"}}],
                },
            })
            vnet = self._call("GET", vnet_path, API_NETWORK, wait=False)
        subnets = (vnet.get("properties") or {}).get("subnets") or []
        if not subnets:
            raise AzureError(f"虚拟网络 {VNET} 没有子网")
        return subnets[0]["id"], nsg["id"]

    def insert_instance(self, rg: str, name: str, body: dict) -> None:
        self._call("PUT", self._vm_path(rg, name), API_COMPUTE, body)

    def delete_instance(self, rg: str, name: str) -> list[str]:
        """删除虚拟机，并清理残留的网卡、公网 IP、系统盘。返回清理掉的资源名。"""
        try:
            vm = self.get_vm_raw(rg, name)
        except AzureError as e:
            if "HTTP 404" in str(e):
                return []
            raise
        props = vm.get("properties") or {}
        nic_ids = [n["id"] for n in (props.get("networkProfile") or {}).get("networkInterfaces") or []]
        pip_ids: list[str] = []
        for nid in nic_ids:
            try:
                nic = self._call("GET", nid, API_NETWORK, wait=False)
            except AzureError:
                continue
            for c in (nic.get("properties") or {}).get("ipConfigurations") or []:
                pid = ((c.get("properties") or {}).get("publicIPAddress") or {}).get("id")
                if pid:
                    pip_ids.append(pid)
        disk_id = (((props.get("storageProfile") or {}).get("osDisk") or {}).get("managedDisk") or {}).get("id", "")
        self._call("DELETE", self._vm_path(rg, name), API_COMPUTE)
        cleaned = []
        for rid, api in [(n, API_NETWORK) for n in nic_ids] + [(p, API_NETWORK) for p in pip_ids] + (
            [(disk_id, API_DISKS)] if disk_id else []
        ):
            try:
                self.delete_resource(rid, api)
                cleaned.append(rid.rsplit("/", 1)[-1])
            except AzureError as e:
                log.warning("清理 %s 失败：%s", rid, e)
        return cleaned
