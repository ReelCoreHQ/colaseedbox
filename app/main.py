"""可乐保种组 Docker 版（colaseedgroup-docker）：不需要 MoviePilot 的独立服务。

执行器逻辑与 MoviePilot 插件 0.7.0 是同一份（app/seedgroup/，逐字节搬来）；
这里是外壳：配置、定时运行、端口 3006 上的小面板（要登录，见 auth.py）。
"""

import asyncio
import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from . import downloaders as downloaders_mod
from . import view
from .auth import MAX_PASSWORD, SESSION_TTL, Auth, PasswordError
from .config import ConfigError, Store, merge_update, missing_fields, public_view
from .proxies import Trusted, client_ip, parse_trusted
from .runner import Runner
from .seedgroup import VERSION

BASE = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("COLASEEDGROUP_DATA", "/data"))
PORT = int(os.environ.get("COLASEEDGROUP_PORT", "3006"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("colaseedgroup")
# uvicorn 的访问日志带客户端地址：不要
logging.getLogger("uvicorn.access").disabled = True

store = Store(DATA_DIR)
runner = Runner(store)
auth = Auth(DATA_DIR, env_password=os.environ.get("COLASEEDGROUP_PASSWORD"))
try:
    trusted_proxies = parse_trusted(os.environ.get("COLASEEDGROUP_TRUSTED_PROXIES"))
    TRUSTED_PROXIES_ERROR = ""
except ValueError as err:
    trusted_proxies, TRUSTED_PROXIES_ERROR = Trusted(), str(err)

COOKIE = "colaseedgroup_session"
# 不登录也能访问的路径；其余全部要会话（新加的接口默认受保护）
PUBLIC_PATHS = {"/api/health", "/api/login", "/api/logout", "/login", "/panel.css"}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    os.umask(0o077)
    if not os.access(DATA_DIR, os.W_OK):
        log.error("数据目录 %s 对当前用户 uid=%s gid=%s 不可写：把宿主机上的 data 目录 chown 给这个 uid/gid，"
                  "或在 compose 里把 PUID/PGID 改成目录主人的", DATA_DIR, os.getuid(), os.getgid())
        raise SystemExit(1)
    if TRUSTED_PROXIES_ERROR:
        log.error("%s", TRUSTED_PROXIES_ERROR)
        raise SystemExit(1)
    try:
        auth.ensure()
    except PasswordError:
        log.error("环境变量 COLASEEDGROUP_PASSWORD 太短：至少 8 位")
        raise SystemExit(1) from None
    cfg = store.load()
    log.info("可乐保种组 Docker 版 %s 启动，每 %s 分钟对齐一次", VERSION, cfg["interval"])
    if os.environ.get("COLASEEDGROUP_NO_SCHEDULER") != "1":
        runner.start()
    yield
    runner.stop()


app = FastAPI(title="colaseedgroup-docker", version=VERSION, docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)


def _require_json(request: Request) -> None:
    """改动类接口只收 application/json：浏览器跨站表单发不出这种请求（简单 CSRF 防护）。"""
    ctype = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if ctype != "application/json":
        raise HTTPException(415, "需要 application/json")


def _cookie_secure(request: Request) -> bool:
    """COLASEEDGROUP_COOKIE_SECURE=auto|1|0。auto：服务自己看到的请求是 https 才加 Secure。

    反向代理（Caddy / Nginx）把 https 转成 http 发进来时 auto 不加 Secure——Cookie 照样能在 https 下用；
    想在这种情况下也加 Secure，设成 1（之后只能经 https 登录）。
    """
    mode = os.environ.get("COLASEEDGROUP_COOKIE_SECURE", "auto").strip().lower()
    if mode in ("1", "true", "yes", "on"):
        return True
    if mode in ("0", "false", "no", "off"):
        return False
    return request.url.scheme == "https"


def _client_key(request: Request) -> str:
    # 只用于登录失败计数（内存里），不写日志。没配 COLASEEDGROUP_TRUSTED_PROXIES 时就是直连对端地址
    # （反向代理后面大家共用代理的地址）；配了就按 proxies.client_ip 从转发头里找真实客户端。
    return client_ip(request.client.host if request.client else None,
                     request.headers.getlist("x-forwarded-for"), trusted_proxies,
                     request.headers.get("cf-connecting-ip"))


def _issue_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(COOKIE, token, max_age=SESSION_TTL, path="/", httponly=True, samesite="strict",
                        secure=_cookie_secure(request))


def _page(name: str) -> FileResponse:
    return FileResponse(BASE / "templates" / name, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-store"})


@app.middleware("http")
async def require_session(request: Request, call_next):
    path = request.url.path
    if path not in PUBLIC_PATHS and not auth.valid(request.cookies.get(COOKIE)):
        if path.startswith("/api/"):
            return JSONResponse({"error": {"code": "login_required"}}, status_code=401,
                                headers={"Cache-Control": "no-store"})
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


async def _json_body(request: Request) -> dict:
    _require_json(request)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "不是合法 JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "必须是 JSON 对象")
    return body


def _throttled(retry: int) -> JSONResponse:
    return JSONResponse({"error": {"code": "too_many_attempts", "retry_after": retry}}, status_code=429,
                        headers={"Retry-After": str(retry)})


@app.get("/")
def index() -> FileResponse:
    return _page("index.html")


@app.get("/panel.css")
def panel_css() -> FileResponse:
    """面板与登录页共用的样式（登录页也要用，所以不要会话）。"""
    return FileResponse(BASE / "templates" / "panel.css", media_type="text/css; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})


@app.get("/login")
def login_page(request: Request) -> Response:
    if auth.valid(request.cookies.get(COOKIE)):
        return RedirectResponse("/", status_code=303)
    return _page("login.html")


@app.post("/api/login")
async def login(request: Request):
    body = await _json_body(request)
    key = _client_key(request)
    retry = auth.throttle.retry_after(key)
    if retry:
        return _throttled(retry)
    password = body.get("password")
    if not isinstance(password, str) or not auth.check(password):
        auth.throttle.failed(key)
        log.info("面板登录失败：密码不对")
        retry = auth.throttle.retry_after(key)
        if retry:
            return _throttled(retry)
        return JSONResponse({"error": {"code": "wrong_password"}}, status_code=401)
    auth.throttle.succeeded(key)
    response = JSONResponse({"ok": True})
    _issue_cookie(response, request, auth.new_session())
    return response


@app.post("/api/logout")
def logout(request: Request):
    _require_json(request)
    auth.end_session(request.cookies.get(COOKIE))
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict", secure=_cookie_secure(request))
    return response


@app.post("/api/password")
async def change_password(request: Request):
    body = await _json_body(request)
    key = _client_key(request)
    retry = auth.throttle.retry_after(key)
    if retry:
        return _throttled(retry)
    current, new = body.get("current"), body.get("new")
    if not isinstance(current, str) or not isinstance(new, str) or len(current) > MAX_PASSWORD:
        return JSONResponse({"error": {"code": "invalid"}}, status_code=422)
    try:
        token = auth.change_password(current, new)
    except PasswordError as err:
        if err.code == "wrong_password":
            auth.throttle.failed(key)
            return JSONResponse({"error": {"code": err.code}}, status_code=403)
        return JSONResponse({"error": {"code": err.code}}, status_code=409 if err.code == "password_from_env" else 422)
    auth.throttle.succeeded(key)
    response = JSONResponse({"ok": True})
    _issue_cookie(response, request, token)
    return response


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "service": "colaseedgroup-docker", "version": VERSION}


@app.get("/api/status")
def status() -> dict:
    cfg = store.load()
    return view.status(cfg, store.load_state(), store.load_last_run(), version=VERSION,
                       running=runner.running, next_run_at=runner.next_run_at, missing=missing_fields(cfg))


def _config_view(cfg: dict) -> dict:
    return {**public_view(cfg), "password_from_env": auth.from_env}


@app.get("/api/config")
def get_config() -> dict:
    return _config_view(store.load())


@app.post("/api/config")
async def set_config(request: Request):
    body = await _json_body(request)
    try:
        cfg, notes = merge_update(store.load(), body)
        cfg = store.save(cfg)
    except ConfigError as err:
        return JSONResponse({"error": {"code": err.code, "field": err.field}}, status_code=422)
    log.info("配置已保存")
    runner.wake()
    return {**_config_view(cfg), "notes": notes}


@app.post("/api/downloaders/test")
async def test_downloader(request: Request):
    """设置里每台下载器的「测试连接」：{name, type, url, username, password}，用填的值登录并列一次种子、
    读剩余空间，只读。密码留空 = 用已保存的（同名且地址、类型、用户名都没改时，规则同保存配置）。
    回结果码与数字，从不回地址、用户名、密码，日志里也不写。"""
    body = await _json_body(request)
    row = {k: body.get(k) for k in ("name", "type", "url", "username", "password")}
    if not str(row.get("name") or "").strip():
        row["name"] = "probe"
    try:
        merged, notes = merge_update(store.load(), {"downloaders": [row]})
    except ConfigError as err:
        return JSONResponse({"error": {"code": err.code, "field": err.field}}, status_code=422)
    if any(n.startswith("password_cleared:") for n in notes):
        # 改了地址 / 类型 / 用户名：已保存的密码不跟过去（防止把密码发到别的地址）
        return {"result": "password_required"}
    result = await asyncio.to_thread(downloaders_mod.probe, merged["downloaders"][0], merged["save_path"])
    log.info("测试下载器连接：%s", result["result"])
    free = result.get("free_bytes")
    return {**result, "free_text": view.human_size(free) if free is not None else ""}


ACTIONS = ("retry", "resume", "takeover")


@app.post("/api/action")
async def manual_action(request: Request):
    """面板上的手动操作：{action: retry|resume|takeover, torrent_id}。retry 不带 torrent_id = 全部重试；
    takeover 必须带 confirm: true（面板先弹确认框）。"""
    body = await _json_body(request)
    action, tid = body.get("action"), body.get("torrent_id")
    if action not in ACTIONS or (tid is not None and (isinstance(tid, bool) or not isinstance(tid, int))):
        return JSONResponse({"error": {"code": "invalid"}}, status_code=422)
    if action != "retry" and tid is None:
        return JSONResponse({"error": {"code": "invalid"}}, status_code=422)
    if action == "takeover" and body.get("confirm") is not True:
        return JSONResponse({"error": {"code": "not_confirmed"}}, status_code=422)
    result = await asyncio.to_thread(runner.act, action, tid)
    return JSONResponse(result, status_code=200 if result.get("ok") else 409)


@app.post("/api/run")
def run_now(request: Request) -> dict:
    _require_json(request)
    if runner.running:
        return {"started": False, "status": "busy"}
    missing = missing_fields(store.load())
    if missing:
        return {"started": False, "status": "not_configured", "missing": missing}
    threading.Thread(target=runner.run_cycle, args=("manual",), name="colaseedgroup-run", daemon=True).start()
    return {"started": True}
