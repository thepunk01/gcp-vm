"""业务层：同步实例、开关机、更换 IP、自动检测循环。"""

from __future__ import annotations

import json
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
from . import azure as az
from .firewall import build_body, summarize
from .provision import IMAGES, build_instance_body, filter_machine_types, sort_zones
from .integrations import send_telegram
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
EXTRA_PREFIX = "gcpvmx-"  # 附加 IP 的静态地址 / 转发规则 / 目标实例（与 gcpvm- 区分，不会被残留清理误删）
MAX_EXTRA_IPS = 8
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
        self._acct_fails: dict[int, int] = {}
        self._suspended_seen: dict[int, int] = {}
        self._stopped_since: dict[int, float] = {}
        self._unreachable: set[int] = set()  # 已判定停用、且连查询都失败的账号
        self._replace_warned: set[int] = set()
        self._replace_lock = threading.Lock()

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
        """云实例（GCP / Azure）。"""
        inst = self._instance(instance_id)
        if inst.get("provider") == "manual":
            raise ManagerError("自有服务器没有云厂商 API，不支持此操作（开关机 / 换 IP / 启动脚本等）")
        if inst.get("status") == "LOST":
            raise ManagerError("该实例在云端已不存在，等待自动替补（或删除这条记录）")
        return inst

    def _only_gcp(self, instance_id: int) -> dict:
        inst = self._gcp_instance(instance_id)
        if inst.get("provider") == "azure":
            raise ManagerError("Azure 实例不支持启动脚本 / 串口日志，请用「SSH 执行」")
        return inst

    @staticmethod
    def _loc(inst: dict) -> tuple[str, str]:
        """客户端定位实例的参数：GCP 为 (zone, name)，Azure 为 (资源组, name)。"""
        if inst.get("provider") == "azure":
            return inst["project"], inst["name"]
        return inst["zone"], inst["name"]

    def _event(self, inst: dict | None, type_: str, msg: str, level: str = "info", notify: bool = False):
        iid = inst["id"] if inst else None
        prefix = f"[{inst['name']}] " if inst else ""
        log.log(logging.WARNING if level in ("warn", "error") else logging.INFO, "%s%s", prefix, msg)
        self.db.add_event(type_, msg, iid, level)
        if notify:
            send_telegram(self.db.get_settings(), f"GCP-VM {prefix}{msg}")

    def _refresh(self, inst: dict, client: GCPClient | None = None) -> dict:
        client = client or self._client(inst["account_id"])
        live = client.get_instance(*self._loc(inst))
        return self.db.upsert_instance(inst["account_id"], live) | {"_live": live}

    # ---------------- sync ----------------
    def sync_account(self, account_id: int) -> int:
        client = self._client(account_id)
        items = client.list_instances()
        keep = [self.db.upsert_instance(account_id, it)["id"] for it in items]
        # 开启自动替补时，云端消失的「替补」实例保留记录并标记 LOST，等待替补
        if self.db.get_settings().get("replace_enabled"):
            for row in self.db.list_instances():
                if row["account_id"] != account_id or row["id"] in keep or not row.get("auto_replace"):
                    continue
                keep.append(row["id"])
                if row["status"] != "LOST":
                    self.db.update_instance(row["id"], status="LOST")
                    self._event(row, "replace", f"实例在云端已不存在（被删除或回收），IP {row['external_ip'] or '-'}，等待自动替补", "error", notify=True)
        self.db.remove_missing_instances(account_id, keep)
        return len(items)

    def sync_all(self) -> dict:
        result: dict = {}
        for acc in self.db.list_accounts("cloud"):
            if acc.get("status") == "banned":
                result[acc["name"]] = "已标记为被封/停用，跳过（可在账号页解除标记）"
                continue
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
            getattr(client, action)(*self._loc(inst))
            inst = self._refresh(inst, client)
            self._event(
                inst, action, f"{source}{label}成功，当前状态 {inst['status']}，IP {inst['external_ip'] or '-'}",
                notify=source != "手动",
            )
            return inst
        except GCPError as e:
            self._event(inst, action, f"{source}{label}失败：{e}", "error", notify=True)
            self._note_account_error(inst, e)
            raise ManagerError(str(e)) from e
        finally:
            lock.release()

    def _note_account_error(self, inst: dict, e: Exception) -> None:
        """操作失败且是账号级错误（结算停用 / 项目暂停 / 无权限）：连续出现 ACCOUNT_FAIL_LIMIT 次判定账号停用。
        只出现一次不判定，避免 GCP 偶发的结算校验错误把整个账号的机器都替补掉。判定后由自动替补接手。"""
        if inst.get("provider") != "gcp" or not self._is_account_fatal(e):
            return
        acc = self.db.get_account(inst["account_id"], with_key=False)
        if not acc or acc.get("status") == "banned":
            return
        n = self._acct_fails.get(acc["id"], 0) + 1
        self._acct_fails[acc["id"]] = n
        if n < self.ACCOUNT_FAIL_LIMIT:
            return
        self.db.set_account_status(acc["id"], "banned", str(e))
        follow = ("。仍在正常运行的实例会保留；停机、没有外网 IP、被墙的勾选了自动替补的实例会被替补"
                  if self.db.get_settings().get("replace_enabled") else "")
        self._event(inst, "replace", f"账号 {acc['name']} 无法操作（结算停用 / 被暂停 / 无权限），已判定为停用（换 IP、新建等不可用）{follow}。原因：{str(e)[:200]}",
                    "error", notify=True)

    # ---------------- change ip ----------------
    def change_ip(self, instance_id: int, reason: str = "手动") -> dict:
        inst = self._gcp_instance(instance_id)
        lock = self._lock_for(instance_id)
        if not lock.acquire(timeout=1):
            raise ManagerError("该实例正在执行其他操作，请稍后")
        try:
            if inst.get("provider") == "azure":
                return self._change_ip_azure(inst, reason)
            return self._change_ip_locked(inst, reason)
        except ManagerError as e:
            self._note_account_error(inst, e)
            raise
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
        verify, vinfo = self._make_verifier(inst)

        try:
            if inst["ip_mode"] == "static":
                new_ip = self._swap_static(client, inst, live, region, ac_name, tier, avoid, verify)
            else:
                new_ip = self._swap_ephemeral(client, inst, live, ac_name, tier, avoid, verify)
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
        return self._after_change(inst, client, old_ip, new_ip, vinfo, reason)

    def _after_change(self, inst: dict, client, old_ip: str, new_ip: str, vinfo: dict, reason: str) -> dict:
        inst = self.db.update_instance(
            inst["id"], last_change_at=time.time(), fail_count=0,
            cn_state=STATE_OK if vinfo["verified"] else STATE_UNKNOWN,
        )
        inst = self._refresh(inst, client)
        note = ""
        if vinfo["verified"]:
            note = "，已验证国内可达" + (f"（跳过 {vinfo['rejected']} 个被墙的新 IP）" if vinfo["rejected"] else "")
        elif vinfo["skipped"]:
            note = f"，未验证（{vinfo['skipped']}）"
        self._event(
            inst, "change_ip", f"IP 已更换（{reason}）：{old_ip or '-'} -> {new_ip}{note}", "warn", notify=True
        )
        return inst

    def _change_ip_azure(self, inst: dict, reason: str) -> dict:
        """Azure：新建 Standard 静态公网 IP 换到网卡上，验证通过后删除旧 IP；被墙的候选 IP 先占住，结束后统一删除。"""
        client = self._client(inst["account_id"])
        rg, name = inst["project"], inst["name"]
        live = client.get_instance(rg, name)
        if not live["nic_id"]:
            raise ManagerError("找不到虚拟机的网卡")
        old_ip, old_pip = live["external_ip"], live["pip_id"]
        if old_ip and reason != "手动":
            self.db.add_blocked_ip(old_ip, inst["id"])
        try:  # 清理之前中断残留的、未绑定的本系统公网 IP
            for p in client.list_public_ips(rg):
                if p["name"].startswith(az.PREFIX) and not p["attached"] and p["id"].lower() != old_pip.lower():
                    client.delete_resource(p["id"])
                    self._event(inst, "change_ip", f"已删除残留的未使用公网 IP {p['name']}（{p['address'] or '-'}）")
        except GCPError as e:
            log.warning("[%s] 清理残留公网 IP 失败：%s", name, e)
        avoid = self.db.recent_blocked_ips()
        if old_ip:
            avoid.add(old_ip)
        verify, vinfo = self._make_verifier(inst)
        stamp = datetime.now().strftime("%m%d%H%M%S") + secrets.token_hex(2)
        rejected: list[str] = []
        attached = ""
        new_ip = ""
        try:
            try:
                for attempt in range(1, MAX_EPHEMERAL_TRIES + 1):
                    pname = f"{az.PREFIX}{name}"[:60].rstrip("-") + f"-{stamp}-{attempt}"
                    pid, ip = client.create_public_ip(rg, inst["zone"], pname, live.get("pip_zones") or None)
                    if not ip or ip in avoid:
                        rejected.append(pid)
                        continue
                    client.set_nic_public_ip(live["nic_id"], live["ipconfig_name"], pid)
                    attached = pid
                    if verify is None:
                        new_ip = ip
                        break
                    ok, detail = verify(ip)
                    if ok:
                        new_ip = ip
                        break
                    avoid.add(ip)
                    self._reject_candidate(inst, ip, detail, attempt)
                    rejected.append(pid)
                if not new_ip:
                    raise GCPError(f"连续 {MAX_EPHEMERAL_TRIES} 个新公网 IP 都是被墙的，本次放弃；下一轮检测会继续尝试")
            finally:
                for pid in rejected:
                    if pid == attached:
                        continue
                    try:
                        client.delete_resource(pid)
                    except GCPError as e:
                        self._event(inst, "change_ip", f"删除被拒绝的公网 IP 失败：{e}", "warn")
        except GCPError as e:
            self._event(inst, "change_ip", f"更换 IP 失败（{reason}）：{e}", "error", notify=True)
            raise ManagerError(str(e)) from e
        if old_pip and old_pip.lower() != attached.lower():
            try:
                client.delete_resource(old_pip)
            except GCPError as e:
                self._event(inst, "change_ip", f"删除旧公网 IP {old_ip} 失败：{e}，请到 Azure 门户删除以免计费", "warn")
        return self._after_change(inst, client, old_ip, new_ip, vinfo, reason)

    def _make_verifier(self, inst: dict):
        """生成“新 IP 国内可达性”验证函数：返回 (是否接受, 说明)。

        只有探针明确判定为被墙才拒绝；探针不可用 / 服务暂时不通时接受（无法验证），避免卡住。
        """
        settings = self.db.get_settings()
        vinfo = {"verified": False, "rejected": 0, "skipped": ""}
        if not settings.get("verify_new_ip", True):
            return None, vinfo
        port = int(inst.get("check_port") or 22)

        def verify(ip: str) -> tuple[bool, str]:
            res = self.checker_cls(settings).check(ip, port)
            if res.state == STATE_DOWN:  # 刚绑定，服务可能还没在新 IP 上响应，稍等再测一次
                self._sleep(5)
                res = self.checker_cls(settings).check(ip, port)
            if res.state == STATE_BLOCKED:
                vinfo["rejected"] += 1
                return False, res.detail
            if res.state == STATE_OK:
                vinfo["verified"] = True
                vinfo["skipped"] = ""
            else:
                vinfo["skipped"] = res.detail or "探针不可用"
            return True, res.detail

        return verify, vinfo

    def _reject_candidate(self, inst: dict, ip: str, detail: str, n: int) -> None:
        self.db.add_blocked_ip(ip, inst["id"])
        self._event(inst, "change_ip", f"新 IP {ip} 验证未通过（{detail}），已记为被墙，继续更换（第 {n} 个）", "warn")

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

    def _swap_ephemeral(self, client, inst, live, ac_name, tier, avoid: set[str], verify=None) -> str:
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
                    if verify is None:
                        return new_ip
                    ok, detail = verify(new_ip)
                    if ok:
                        return new_ip
                    avoid.add(new_ip)
                    self._reject_candidate(inst, new_ip, detail, attempt)
                    continue
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

    def _swap_static(self, client, inst, live, region, ac_name, tier, avoid: set[str], verify=None) -> str:
        """预留新的静态 IP 并绑定。

        - 预留到已知被墙的 IP：保留（占住）它，继续预留下一个；
        - 开启验证时：绑定后用国内探针检测，被墙就记入名单、保留占住，继续换下一个；
        - 拿到可用 IP 后统一释放这些被拒绝的地址，保证不会换回被墙 IP。
        """
        zone, iname = inst["zone"], inst["name"]
        old_ip = live["external_ip"]
        old_addr = client.find_address_by_ip(region, old_ip) if old_ip else None
        base = (
            f"{STATIC_PREFIX}{iname}"[:40].rstrip("-")
            + "-" + datetime.now().strftime("%m%d%H%M%S") + secrets.token_hex(2)
        )
        rejected: list[str] = []
        attached = ""  # 当前绑定在实例上的（本次新预留的）地址名
        new_ip = ""
        try:
            for attempt in range(1, MAX_EPHEMERAL_TRIES + 1):
                name = f"{base}-{attempt}"
                ip = client.reserve_address(region, name, tier)
                if not ip or ip in avoid:
                    log.info("[%s] 第 %d 次预留到的静态 IP %s 在规避列表中，先占住并重新预留", iname, attempt, ip)
                    rejected.append(name)
                    continue
                try:
                    cur = client.get_instance(zone, iname)
                    if cur["access_config_name"]:
                        client.delete_access_config(zone, iname, cur["nic_name"], cur["access_config_name"])
                    client.add_access_config(zone, iname, live["nic_name"], ac_name, ip, tier)
                except GCPError:
                    try:
                        client.release_address(region, name)
                    except GCPError:
                        pass
                    raise
                attached = name
                if verify is None:
                    new_ip = ip
                    break
                ok, detail = verify(ip)
                if ok:
                    new_ip = ip
                    break
                avoid.add(ip)
                self._reject_candidate(inst, ip, detail, attempt)
                rejected.append(name)
            if not new_ip:
                raise GCPError(
                    f"连续 {MAX_EPHEMERAL_TRIES} 个新静态 IP 都是被墙的（已知被墙或验证国内不可达），本次放弃；"
                    "下一轮检测会继续尝试，若持续如此，建议换区域"
                )
        finally:
            for name in rejected:
                if name == attached:
                    continue  # 正绑定在实例上的不能释放，下次换 IP 时会作为旧地址释放
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

    # ---------------- check ----------------
    def check_instance(self, instance_id: int, settings: dict | None = None) -> dict:
        """只检测，不处理。"""
        inst = self._instance(instance_id)
        settings = settings or self.db.get_settings()
        result = self.checker_cls(settings).check(inst["external_ip"], inst["check_port"])
        self.db.update_instance(instance_id, cn_state=result.state, last_check=time.time())
        return result.to_dict()

    def _check_only(self, inst: dict, settings: dict) -> None:
        if inst["status"] != "RUNNING" or not inst["external_ip"] or not inst.get("auto_replace"):
            return
        res = self.checker_cls(settings).check(inst["external_ip"], inst["check_port"])
        if res.state == STATE_BLOCKED:
            self.db.update_instance(inst["id"], cn_state=STATE_BLOCKED, fail_count=inst["fail_count"] + 1, last_check=time.time())
        else:
            self.db.update_instance(inst["id"], cn_state=res.state, fail_count=0 if res.state == STATE_OK else inst["fail_count"], last_check=time.time())

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
        if inst["status"] == "LOST":
            return
        if inst.get("account_status") == "banned":
            self._check_only(inst, settings)  # 停用账号没法换 IP，只更新检测状态，供替补判断
            return
        if not (inst["auto_ip"] or inst["auto_start"]):
            return
        client = self._client(inst["account_id"])
        provider = inst.get("provider", "gcp")
        inst = self._refresh(inst, client) | {"provider": provider}

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

        # 没有外网 IP（例如上次换 IP 删掉旧 IP 后加新 IP 失败）：先补回一个
        if not inst["external_ip"] and inst.get("provider") == "gcp":
            self._event(inst, "change_ip", "实例没有外网 IP（可能上次换 IP 中途失败），尝试重新分配", "warn")
            try:
                self.change_ip(inst["id"], reason="重新分配外网 IP")
            except ManagerError:
                pass
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
            try:
                self.replace_cycle(settings)
            except Exception as e:  # noqa: BLE001
                log.exception("自动替补出错")
                self._event(None, "replace", f"自动替补出错：{e}", "error")
            extras = {}
            for x in self.db.list_extra_ips():
                extras.setdefault(x["instance_id"], []).append(x)
            for inst in self.db.list_instances():
                try:
                    self.process_instance(inst, settings)
                    if inst["id"] in extras:
                        self.process_extras(inst["id"], settings)
                except Exception as e:  # noqa: BLE001
                    log.exception("处理实例 %s 出错", inst["name"])
                    self._event(inst, "check", f"检测出错：{e}", "error")
        finally:
            self._cycle_lock.release()

    # ---------------- 附加 IP（GCP 协议转发） ----------------
    @staticmethod
    def _ti_name(inst: dict) -> str:
        return f"{EXTRA_PREFIX}ti-{inst['name']}"[:63].rstrip("-")

    def _extra_ctx(self, extra_id: int) -> tuple[dict, dict]:
        ex = self.db.get_extra_ip(extra_id)
        if not ex:
            raise ManagerError("附加 IP 不存在")
        return ex, self._only_gcp(ex["instance_id"])

    def _new_extra(self, client, inst: dict, region: str, avoid: set[str], verify) -> tuple[str, str, dict]:
        """预留静态地址 + 建转发规则；被墙（已知或验证不通过）的先占住继续换，最后统一释放。返回 (ip, 资源名, 验证信息)。"""
        tier = inst.get("network_tier") or "PREMIUM"
        self._cleanup_extra_leftovers(client, inst, region)
        target = client.ensure_target_instance(inst["zone"], inst["name"], self._ti_name(inst))
        base = f"{EXTRA_PREFIX}{inst['name']}"[:40].rstrip("-") + "-" + datetime.now().strftime("%m%d%H%M%S") + secrets.token_hex(2)
        rejected: list[str] = []

        def reserve(name: str) -> str:
            try:
                return client.reserve_address(region, name, tier)
            except GCPError as e:
                if not self._is_quota(e) or not rejected:
                    raise
            # 静态地址配额不够：先释放占住的被墙候选（它们已在规避列表里，再分到会被跳过），再试一次
            self._release_names(client, inst, region, rejected)
            rejected.clear()
            return client.reserve_address(region, name, tier)

        try:
            for attempt in range(1, MAX_EPHEMERAL_TRIES + 1):
                name = f"{base}-{attempt}"
                ip = reserve(name)
                if not ip or ip in avoid:
                    rejected.append(name)
                    continue
                try:
                    client.insert_forwarding_rule(region, name, ip, target, tier)
                except GCPError:
                    rejected.append(name)
                    raise
                if verify is None:
                    return ip, name, {}
                ok, detail = verify(ip)
                if ok:
                    return ip, name, {}
                avoid.add(ip)
                self._reject_candidate(inst, ip, detail, attempt)
                client.delete_forwarding_rule(region, name)
                rejected.append(name)
            raise GCPError(f"连续 {MAX_EPHEMERAL_TRIES} 个新 IP 都是被墙的，本次放弃")
        finally:
            self._release_names(client, inst, region, rejected)

    @staticmethod
    def _is_quota(e: Exception) -> bool:
        return "quota" in str(e).lower()

    def _release_names(self, client, inst: dict, region: str, names: list[str]) -> None:
        for name in names:
            try:
                client.release_address(region, name)
            except GCPError as e:
                self._event(inst, "change_ip", f"释放被拒绝的地址 {name} 失败：{e}", "warn")

    def _cleanup_extra_leftovers(self, client, inst: dict, region: str) -> None:
        """释放附加 IP 残留的静态地址（未绑定转发规则、面板也没在用、创建超过 30 分钟），避免占满配额。"""
        known = {x["res_name"] for x in self.db.list_extra_ips()}
        now = datetime.now()
        try:
            addrs = client.list_addresses(region)
        except GCPError as e:
            log.warning("查询静态地址失败：%s", e)
            return
        for a in addrs:
            if not a["name"].startswith(EXTRA_PREFIX) or a["status"] != "RESERVED" or a["name"] in known:
                continue
            m = re.search(r"-(\d{10})[0-9a-f]{4}-\d+$", a["name"])
            if m:
                try:
                    made = datetime.strptime(f"{now.year}{m.group(1)}", "%Y%m%d%H%M%S")
                    if 0 <= (now - made).total_seconds() < 1800:
                        continue  # 可能是正在进行中的操作
                except ValueError:
                    pass
            try:
                client.release_address(region, a["name"])
                self._event(inst, "change_ip", f"已释放残留的附加 IP 地址 {a['name']}（{a['address'] or '-'}）")
            except GCPError as e:
                log.warning("释放残留地址 %s 失败：%s", a["name"], e)

    def _drop_extra_resources(self, client, inst: dict, ex: dict) -> None:
        if not ex.get("res_name"):
            return
        client.delete_forwarding_rule(ex["region"], ex["res_name"])
        try:
            client.release_address(ex["region"], ex["res_name"])
        except GCPError as e:
            if "HTTP 404" not in str(e):
                raise

    def add_extra_ip(self, instance_id: int) -> dict:
        inst = self._only_gcp(instance_id)
        if len(self.db.list_extra_ips(instance_id)) >= MAX_EXTRA_IPS:
            raise ManagerError(f"每台实例最多 {MAX_EXTRA_IPS} 个附加 IP")
        lock = self._lock_for(instance_id)
        if not lock.acquire(blocking=False):
            raise ManagerError("该实例正在处理其他操作，请稍后再试")
        try:
            client = self._client(inst["account_id"])
            region = region_of(inst["zone"])
            avoid = self.db.recent_blocked_ips() | {inst["external_ip"]} | {x["ip"] for x in self.db.list_extra_ips(instance_id)}
            verify, vinfo = self._make_verifier(inst) if inst["status"] == "RUNNING" else (None, {"verified": False})
            try:
                ip, name, _ = self._new_extra(client, inst, region, avoid, verify)
            except GCPError as e:
                msg = str(e)
                if "restrictProtocolForwardingCreationForTypes" in msg:
                    msg = ("该项目的组织政策禁止创建外部协议转发（constraints/compute.restrictProtocolForwardingCreationForTypes）。"
                           "请在 GCP 控制台「IAM 和管理 → 组织政策」把「Restrict Protocol Forwarding Based on type of IP Address」"
                           "改为允许全部（需要组织政策管理员权限），再重试。")
                elif "QUOTA" in msg.upper() or "Quota" in msg:
                    msg = f"该区域的外部 IP 配额不足（IN_USE_ADDRESSES / STATIC_ADDRESSES），请在 GCP 配额页申请提高。原始错误：{msg}"
                self._event(inst, "change_ip", f"添加附加 IP 失败：{msg}", "error")
                raise ManagerError(msg) from e
            state = STATE_OK if vinfo.get("verified") else STATE_UNKNOWN
            ex = self.db.add_extra_ip(instance_id, ip, region, name, state)
            self._event(inst, "change_ip", f"已添加附加 IP {ip}（所有端口转发到本机）", notify=True)
            return ex
        finally:
            lock.release()

    def change_extra_ip(self, extra_id: int, reason: str = "手动") -> dict:
        ex, inst = self._extra_ctx(extra_id)
        lock = self._lock_for(inst["id"])
        if not lock.acquire(blocking=False):
            raise ManagerError("该实例正在处理其他操作，请稍后再试")
        try:
            client = self._client(inst["account_id"])
            old = ex["ip"]
            if old and reason != "手动":
                self.db.add_blocked_ip(old, inst["id"])
            avoid = self.db.recent_blocked_ips() | {old, inst["external_ip"]} | {x["ip"] for x in self.db.list_extra_ips(inst["id"])}
            verify, vinfo = self._make_verifier(inst) if inst["status"] == "RUNNING" else (None, {"verified": False})
            dropped = False
            try:
                try:
                    ip, name, _ = self._new_extra(client, inst, ex["region"], avoid, verify)
                except GCPError as e:
                    if not self._is_quota(e) or not ex.get("res_name"):
                        raise
                    self._event(inst, "change_ip", f"静态地址配额已满，先释放旧附加 IP {old} 再分配新的", "warn")
                    self._drop_extra_resources(client, inst, ex)
                    dropped = True
                    self.db.update_extra_ip(extra_id, ip="", res_name="", cn_state=STATE_UNKNOWN)
                    ip, name, _ = self._new_extra(client, inst, ex["region"], avoid, verify)
            except GCPError as e:
                msg = str(e)
                if self._is_quota(e):
                    msg = f"该区域静态 IP 配额已满（STATIC_ADDRESSES），请到 GCP「IAM 和管理 → 配额」申请提高，或删掉不用的 IP。原始错误：{msg}"
                self._event(inst, "change_ip", f"附加 IP {old or '(空)'} 更换失败（{reason}）：{msg}", "error", notify=True)
                raise ManagerError(msg) from e
            if not dropped:
                try:
                    self._drop_extra_resources(client, inst, ex)
                except GCPError as e:
                    self._event(inst, "change_ip", f"删除旧附加 IP {old} 的转发规则/地址失败：{e}", "warn")
            ex = self.db.update_extra_ip(
                extra_id, ip=ip, res_name=name, fail_count=0, last_change_at=time.time(),
                cn_state=STATE_OK if vinfo.get("verified") else STATE_UNKNOWN,
            )
            self._event(inst, "change_ip", f"IP 已更换（{reason}·附加 IP）：{old} -> {ip}", "warn", notify=True)
            return ex
        finally:
            lock.release()

    def remove_extra_ip(self, extra_id: int) -> None:
        ex = self.db.get_extra_ip(extra_id)
        if not ex:
            raise ManagerError("附加 IP 不存在")
        inst = self._instance(ex["instance_id"])
        if inst.get("status") != "LOST" and inst.get("account_status") != "banned":
            client = self._client(inst["account_id"])
            try:
                self._drop_extra_resources(client, inst, ex)
                if len(self.db.list_extra_ips(inst["id"])) <= 1:
                    client.delete_target_instance(inst["zone"], self._ti_name(inst))
            except GCPError as e:
                raise ManagerError(f"删除附加 IP 失败：{e}") from e
        self.db.delete_extra_ip(extra_id)
        self._event(inst, "change_ip", f"已删除附加 IP {ex['ip']}", "warn")

    def _remove_all_extras(self, client, inst: dict) -> None:
        extras = self.db.list_extra_ips(inst["id"])
        for ex in extras:
            try:
                self._drop_extra_resources(client, inst, ex)
            except GCPError as e:
                log.warning("清理附加 IP %s 失败：%s", ex["ip"], e)
            self.db.delete_extra_ip(ex["id"])
        if extras:
            try:
                client.delete_target_instance(inst["zone"], self._ti_name(inst))
            except GCPError as e:
                log.warning("删除目标实例失败：%s", e)

    def check_extra_ip(self, extra_id: int) -> dict:
        ex, inst = self._extra_ctx(extra_id)
        res = self.checker_cls(self.db.get_settings()).check(ex["ip"], inst["check_port"])
        self.db.update_extra_ip(extra_id, cn_state=res.state, last_check=time.time())
        return res.to_dict()

    def process_extras(self, instance_id: int, settings: dict) -> None:
        """附加 IP 的被墙检测与自动更换（跟随实例的「被墙自动换 IP」开关；冷却按每个 IP 单独计算）。"""
        inst = self.db.get_instance(instance_id)
        if not inst or not inst["auto_ip"] or inst["status"] != "RUNNING":
            return
        inst = self._instance(instance_id)
        if inst.get("account_status") == "banned":
            return
        threshold = int(settings["fail_threshold"])
        for ex in self.db.list_extra_ips(instance_id):
            if not ex["ip"]:
                try:
                    self.change_extra_ip(ex["id"], reason="重新分配")
                except ManagerError:
                    pass
                continue
            res = self.checker_cls(settings).check(ex["ip"], inst["check_port"])
            now = time.time()
            if res.state != STATE_BLOCKED:
                if res.state == STATE_OK and ex["cn_state"] == STATE_BLOCKED:
                    self._event(inst, "check", f"附加 IP {ex['ip']} 国内恢复可达")
                self.db.update_extra_ip(ex["id"], cn_state=res.state, fail_count=0 if res.state == STATE_OK else ex["fail_count"], last_check=now)
                continue
            fails = ex["fail_count"] + 1
            self.db.update_extra_ip(ex["id"], cn_state=STATE_BLOCKED, fail_count=fails, last_check=now)
            self._event(inst, "check", f"附加 IP {ex['ip']}:{inst['check_port']} {res.detail}（{fails}/{threshold}）", "warn")
            if fails < threshold:
                continue
            why = self._can_auto_change(inst | {"last_change_at": ex["last_change_at"]}, settings)
            if why:
                self._event(inst, "check", f"附加 IP {ex['ip']} 暂不自动更换：{why}", "warn")
                continue
            self.db.add_event("change_ip_auto", f"自动换附加 IP {ex['ip']}", inst["id"])
            try:
                self.change_extra_ip(ex["id"], reason="自动-被墙")
            except ManagerError:
                pass

    # ---------------- 自动替补（仅 GCP） ----------------
    ACCOUNT_FAIL_LIMIT = 2  # 连续几轮检测都确认账号不可用，才判定为被封

    @staticmethod
    def _is_account_fatal(e: Exception) -> bool:
        """账号级不可用：认证失败、无权限、项目被暂停/删除、结算停用、API 被禁用。网络错误、5xx 不算。"""
        msg = str(e)
        low = msg.lower()
        if "获取访问令牌失败" in msg and ("http 400" in low or "http 401" in low):
            return True
        hints = ("http 401", "http 403", "suspend", "billing", "invalid_grant", "scheduled for deletion",
                 "has been deleted", "account not found", "is disabled", "it is disabled", "project not found")
        return any(h in low for h in hints)

    def _gcp_accounts(self) -> list[dict]:
        return self.db.list_accounts("gcp")

    def _slot_counts(self) -> dict[int, int]:
        counts: dict[int, int] = {}
        for i in self.db.list_instances():
            if i["provider"] == "gcp" and i["status"] != "LOST":
                counts[i["account_id"]] = counts.get(i["account_id"], 0) + 1
        return counts

    def _candidate_accounts(self, settings: dict, after_id: int = 0, exclude: int | None = None) -> list[dict]:
        """有空位、未被封的 GCP 账号，从 after_id 之后的下一个账号开始轮。"""
        cap = max(1, int(settings.get("replace_max_per_account", 3)))
        counts = self._slot_counts()
        accs = [a for a in self._gcp_accounts() if a["status"] != "banned" and a["id"] != exclude
                and counts.get(a["id"], 0) < cap]
        return sorted(accs, key=lambda a: (a["id"] <= after_id, a["id"]))

    @staticmethod
    def template_for(settings: dict, group: str) -> dict:
        """分组有专属替补模板就用它，否则用默认模板。"""
        tpl = dict(config.DEFAULT_SETTINGS["replace_template"]) | dict(settings.get("replace_template") or {})
        own = (settings.get("replace_templates") or {}).get((group or "").strip())
        if own:
            tpl = tpl | dict(own) | {"grp": group.strip()}
        return tpl

    @staticmethod
    def _boot_names(inst: dict | None) -> list[str]:
        try:
            names = json.loads((inst or {}).get("boot_scripts") or "[]")
        except ValueError:
            return []
        return [str(n) for n in names if str(n).strip()] if isinstance(names, list) else []

    def _create_from_template(self, acc: dict, settings: dict, base: dict | None = None, group: str = "",
                              place: tuple[str, str] | None = None) -> dict:
        group = ((base or {}).get("grp") or group or "").strip()
        tpl = self.template_for(settings, group)
        if place:
            tpl = tpl | {"zone": place[0], "machine_type": place[1]}
        prefix = re.sub(r"[^a-z0-9-]", "", str(tpl.get("name_prefix") or "gcp").lower()).strip("-") or "gcp"
        if not prefix[0].isalpha():
            prefix = "vm-" + prefix
        name = f"{prefix[:40]}-{datetime.now():%m%d%H%M}-{secrets.token_hex(2)}"
        token = secrets.token_hex(16)
        script = str(tpl.get("startup_script") or "").replace("\r\n", "\n")
        # 原实例上勾选的常用脚本，按顺序追加到开机脚本
        boot = self._boot_names(base)
        presets = {p["name"]: p["script"] for p in settings.get("script_presets") or []}
        missing = [n for n in boot if n not in presets]
        for n in boot:
            if n in presets:
                script += f"\n\n# ===== 常用脚本：{n} =====\necho 'gcpvm: 开始执行「{n}」'\n(\n{presets[n]}\n) || echo 'gcpvm: 「{n}」执行失败'\n"
        panel = str(settings.get("panel_url") or "")
        if tpl.get("install_agent", True) and panel:
            try:
                agent = agent_install_script(panel, token, str(settings.get("watch_service") or ""))
                script = f"# 面板流量 / 服务状态上报\n(\n{agent}\n) || echo 'gcpvm: 上报安装失败'\n\n{script}"
            except ValueError as e:
                log.warning("生成上报脚本失败：%s", e)
        spot = bool(tpl.get("spot"))
        data = {
            "name": name, "zone": tpl.get("zone"), "machine_type": tpl.get("machine_type"), "image": tpl.get("image"),
            "disk_size_gb": tpl.get("disk_size_gb", 20), "disk_type": tpl.get("disk_type", "pd-balanced"),
            "network_tier": tpl.get("network_tier", "PREMIUM"), "spot": spot, "tags": tpl.get("tags", ""),
            "startup_script": script, "run_once": True,
            "auto_ip": bool(base["auto_ip"]) if base else True,
            "auto_start": bool(base["auto_start"]) if base else spot,
            "check_port": int(base["check_port"] if base else tpl.get("check_port") or 22),
            "note": f"自动替补 {base['name']}" if base else "按替补模板新建",
        }
        if not script.strip():
            data.pop("startup_script")
        inst = self.create_instance(acc["id"], data)
        if missing:
            self._event(inst, "replace", f"勾选的常用脚本已不存在，已跳过：{'、'.join(missing)}", "warn")
        cfg = {
            "auto_replace": True,
            "boot_scripts": boot,
            "grp": group or tpl.get("grp") or "",
            "ip_mode": (base or {}).get("ip_mode") or tpl.get("ip_mode") or "static",
        }
        if cfg["ip_mode"] not in ("ephemeral", "static"):
            cfg["ip_mode"] = "static"
        self.db.update_instance(inst["id"], traffic_token=token)
        inst = self.db.update_instance_config(inst["id"], cfg)
        return inst

    def _places(self, settings: dict, base: dict | None, group: str) -> list[tuple[str, str] | None]:
        """新建的位置顺序：开启「沿用原实例」时先用原实例的可用区和机型，再退回模板。None 表示模板。"""
        tpl = self.template_for(settings, ((base or {}).get("grp") or group or "").strip())
        orig = None
        if base and tpl.get("follow_original", True) and base.get("zone") and base.get("machine_type"):
            orig = (base["zone"], base["machine_type"])
        if orig and orig != (tpl.get("zone"), tpl.get("machine_type")):
            return [orig, None]
        return [None]

    def _try_create(self, accounts: list[dict], settings: dict, base: dict | None, group: str = "") -> tuple[dict | None, list[str]]:
        errors: list[str] = []
        places = self._places(settings, base, group)
        for acc in accounts:
            for place in places:
                where = f"{place[0]} · {place[1]}" if place else "模板配置"
                try:
                    return self._create_from_template(acc, settings, base, group, place), errors
                except ManagerError as e:
                    errors.append(f"{acc['name']}（{where}）：{e}")
                    fatal = self._is_account_fatal(e)
                    nxt = "尝试下一个账号" if fatal or place is places[-1] else "改用模板的可用区和机型重试"
                    self._event(None, "replace", f"[{acc['name']}] 新建失败（{where}）：{e}，{nxt}", "warn")
                    if fatal:
                        self._acct_fails[acc["id"]] = self._acct_fails.get(acc["id"], 0) + 1
                        break
        return None, errors

    def create_from_template(self, group: str = "") -> dict:
        """手动：按（分组的）替补模板在第一个有空位的账号新建一台（用于首次开满或测试模板）。"""
        settings = self.db.get_settings()
        with self._replace_lock:
            accounts = self._candidate_accounts(settings)
            if not accounts:
                raise ManagerError(f"没有可用的 GCP 账号（都已开满 {settings.get('replace_max_per_account', 3)} 台或已被封）")
            inst, errors = self._try_create(accounts, settings, None, group)
            if not inst:
                raise ManagerError("所有可用账号都创建失败：" + "；".join(errors)[:800])
            return inst

    def replace_cycle(self, settings: dict | None = None) -> None:
        settings = settings or self.db.get_settings()
        if not settings.get("replace_enabled"):
            return
        with self._replace_lock:
            # 1) 逐个账号同步：发现账号被封、实例被删除。已判定停用的账号也尽量同步（只读），
            #    结算受限时往往还能查询，正在正常运行的实例就不必替补
            for acc in self._gcp_accounts():
                if acc["status"] == "banned":
                    try:
                        self.sync_account(acc["id"])
                        self._unreachable.discard(acc["id"])
                    except Exception:  # noqa: BLE001
                        self._unreachable.add(acc["id"])
                    continue
                try:
                    self.sync_account(acc["id"])
                    self._acct_fails.pop(acc["id"], None)
                except GCPError as e:
                    if not self._is_account_fatal(e):
                        log.warning("同步账号 %s 失败（不判定为被封）：%s", acc["name"], e)
                        continue
                    n = self._acct_fails.get(acc["id"], 0) + 1
                    self._acct_fails[acc["id"]] = n
                    if n < self.ACCOUNT_FAIL_LIMIT:
                        self._event(None, "replace", f"[{acc['name']}] 账号访问失败（{n}/{self.ACCOUNT_FAIL_LIMIT}）：{str(e)[:300]}", "warn")
                        continue
                    self.db.set_account_status(acc["id"], "banned", str(e))
                    self._unreachable.add(acc["id"])
                    self._event(None, "replace", f"账号 {acc['name']} 判定为被封/停用：{str(e)[:300]}", "error", notify=True)
                except Exception as e:  # noqa: BLE001
                    log.warning("同步账号 %s 出错：%s", acc["name"], e)
            # 2) 停机的替补实例：停机超过设定时间先尝试开机，开不了就替补（账号级错误则判定账号停用）
            stopped_fail = self._handle_stopped(settings)
            # 3) 找出需要替补的实例
            todo: list[tuple[dict, str]] = [(i, "停机且无法开机") for i in stopped_fail]
            for inst in self.db.list_instances():
                if inst["provider"] != "gcp" or not inst.get("auto_replace"):
                    continue
                if inst["status"] == "SUSPENDED" and inst["account_status"] != "banned":
                    n = self._suspended_seen.get(inst["id"], 0) + 1
                    self._suspended_seen[inst["id"]] = n
                    if n >= 2:
                        todo.append((inst, "实例被挂起"))
                    continue
                self._suspended_seen.pop(inst["id"], None)
                if any(t[0]["id"] == inst["id"] for t in todo):
                    continue
                if inst["account_status"] == "banned":
                    why = self._banned_reason(inst, settings)
                    if why:
                        todo.append((inst, why))
                elif inst["status"] == "LOST":
                    todo.append((inst, "实例被删除"))
            # 4) 逐个替补
            limit = int(settings.get("replace_max_per_day", 6))
            for old, reason in todo:
                if self.db.count_type_since("replace_new", time.time() - 86400) >= limit:
                    if -1 not in self._replace_warned:
                        self._replace_warned.add(-1)
                        self._event(None, "replace", f"24 小时内已自动替补 {limit} 台，达到上限，暂停替补", "error", notify=True)
                    return
                self._replace_warned.discard(-1)
                self._replace_one(old, reason, settings)

    def _banned_reason(self, inst: dict, settings: dict) -> str:
        """停用账号里的实例是否需要替补：账号无法访问，或实例已停止 / 没有外网 IP / 被墙（停用账号没法换 IP）。
        仍在正常运行的保留，返回空字符串。"""
        if inst["account_id"] in self._unreachable or inst["status"] == "LOST":
            return "账号被封"
        if inst["status"] != "RUNNING":
            return "账号停用且实例已停止"
        if not inst["external_ip"]:
            return "账号停用且实例没有外网 IP"
        if inst["cn_state"] == STATE_BLOCKED and inst["fail_count"] >= int(settings["fail_threshold"]):
            return "账号停用且实例被墙"
        return ""

    def _handle_stopped(self, settings: dict) -> list[dict]:
        """返回「停机且开不了、账号本身正常」需要替补的实例。账号级错误会把账号标记为停用（随后整号替补）。"""
        minutes = int(settings.get("replace_stopped_minutes", 10) or 0)
        now = time.time()
        failed: list[dict] = []
        for inst in self.db.list_instances():
            if inst["provider"] != "gcp" or not inst.get("auto_replace") or inst["account_status"] == "banned" \
                    or inst["status"] not in ("TERMINATED", "STOPPED"):
                self._stopped_since.pop(inst["id"], None)
                continue
            since = self._stopped_since.setdefault(inst["id"], now)
            if not minutes or now - since < minutes * 60:
                continue
            acc = self.db.get_account(inst["account_id"], with_key=False) or {}
            if acc.get("status") == "banned":
                continue  # 同一轮里账号刚被判定停用，交给整号替补
            try:
                self.power(inst["id"], "start", source=f"停机超过 {minutes} 分钟，自动")
                self._stopped_since.pop(inst["id"], None)
            except ManagerError as e:
                if self._is_account_fatal(e):
                    self.db.set_account_status(inst["account_id"], "banned", str(e))
                    self._event(inst, "replace", f"账号 {inst['account_name']} 无法开机（结算停用 / 无权限），判定为停用：{str(e)[:200]}", "error", notify=True)
                else:
                    failed.append(inst)
        return failed

    def _replace_one(self, old: dict, reason: str, settings: dict) -> None:
        accounts = self._candidate_accounts(settings, after_id=old["account_id"], exclude=old["account_id"])
        if not accounts:
            if old["id"] not in self._replace_warned:
                self._replace_warned.add(old["id"])
                self._event(old, "replace", f"需要替补（{reason}），但没有可用的 GCP 账号（都已开满或被封），请添加新账号", "error", notify=True)
            return
        new, errors = self._try_create(accounts, settings, old)
        if not new:
            if old["id"] not in self._replace_warned:
                self._replace_warned.add(old["id"])
                self._event(old, "replace", f"替补失败（{reason}）：所有可用账号都创建失败。{'；'.join(errors)[:600]}", "error", notify=True)
            return
        self._replace_warned.discard(old["id"])
        # 原实例有附加 IP 的，新实例补上同样数量
        n_extra = len(self.db.list_extra_ips(old["id"]))
        added = []
        for _ in range(n_extra):
            try:
                added.append(self.add_extra_ip(new["id"])["ip"])
            except ManagerError as e:
                self._event(new, "replace", f"为替补实例添加附加 IP 失败：{e}", "warn")
                break
        msg = (f"自动替补：{old['name']}（{old['account_name']}，{reason}，旧 IP {old['external_ip'] or '-'}）"
               f" → {new['name']}（{new['account_name']}，IP {new['external_ip'] or '-'}"
               f"{'，附加 IP ' + '、'.join(added) if added else ''}）")
        self.db.add_event("replace_new", msg, new["id"], "warn")
        send_telegram(self.db.get_settings(), f"GCP-VM {msg}")
        log.warning(msg)
        if reason in ("实例被挂起", "停机且无法开机") or reason.startswith("账号停用且"):
            try:
                self._remove_all_extras(self._client(old["account_id"]), old)
                self._client(old["account_id"]).delete_instance(old["zone"], old["name"])
            except GCPError as e:
                self._event(old, "replace", f"删除被挂起的旧实例失败：{e}", "warn")
        elif reason == "实例被删除" and self.db.list_extra_ips(old["id"]):
            try:  # 实例没了，但旧账号里的附加 IP 转发规则 / 地址还在计费，顺手清掉
                self._remove_all_extras(self._client(old["account_id"]), old)
            except GCPError as e:
                self._event(old, "replace", f"清理旧实例的附加 IP 失败：{e}", "warn")
        self._suspended_seen.pop(old["id"], None)
        self._stopped_since.pop(old["id"], None)
        self.db.delete_instance(old["id"])

    def reset_account_status(self, account_id: int) -> None:
        acc = self.db.get_account(account_id, with_key=False)
        if not acc:
            raise ManagerError("账号不存在")
        self.db.set_account_status(account_id, "ok", "")
        self._acct_fails.pop(account_id, None)
        self._unreachable.discard(account_id)
        self._event(None, "account", f"账号 {acc['name']} 已解除「被封」标记")

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
            elif inst.get("provider") == "azure":
                code = self._exec_azure(job, inst, script, timeout)
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

    def _azure_install_key(self, inst: dict, job: Job | None = None) -> None:
        """通过 Azure「运行命令」把面板公钥写入 root（不依赖 SSH，首次约 30-60 秒）。"""
        if job:
            job.append("[gcpvm] 通过 Azure「运行命令」写入面板公钥（约 30-60 秒）…\n")
        client = self._client(inst["account_id"])
        try:
            out = client.run_command(inst["project"], inst["name"], az.root_key_script(self.ssh_key.authorized_keys_line()))
        except GCPError as e:
            raise ManagerError(f"写入面板公钥失败：{e}") from e
        if "gcpvm-key-ok" not in out:
            raise ManagerError(f"写入面板公钥失败：{out[-300:] or '运行命令没有输出'}")
        self.db.update_instance(inst["id"], ssh_user="root")
        self._event(inst, "exec", "已通过 Azure 运行命令写入面板 SSH 公钥（root）")

    def _exec_azure(self, job: Job, inst: dict, script: str, timeout: int) -> int:
        ip = inst["external_ip"]
        if not inst.get("ssh_user"):
            self._azure_install_key(inst, job)
        job.append(f"[gcpvm] 连接 root@{ip}…\n")
        run = lambda wait: self.ssh_runner(  # noqa: E731
            ip, script, self.ssh_key.private_path, job.append, user="root", port=22,
            timeout=timeout, auth_wait=wait, cancelled=lambda: job.cancelled,
        )
        try:
            return run(5)
        except RemoteError as e:
            if "认证失败" not in str(e) or job.cancelled:
                if "无法连接" in str(e):
                    raise RemoteError(f"{e}（Azure 需在网络安全组放行 22 端口）") from e
                raise
        self._azure_install_key(inst, job)
        return run(30)

    # ---------------- startup script ----------------
    def get_startup_script(self, instance_id: int) -> dict:
        inst = self._only_gcp(instance_id)
        meta = self._client(inst["account_id"]).get_metadata(inst["zone"], inst["name"])
        script, run_once = unwrap_startup_script(meta.get("startup-script", ""))
        return {"script": script, "run_once": run_once, "has_script": bool(script)}

    def set_startup_script(
        self, instance_id: int, script: str, run_once: bool = True, reboot: bool = False
    ) -> dict:
        script = self._check_script(script)
        inst = self._only_gcp(instance_id)
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
        inst = self._only_gcp(instance_id)
        self._client(inst["account_id"]).set_metadata(
            inst["zone"], inst["name"], {"startup-script": None}
        )
        self._event(inst, "startup", "已清除启动脚本")

    def serial_log(self, instance_id: int, start: int | None = None, only_startup: bool = True) -> dict:
        inst = self._only_gcp(instance_id)
        data = self._client(inst["account_id"]).get_serial_output(inst["zone"], inst["name"], start)
        contents = data.get("contents", "")
        if only_startup:
            contents = filter_startup_log(contents)
        return {"contents": contents, "next": int(data.get("next", 0) or 0)}

    # ---------------- firewall ----------------
    def _account_client(self, account_id: int, gcp_only: bool = False) -> tuple[dict, GCPClient]:
        acc = self.db.get_account(account_id, with_key=False)
        if not acc or acc.get("kind") == "manual":
            raise ManagerError("账号不存在")
        if gcp_only and acc.get("kind") == "azure":
            raise ManagerError("Azure 账号的防火墙请在 Azure 门户的「网络安全组」管理（面板新建的实例默认放行全部入站）")
        return acc, self._client(account_id)

    def list_firewalls(self, account_id: int) -> dict:
        _, client = self._account_client(account_id, gcp_only=True)
        try:
            rules = [summarize(r) for r in client.list_firewalls()]
            networks = client.list_networks()
        except GCPError as e:
            raise ManagerError(str(e)) from e
        rules.sort(key=lambda r: (r["network"], r["direction"], r["priority"], r["name"]))
        return {"rules": rules, "networks": networks}

    def create_firewall(self, account_id: int, data: dict) -> None:
        acc, client = self._account_client(account_id, gcp_only=True)
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
        acc, client = self._account_client(account_id, gcp_only=True)
        try:
            client.patch_firewall(name, {"disabled": bool(disabled)})
        except GCPError as e:
            raise ManagerError(str(e)) from e
        self._event(None, "firewall", f"[{acc['name']}] {'禁用' if disabled else '启用'}防火墙规则 {name}")

    def delete_firewall(self, account_id: int, name: str) -> None:
        acc, client = self._account_client(account_id, gcp_only=True)
        try:
            client.delete_firewall(name)
        except GCPError as e:
            raise ManagerError(str(e)) from e
        self._event(None, "firewall", f"[{acc['name']}] 删除防火墙规则 {name}", "warn")

    # ---------------- create / delete instance ----------------
    def create_options(self, account_id: int) -> dict:
        acc, client = self._account_client(account_id)
        if acc.get("kind") == "azure":
            try:
                zones = az.sort_locations(client.list_zones())
            except GCPError as e:
                raise ManagerError(str(e)) from e
            return {
                "provider": "azure", "zones": zones, "networks": [], "default_zone": "japaneast",
                "images": [{"id": i["id"], "label": i["label"]} for i in az.IMAGES],
                "disk_types": list(az.DISK_TYPES), "default_disk_gb": 30, "default_machine_type": "Standard_B2s",
            }
        try:
            zones = sort_zones(client.list_zones())
            networks = client.list_networks()
        except GCPError as e:
            raise ManagerError(str(e)) from e
        return {
            "provider": "gcp", "zones": zones, "networks": networks, "default_zone": "asia-east1-b",
            "images": [{"id": i["id"], "label": i["label"]} for i in IMAGES],
            "disk_types": ["pd-balanced", "pd-standard", "pd-ssd"], "default_disk_gb": 20, "default_machine_type": "e2-small",
        }

    def machine_types(self, account_id: int, zone: str) -> list[dict]:
        acc, client = self._account_client(account_id)
        try:
            if acc.get("kind") == "azure":
                return az.filter_sizes(client.list_machine_types(zone), zone)
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
        if "SkuNotAvailable" in msg or "NotAvailableForSubscription" in msg:
            return "该区域暂时不提供这个机型（或订阅不可用），请换区域或机型。原始错误：" + msg
        if "QUOTA" in msg.upper() or "Quota" in msg:
            return (
                "超出 GCP 配额（例如该区域或全部区域的 vCPU 数、外网 IP 数）。"
                "可关停/删除不用的实例，或在 GCP 控制台「IAM 和管理 → 配额」申请提高。原始错误：" + msg
            )
        return msg

    def _create_azure(self, acc: dict, client, data: dict, cfg: dict) -> dict:
        script = str(data.get("startup_script") or "").replace("\r\n", "\n")
        if script.strip():
            self._check_script(script)
        # customData 由 cloud-init 在首次开机时执行一次：先写入面板公钥（root），再执行用户脚本
        custom = "#!/bin/bash\n" + az.root_key_script(self.ssh_key.authorized_keys_line()) + "\n" + script
        try:
            loc, _ = az.build_vm_body(data, self.ssh_key.ensure(), "x", "x", custom)  # 先校验参数
        except ValueError as e:
            raise ManagerError(str(e)) from e
        name, rg = data["name"].strip(), f"{az.PREFIX}{loc}"
        try:
            subnet, nsg = client.ensure_network(rg, loc)
            _, body = az.build_vm_body(data, self.ssh_key.ensure(), subnet, nsg, custom)
            client.insert_instance(rg, name, body)
        except GCPError as e:
            self._event(None, "create", f"[{acc['name']}] 创建实例 {name} 失败：{self._explain_create(e)}", "error")
            raise ManagerError(self._explain_create(e)) from e
        self.sync_account(acc["id"])
        inst = next(
            (i for i in self.db.list_instances()
             if i["account_id"] == acc["id"] and i["name"] == name and i["project"].lower() == rg.lower()),
            None,
        )
        if not inst:
            raise ManagerError("实例已创建，但同步时未找到，请稍后点「从 GCP 同步」")
        cfg = {k: v for k, v in cfg.items() if k != "ip_mode"}
        self.db.update_instance(inst["id"], ssh_user="root")
        inst = self.db.update_instance_config(inst["id"], cfg)
        self._event(
            inst, "create",
            f"新建 Azure 实例：{loc} · {data['machine_type']}{' · Spot' if data.get('spot') else ''}，IP {inst['external_ip'] or '-'}",
            notify=True,
        )
        return inst

    def create_instance(self, account_id: int, data: dict) -> dict:
        acc, client = self._account_client(account_id)
        if acc.get("kind") == "azure":
            cfg = {
                "auto_ip": bool(data.get("auto_ip", True)),
                "auto_start": bool(data.get("auto_start", bool(data.get("spot")))),
                "check_port": int(data.get("check_port") or 22),
                "note": str(data.get("note") or "")[:200],
            }
            if not 1 <= cfg["check_port"] <= 65535:
                raise ManagerError("检测端口范围 1-65535")
            return self._create_azure(acc, client, data, cfg)
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
        raw = self._instance(instance_id)
        if raw.get("status") == "LOST" or raw.get("account_status") == "banned":
            self._event(raw, "delete", "已删除记录（云端实例已不存在或账号已被封）", "warn")
            self.db.delete_instance(instance_id)
            return
        inst = self._gcp_instance(instance_id)
        lock = self._lock_for(instance_id)
        if not lock.acquire(timeout=1):
            raise ManagerError("该实例正在执行其他操作，请稍后")
        try:
            if inst.get("provider") == "azure":
                self._delete_azure(inst)
                return
            client = self._client(inst["account_id"])
            region = region_of(inst["zone"])
            self._remove_all_extras(client, inst)
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

    def _delete_azure(self, inst: dict) -> None:
        client = self._client(inst["account_id"])
        try:
            cleaned = client.delete_instance(inst["project"], inst["name"])
        except GCPError as e:
            self._event(inst, "delete", f"删除实例失败：{e}", "error")
            raise ManagerError(str(e)) from e
        extra = f"，已清理网卡/公网 IP/系统盘（{len(cleaned)} 个资源）" if cleaned else ""
        self._event(inst, "delete", f"已删除 Azure 实例（{inst['zone']}，IP {inst['external_ip'] or '-'}）{extra}", "warn", notify=True)
        self.db.delete_instance(inst["id"])

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
