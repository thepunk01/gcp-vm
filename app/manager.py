"""业务层：同步实例、开关机、更换 IP、自动检测循环。"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from .checker import STATE_BLOCKED, STATE_DOWN, STATE_OK, STATE_UNKNOWN, BlockChecker
from .db import Database
from .gcp import ClientPool, GCPClient, GCPError, region_of
from .integrations import DNSError, send_telegram, update_cloudflare_a_record

log = logging.getLogger(__name__)

STOPPED_STATES = {"TERMINATED", "STOPPED", "SUSPENDED"}
STATIC_PREFIX = "gcpvm-"


class ManagerError(Exception):
    pass


class Manager:
    def __init__(self, db: Database, pool: ClientPool | None = None, checker_cls=BlockChecker):
        self.db = db
        self.pool = pool or ClientPool()
        self.checker_cls = checker_cls
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
        inst = self._instance(instance_id)
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
        inst = self._instance(instance_id)
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
        avoid = self.db.recent_blocked_ips()
        if old_ip:
            avoid.add(old_ip)

        try:
            if inst["ip_mode"] == "static":
                new_ip = self._swap_static(client, inst, live, region, ac_name, tier)
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

        if old_ip and reason != "手动":
            self.db.add_blocked_ip(old_ip, inst["id"])
        inst = self.db.update_instance(
            inst["id"], last_change_at=time.time(), fail_count=0, cn_state=STATE_UNKNOWN
        )
        inst = self._refresh(inst, client)
        self._event(
            inst, "change_ip", f"IP 已更换（{reason}）：{old_ip or '-'} -> {new_ip}", "warn", notify=True
        )
        self._update_dns(inst, new_ip)
        return inst

    def _swap_ephemeral(self, client, inst, live, ac_name, tier, avoid: set[str]) -> str:
        new_ip = ""
        for attempt in range(1, 4):
            if live["external_ip"] or attempt > 1:
                cur = client.get_instance(inst["zone"], inst["name"])
                if cur["access_config_name"]:
                    client.delete_access_config(
                        inst["zone"], inst["name"], cur["nic_name"], cur["access_config_name"]
                    )
            client.add_access_config(inst["zone"], inst["name"], live["nic_name"], ac_name, None, tier)
            new_ip = client.get_instance(inst["zone"], inst["name"])["external_ip"]
            if new_ip and new_ip not in avoid:
                return new_ip
            log.info("[%s] 第 %d 次分配到的 IP %s 在规避列表中，重新分配", inst["name"], attempt, new_ip)
        if not new_ip:
            raise GCPError("未能分配到新的外网 IP")
        return new_ip

    def _swap_static(self, client, inst, live, region, ac_name, tier) -> str:
        old_ip = live["external_ip"]
        old_addr = client.find_address_by_ip(region, old_ip) if old_ip else None
        addr_name = f"{STATIC_PREFIX}{inst['name']}-{datetime.now().strftime('%Y%m%d%H%M%S')}"[:63]
        new_ip = client.reserve_address(region, addr_name, tier)
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
