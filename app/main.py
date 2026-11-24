"""可乐保种组 Docker 版（colaseedgroup-docker）：不需要 MoviePilot 的独立服务。

执行器逻辑与 MoviePilot 插件 0.7.0 是同一份（app/seedgroup/，逐字节搬来）；
这里是外壳：配置、定时运行、端口 3006 上的小面板。
"""

import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from . import view
from .config import ConfigError, Store, merge_update, missing_fields, public_view
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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    os.umask(0o077)
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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(BASE / "templates" / "index.html", media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-store"})


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "service": "colaseedgroup-docker", "version": VERSION}


@app.get("/api/status")
def status() -> dict:
    cfg = store.load()
    return view.status(cfg, store.load_state(), store.load_last_run(), version=VERSION,
                       running=runner.running, next_run_at=runner.next_run_at, missing=missing_fields(cfg))


@app.get("/api/config")
def get_config() -> dict:
    return public_view(store.load())


@app.post("/api/config")
async def set_config(request: Request):
    _require_json(request)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "不是合法 JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "配置必须是 JSON 对象")
    try:
        cfg, notes = merge_update(store.load(), body)
        cfg = store.save(cfg)
    except ConfigError as err:
        return JSONResponse({"error": {"code": err.code, "field": err.field}}, status_code=422)
    log.info("配置已保存")
    runner.wake()
    return {**public_view(cfg), "notes": notes}


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
