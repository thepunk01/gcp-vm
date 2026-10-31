"""新建实例：可选的系统镜像、区域名称、机型筛选与请求体构造。"""

from __future__ import annotations

import re

from .firewall import NAME_RE

IMAGES = [
    {"id": "debian-12", "label": "Debian 12", "source": "projects/debian-cloud/global/images/family/debian-12"},
    {"id": "debian-13", "label": "Debian 13", "source": "projects/debian-cloud/global/images/family/debian-13"},
    {"id": "ubuntu-2404", "label": "Ubuntu 24.04 LTS", "source": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64"},
    {"id": "ubuntu-2204", "label": "Ubuntu 22.04 LTS", "source": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2204-lts"},
    {"id": "rocky-9", "label": "Rocky Linux 9", "source": "projects/rocky-linux-cloud/global/images/family/rocky-linux-9"},
    {"id": "alma-9", "label": "AlmaLinux 9", "source": "projects/almalinux-cloud/global/images/family/almalinux-9"},
]
IMAGE_BY_ID = {i["id"]: i for i in IMAGES}

# 常用区域排在前面，并带中文名
REGION_NAMES = {
    "asia-east1": "台湾",
    "asia-east2": "香港",
    "asia-northeast1": "东京",
    "asia-northeast2": "大阪",
    "asia-northeast3": "首尔",
    "asia-southeast1": "新加坡",
    "asia-southeast2": "雅加达",
    "asia-south1": "孟买",
    "us-west1": "俄勒冈",
    "us-west2": "洛杉矶",
    "us-west3": "盐湖城",
    "us-west4": "拉斯维加斯",
    "us-central1": "爱荷华",
    "us-east1": "南卡罗来纳",
    "us-east4": "弗吉尼亚",
    "europe-west1": "比利时",
    "europe-west2": "伦敦",
    "europe-west3": "法兰克福",
    "europe-west4": "荷兰",
    "australia-southeast1": "悉尼",
}
REGION_ORDER = list(REGION_NAMES)

# 只列出常用的 x86 机型系列（都支持 pd-balanced 磁盘）
FAMILIES = ["e2", "n2d", "n2", "t2d", "n1", "c2d", "c3d", "c3"]
MAX_CPUS = 32
DISK_TYPES = ("pd-balanced", "pd-standard", "pd-ssd")
TAG_RE = re.compile(r"^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$")
MT_RE = re.compile(r"^[a-z0-9][-a-z0-9]{1,62}$")


def zone_label(zone: str, region: str) -> str:
    name = REGION_NAMES.get(region)
    return f"{name} · {zone}" if name else zone


def sort_zones(zones: list[dict]) -> list[dict]:
    def key(z):
        r = z["region"]
        return (REGION_ORDER.index(r) if r in REGION_ORDER else len(REGION_ORDER), r, z["name"])

    out = [
        {"name": z["name"], "region": z["region"], "label": zone_label(z["name"], z["region"])}
        for z in zones
        if z.get("status", "UP") == "UP"
    ]
    return sorted(out, key=key)


def filter_machine_types(items: list[dict]) -> list[dict]:
    out = []
    for m in items:
        family = m["name"].split("-", 1)[0]
        if family not in FAMILIES or m.get("deprecated") or m["cpus"] > MAX_CPUS:
            continue
        mem = m["memory_mb"] / 1024
        mem_s = f"{mem:g} GB" if mem >= 1 else f"{m['memory_mb']} MB"
        cpu_s = f"{m['cpus']} vCPU" + ("（共享）" if m.get("shared") else "")
        out.append({**m, "label": f"{m['name']} · {cpu_s} · {mem_s}"})
    out.sort(key=lambda m: (FAMILIES.index(m["name"].split("-", 1)[0]), m["cpus"], m["memory_mb"], m["name"]))
    return out


def _split(value) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v for v in re.split(r"[,\s]+", str(value or "")) if v]


def build_instance_body(data: dict, ssh_line: str, startup: str = "") -> tuple[str, dict]:
    """返回 (zone, GCP instances.insert 请求体)。startup 为已包装好的启动脚本。"""
    name = str(data.get("name", "")).strip()
    if not NAME_RE.match(name):
        raise ValueError("实例名称只能用小写字母、数字和短横线，以字母开头，最长 63 位")
    zone = str(data.get("zone", "")).strip()
    if not re.match(r"^[a-z]+-[a-z]+\d+-[a-z]$", zone):
        raise ValueError("请选择可用区")
    mt = str(data.get("machine_type", "")).strip()
    if not MT_RE.match(mt):
        raise ValueError("请选择机型")
    image_id = str(data.get("image", "debian-12"))
    if image_id in IMAGE_BY_ID:
        source = IMAGE_BY_ID[image_id]["source"]
    elif image_id.startswith("projects/") and "/images/" in image_id:
        source = image_id
    else:
        raise ValueError("请选择系统镜像")
    try:
        disk_gb = int(data.get("disk_size_gb", 20))
    except (TypeError, ValueError):
        raise ValueError("磁盘大小必须是整数") from None
    if not 10 <= disk_gb <= 2000:
        raise ValueError("磁盘大小范围 10-2000 GB")
    disk_type = str(data.get("disk_type", "pd-balanced"))
    if disk_type not in DISK_TYPES:
        raise ValueError("不支持的磁盘类型")
    tier = str(data.get("network_tier", "PREMIUM")).upper()
    if tier not in ("PREMIUM", "STANDARD"):
        raise ValueError("网络层级只能是 PREMIUM 或 STANDARD")
    network = str(data.get("network") or "default").strip()
    tags = _split(data.get("tags"))
    for t in tags:
        if not TAG_RE.match(t):
            raise ValueError(f"网络标记格式不正确：{t}")

    metadata = [
        {"key": "ssh-keys", "value": ssh_line},
        # 保证面板 SSH 执行可用（项目级开启 OS Login 时元数据密钥不生效）
        {"key": "enable-oslogin", "value": "FALSE"},
    ]
    if startup:
        metadata.append({"key": "startup-script", "value": startup})

    body: dict = {
        "name": name,
        "machineType": f"zones/{zone}/machineTypes/{mt}",
        "disks": [{
            "boot": True,
            "autoDelete": True,
            "initializeParams": {
                "sourceImage": source,
                "diskSizeGb": str(disk_gb),
                "diskType": f"zones/{zone}/diskTypes/{disk_type}",
            },
        }],
        "networkInterfaces": [{
            "network": f"global/networks/{network}",
            "accessConfigs": [{"name": "External NAT", "type": "ONE_TO_ONE_NAT", "networkTier": tier}],
        }],
        "metadata": {"items": metadata},
        "labels": {"managed-by": "gcpvm"},
    }
    if tags:
        body["tags"] = {"items": tags}
    if data.get("spot"):
        body["scheduling"] = {
            "provisioningModel": "SPOT",
            "instanceTerminationAction": "STOP",
            "onHostMaintenance": "TERMINATE",
            "automaticRestart": False,
        }
    return zone, body
