"""远程执行：SSH 密钥管理、通过 SSH 在实例上执行脚本、启动脚本封装。

SSH 方式：面板持有一把 ed25519 密钥，执行前把公钥写入实例元数据 ssh-keys，
GCP guest agent 会自动创建用户 gcpvm 并授予免密 sudo，随后用 paramiko 连接执行。
"""

from __future__ import annotations

import base64
import codecs
import hashlib
import logging
import socket
import threading
import time
from pathlib import Path
from typing import Callable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

log = logging.getLogger(__name__)

SSH_USER = "gcpvm"
MAX_SCRIPT_BYTES = 64 * 1024


class RemoteError(Exception):
    """远程执行失败。"""


# ---------------- 密钥 ----------------
class SSHKey:
    """面板自己的 SSH 密钥，首次使用时生成并保存在数据目录。"""

    def __init__(self, data_dir: Path | str, user: str = SSH_USER):
        self.dir = Path(data_dir) / "ssh"
        self.private_path = self.dir / "id_ed25519"
        self.user = user
        self._lock = threading.Lock()
        self._public: str | None = None

    def ensure(self) -> str:
        """返回 OpenSSH 格式公钥（不含注释），不存在则生成。"""
        with self._lock:
            if self._public:
                return self._public
            if not self.private_path.exists():
                self.dir.mkdir(parents=True, exist_ok=True)
                key = Ed25519PrivateKey.generate()
                pem = key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.OpenSSH,
                    serialization.NoEncryption(),
                )
                self.private_path.write_bytes(pem)
                try:
                    self.private_path.chmod(0o600)
                except OSError:
                    pass
                log.info("已生成面板 SSH 密钥：%s", self.private_path)
            key = serialization.load_ssh_private_key(self.private_path.read_bytes(), None)
            self._public = key.public_key().public_bytes(
                serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
            ).decode()
            return self._public

    def authorized_keys_line(self) -> str:
        """写入自有服务器 /root/.ssh/authorized_keys 的一行。"""
        return f"{self.ensure()} gcpvm-panel"

    def metadata_line(self) -> str:
        """GCP 元数据 ssh-keys 中的一行：user:ssh-ed25519 AAAA... user"""
        return f"{self.user}:{self.ensure()} {self.user}"


def merge_ssh_keys(existing: str, line: str) -> str | None:
    """把 line 合并进 ssh-keys 元数据值。已存在则返回 None（无需修改）。

    同一用户名的旧公钥（比如面板密钥被重新生成过）会被替换。
    """
    user, _, rest = line.partition(":")
    key_body = " ".join(rest.split()[:2])
    kept: list[str] = []
    for old in (existing or "").splitlines():
        if not old.strip():
            continue
        o_user, _, o_rest = old.partition(":")
        if o_user == user and " ".join(o_rest.split()[:2]) == key_body:
            return None
        if o_user == user:
            continue  # 同名旧密钥，替换掉
        kept.append(old)
    kept.append(line)
    return "\n".join(kept)


def oslogin_enabled(instance_meta: dict[str, str], project_meta: dict[str, str]) -> bool:
    """实例级设置优先于项目级。启用 OS Login 后元数据 ssh-keys 不生效。"""
    value = instance_meta.get("enable-oslogin")
    if value is None:
        value = project_meta.get("enable-oslogin", "")
    return str(value).strip().lower() in ("true", "1", "yes")


# ---------------- SSH 执行 ----------------
def run_ssh(
    host: str,
    script: str,
    key_path: Path | str,
    on_output: Callable[[str], None],
    *,
    user: str = SSH_USER,
    port: int = 22,
    timeout: float = 600,
    auth_wait: float = 30,
    cancelled: Callable[[], bool] = lambda: False,
    password: str | None = None,
) -> int:
    """以 root（sudo）在远端执行 bash 脚本，实时回调输出，返回退出码。

    auth_wait：刚写入公钥时 guest agent 需要几秒到几十秒才生效，在这段时间内重试认证。
    """
    import paramiko  # 延迟导入，测试时不需要

    pkey = None if password else paramiko.Ed25519Key.from_private_key_file(str(key_path))
    deadline_auth = time.time() + max(auth_wait, 0)
    client: paramiko.SSHClient | None = None
    last_err: Exception | None = None
    while True:
        if cancelled():
            raise RemoteError("已取消")
        client = paramiko.SSHClient()
        # 实例换 IP 很频繁，主机指纹无法固定，这里不校验主机密钥
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                host, port=port, username=user, pkey=pkey, password=password, timeout=15,
                banner_timeout=20, auth_timeout=20, allow_agent=False, look_for_keys=False,
            )
            break
        except paramiko.AuthenticationException as e:
            last_err = e
            client.close()
            if time.time() >= deadline_auth:
                raise RemoteError(
                    "SSH 认证失败：公钥可能尚未生效，或实例未运行 Google guest agent。请稍后重试"
                ) from e
            on_output("[gcpvm] 等待公钥生效…\n")
            time.sleep(5)
        except (OSError, socket.timeout, paramiko.SSHException) as e:
            last_err = e
            client.close()
            if time.time() >= deadline_auth:
                raise RemoteError(
                    f"无法连接 {host}:{port}：{e}。请确认实例在运行、防火墙已放行 22 端口"
                ) from e
            on_output(f"[gcpvm] 连接失败（{e}），重试中…\n")
            time.sleep(5)
    log.debug("ssh connected to %s after %s", host, last_err)

    try:
        transport = client.get_transport()
        transport.set_keepalive(30)  # type: ignore[union-attr]
        chan = transport.open_session()  # type: ignore[union-attr]
        chan.set_combine_stderr(True)
        chan.exec_command(shell_command(user))
        chan.sendall(script.encode())
        chan.shutdown_write()
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        deadline = time.time() + timeout
        while True:
            if cancelled():
                chan.close()
                raise RemoteError("已手动停止")
            if time.time() > deadline:
                chan.close()
                raise RemoteError(f"执行超时（{int(timeout)} 秒），已断开")
            if chan.recv_ready():
                data = chan.recv(32768)
                if data:
                    on_output(decoder.decode(data))
                continue
            if chan.exit_status_ready():
                break
            time.sleep(0.1)
        while chan.recv_ready():
            on_output(decoder.decode(chan.recv(32768)))
        tail = decoder.decode(b"", final=True)
        if tail:
            on_output(tail)
        return chan.recv_exit_status()
    finally:
        client.close()


def shell_command(user: str) -> str:
    """root 直接执行（很多精简系统没有 sudo），其他用户通过免密 sudo 提权。"""
    base = "env DEBIAN_FRONTEND=noninteractive bash -s"
    return base if user == "root" else f"sudo -n {base}"


# ---------------- 启动脚本 ----------------
def wrap_startup_script(script: str, run_once: bool) -> str:
    """包装启动脚本。run_once 时用标记文件保证同一脚本只执行一次（GCP 默认每次开机都执行）。"""
    if not run_once:
        return script
    digest = hashlib.sha256(script.encode()).hexdigest()[:16]
    b64 = base64.b64encode(script.encode()).decode()
    return f"""#!/bin/bash
# gcpvm: run-once wrapper {digest}
DIR=/var/lib/gcpvm
MARK=$DIR/startup-{digest}.done
if [ -f "$MARK" ]; then echo "gcpvm: 启动脚本 {digest} 已执行过，跳过"; exit 0; fi
mkdir -p "$DIR"
echo '{b64}' | base64 -d > "$DIR/startup-{digest}.sh"
export DEBIAN_FRONTEND=noninteractive
if bash "$DIR/startup-{digest}.sh"; then
  touch "$MARK"; echo "gcpvm: 启动脚本 {digest} 执行成功"
else
  rc=$?; echo "gcpvm: 启动脚本 {digest} 执行失败，退出码 $rc"; exit $rc
fi
"""


def unwrap_startup_script(value: str) -> tuple[str, bool]:
    """从元数据中还原用户脚本，返回 (脚本, 是否仅执行一次)。"""
    if "# gcpvm: run-once wrapper" in value:
        for line in value.splitlines():
            if line.startswith("echo '") and "| base64 -d" in line:
                try:
                    return base64.b64decode(line.split("'")[1]).decode(), True
                except (ValueError, IndexError):
                    break
    return value, False


def filter_startup_log(contents: str) -> str:
    keys = ("startup-script", "gcpvm:")
    return "\n".join(line for line in contents.splitlines() if any(k in line for k in keys))
