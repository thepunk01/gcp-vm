"""进程级配置（来自环境变量）。运行期可调的参数存放在数据库 settings 表中。"""

from __future__ import annotations

import os
import secrets
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


DATA_DIR = Path(_env("GCPVM_DATA_DIR", "./data")).resolve()
DB_PATH = DATA_DIR / "gcpvm.db"

HOST = _env("GCPVM_HOST", "0.0.0.0")
PORT = int(_env("GCPVM_PORT", "8080"))

# 管理后台密码，必须设置
ADMIN_PASSWORD = _env("GCPVM_ADMIN_PASSWORD")

# 登录令牌有效期（小时）
TOKEN_TTL_HOURS = int(_env("GCPVM_TOKEN_TTL_HOURS", "24"))

# 是否启动后台监控循环（测试时可关闭）
ENABLE_MONITOR = _env("GCPVM_ENABLE_MONITOR", "1") == "1"


def secret_key() -> str:
    """签名密钥：优先读环境变量，否则在数据目录生成并持久化。"""
    env_key = _env("GCPVM_SECRET_KEY")
    if env_key:
        return env_key
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    key_file = DATA_DIR / ".secret_key"
    if key_file.exists():
        return key_file.read_text().strip()
    key = secrets.token_urlsafe(48)
    key_file.write_text(key)
    try:
        key_file.chmod(0o600)
    except OSError:
        pass
    return key


# settings 表的默认值（可在 Web 控制台修改）
DEFAULT_SETTINGS: dict = {
    # 检测模式：agent = 通过部署在国内的探针检测；local = 本机直接检测（本系统部署在国内时使用）
    "detect_mode": "agent",
    # 国内探针列表：[{"name": "上海电信", "url": "http://1.2.3.4:9800", "token": "xxx"}]
    "probes": [],
    # 主动上报探针（适合没有公网 IP 的国内设备）：[{"name": "家里电信", "token": "xxx"}]
    "push_probes": [],
    # 检测间隔（秒）
    "check_interval": 300,
    # 连续判定被墙多少次后才更换 IP（防误判）
    "fail_threshold": 3,
    # 国内探针成功率低于该比例视为不可达
    "cn_success_ratio": 0.5,
    # 单次 TCP 连接超时（秒）与重试次数
    "tcp_timeout": 5,
    "tcp_tries": 3,
    # 对照检测：本机（海外）也能连通才判定为被墙，否则视为实例/服务故障
    "control_check": True,
    # 换 IP 时先用国内探针验证新 IP 可达，被墙则继续换下一个
    "verify_new_ip": True,
    # 两次自动换 IP 之间的最短间隔（分钟）
    "cooldown_minutes": 30,
    # 每个实例每天最多自动换 IP 次数
    "max_changes_per_day": 10,
    # Telegram 通知
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    # 服务监控：上报中断（失联）或 watch_service 停止超过 service_alert_seconds 秒就通知
    "service_alert_enabled": True,
    "service_alert_seconds": 60,
    "watch_service": "nyanpass",
    # 公开速率页（可用 iframe 嵌入 NY 面板公告）
    "status_page_enabled": False,
    "status_page_key": "",
    "status_page_title": "实时速率",
    # 面板对外地址（保存设置时由浏览器自动填写），用于给自动新建的实例生成上报脚本
    "panel_url": "",
    # GCP 自动替补：账号被封/停用、实例被删除或挂起时，按模板在下一个有空位的账号新建实例
    "replace_enabled": False,
    "replace_max_per_account": 3,
    "replace_max_per_day": 6,
    # 替补实例停机超过这么多分钟：先尝试开机，开不了（结算停用 / 无资源等）就替补；0 = 不处理停机
    "replace_stopped_minutes": 10,
    "replace_template": {
        "name_prefix": "gcp",
        "zone": "asia-east1-b",
        "machine_type": "e2-highcpu-4",
        "image": "debian-12",
        "disk_size_gb": 20,
        "disk_type": "pd-balanced",
        "network_tier": "PREMIUM",
        "spot": False,
        "tags": "",
        "check_port": 22,
        "grp": "",
        "ip_mode": "static",
        "follow_original": True,   # 替补时沿用原实例的可用区和机型，开不出来再用模板的
        "install_agent": True,
        "startup_script": "",
    },
    # 按分组的替补模板：{"台湾入口": {...同 replace_template...}}；分组没有专属模板时用 replace_template
    "replace_templates": {},
    # 定时 Telegram 报告（北京时间）：各分组峰值带宽、换 IP 次数
    "report_enabled": False,
    "report_chat_id": "",          # 群组 Chat ID（如 -100xxxx），留空用通知的 Chat ID
    "report_times": "09:00,21:00",
    "report_title": "AtomicGCP 线路运行报告",
    "report_peaks": True,
    "report_ip_changes": True,
    "report_total": False,         # 是否附带「合计」一栏
    "report_state": {},            # 内部：本周期峰值与起始时间
    # 「执行命令」里的常用脚本：[{"name": "...", "script": "..."}]
    "script_presets": [
        {
            "name": "查看系统信息",
            "script": "uname -a\ngrep PRETTY_NAME /etc/os-release\nuptime\nfree -h\ndf -h /",
        },
        {
            "name": "更新系统",
            "script": (
                "if command -v apt-get >/dev/null; then\n  apt-get update && apt-get -y upgrade\n"
                "elif command -v dnf >/dev/null; then\n  dnf -y upgrade\nelse\n  yum -y update\nfi"
            ),
        },
        {
            "name": "开启 BBR",
            "script": (
                "cat >/etc/sysctl.d/99-bbr.conf <<'EOF'\nnet.core.default_qdisc=fq\n"
                "net.ipv4.tcp_congestion_control=bbr\nEOF\nsysctl --system >/dev/null\n"
                "sysctl net.ipv4.tcp_congestion_control"
            ),
        },
        {
            "name": "安装 Docker",
            "script": "curl -fsSL https://get.docker.com | sh\nsystemctl enable --now docker\ndocker --version",
        },
    ],
}
