"""Web API 与控制台（Starlette）。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import jwt
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import __version__, config
from .checker import BlockChecker
from .db import Database
from .gcp import GCPClient, GCPError, validate_service_account_key
from .manager import Manager, ManagerError
from .pushprobe import HUB
from .remote import RemoteError

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
PROBE_DIR = Path(__file__).parent.parent / "probe"
PROBE_FILES = {"probe.py", "gcpvm-probe.openwrt", "gcpvm-probe.service"}


# ---------------- auth ----------------
class Auth:
    def __init__(self, password: str | None, secret: str, ttl_hours: int):
        self.password = password
        self.secret = secret
        self.ttl = ttl_hours * 3600
        self._fails: dict[str, list[float]] = {}

    def login(self, ip: str, password: str) -> str:
        now = time.time()
        recent = [t for t in self._fails.get(ip, []) if now - t < 600]
        if len(recent) >= 10:
            raise HTTPException(429, "登录失败次数过多，请 10 分钟后再试")
        if not self.password or not hmac.compare_digest(password.encode(), self.password.encode()):
            self._fails[ip] = recent + [now]
            raise HTTPException(401, "密码错误")
        self._fails.pop(ip, None)
        return jwt.encode({"sub": "admin", "exp": int(now + self.ttl)}, self.secret, algorithm="HS256")

    def verify(self, request: Request) -> None:
        header = request.headers.get("authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else ""
        try:
            jwt.decode(token, self.secret, algorithms=["HS256"])
        except jwt.PyJWTError:
            raise HTTPException(401, "未登录或登录已过期") from None


# ---------------- app ----------------
def create_app(
    db: Database | None = None,
    manager: Manager | None = None,
    password: str | None = None,
    enable_monitor: bool | None = None,
) -> Starlette:
    db = db or Database(config.DB_PATH)
    manager = manager or Manager(db)
    auth = Auth(password if password is not None else config.ADMIN_PASSWORD, config.secret_key(), config.TOKEN_TTL_HOURS)
    run_monitor = config.ENABLE_MONITOR if enable_monitor is None else enable_monitor
    HUB.configure(
        db.get_settings,
        lambda: [
            (i["external_ip"], int(i["check_port"]))
            for i in db.list_instances()
            if i["status"] == "RUNNING" and i["external_ip"]
        ],
    )

    async def body(request: Request) -> dict:
        try:
            data = await request.json()
        except (json.JSONDecodeError, ValueError):
            raise HTTPException(400, "请求体必须是 JSON") from None
        if not isinstance(data, dict):
            raise HTTPException(400, "请求体必须是 JSON 对象")
        return data

    def protected(handler):
        async def wrapper(request: Request):
            auth.verify(request)
            try:
                return await handler(request)
            except (ManagerError, ValueError, GCPError, RemoteError) as e:
                return JSONResponse({"error": str(e)}, status_code=400)
        return wrapper

    def int_param(request: Request, name: str = "id") -> int:
        try:
            return int(request.path_params[name])
        except (KeyError, ValueError):
            raise HTTPException(400, "参数错误") from None

    # ---- public ----
    def asset_version(name: str) -> str:
        return hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:10]

    # 给 app.js / app.css 加上内容哈希，避免浏览器或 Cloudflare 缓存旧版本
    index_html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for asset in ("app.js", "app.css"):
        index_html = index_html.replace(f"/static/{asset}\"", f"/static/{asset}?v={asset_version(asset)}\"")

    async def index(_: Request):
        return HTMLResponse(index_html, headers={"Cache-Control": "no-cache"})

    async def probe_file(request: Request):
        # 供探针设备直接从面板下载（国内访问 GitHub 不稳定）
        name = request.path_params.get("name", "")
        path = PROBE_DIR / name
        if name not in PROBE_FILES or not path.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(path, media_type="text/plain; charset=utf-8")

    async def health(_: Request):
        return JSONResponse({"ok": True, "version": __version__})

    async def login(request: Request):
        data = await body(request)
        ip = request.client.host if request.client else "?"
        token = auth.login(ip, str(data.get("password", "")))
        return JSONResponse({"token": token})

    # ---- accounts ----
    @protected
    async def list_accounts(_: Request):
        counts: dict[int, int] = {}
        for i in db.list_instances():
            if i["status"] != "LOST":
                counts[i["account_id"]] = counts.get(i["account_id"], 0) + 1
        return JSONResponse([a | {"instances": counts.get(a["id"], 0)} for a in db.list_accounts("cloud")])

    @protected
    async def reset_account_status(request: Request):
        await asyncio.to_thread(manager.reset_account_status, int_param(request))
        return JSONResponse({"ok": True})

    @protected
    async def replace_create(request: Request):
        group = str((await body(request)).get("group") or "").strip()
        inst = await asyncio.to_thread(manager.create_from_template, group)
        return JSONResponse({"instance": _clean(inst)})

    @protected
    async def add_account(request: Request):
        data = await body(request)
        if data.get("kind") == "azure":
            from .azure import AzureClient, validate_azure_key
            key = validate_azure_key(data.get("key") if isinstance(data.get("key"), dict) else data)
            await asyncio.to_thread(lambda: AzureClient(key).list_instances())
            acc = db.add_account(str(data.get("name") or "").strip() or f"azure-{key['subscription_id'][:8]}", key, "azure")
            n = await asyncio.to_thread(manager.sync_account, acc["id"])
            db.add_event("account", f"添加 Azure 账号 {acc['name']}（订阅 {acc['project_id']}），发现 {n} 台实例")
            return JSONResponse(acc)
        key = data.get("key")
        if isinstance(key, str):
            try:
                key = json.loads(key)
            except json.JSONDecodeError:
                raise ValueError("服务账号密钥不是合法 JSON") from None
        key = validate_service_account_key(key)
        # 先验证密钥可用
        await asyncio.to_thread(lambda: GCPClient(key).list_instances())
        acc = db.add_account(str(data.get("name") or key["project_id"]), key)
        n = await asyncio.to_thread(manager.sync_account, acc["id"])
        db.add_event("account", f"添加账号 {acc['name']}（项目 {acc['project_id']}），发现 {n} 台实例")
        return JSONResponse(acc)

    @protected
    async def delete_account(request: Request):
        aid = int_param(request)
        db.delete_account(aid)
        manager.pool.drop(aid)
        return JSONResponse({"ok": True})

    # ---- firewall ----
    @protected
    async def list_firewalls(request: Request):
        return JSONResponse(await asyncio.to_thread(manager.list_firewalls, int_param(request)))

    @protected
    async def create_firewall(request: Request):
        aid = int_param(request)
        data = await body(request)
        await asyncio.to_thread(manager.create_firewall, aid, data)
        return JSONResponse({"ok": True})

    @protected
    async def update_firewall(request: Request):
        aid = int_param(request)
        data = await body(request)
        if "disabled" not in data:
            raise ValueError("只支持修改 disabled")
        await asyncio.to_thread(
            manager.set_firewall_disabled, aid, request.path_params["name"], bool(data["disabled"])
        )
        return JSONResponse({"ok": True})

    @protected
    async def delete_firewall(request: Request):
        await asyncio.to_thread(manager.delete_firewall, int_param(request), request.path_params["name"])
        return JSONResponse({"ok": True})

    # ---- 自有服务器（非 GCP） ----
    @protected
    async def add_server(request: Request):
        data = await body(request)
        panel_url = str(data.get("panel_url") or "")
        if panel_url:
            from .traffic import normalize_panel_url
            panel_url = normalize_panel_url(panel_url)
        inst, job = await asyncio.to_thread(manager.add_server, data, panel_url)
        return JSONResponse({"instance": _clean(inst), "job": job.summary() if job else None})

    @protected
    async def update_server(request: Request):
        inst = await asyncio.to_thread(manager.update_server, int_param(request), await body(request))
        return JSONResponse({"instance": _clean(inst)})

    @protected
    async def remove_server(request: Request):
        await asyncio.to_thread(manager.remove_server, int_param(request))
        return JSONResponse({"ok": True})

    @protected
    async def server_bootstrap_cmd(request: Request):
        """生成在服务器上执行的一键接入命令。"""
        from .traffic import normalize_panel_url
        iid = int_param(request)
        inst = db.get_instance(iid)
        if not inst or inst.get("provider") != "manual":
            raise HTTPException(404, "服务器不存在")
        panel = normalize_panel_url(str((await body(request)).get("panel_url", "")))
        manager.server_bootstrap_script(iid, panel)  # 确保已生成令牌
        token = db.get_instance(iid)["traffic_token"]
        url = f"{panel}/api/servers/bootstrap.sh?token={token}&panel={panel}"
        return JSONResponse({"command": f"curl -fsSL '{url}' | bash"})

    async def server_bootstrap_sh(request: Request):
        """服务器上 curl 获取的接入脚本（凭服务器自己的上报令牌）。"""
        from .traffic import normalize_panel_url
        token = request.query_params.get("token", "")
        try:
            panel = normalize_panel_url(request.query_params.get("panel", ""))
        except ValueError:
            raise HTTPException(400, "panel 参数不正确") from None
        script = await asyncio.to_thread(manager.bootstrap_by_token, token, panel)
        if script is None:
            raise HTTPException(404, "not found")
        return PlainTextResponse(script, headers={"Cache-Control": "no-store"})

    # ---- create / delete instance ----
    @protected
    async def create_options(request: Request):
        return JSONResponse(await asyncio.to_thread(manager.create_options, int_param(request)))

    @protected
    async def machine_types(request: Request):
        zone = request.query_params.get("zone", "")
        if not zone:
            raise ValueError("缺少 zone 参数")
        return JSONResponse(await asyncio.to_thread(manager.machine_types, int_param(request), zone))

    @protected
    async def create_instance(request: Request):
        aid = int_param(request)
        data = await body(request)
        inst = await asyncio.to_thread(manager.create_instance, aid, data)
        return JSONResponse({"instance": _clean(inst)})

    @protected
    async def delete_instance(request: Request):
        iid = int_param(request)
        inst = db.get_instance(iid)
        if not inst:
            raise HTTPException(404, "实例不存在")
        if (await body(request)).get("confirm_name") != inst["name"]:
            raise ValueError("实例名称不匹配，未删除")
        if inst.get("provider") == "manual":
            await asyncio.to_thread(manager.remove_server, iid)
            return JSONResponse({"ok": True})
        await asyncio.to_thread(manager.delete_instance, iid)
        return JSONResponse({"ok": True})

    # ---- instances ----
    @protected
    async def list_instances(_: Request):
        return JSONResponse(db.list_instances())

    @protected
    async def sync(_: Request):
        result = await asyncio.to_thread(manager.sync_all)
        return JSONResponse({"result": result, "instances": db.list_instances()})

    @protected
    async def update_instance(request: Request):
        iid = int_param(request)
        if not db.get_instance(iid):
            raise HTTPException(404, "实例不存在")
        inst = db.update_instance_config(iid, await body(request))
        return JSONResponse(inst)

    @protected
    async def instance_action(request: Request):
        iid = int_param(request)
        action = (await body(request)).get("action")
        if action in ("start", "stop", "reset"):
            inst = await asyncio.to_thread(manager.power, iid, action)
            return JSONResponse({"instance": _clean(inst)})
        if action == "change_ip":
            inst = await asyncio.to_thread(manager.change_ip, iid, "手动")
            return JSONResponse({"instance": _clean(inst)})
        if action == "check":
            result = await asyncio.to_thread(manager.check_instance, iid)
            return JSONResponse({"check": result, "instance": db.get_instance(iid)})
        if action == "refresh":
            inst = db.get_instance(iid)
            if not inst:
                raise HTTPException(404, "实例不存在")
            if inst.get("provider") == "manual":
                return JSONResponse({"instance": _clean(inst)})
            inst = await asyncio.to_thread(manager._refresh, inst)
            return JSONResponse({"instance": _clean(inst)})
        raise HTTPException(400, "不支持的操作")

    # ---- remote exec / startup script ----
    @protected
    async def exec_command(request: Request):
        iid = int_param(request)
        data = await body(request)
        job = await asyncio.to_thread(
            manager.start_exec, iid, str(data.get("script", "")), int(data.get("timeout") or 600)
        )
        return JSONResponse(job.summary())

    @protected
    async def list_jobs(request: Request):
        iid = request.query_params.get("instance_id")
        return JSONResponse([j.summary() for j in manager.jobs.list(int(iid) if iid else None)])

    @protected
    async def get_job(request: Request):
        job = manager.jobs.get(int_param(request))
        if not job:
            raise HTTPException(404, "任务不存在（面板重启后任务记录会清空）")
        try:
            offset = max(0, int(request.query_params.get("offset", 0)))
        except ValueError:
            offset = 0
        text, end = job.output(offset)
        return JSONResponse(job.summary() | {"output": text, "offset": end})

    @protected
    async def cancel_job(request: Request):
        job = manager.jobs.get(int_param(request))
        if not job:
            raise HTTPException(404, "任务不存在")
        job.cancel()
        return JSONResponse({"ok": True})

    @protected
    async def get_startup(request: Request):
        return JSONResponse(await asyncio.to_thread(manager.get_startup_script, int_param(request)))

    @protected
    async def put_startup(request: Request):
        iid = int_param(request)
        data = await body(request)
        inst = await asyncio.to_thread(
            manager.set_startup_script, iid, str(data.get("script", "")),
            bool(data.get("run_once", True)), bool(data.get("reboot", False)),
        )
        return JSONResponse({"instance": _clean(inst)})

    @protected
    async def delete_startup(request: Request):
        await asyncio.to_thread(manager.clear_startup_script, int_param(request))
        return JSONResponse({"ok": True})

    @protected
    async def serial_log(request: Request):
        iid = int_param(request)
        start = request.query_params.get("start")
        only = request.query_params.get("all") != "1"
        result = await asyncio.to_thread(
            manager.serial_log, iid, int(start) if start not in (None, "") else None, only
        )
        return JSONResponse(result)

    @protected
    async def ssh_key(_: Request):
        pub = await asyncio.to_thread(manager.ssh_key.ensure)
        return JSONResponse({"user": manager.ssh_key.user, "public_key": pub})

    # ---- traffic ----
    async def traffic_report(request: Request):
        header = request.headers.get("authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else ""
        data = await body(request)
        try:
            ok = await asyncio.to_thread(manager.traffic_report, token, data.get("rx"), data.get("tx"), data.get("svc"))
        except ManagerError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        if not ok:
            raise HTTPException(401, "上报令牌无效")
        return JSONResponse({"ok": True})

    @protected
    async def traffic_rates(_: Request):
        rates = manager.traffic.all_rates(db.list_instances())
        return JSONResponse({str(k): v for k, v in rates.items()})

    @protected
    async def traffic_agent(request: Request):
        iid = int_param(request)
        data = await body(request)
        if data.get("uninstall"):
            from .traffic import UNINSTALL
            return JSONResponse({"script": UNINSTALL})
        script = await asyncio.to_thread(manager.traffic_agent_script, iid, str(data.get("panel_url", "")))
        return JSONResponse({"script": script})

    def public_payload(key: str) -> dict | None:
        s = db.get_settings()
        expected = s.get("status_page_key") or ""
        if not s.get("status_page_enabled") or not expected or not hmac.compare_digest(key.encode(), expected.encode()):
            return None
        groups = manager.traffic.groups(db.list_instances())
        return {"title": s.get("status_page_title") or "实时速率", "groups": groups, "ts": time.time()}

    async def public_traffic(request: Request):
        payload = public_payload(request.query_params.get("key", ""))
        if payload is None:
            raise HTTPException(404, "not found")
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    streams = {"n": 0}
    MAX_STREAMS = 300

    async def public_traffic_stream(request: Request):
        """SSE：每 2 秒推送一次各组速率（与实例上报间隔一致）。"""
        key = request.query_params.get("key", "")
        if public_payload(key) is None:
            raise HTTPException(404, "not found")
        if streams["n"] >= MAX_STREAMS:
            raise HTTPException(503, "too many viewers")

        async def gen():
            streams["n"] += 1
            try:
                yield "retry: 3000\n\n"
                while not await request.is_disconnected():
                    payload = public_payload(key)
                    if payload is None:
                        yield "event: closed\ndata: {}\n\n"
                        return
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    await asyncio.sleep(2)
            finally:
                streams["n"] -= 1

        return StreamingResponse(
            gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    async def status_page(_: Request):
        return FileResponse(STATIC_DIR / "status.html", headers={"Cache-Control": "no-cache"})

    # ---- push probes (探针令牌鉴权，不走管理员登录) ----
    def probe_name(request: Request) -> str:
        header = request.headers.get("authorization", "")
        name = HUB.authenticate(header[7:] if header.lower().startswith("bearer ") else "")
        if not name:
            raise HTTPException(401, "探针令牌无效")
        return name

    async def probe_tasks(request: Request):
        name = probe_name(request)
        return JSONResponse(await asyncio.to_thread(HUB.tasks, name))

    async def probe_report(request: Request):
        name = probe_name(request)
        data = await body(request)
        results = data.get("results")
        if not isinstance(results, list):
            raise HTTPException(400, "results 必须是列表")
        return JSONResponse({"ok": True, "accepted": HUB.report(name, results)})

    @protected
    async def push_probe_status(_: Request):
        return JSONResponse(HUB.status())

    # ---- events / settings / probes ----
    @protected
    async def events(request: Request):
        limit = min(int(request.query_params.get("limit", 200)), 1000)
        iid = request.query_params.get("instance_id")
        return JSONResponse(db.list_events(limit, int(iid) if iid else None))

    @protected
    async def get_settings(_: Request):
        return JSONResponse(db.get_settings())

    @protected
    async def put_settings(request: Request):
        data = await body(request)
        for k in ("check_interval", "fail_threshold", "tcp_tries", "cooldown_minutes", "max_changes_per_day"):
            if k in data:
                data[k] = int(data[k])
                if data[k] < 0:
                    raise ValueError(f"{k} 不能为负数")
        if "check_interval" in data and data["check_interval"] < 30:
            raise ValueError("检测间隔不能小于 30 秒")
        if "fail_threshold" in data and data["fail_threshold"] < 1:
            raise ValueError("连续失败阈值至少为 1")
        for k in ("tcp_timeout", "cn_success_ratio"):
            if k in data:
                data[k] = float(data[k])
        if "detect_mode" in data and data["detect_mode"] not in ("agent", "local"):
            raise ValueError("detect_mode 只能是 agent 或 local")
        if "status_page_title" in data:
            data["status_page_title"] = str(data["status_page_title"]).strip()[:40] or "实时速率"
        if "status_page_key" in data:
            data["status_page_key"] = str(data["status_page_key"]).strip()
        if "status_page_enabled" in data:
            data["status_page_enabled"] = bool(data["status_page_enabled"])
            key = data.get("status_page_key", db.get_settings().get("status_page_key", ""))
            if data["status_page_enabled"] and not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", key or ""):
                raise ValueError("公开速率页的访问密钥需为 16-64 位字母、数字、-、_")
        if "service_alert_seconds" in data:
            data["service_alert_seconds"] = int(data["service_alert_seconds"])
            if data["service_alert_seconds"] < 20:
                raise ValueError("服务监控判定时间不能小于 20 秒")
        if "verify_new_ip" in data:
            data["verify_new_ip"] = bool(data["verify_new_ip"])
        if "service_alert_enabled" in data:
            data["service_alert_enabled"] = bool(data["service_alert_enabled"])
        if "watch_service" in data:
            data["watch_service"] = str(data["watch_service"]).strip()
            if not re.fullmatch(r"[A-Za-z0-9@._-]{0,64}", data["watch_service"]):
                raise ValueError("监控的服务名只能包含字母、数字和 @ . _ -")
        if "script_presets" in data:
            if not isinstance(data["script_presets"], list):
                raise ValueError("script_presets 必须是列表")
            data["script_presets"] = [
                {"name": str(p.get("name", "")).strip()[:60], "script": str(p.get("script", ""))[:65536]}
                for p in data["script_presets"]
                if isinstance(p, dict) and str(p.get("name", "")).strip() and str(p.get("script", "")).strip()
            ][:50]
        if "push_probes" in data:
            if not isinstance(data["push_probes"], list):
                raise ValueError("push_probes 必须是列表")
            clean, names = [], set()
            for p in data["push_probes"]:
                if not isinstance(p, dict):
                    continue
                name, tok = str(p.get("name", "")).strip()[:40], str(p.get("token", "")).strip()
                if not name and not tok:
                    continue
                if not name:
                    raise ValueError("主动上报探针需要填写名称")
                if len(tok) < 16:
                    raise ValueError(f"探针「{name}」的令牌至少 16 位")
                if name in names:
                    raise ValueError(f"探针名称重复：{name}")
                names.add(name)
                clean.append({"name": name, "token": tok})
            data["push_probes"] = clean
        if "panel_url" in data:
            from .traffic import normalize_panel_url
            try:
                data["panel_url"] = normalize_panel_url(str(data["panel_url"])) if data["panel_url"] else ""
            except ValueError:
                data["panel_url"] = ""
        if "replace_enabled" in data:
            data["replace_enabled"] = bool(data["replace_enabled"])
        for k, lo, hi in (("replace_max_per_account", 1, 100), ("replace_max_per_day", 1, 200)):
            if k in data:
                data[k] = int(data[k])
                if not lo <= data[k] <= hi:
                    raise ValueError(f"{k} 范围 {lo}-{hi}")
        def clean_template(t, label: str) -> dict:
            if not isinstance(t, dict):
                raise ValueError(f"{label}必须是对象")
            from .config import DEFAULT_SETTINGS
            base = DEFAULT_SETTINGS["replace_template"]
            clean_t = {}
            for k, dv in base.items():
                v = t.get(k, dv)
                if isinstance(dv, bool):
                    clean_t[k] = bool(v)
                elif isinstance(dv, int):
                    clean_t[k] = int(v or dv)
                else:
                    clean_t[k] = str(v or "").strip() if k != "startup_script" else str(v or "")[:65536]
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", clean_t["name_prefix"] or "x"):
                raise ValueError(f"{label}：实例名前缀只能用小写字母、数字和短横线，以字母开头")
            if clean_t["zone"] and not re.fullmatch(r"[a-z]+-[a-z]+\d+-[a-z]", clean_t["zone"]):
                raise ValueError(f"{label}：可用区格式不正确，例如 asia-east1-b")
            if not 1 <= clean_t["check_port"] <= 65535:
                raise ValueError(f"{label}：检测端口范围 1-65535")
            return clean_t

        if "replace_template" in data:
            data["replace_template"] = clean_template(data["replace_template"], "默认替补模板")
        if "replace_templates" in data:
            ts = data["replace_templates"]
            if not isinstance(ts, dict):
                raise ValueError("replace_templates 必须是对象")
            out = {}
            for g, t in list(ts.items())[:50]:
                g = str(g).strip()[:40]
                if g:
                    out[g] = clean_template(t, f"分组「{g}」的替补模板") | {"grp": g}
            data["replace_templates"] = out
        if "probes" in data:
            if not isinstance(data["probes"], list):
                raise ValueError("probes 必须是列表")
            data["probes"] = [
                {"name": str(p.get("name", "")), "url": str(p.get("url", "")).strip(), "token": str(p.get("token", ""))}
                for p in data["probes"] if isinstance(p, dict) and p.get("url")
            ]
        return JSONResponse(db.update_settings(data))

    @protected
    async def test_probe(request: Request):
        data = await body(request)
        host = str(data.get("host", "")).strip()
        if not host:
            raise ValueError("请填写目标 IP")
        port = int(data.get("port", 22))
        result = await asyncio.to_thread(BlockChecker(db.get_settings()).check, host, port)
        return JSONResponse(result.to_dict())

    @protected
    async def telegram_test(request: Request):
        from .integrations import test_telegram
        data = await body(request)
        saved = db.get_settings()
        token = str(data.get("telegram_bot_token") or saved.get("telegram_bot_token") or "").strip()
        chat = str(data.get("telegram_chat_id") or saved.get("telegram_chat_id") or "").strip()
        ok, msg = await asyncio.to_thread(test_telegram, token, chat)
        return JSONResponse({"ok": ok, "message": msg}, status_code=200 if ok else 400)

    @protected
    async def run_cycle(_: Request):
        asyncio.get_running_loop().run_in_executor(None, manager.run_cycle)
        return JSONResponse({"ok": True, "message": "已触发一轮检测，结果见事件日志"})

    # ---- background monitor ----
    async def monitor_loop():
        await asyncio.sleep(5)
        while True:
            started = time.time()
            try:
                await asyncio.to_thread(manager.run_cycle)
            except Exception:  # noqa: BLE001
                log.exception("检测循环出错")
            interval = max(30, int(db.get_settings().get("check_interval", 300)))
            await asyncio.sleep(max(5, interval - (time.time() - started)))

    async def alert_loop():
        await asyncio.sleep(10)
        while True:
            try:
                await asyncio.to_thread(manager.check_alerts)
            except Exception:  # noqa: BLE001
                log.exception("服务监控检查出错")
            await asyncio.sleep(10)

    @asynccontextmanager
    async def lifespan(_app):
        if not auth.password:
            log.warning("未设置 GCPVM_ADMIN_PASSWORD，将无法登录控制台！")
        tasks = [asyncio.create_task(monitor_loop()), asyncio.create_task(alert_loop())] if run_monitor else []
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()

    async def http_error(_: Request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    routes = [
        Route("/", index),
        Route("/api/health", health),
        Route("/status", status_page),
        Route("/api/public/traffic", public_traffic),
        Route("/api/public/traffic/stream", public_traffic_stream),
        Route("/api/traffic-agent/report", traffic_report, methods=["POST"]),
        Route("/api/traffic", traffic_rates),
        Route("/api/instances/{id}/traffic-agent", traffic_agent, methods=["POST"]),
        Route("/probe/{name}", probe_file),
        Route("/api/login", login, methods=["POST"]),
        Route("/api/accounts", list_accounts),
        Route("/api/accounts", add_account, methods=["POST"]),
        Route("/api/accounts/{id}", delete_account, methods=["DELETE"]),
        Route("/api/accounts/{id}/reset-status", reset_account_status, methods=["POST"]),
        Route("/api/replace/create", replace_create, methods=["POST"]),
        Route("/api/servers", add_server, methods=["POST"]),
        Route("/api/servers/bootstrap.sh", server_bootstrap_sh),
        Route("/api/servers/{id}", update_server, methods=["PATCH"]),
        Route("/api/servers/{id}", remove_server, methods=["DELETE"]),
        Route("/api/servers/{id}/bootstrap-command", server_bootstrap_cmd, methods=["POST"]),
        Route("/api/accounts/{id}/create-options", create_options),
        Route("/api/accounts/{id}/machine-types", machine_types),
        Route("/api/accounts/{id}/instances", create_instance, methods=["POST"]),
        Route("/api/accounts/{id}/firewalls", list_firewalls),
        Route("/api/accounts/{id}/firewalls", create_firewall, methods=["POST"]),
        Route("/api/accounts/{id}/firewalls/{name}", update_firewall, methods=["PATCH"]),
        Route("/api/accounts/{id}/firewalls/{name}", delete_firewall, methods=["DELETE"]),
        Route("/api/instances", list_instances),
        Route("/api/sync", sync, methods=["POST"]),
        Route("/api/instances/{id}", update_instance, methods=["PATCH"]),
        Route("/api/instances/{id}", delete_instance, methods=["DELETE"]),
        Route("/api/instances/{id}/action", instance_action, methods=["POST"]),
        Route("/api/instances/{id}/exec", exec_command, methods=["POST"]),
        Route("/api/instances/{id}/startup-script", get_startup),
        Route("/api/instances/{id}/startup-script", put_startup, methods=["PUT"]),
        Route("/api/instances/{id}/startup-script", delete_startup, methods=["DELETE"]),
        Route("/api/instances/{id}/serial-log", serial_log),
        Route("/api/jobs", list_jobs),
        Route("/api/jobs/{id}", get_job),
        Route("/api/jobs/{id}/cancel", cancel_job, methods=["POST"]),
        Route("/api/ssh-key", ssh_key),
        Route("/api/probe-agent/tasks", probe_tasks),
        Route("/api/probe-agent/report", probe_report, methods=["POST"]),
        Route("/api/push-probes", push_probe_status),
        Route("/api/events", events),
        Route("/api/settings", get_settings),
        Route("/api/settings", put_settings, methods=["PUT"]),
        Route("/api/probe-test", test_probe, methods=["POST"]),
        Route("/api/run-check", run_cycle, methods=["POST"]),
        Route("/api/telegram-test", telegram_test, methods=["POST"]),
        Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
    ]
    app = Starlette(routes=routes, lifespan=lifespan, exception_handlers={HTTPException: http_error})
    app.state.db = db
    app.state.manager = manager
    return app


def _clean(inst: dict) -> dict:
    return {k: v for k, v in inst.items() if not k.startswith("_")}
