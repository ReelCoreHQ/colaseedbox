"""独立版的配置与数据文件。

都在数据目录（默认 ``/data``，容器卷）里：

| 文件 | 内容 | 权限 |
|---|---|---|
| ``config.json`` | 站点地址、API Key、实例 ID、下载器（含密码）、专用目录/标签、运行间隔、上限 | 0600 |
| ``state.json`` | 执行器账本：本服务加过的 hash（owned）、上次 revision、加种退避 | 0600 |
| ``last_run.json`` | 上一轮结果（给面板看） | 0600 |

API Key 与下载器密码只在 ``config.json`` 里，接口永远不回传明文，日志不写。
"""

import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from .seedgroup.clients import norm_path
from .seedgroup.engine import HARD_MAX_ADDS, HARD_MAX_DELETES

DEFAULT_SITE = "https://www.colapt.com"
DEFAULT_TAG = "可乐保种组"
DEFAULT_SAVE_PATH = "/downloads/colaseedgroup"
MIN_INTERVAL = 10
MAX_INTERVAL = 24 * 60
DEFAULT_INTERVAL = MIN_INTERVAL   # 默认就按最短间隔跑（站点每 10 分钟派一次单）
DEFAULT_MAX_ADDS = 10
SUPPORTED_TYPES = ("qbittorrent", "transmission")
MAX_DOWNLOADERS = 8
_INSTANCE_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")  # 字母开头、不含点：名字不可能是 IP

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "site_url": DEFAULT_SITE,
    "api_key": "",
    "instance_id": "",
    "downloaders": [],
    "save_path": DEFAULT_SAVE_PATH,
    "tag": DEFAULT_TAG,
    "interval": DEFAULT_INTERVAL,
    "max_adds": DEFAULT_MAX_ADDS,
    "max_deletes": 5,
    "delete_files": True,
    "auto_takeover": False,
}


class ConfigError(ValueError):
    """配置不合法。``field`` 是出错的字段名，``code`` 是固定原因码。"""

    def __init__(self, field: str, code: str):
        super().__init__(f"{field}:{code}")
        self.field = field
        self.code = code


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _site_url(value: Any) -> str:
    url = str(value or "").strip().rstrip("/") or DEFAULT_SITE
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.netloc or parts.query or parts.fragment:
        raise ConfigError("site_url", "invalid")
    return url


def _downloader_url(value: Any) -> str:
    url = str(value or "").strip().rstrip("/")
    if "://" not in url:
        url = "http://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ConfigError("downloaders.url", "invalid")
    return url


def write_private(path: Path, data: Dict[str, Any]) -> None:
    """原子写 JSON，文件权限 0600（只有容器里跑服务的用户能读）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    if st.st_mode & 0o077:
        # 旧版或手工放进来的文件权限太宽：收紧到 0600
        os.chmod(path, 0o600)
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def normalize(raw: Dict[str, Any]) -> Dict[str, Any]:
    """把任意来源的配置收敛成合法形状；钳制数值、规整路径。字段不合法抛 ConfigError。"""
    cfg = dict(DEFAULTS)
    cfg["enabled"] = bool(raw.get("enabled", DEFAULTS["enabled"]))
    cfg["site_url"] = _site_url(raw.get("site_url"))
    cfg["api_key"] = str(raw.get("api_key") or "").strip()
    instance = str(raw.get("instance_id") or "").strip()
    cfg["instance_id"] = instance if _INSTANCE_RE.match(instance) else ""
    save_path = norm_path(str(raw.get("save_path") or "")) or DEFAULT_SAVE_PATH
    if save_path in ("/", ""):
        raise ConfigError("save_path", "too_broad")
    cfg["save_path"] = save_path
    tag = str(raw.get("tag") or "").strip() or DEFAULT_TAG
    if "," in tag or len(tag) > 64:
        raise ConfigError("tag", "invalid")
    cfg["tag"] = tag
    cfg["interval"] = _int(raw.get("interval"), DEFAULT_INTERVAL, MIN_INTERVAL, MAX_INTERVAL)
    cfg["max_adds"] = _int(raw.get("max_adds"), DEFAULT_MAX_ADDS, 0, HARD_MAX_ADDS)
    cfg["max_deletes"] = _int(raw.get("max_deletes"), 5, 0, HARD_MAX_DELETES)
    cfg["delete_files"] = bool(raw.get("delete_files", True))
    cfg["auto_takeover"] = bool(raw.get("auto_takeover", False))
    downloaders: List[Dict[str, Any]] = []
    names = set()
    for item in (raw.get("downloaders") or [])[:MAX_DOWNLOADERS]:
        if not isinstance(item, dict):
            raise ConfigError("downloaders", "invalid")
        name = str(item.get("name") or "").strip()
        kind = str(item.get("type") or "").strip()
        if not _NAME_RE.match(name) or name in names:
            raise ConfigError("downloaders.name", "invalid")
        if kind not in SUPPORTED_TYPES:
            raise ConfigError("downloaders.type", "unsupported")
        names.add(name)
        downloaders.append({
            "name": name,
            "type": kind,
            "url": _downloader_url(item.get("url")),
            "username": str(item.get("username") or ""),
            "password": str(item.get("password") or ""),
        })
    cfg["downloaders"] = downloaders
    return cfg


def public_view(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """给面板看的配置：API Key 与下载器密码只回「是否已设置」。"""
    out = {k: v for k, v in cfg.items() if k not in ("api_key", "downloaders")}
    out["api_key_set"] = bool(cfg.get("api_key"))
    out["downloaders"] = [
        {"name": d["name"], "type": d["type"], "url": d["url"], "username": d["username"],
         "password_set": bool(d.get("password"))}
        for d in cfg.get("downloaders") or []
    ]
    return out


def merge_update(current: Dict[str, Any], body: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """把面板提交的改动并进当前配置，返回（新配置, 提示码）。

    秘密字段的规则：
    - ``api_key`` 留空 = 不改；``clear_api_key: true`` = 清掉。
    - 改了站点地址却没同时填新 Key → 清掉旧 Key（防止有人改个地址把 Key 发去别处）。
    - 下载器同理：密码留空 = 沿用同名下载器的旧密码；但地址/类型/用户名变了就不沿用。
    """
    notes: List[str] = []
    merged = dict(current)
    for key in ("enabled", "site_url", "save_path", "tag", "interval", "max_adds", "max_deletes", "delete_files",
                "auto_takeover"):
        if key in body:
            merged[key] = body[key]
    new_key = str(body.get("api_key") or "").strip()
    if new_key:
        merged["api_key"] = new_key
    elif body.get("clear_api_key"):
        merged["api_key"] = ""
    if "site_url" in body and not new_key and current.get("api_key"):
        try:
            changed = _site_url(body.get("site_url")) != current.get("site_url")
        except ConfigError:
            changed = True
        if changed:
            merged["api_key"] = ""
            notes.append("api_key_cleared_site_changed")
    if "downloaders" in body:
        old = {d["name"]: d for d in current.get("downloaders") or []}
        rows = []
        for item in body.get("downloaders") or []:
            if not isinstance(item, dict):
                raise ConfigError("downloaders", "invalid")
            row = dict(item)
            if not str(row.get("password") or ""):
                prev = old.get(str(row.get("name") or "").strip())
                row["password"] = ""
                if prev and prev.get("password"):
                    same = (str(row.get("type") or "") == prev["type"]
                            and str(row.get("username") or "") == prev["username"])
                    try:
                        same = same and _downloader_url(row.get("url")) == prev["url"]
                    except ConfigError:
                        same = False
                    if same:
                        row["password"] = prev["password"]
                    else:
                        notes.append(f"password_cleared:{prev['name']}")
            rows.append(row)
        merged["downloaders"] = rows
    return normalize(merged), notes


class Store:
    """数据目录读写。线程安全（面板与调度线程会同时读写）。"""

    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.config_path = self.dir / "config.json"
        self.state_path = self.dir / "state.json"
        self.last_run_path = self.dir / "last_run.json"
        self._lock = threading.RLock()

    def load(self) -> Dict[str, Any]:
        with self._lock:
            raw = read_json(self.config_path) or {}
            broken = False
            try:
                cfg = normalize(raw)
            except ConfigError:
                # 文件被手改坏了：按默认值跑（未配置 = 不连站点、不加不删），不覆盖原文件
                broken = True
                cfg = normalize({"instance_id": raw.get("instance_id")})
            cfg["broken"] = broken
            if not cfg["instance_id"] and not broken:
                cfg["instance_id"] = uuid.uuid4().hex
                write_private(self.config_path, {k: v for k, v in cfg.items() if k != "broken"})
            return cfg

    def save(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            clean = normalize(cfg)
            if not clean["instance_id"]:
                clean["instance_id"] = self.load()["instance_id"] or uuid.uuid4().hex
            write_private(self.config_path, clean)
            return clean

    def load_state(self) -> Dict[str, Any]:
        with self._lock:
            return read_json(self.state_path) or {}

    def save_state(self, state: Dict[str, Any]) -> None:
        with self._lock:
            write_private(self.state_path, state)

    def load_last_run(self) -> Dict[str, Any]:
        with self._lock:
            return read_json(self.last_run_path) or {}

    def save_last_run(self, report: Dict[str, Any]) -> None:
        with self._lock:
            write_private(self.last_run_path, report)


def missing_fields(cfg: Dict[str, Any]) -> List[str]:
    """还缺什么才能跑：api_key / downloaders / enabled。"""
    out = []
    if cfg.get("broken"):
        out.append("config_broken")
    if not cfg.get("enabled"):
        out.append("enabled")
    if not cfg.get("api_key"):
        out.append("api_key")
    if not cfg.get("downloaders"):
        out.append("downloaders")
    return out
