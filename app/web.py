"""Web API 与控制台（Starlette）。"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import jwt
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import __version__, config
from .checker import BlockChecker
from .db import Database
from .gcp import GCPClient, GCPError, validate_service_account_key
from .manager import Manager, ManagerError
from .remote import RemoteError

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


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
    async def index(_: Request):
        return FileResponse(STATIC_DIR / "index.html")

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
        return JSONResponse(db.list_accounts())

    @protected
    async def add_account(request: Request):
        data = await body(request)
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
        if "script_presets" in data:
            if not isinstance(data["script_presets"], list):
                raise ValueError("script_presets 必须是列表")
            data["script_presets"] = [
                {"name": str(p.get("name", "")).strip()[:60], "script": str(p.get("script", ""))[:65536]}
                for p in data["script_presets"]
                if isinstance(p, dict) and str(p.get("name", "")).strip() and str(p.get("script", "")).strip()
            ][:50]
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

    @asynccontextmanager
    async def lifespan(_app):
        if not auth.password:
            log.warning("未设置 GCPVM_ADMIN_PASSWORD，将无法登录控制台！")
        task = asyncio.create_task(monitor_loop()) if run_monitor else None
        try:
            yield
        finally:
            if task:
                task.cancel()

    async def http_error(_: Request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    routes = [
        Route("/", index),
        Route("/api/health", health),
        Route("/api/login", login, methods=["POST"]),
        Route("/api/accounts", list_accounts),
        Route("/api/accounts", add_account, methods=["POST"]),
        Route("/api/accounts/{id}", delete_account, methods=["DELETE"]),
        Route("/api/instances", list_instances),
        Route("/api/sync", sync, methods=["POST"]),
        Route("/api/instances/{id}", update_instance, methods=["PATCH"]),
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
        Route("/api/events", events),
        Route("/api/settings", get_settings),
        Route("/api/settings", put_settings, methods=["PUT"]),
        Route("/api/probe-test", test_probe, methods=["POST"]),
        Route("/api/run-check", run_cycle, methods=["POST"]),
        Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
    ]
    app = Starlette(routes=routes, lifespan=lifespan, exception_handlers={HTTPException: http_error})
    app.state.db = db
    app.state.manager = manager
    return app


def _clean(inst: dict) -> dict:
    return {k: v for k, v in inst.items() if not k.startswith("_")}
