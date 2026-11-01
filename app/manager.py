"""业务层：同步实例、开关机、更换 IP、自动检测循环。"""

from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from datetime import datetime

from . import config
from .checker import STATE_BLOCKED, STATE_DOWN, STATE_OK, STATE_UNKNOWN, BlockChecker
from .db import Database
from .gcp import ClientPool, GCPClient, GCPError, region_of
from .firewall import build_body, summarize
from .provision import IMAGES, build_instance_body, filter_machine_types, sort_zones
from .integrations import DNSError, send_telegram, update_cloudflare_a_record
from .jobs import Job, JobStore
from .traffic import DOWN_STATES, SVC_STATE_RE, TrafficHub, agent_install_script, bootstrap_script
from .remote import (
    MAX_SCRIPT_BYTES,
    RemoteError,
    SSHKey,
    filter_startup_log,
    merge_ssh_keys,
    oslogin_enabled,
    run_ssh,
    unwrap_startup_script,
    wrap_startup_script,
)

log = logging.getLogger(__name__)

STOPPED_STATES = {"TERMINATED", "STOPPED", "SUSPENDED"}
STATIC_PREFIX = "gcpvm-"
HOLD_PREFIX = "gcpvm-hold-"
MAX_EPHEMERAL_TRIES = 6
# 占不住被墙 IP 时（如没有静态 IP 配额），释放后等待一段时间再申请，避免立刻被分回同一个 IP
RELEASE_WAITS = (10, 20, 30, 45, 60, 60)


class ManagerError(Exception):
    pass


class Manager:
    def __init__(
        self,
        db: Database,
        pool: ClientPool | None = None,
        checker_cls=BlockChecker,
        ssh_key: SSHKey | None = None,
        ssh_runner=run_ssh,
    ):
        self.db = db
        self.pool = pool or ClientPool()
        self.checker_cls = checker_cls
        self.ssh_key = ssh_key or SSHKey(config.DATA_DIR)
        self.ssh_runner = ssh_runner
        self.jobs = JobStore()
        self.traffic = TrafficHub()
        self._sleep = time.sleep
        self._started = time.time()
        self._alert_state: dict[int, str] = {}
        self._alert_since: dict[int, tuple[str, float]] = {}
        self._locks: dict[int, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._cycle_lock = threading.Lock()

    # ---------------- helpers ----------------
    def _lock_for(self, instance_id: int) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(instance_id, threading.Lock())

    def _client(self, account_id: int) -> GCPClient:
        account = self.db.get_account(account_id)
        if not account:
            raise ManagerError("账号不存在")
        return self.pool.get(account)

    def _instance(self, instance_id: int) -> dict:
        inst = self.db.get_instance(instance_id)
        if not inst:
            raise ManagerError("实例不存在")
        return inst

    def _gcp_instance(self, instance_id: int) -> dict:
        inst = self._instance(instance_id)
        if inst.get("provider") == "manual":
            raise ManagerError("自有服务器没有云厂商 API，不支持此操作（开关机 / 换 IP / 启动脚本等）")
        return inst

    def _event(self, inst: dict | None, type_: str, msg: str, level: str = "info", notify: bool = False):
        iid = inst["id"] if inst else None
        prefix = f"[{inst['name']}] " if inst else ""
        log.log(logging.WARNING if level in ("warn", "error") else logging.INFO, "%s%s", prefix, msg)
        self.db.add_event(type_, msg, iid, level)
        if notify:
            send_telegram(self.db.get_settings(), f"GCP-VM {prefix}{msg}")

    def _refresh(self, inst: dict, client: GCPClient | None = None) -> dict:
        client = client or self._client(inst["account_id"])
        live = client.get_instance(inst["zone"], inst["name"])
        return self.db.upsert_instance(inst["account_id"], live) | {"_live": live}

    # ---------------- sync ----------------
    def sync_account(self, account_id: int) -> int:
        client = self._client(account_id)
        items = client.list_instances()
        keep = [self.db.upsert_instance(account_id, it)["id"] for it in items]
        self.db.remove_missing_instances(account_id, keep)
        return len(items)

    def sync_all(self) -> dict:
        result: dict = {}
        for acc in self.db.list_accounts():
            try:
                result[acc["name"]] = self.sync_account(acc["id"])
            except Exception as e:  # noqa: BLE001
                result[acc["name"]] = f"失败：{e}"
                self._event(None, "sync", f"同步账号 {acc['name']} 失败：{e}", "error")
        return result

    # ---------------- power ----------------
    def power(self, instance_id: int, action: str, source: str = "手动") -> dict:
        if action not in ("start", "stop", "reset"):
            raise ManagerError("不支持的操作")
        inst = self._gcp_instance(instance_id)
        label = {"start": "开机", "stop": "关机", "reset": "重启"}[action]
        lock = self._lock_for(instance_id)
        if not lock.acquire(timeout=1):
            raise ManagerError("该实例正在执行其他操作，请稍后")
        try:
            client = self._client(inst["account_id"])
            getattr(client, action)(inst["zone"], inst["name"])
            inst = self._refresh(inst, client)
            self._event(
                inst, action, f"{source}{label}成功，当前状态 {inst['status']}，IP {inst['external_ip'] or '-'}",
                notify=source != "手动",
            )
            return inst
        except GCPError as e:
            self._event(inst, action, f"{source}{label}失败：{e}", "error", notify=True)
            raise ManagerError(str(e)) from e
        finally:
            lock.release()

    # ---------------- change ip ----------------
    def change_ip(self, instance_id: int, reason: str = "手动") -> dict:
        inst = self._gcp_instance(instance_id)
        lock = self._lock_for(instance_id)
        if not lock.acquire(timeout=1):
            raise ManagerError("该实例正在执行其他操作，请稍后")
        try:
            return self._change_ip_locked(inst, reason)
        finally:
            lock.release()

    def _change_ip_locked(self, inst: dict, reason: str) -> dict:
        client = self._client(inst["account_id"])
        live = client.get_instance(inst["zone"], inst["name"])
        old_ip = live["external_ip"]
        nic = live["nic_name"]
        ac_name = live["access_config_name"] or "External NAT"
        tier = live["network_tier"] or None
        region = region_of(inst["zone"])
        if old_ip and reason != "手动":
            self.db.add_blocked_ip(old_ip, inst["id"])
        self._cleanup_leftover_addresses(client, inst, region)
        avoid = self.db.recent_blocked_ips()
        if old_ip:
            avoid.add(old_ip)

        try:
            if inst["ip_mode"] == "static":
                new_ip = self._swap_static(client, inst, live, region, ac_name, tier, avoid)
            else:
                new_ip = self._swap_ephemeral(client, inst, live, ac_name, tier, avoid)
        except GCPError as e:
            self._event(inst, "change_ip", f"更换 IP 失败（{reason}）：{e}", "error", notify=True)
            # 尽力恢复：若外网 IP 已被删除，补一个临时 IP，避免实例失联
            try:
                cur = client.get_instance(inst["zone"], inst["name"])
                if not cur["external_ip"]:
                    client.add_access_config(inst["zone"], inst["name"], nic, ac_name, None, tier)
                    self._event(inst, "change_ip", "已自动补回临时外网 IP", "warn")
            except GCPError as e2:
                self._event(inst, "change_ip", f"补回外网 IP 失败：{e2}", "error", notify=True)
            raise ManagerError(str(e)) from e

        inst = self.db.update_instance(
            inst["id"], last_change_at=time.time(), fail_count=0, cn_state=STATE_UNKNOWN
        )
        inst = self._refresh(inst, client)
        self._event(
            inst, "change_ip", f"IP 已更换（{reason}）：{old_ip or '-'} -> {new_ip}", "warn", notify=True
        )
        self._update_dns(inst, new_ip)
        return inst

    def _cleanup_leftover_addresses(self, client, inst: dict, region: str) -> None:
        """释放本系统创建、但已不再使用的静态地址（换 IP 被中断时可能残留），避免占满配额、产生费用。"""
        try:
            leftovers = [
                a for a in client.list_addresses(region)
                if a["name"].startswith(STATIC_PREFIX) and a["status"] == "RESERVED"
            ]
        except GCPError as e:
            log.warning("[%s] 查询静态地址失败：%s", inst["name"], e)
            return
        for a in leftovers:
            try:
                client.release_address(region, a["name"])
                self._event(inst, "change_ip", f"已释放残留的未使用静态地址 {a['name']}（{a['address']}）")
            except GCPError as e:
                log.warning("[%s] 释放残留地址 %s 失败：%s", inst["name"], a["name"], e)

    def _swap_ephemeral(self, client, inst, live, ac_name, tier, avoid: set[str]) -> str:
        """释放旧临时 IP 并重新分配，直到拿到不在规避列表里的 IP。

        GCP 常把刚释放的临时 IP 立刻分回来（包括别的实例刚因被墙释放的 IP）。
        所以分到规避列表里的 IP 时，先把它升级成静态地址“占住”，让地址池无法再分配它，
        换到可用 IP 后再统一释放这些占位地址。
        """
        zone, name = inst["zone"], inst["name"]
        region = region_of(zone)
        held: list[str] = []
        hold_errors: list[str] = []
        new_ip = ""
        stamp = datetime.now().strftime("%H%M%S") + secrets.token_hex(2)
        try:
            for attempt in range(1, MAX_EPHEMERAL_TRIES + 1):
                cur = client.get_instance(zone, name)
                wait = 0
                if cur["access_config_name"]:
                    ip = cur["external_ip"]
                    if ip and ip in avoid:
                        hold = f"{HOLD_PREFIX}{name}"[:48].rstrip("-") + f"-{stamp}-{attempt}"
                        try:
                            client.reserve_address(region, hold, tier, address=ip)
                            held.append(hold)
                        except GCPError as e:
                            log.warning("[%s] 占住 IP %s 失败：%s", name, ip, e)
                            hold_errors.append(str(e))
                            wait = RELEASE_WAITS[min(attempt, len(RELEASE_WAITS)) - 1]
                    client.delete_access_config(zone, name, cur["nic_name"], cur["access_config_name"])
                if wait:
                    log.info("[%s] 无法占住被墙 IP，释放后等待 %d 秒再申请新 IP", name, wait)
                    self._sleep(wait)
                client.add_access_config(zone, name, live["nic_name"], ac_name, None, tier)
                new_ip = client.get_instance(zone, name)["external_ip"]
                if new_ip and new_ip not in avoid:
                    return new_ip
                log.info("[%s] 第 %d 次分配到的 IP %s 在规避列表中，重新分配", name, attempt, new_ip or "-")
            if not new_ip:
                raise GCPError("未能分配到新的外网 IP")
            reason = ""
            if hold_errors:
                reason = f"。原因：无法占住被墙 IP（升级为静态地址失败：{hold_errors[-1][:300]}），GCP 会把它重新分配回来"
            raise GCPError(
                f"连续 {MAX_EPHEMERAL_TRIES} 次都分配到已知被墙的 IP（当前 {new_ip}），本次放弃{reason}；"
                "下一轮检测会继续尝试，若持续如此，建议切换网络层级或换区域"
            )
        finally:
            for hold in held:
                try:
                    client.release_address(region, hold)
                except GCPError as e:
                    self._event(inst, "change_ip", f"释放占位地址 {hold} 失败：{e}，请到 GCP 控制台手动释放以免计费", "warn")

    def _swap_static(self, client, inst, live, region, ac_name, tier, avoid: set[str]) -> str:
        """预留新的静态 IP 并绑定。预留到已知被墙的 IP 时先保留（占住）它、继续预留下一个，
        拿到可用 IP 后再统一释放这些被拒绝的地址，保证不会换回被墙 IP。"""
        old_ip = live["external_ip"]
        old_addr = client.find_address_by_ip(region, old_ip) if old_ip else None
        base = (
            f"{STATIC_PREFIX}{inst['name']}"[:40].rstrip("-")
            + "-" + datetime.now().strftime("%m%d%H%M%S") + secrets.token_hex(2)
        )
        rejected: list[str] = []
        new_ip, addr_name = "", ""
        try:
            for attempt in range(1, MAX_EPHEMERAL_TRIES + 1):
                name = f"{base}-{attempt}"
                ip = client.reserve_address(region, name, tier)
                if ip and ip not in avoid:
                    new_ip, addr_name = ip, name
                    break
                log.info("[%s] 第 %d 次预留到的静态 IP %s 在规避列表中，先占住并重新预留", inst["name"], attempt, ip)
                rejected.append(name)
            if not new_ip:
                raise GCPError(
                    f"连续 {MAX_EPHEMERAL_TRIES} 次预留到的静态 IP 都是已知被墙的 IP，本次放弃；"
                    "下一轮检测会继续尝试，若持续如此，建议切换网络层级或换区域"
                )
            try:
                if live["access_config_name"]:
                    client.delete_access_config(inst["zone"], inst["name"], live["nic_name"], live["access_config_name"])
                client.add_access_config(inst["zone"], inst["name"], live["nic_name"], ac_name, new_ip, tier)
            except GCPError:
                try:
                    client.release_address(region, addr_name)
                except GCPError:
                    pass
                raise
        finally:
            for name in rejected:
                try:
                    client.release_address(region, name)
                except GCPError as e:
                    self._event(inst, "change_ip", f"释放被拒绝的静态地址 {name} 失败：{e}，请到 GCP 控制台手动释放以免计费", "warn")
        # 只释放本系统创建的旧静态 IP，用户自己预留的不动
        if old_addr and old_addr.get("name", "").startswith(STATIC_PREFIX):
            try:
                client.release_address(region, old_addr["name"])
            except GCPError as e:
                self._event(inst, "change_ip", f"释放旧静态 IP {old_ip} 失败：{e}", "warn")
        elif old_addr:
            self._event(inst, "change_ip", f"旧静态 IP {old_ip}（{old_addr.get('name')}）非本系统创建，已保留，请自行释放以免计费", "warn")
        return new_ip

    def _update_dns(self, inst: dict, ip: str) -> None:
        if not inst.get("dns_record"):
            return
        settings = self.db.get_settings()
        try:
            msg = update_cloudflare_a_record(
                settings.get("cf_api_token", ""), inst.get("dns_zone_id", ""), inst["dns_record"], ip
            )
            self._event(inst, "dns", msg)
        except DNSError as e:
            self._event(inst, "dns", str(e), "error", notify=True)

    # ---------------- check ----------------
    def check_instance(self, instance_id: int, settings: dict | None = None) -> dict:
        """只检测，不处理。"""
        inst = self._instance(instance_id)
        settings = settings or self.db.get_settings()
        result = self.checker_cls(settings).check(inst["external_ip"], inst["check_port"])
        self.db.update_instance(instance_id, cn_state=result.state, last_check=time.time())
        return result.to_dict()

    def _can_auto_change(self, inst: dict, settings: dict) -> str | None:
        """返回不能自动换 IP 的原因，可以则返回 None。"""
        cooldown = float(settings["cooldown_minutes"]) * 60
        if inst["last_change_at"] and time.time() - inst["last_change_at"] < cooldown:
            left = int((cooldown - (time.time() - inst["last_change_at"])) / 60) + 1
            return f"冷却中（约 {left} 分钟后可再次更换）"
        n = self.db.count_events_since(inst["id"], "change_ip_auto", time.time() - 86400)
        if n >= int(settings["max_changes_per_day"]):
            return f"24 小时内已自动更换 {n} 次，达到上限"
        return None

    def process_instance(self, inst: dict, settings: dict) -> None:
        if inst.get("provider") == "manual":
            self._process_manual(inst, settings)
            return
        if not (inst["auto_ip"] or inst["auto_start"]):
            return
        client = self._client(inst["account_id"])
        inst = self._refresh(inst, client)

        # 自动开机（如 Spot/抢占式实例被回收）
        if inst["status"] in STOPPED_STATES:
            if inst["auto_start"]:
                self._event(inst, "auto_start", f"检测到实例处于 {inst['status']}，自动开机", "warn")
                try:
                    self.power(inst["id"], "start", source="自动")
                except ManagerError:
                    pass
            return

        if not inst["auto_ip"] or inst["status"] != "RUNNING":
            return

        result = self.checker_cls(settings).check(inst["external_ip"], inst["check_port"])
        now = time.time()
        if result.state == STATE_OK:
            if inst["cn_state"] == STATE_BLOCKED:
                self._event(inst, "check", f"{inst['external_ip']} 国内恢复可达")
            self.db.update_instance(inst["id"], cn_state=STATE_OK, fail_count=0, last_check=now)
            return
        if result.state in (STATE_UNKNOWN, STATE_DOWN):
            if result.state != inst["cn_state"]:
                self._event(inst, "check", result.detail, "warn")
            self.db.update_instance(inst["id"], cn_state=result.state, last_check=now)
            return

        # STATE_BLOCKED
        fails = inst["fail_count"] + 1
        threshold = int(settings["fail_threshold"])
        self.db.update_instance(inst["id"], cn_state=STATE_BLOCKED, fail_count=fails, last_check=now)
        self._event(inst, "check", f"{inst['external_ip']}:{inst['check_port']} {result.detail}（{fails}/{threshold}）", "warn")
        if fails < threshold:
            return
        reason = self._can_auto_change(inst, settings)
        if reason:
            self._event(inst, "check", f"暂不自动换 IP：{reason}", "warn")
            return
        self.db.add_event("change_ip_auto", "自动换 IP", inst["id"])
        try:
            self.change_ip(inst["id"], reason="自动-被墙")
        except ManagerError:
            pass

    def _process_manual(self, inst: dict, settings: dict) -> None:
        """自有服务器：没有换 IP 的 API，只做被墙检测并通知（被墙 / 恢复各通知一次）。"""
        if not inst["auto_ip"] or not inst["external_ip"]:
            return
        result = self.checker_cls(settings).check(inst["external_ip"], inst["check_port"])
        now = time.time()
        threshold = int(settings["fail_threshold"])
        addr = f"{inst['external_ip']}:{inst['check_port']}"
        if result.state == STATE_OK:
            if inst["cn_state"] == STATE_BLOCKED and inst["fail_count"] >= threshold:
                self._event(inst, "check", f"{addr} 国内已恢复可达", notify=True)
            self.db.update_instance(inst["id"], cn_state=STATE_OK, fail_count=0, last_check=now)
            return
        if result.state in (STATE_UNKNOWN, STATE_DOWN):
            if result.state != inst["cn_state"]:
                self._event(inst, "check", result.detail, "warn")
            self.db.update_instance(inst["id"], cn_state=result.state, last_check=now)
            return
        fails = inst["fail_count"] + 1
        self.db.update_instance(inst["id"], cn_state=STATE_BLOCKED, fail_count=fails, last_check=now)
        self._event(inst, "check", f"{addr} {result.detail}（{fails}/{threshold}）", "warn")
        if fails == threshold:
            self._event(
                inst, "blocked",
                f"疑似被墙：{addr} 国内不可达、海外可达（{result.detail}）。自有服务器无法自动换 IP，请到服务商后台更换 IP，"
                "再在面板「设置」里更新 IP",
                "error", notify=True,
            )

    # ---------------- 自有服务器 ----------------
    @staticmethod
    def _check_server_fields(data: dict, partial: bool = False) -> dict:
        import ipaddress
        out: dict = {}
        if "name" in data or not partial:
            name = str(data.get("name", "")).strip()
            if not name or len(name) > 40:
                raise ManagerError("服务器名称不能为空，最长 40 个字符")
            out["name"] = name
        if "ip" in data or not partial:
            ip = str(data.get("ip", "")).strip()
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                raise ManagerError("请填写正确的服务器 IP") from None
            out["ip"] = ip
        if "ssh_port" in data or not partial:
            raw = data.get("ssh_port")
            try:
                port = 22 if raw in (None, "") else int(raw)
            except (TypeError, ValueError):
                raise ManagerError("SSH 端口必须是数字") from None
            if not 1 <= port <= 65535:
                raise ManagerError("SSH 端口范围 1-65535")
            out["ssh_port"] = port
        if "ssh_user" in data or not partial:
            user = str(data.get("ssh_user") or "root").strip()
            if not re.fullmatch(r"[a-z_][a-z0-9_.-]{0,31}", user):
                raise ManagerError("SSH 用户名格式不正确")
            out["ssh_user"] = user
        return out

    def add_server(self, data: dict, panel_url: str = "") -> tuple[dict, Job | None]:
        """添加自有服务器；填写了密码时，后台用密码登录一次，写入面板公钥并安装上报（密码不保存）。"""
        fields = self._check_server_fields(data)
        check_port = int(data.get("check_port") or 22)
        if not 1 <= check_port <= 65535:
            raise ManagerError("检测端口范围 1-65535")
        try:
            inst = self.db.add_manual_server(fields | {
                "grp": str(data.get("grp") or "").strip()[:40],
                "note": str(data.get("note") or "")[:200],
                "check_port": check_port,
                "auto_ip": bool(data.get("auto_ip", True)),
            })
        except Exception as e:  # sqlite3.IntegrityError
            if "UNIQUE" in str(e):
                raise ManagerError(f"已有同名服务器：{fields['name']}") from None
            raise
        self._event(inst, "server", f"添加自有服务器 {fields['ip']}")
        job = None
        password = str(data.get("password") or "")
        if password:
            if not panel_url:
                raise ManagerError("缺少面板地址，无法生成上报脚本")
            script = self.server_bootstrap_script(inst["id"], panel_url)
            job = self.jobs.create(inst["id"], inst["name"], "# 接入面板（写入公钥 + 安装上报）")
            threading.Thread(
                target=self._exec_job, args=(job, inst, script, 600, password), daemon=True, name=f"bootstrap-{job.id}"
            ).start()
        return inst, job

    def update_server(self, instance_id: int, data: dict) -> dict:
        inst = self._instance(instance_id)
        if inst.get("provider") != "manual":
            raise ManagerError("只能修改自有服务器的连接信息")
        fields = self._check_server_fields(data, partial=True)
        if "ip" in fields and fields["ip"] != inst["external_ip"]:
            self._event(inst, "server", f"IP 已手动更新：{inst['external_ip']} -> {fields['ip']}", "warn")
            self.db.update_instance(instance_id, cn_state=STATE_UNKNOWN, fail_count=0)
        return self.db.update_manual_server(instance_id, fields)  # type: ignore[return-value]

    def remove_server(self, instance_id: int) -> None:
        inst = self._instance(instance_id)
        if inst.get("provider") != "manual":
            raise ManagerError("GCP 实例请用「删除实例」")
        self._event(inst, "server", "已从面板移除（不影响服务器本身）", "warn")
        self.db.delete_instance(instance_id)

    def server_bootstrap_script(self, instance_id: int, panel_url: str) -> str:
        inst = self._instance(instance_id)
        svc = str(self.db.get_settings().get("watch_service") or "")
        return bootstrap_script(panel_url, self._traffic_token(inst), self.ssh_key.authorized_keys_line(), svc)

    def bootstrap_by_token(self, token: str, panel_url: str) -> str | None:
        inst = self.db.find_instance_by_traffic_token(token)
        if not inst:
            return None
        return self.server_bootstrap_script(inst["id"], panel_url)

    # ---------------- 服务监控告警 ----------------
    def check_alerts(self, now: float | None = None) -> None:
        """检查已安装上报的实例 / 服务器：上报中断（失联）、转发服务停止，持续超过设定时间就通知，恢复时再通知。"""
        settings = self.db.get_settings()
        if not settings.get("service_alert_enabled", True):
            return
        now = time.time() if now is None else now
        grace = max(20, int(settings.get("service_alert_seconds", 60)))
        svc_name = settings.get("watch_service") or "服务"
        for inst in self.db.list_instances():
            iid = inst["id"]
            if not inst.get("traffic_token"):
                continue
            if inst.get("provider") != "manual" and inst["status"] != "RUNNING":
                self._alert_since.pop(iid, None)
                continue
            lock = self._locks.get(iid)
            if lock and lock.locked():  # 正在换 IP 等操作，上报中断属正常
                continue
            r = self.traffic.instance_rate(iid, now)
            seen = r["last_seen"]
            if not seen and now - self._started < grace * 2:
                continue  # 面板刚启动，等第一批上报
            if not seen or now - seen > grace:
                cur = "lost"
            elif r["svc"] in DOWN_STATES:
                cur = "svc_down"
            else:
                cur = "ok"
            notified = self._alert_state.get(iid, "ok")
            ip = inst["external_ip"] or "-"
            if cur == "ok":
                self._alert_since.pop(iid, None)
                if notified != "ok":
                    self._event(inst, "alert", f"已恢复正常（{svc_name} 运行中，上报正常），IP {ip}", notify=True)
                    self._alert_state[iid] = "ok"
                continue
            prev = self._alert_since.get(iid)
            since = prev[1] if prev and prev[0] == cur else now
            self._alert_since[iid] = (cur, since)
            if cur == notified:
                continue
            if cur == "lost":
                ago = int(now - seen) if seen else None
                msg = (f"失联：已 {ago} 秒没有收到上报" if ago is not None else "失联：面板启动后一直没有收到上报") + \
                    f"（服务器可能宕机、网络中断或上报服务停止），IP {ip}"
            else:
                if now - since < grace:
                    continue
                msg = f"{svc_name} 已停止（状态 {r['svc']}），转发不可用，IP {ip}"
            self._event(inst, "alert", msg, "error", notify=True)
            self._alert_state[iid] = cur

    def run_cycle(self) -> None:
        """执行一轮自动检测。"""
        if not self._cycle_lock.acquire(blocking=False):
            log.info("上一轮检测仍在进行，跳过")
            return
        try:
            settings = self.db.get_settings()
            for inst in self.db.list_instances():
                try:
                    self.process_instance(inst, settings)
                except Exception as e:  # noqa: BLE001
                    log.exception("处理实例 %s 出错", inst["name"])
                    self._event(inst, "check", f"检测出错：{e}", "error")
        finally:
            self._cycle_lock.release()

    # ---------------- remote exec (SSH) ----------------
    @staticmethod
    def _check_script(script: str) -> str:
        script = (script or "").replace("\r\n", "\n")
        if not script.strip():
            raise ManagerError("脚本不能为空")
        if len(script.encode()) > MAX_SCRIPT_BYTES:
            raise ManagerError(f"脚本过长（上限 {MAX_SCRIPT_BYTES // 1024} KB）")
        return script

    @staticmethod
    def _explain(e: GCPError) -> str:
        msg = str(e)
        if "iam.serviceAccountUser" in msg or "does not have access to service account" in msg:
            return (
                "面板服务账号缺少「Service Account User」角色，无法修改以服务账号身份运行的实例的元数据。"
                "请在 GCP 控制台 IAM 中为面板服务账号添加 roles/iam.serviceAccountUser 后重试。原始错误："
                + msg
            )
        return msg

    def ensure_ssh_key(self, inst: dict, client: GCPClient | None = None) -> bool:
        """确保面板公钥已写入实例元数据，返回是否本次新写入。"""
        client = client or self._client(inst["account_id"])
        meta = client.get_metadata(inst["zone"], inst["name"])
        try:
            project_meta = client.get_project_metadata()
        except GCPError:
            project_meta = {}
        if oslogin_enabled(meta, project_meta):
            raise ManagerError(
                "该实例启用了 OS Login（enable-oslogin=TRUE），元数据 SSH 密钥不生效。"
                "请在实例或项目元数据中把 enable-oslogin 设为 FALSE，或改用「启动脚本」"
            )
        merged = merge_ssh_keys(meta.get("ssh-keys", ""), self.ssh_key.metadata_line())
        if merged is None:
            return False
        try:
            client.set_metadata(inst["zone"], inst["name"], {"ssh-keys": merged})
        except GCPError as e:
            raise ManagerError(self._explain(e)) from e
        self._event(inst, "exec", f"已写入面板 SSH 公钥（用户 {self.ssh_key.user}）")
        return True

    def start_exec(self, instance_id: int, script: str, timeout: int = 600) -> Job:
        """后台通过 SSH 执行脚本，立即返回任务，输出通过任务轮询获取。"""
        script = self._check_script(script)
        timeout = max(10, min(int(timeout or 600), 3600))
        inst = self._instance(instance_id)
        if inst.get("provider") != "manual" and inst["status"] != "RUNNING":
            raise ManagerError("实例未运行，无法通过 SSH 执行。可改用「启动脚本」")
        if not inst["external_ip"]:
            raise ManagerError("实例没有外网 IP，无法 SSH 连接")
        if self.jobs.running_for(instance_id):
            raise ManagerError("该实例已有命令在执行，请等待完成或先停止")
        job = self.jobs.create(instance_id, inst["name"], script)
        threading.Thread(
            target=self._exec_job, args=(job, inst, script, timeout), daemon=True, name=f"exec-{job.id}"
        ).start()
        return job

    def _exec_job(self, job: Job, inst: dict, script: str, timeout: int, password: str | None = None) -> None:
        first_line = next((ln.strip() for ln in script.splitlines() if ln.strip()), "")[:80]
        try:
            if inst.get("provider") == "manual":
                user = inst.get("ssh_user") or "root"
                port = int(inst.get("ssh_port") or 22)
                job.append(f"[gcpvm] 连接 {user}@{inst['external_ip']}:{port}{'（密码登录）' if password else ''}…\n")
                try:
                    code = self.ssh_runner(
                        inst["external_ip"], script, self.ssh_key.private_path, job.append,
                        user=user, port=port, timeout=timeout, auth_wait=0 if password else 5,
                        cancelled=lambda: job.cancelled, password=password,
                    )
                except RemoteError as e:
                    if "认证失败" in str(e) and not password:
                        raise RemoteError(
                            "SSH 认证失败：面板公钥还没写入这台服务器。请在服务器上执行「接入命令」，"
                            "或在账号页用 root 密码重新接入"
                        ) from e
                    raise
            else:
                job.append(f"[gcpvm] 连接 {self.ssh_key.user}@{inst['external_ip']}…\n")
                added = self.ensure_ssh_key(inst)
                if added:
                    job.append("[gcpvm] 已写入 SSH 公钥，等待实例生效（通常 10-30 秒）…\n")
                code = self.ssh_runner(
                    inst["external_ip"], script, self.ssh_key.private_path, job.append,
                    user=self.ssh_key.user, timeout=timeout,
                    auth_wait=120 if added else 30, cancelled=lambda: job.cancelled,
                )
            job.append(f"\n[gcpvm] 执行结束，退出码 {code}\n")
            job.finish("ok" if code == 0 else "failed", code)
            self._event(
                inst, "exec", f"执行命令（{first_line}）完成，退出码 {code}",
                "info" if code == 0 else "warn",
            )
        except (ManagerError, RemoteError, GCPError) as e:
            status = "cancelled" if job.cancelled else "failed"
            job.append(f"\n[gcpvm] {e}\n")
            job.finish(status, error=str(e))
            self._event(inst, "exec", f"执行命令（{first_line}）失败：{e}", "error")
        except Exception as e:  # noqa: BLE001
            log.exception("执行命令出错")
            job.append(f"\n[gcpvm] 内部错误：{e}\n")
            job.finish("failed", error=str(e))
            self._event(inst, "exec", f"执行命令（{first_line}）出错：{e}", "error")

    # ---------------- startup script ----------------
    def get_startup_script(self, instance_id: int) -> dict:
        inst = self._gcp_instance(instance_id)
        meta = self._client(inst["account_id"]).get_metadata(inst["zone"], inst["name"])
        script, run_once = unwrap_startup_script(meta.get("startup-script", ""))
        return {"script": script, "run_once": run_once, "has_script": bool(script)}

    def set_startup_script(
        self, instance_id: int, script: str, run_once: bool = True, reboot: bool = False
    ) -> dict:
        script = self._check_script(script)
        inst = self._gcp_instance(instance_id)
        lock = self._lock_for(instance_id)
        if not lock.acquire(timeout=1):
            raise ManagerError("该实例正在执行其他操作，请稍后")
        try:
            client = self._client(inst["account_id"])
            client.set_metadata(
                inst["zone"], inst["name"], {"startup-script": wrap_startup_script(script, run_once)}
            )
            mode = "仅执行一次" if run_once else "每次开机执行"
            self._event(inst, "startup", f"已写入启动脚本（{mode}）")
            if reboot:
                inst = self._refresh(inst, client)
                if inst["status"] == "RUNNING":
                    client.reset(inst["zone"], inst["name"])
                    self._event(inst, "startup", "已重启实例以执行启动脚本", "warn")
                elif inst["status"] in STOPPED_STATES:
                    client.start(inst["zone"], inst["name"])
                    self._event(inst, "startup", "已开机以执行启动脚本")
            return self._refresh(inst, client)
        except GCPError as e:
            self._event(inst, "startup", f"写入启动脚本失败：{self._explain(e)}", "error")
            raise ManagerError(self._explain(e)) from e
        finally:
            lock.release()

    def clear_startup_script(self, instance_id: int) -> None:
        inst = self._gcp_instance(instance_id)
        self._client(inst["account_id"]).set_metadata(
            inst["zone"], inst["name"], {"startup-script": None}
        )
        self._event(inst, "startup", "已清除启动脚本")

    def serial_log(self, instance_id: int, start: int | None = None, only_startup: bool = True) -> dict:
        inst = self._gcp_instance(instance_id)
        data = self._client(inst["account_id"]).get_serial_output(inst["zone"], inst["name"], start)
        contents = data.get("contents", "")
        if only_startup:
            contents = filter_startup_log(contents)
        return {"contents": contents, "next": int(data.get("next", 0) or 0)}

    # ---------------- firewall ----------------
    def _account_client(self, account_id: int) -> tuple[dict, GCPClient]:
        acc = self.db.get_account(account_id, with_key=False)
        if not acc:
            raise ManagerError("账号不存在")
        return acc, self._client(account_id)

    def list_firewalls(self, account_id: int) -> dict:
        _, client = self._account_client(account_id)
        try:
            rules = [summarize(r) for r in client.list_firewalls()]
            networks = client.list_networks()
        except GCPError as e:
            raise ManagerError(str(e)) from e
        rules.sort(key=lambda r: (r["network"], r["direction"], r["priority"], r["name"]))
        return {"rules": rules, "networks": networks}

    def create_firewall(self, account_id: int, data: dict) -> None:
        acc, client = self._account_client(account_id)
        body = build_body(data)
        try:
            client.create_firewall(body)
        except GCPError as e:
            if "alreadyExists" in str(e) or "already exists" in str(e):
                raise ManagerError(f"规则名称 {body['name']} 已存在") from e
            raise ManagerError(str(e)) from e
        s = summarize(body | {"network": body["network"]})
        self._event(None, "firewall", f"[{acc['name']}] 新建防火墙规则 {body['name']}：{s['direction']} {s['action']} {s['rules']} 来自 {s['ranges']}", "warn")

    def set_firewall_disabled(self, account_id: int, name: str, disabled: bool) -> None:
        acc, client = self._account_client(account_id)
        try:
            client.patch_firewall(name, {"disabled": bool(disabled)})
        except GCPError as e:
            raise ManagerError(str(e)) from e
        self._event(None, "firewall", f"[{acc['name']}] {'禁用' if disabled else '启用'}防火墙规则 {name}")

    def delete_firewall(self, account_id: int, name: str) -> None:
        acc, client = self._account_client(account_id)
        try:
            client.delete_firewall(name)
        except GCPError as e:
            raise ManagerError(str(e)) from e
        self._event(None, "firewall", f"[{acc['name']}] 删除防火墙规则 {name}", "warn")

    # ---------------- create / delete instance ----------------
    def create_options(self, account_id: int) -> dict:
        _, client = self._account_client(account_id)
        try:
            zones = sort_zones(client.list_zones())
            networks = client.list_networks()
        except GCPError as e:
            raise ManagerError(str(e)) from e
        return {
            "zones": zones,
            "networks": networks,
            "images": [{"id": i["id"], "label": i["label"]} for i in IMAGES],
        }

    def machine_types(self, account_id: int, zone: str) -> list[dict]:
        _, client = self._account_client(account_id)
        try:
            return filter_machine_types(client.list_machine_types(zone))
        except GCPError as e:
            raise ManagerError(str(e)) from e

    @staticmethod
    def _explain_create(e: GCPError) -> str:
        msg = str(e)
        if "alreadyExists" in msg or "already exists" in msg:
            return "实例名称已存在，请换一个名称"
        if "ZONE_RESOURCE_POOL_EXHAUSTED" in msg or "does not have enough resources" in msg:
            return "该可用区暂时没有足够资源，请换一个可用区或机型后重试"
        if "QUOTA" in msg.upper() or "Quota" in msg:
            return (
                "超出 GCP 配额（例如该区域或全部区域的 vCPU 数、外网 IP 数）。"
                "可关停/删除不用的实例，或在 GCP 控制台「IAM 和管理 → 配额」申请提高。原始错误：" + msg
            )
        return msg

    def create_instance(self, account_id: int, data: dict) -> dict:
        acc, client = self._account_client(account_id)
        script = str(data.get("startup_script") or "").replace("\r\n", "\n")
        startup = ""
        if script.strip():
            startup = wrap_startup_script(self._check_script(script), bool(data.get("run_once", True)))
        zone, body = build_instance_body(data, self.ssh_key.metadata_line(), startup)
        cfg = {
            "auto_ip": bool(data.get("auto_ip", True)),
            "auto_start": bool(data.get("auto_start", bool(data.get("spot")))),
            "check_port": int(data.get("check_port") or 22),
            "note": str(data.get("note") or "")[:200],
        }
        if not 1 <= cfg["check_port"] <= 65535:
            raise ManagerError("检测端口范围 1-65535")
        try:
            client.insert_instance(zone, body)
        except GCPError as e:
            self._event(None, "create", f"[{acc['name']}] 创建实例 {body['name']} 失败：{self._explain_create(e)}", "error")
            raise ManagerError(self._explain_create(e)) from e
        self.sync_account(account_id)
        inst = next(
            (i for i in self.db.list_instances()
             if i["account_id"] == account_id and i["zone"] == zone and i["name"] == body["name"]),
            None,
        )
        if not inst:
            raise ManagerError("实例已创建，但同步时未找到，请稍后点「从 GCP 同步」")
        inst = self.db.update_instance_config(inst["id"], cfg)
        mt = body["machineType"].rsplit("/", 1)[-1]
        self._event(
            inst, "create",
            f"新建实例：{zone} · {mt}{' · Spot' if data.get('spot') else ''}，IP {inst['external_ip'] or '-'}",
            notify=True,
        )
        return inst

    def delete_instance(self, instance_id: int) -> None:
        inst = self._gcp_instance(instance_id)
        lock = self._lock_for(instance_id)
        if not lock.acquire(timeout=1):
            raise ManagerError("该实例正在执行其他操作，请稍后")
        try:
            client = self._client(inst["account_id"])
            region = region_of(inst["zone"])
            addr = None
            try:
                ip = client.get_instance(inst["zone"], inst["name"])["external_ip"]
                addr = client.find_address_by_ip(region, ip) if ip else None
            except GCPError:
                pass
            try:
                client.delete_instance(inst["zone"], inst["name"])
            except GCPError as e:
                if "HTTP 404" not in str(e):
                    self._event(inst, "delete", f"删除实例失败：{e}", "error")
                    raise ManagerError(str(e)) from e
            msg = f"已删除实例（{inst['zone']}，IP {inst['external_ip'] or '-'}）"
            if addr and addr.get("name", "").startswith(STATIC_PREFIX):
                try:
                    client.release_address(region, addr["name"])
                    msg += f"，并释放静态 IP {addr.get('address')}"
                except GCPError as e:
                    msg += f"；释放静态 IP 失败：{e}，请到 GCP 控制台手动释放以免计费"
            elif addr:
                msg += f"；静态 IP {addr.get('address')} 非本系统创建，已保留（未绑定的静态 IP 会计费）"
            self._event(inst, "delete", msg, "warn", notify=True)
            self.db.delete_instance(instance_id)
        finally:
            lock.release()

    # ---------------- traffic ----------------
    def _traffic_token(self, inst: dict) -> str:
        token = inst.get("traffic_token") or ""
        if not token:
            token = secrets.token_hex(16)
            self.db.update_instance(inst["id"], traffic_token=token)
        return token

    def traffic_agent_script(self, instance_id: int, panel_url: str) -> str:
        inst = self._instance(instance_id)
        svc = str(self.db.get_settings().get("watch_service") or "")
        return agent_install_script(panel_url, self._traffic_token(inst), svc)

    def traffic_report(self, token: str, rx, tx, svc=None) -> bool:
        inst = self.db.find_instance_by_traffic_token(token)
        if not inst:
            return False
        try:
            rx_i, tx_i = int(rx), int(tx)
        except (TypeError, ValueError):
            raise ManagerError("rx / tx 必须是整数") from None
        if rx_i < 0 or tx_i < 0:
            raise ManagerError("rx / tx 不能为负数")
        svc_s = str(svc) if svc is not None else None
        if svc_s is not None and not SVC_STATE_RE.match(svc_s):
            svc_s = None
        self.traffic.report(inst["id"], rx_i, tx_i, svc=svc_s)
        return True
