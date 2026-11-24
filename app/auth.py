"""面板登录：一个密码、服务端会话、失败限速。

- 密码只存 scrypt 哈希（``hashlib.scrypt``，每个安装一份随机盐），在 ``data/auth.json``（0600）。
- 第一次启动没有密码时生成一个随机密码，**只在容器日志里显示这一次**（qBittorrent 的做法）。
  忘了密码：删掉 ``data/auth.json`` 再重启容器，会生成并显示一个新的。
- 设了环境变量 ``COLASEEDGROUP_PASSWORD`` 时它就是密码（面板里不能改），``auth.json`` 不用。
- 会话：随机令牌放在 HttpOnly + SameSite=Strict 的 Cookie 里；服务端只在内存里记令牌的 SHA-256，
  7 天过期；改密码时其它会话全部作废；重启容器需要重新登录。
- 登录失败按客户端地址指数退避，另有全局上限；地址只在内存里用于计数，不写日志
  （反向代理后面怎么取真实客户端见 proxies.py）。
"""

import base64
import hashlib
import hmac
import logging
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from typing import Dict, Optional, Tuple

from .config import read_json, write_private

log = logging.getLogger("colaseedgroup")

AUTH_FILE = "auth.json"
MIN_PASSWORD = 8
MAX_PASSWORD = 1024
SESSION_TTL = 7 * 24 * 3600
MAX_SESSIONS = 64
# scrypt 参数：n=2^14, r=8 约 16 MiB 内存、几十毫秒一次
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN = 2 ** 14, 8, 1, 32


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str, salt: Optional[bytes] = None) -> Dict[str, object]:
    """返回存盘用的记录（不含明文）。"""
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                            dklen=SCRYPT_LEN)
    return {"scheme": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
            "salt": _b64(salt), "hash": _b64(digest)}


def verify_password(password: str, record: Dict[str, object]) -> bool:
    """常数时间比较；记录损坏或参数不对一律 False。"""
    try:
        if record.get("scheme") != "scrypt":
            return False
        salt = base64.b64decode(str(record["salt"]), validate=True)
        want = base64.b64decode(str(record["hash"]), validate=True)
        n, r, p = int(record["n"]), int(record["r"]), int(record["p"])
        if not (2 <= n <= 2 ** 16 and 1 <= r <= 16 and 1 <= p <= 4 and len(want) >= 16):
            return False
        got = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(want),
                             maxmem=256 * 1024 * 1024)
    except (KeyError, TypeError, ValueError):
        return False
    return hmac.compare_digest(got, want)


def _well_formed(record: Dict[str, object]) -> bool:
    try:
        return (record.get("scheme") == "scrypt" and int(record["n"]) > 1
                and len(base64.b64decode(str(record["salt"]), validate=True)) >= 8
                and len(base64.b64decode(str(record["hash"]), validate=True)) >= 16)
    except (KeyError, TypeError, ValueError):
        return False


class PasswordError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class Throttle:
    """登录失败限速。

    每个客户端地址：前 ``free`` 次失败不等；之后每多错一次，等待时间翻倍（2 秒起，最多 ``cap`` 秒）。
    全局：``window`` 秒内失败超过 ``global_max`` 次，所有人都要等到窗口里最早那次过期。
    被挡住时不校验密码。成功登录清掉该地址的计数。
    """

    def __init__(self, free: int = 5, cap: int = 900, global_max: int = 30, window: int = 600):
        self.free, self.cap, self.global_max, self.window = free, cap, global_max, window
        self._per: Dict[str, Tuple[int, float]] = {}   # 地址 -> (连续失败次数, 可再试的时间)
        self._global: deque = deque()
        self._lock = threading.Lock()

    def retry_after(self, key: str, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        with self._lock:
            while self._global and self._global[0] <= now - self.window:
                self._global.popleft()
            wait = 0.0
            if len(self._global) >= self.global_max:
                wait = self._global[0] + self.window - now
            _fails, until = self._per.get(key, (0, 0.0))
            wait = max(wait, until - now)
            return max(0, int(wait + 0.999))

    def failed(self, key: str, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            self._global.append(now)
            fails = self._per.get(key, (0, 0.0))[0] + 1
            until = now + min(self.cap, 2 ** (fails - self.free + 1)) if fails >= self.free else 0.0
            if len(self._per) > 4096 and key not in self._per:
                # 内存上限：丢掉已经不在等待期的地址
                self._per = {k: v for k, v in self._per.items() if v[1] > now}
            self._per[key] = (fails, until)

    def succeeded(self, key: str) -> None:
        with self._lock:
            self._per.pop(key, None)


class Auth:
    def __init__(self, data_dir: Path, env_password: Optional[str] = None):
        self.path = Path(data_dir) / AUTH_FILE
        self.env_password = env_password or None
        self._record: Optional[Dict[str, object]] = None
        self._sessions: Dict[str, float] = {}         # sha256(令牌) -> 过期时间
        self._lock = threading.RLock()
        self.throttle = Throttle()

    @property
    def from_env(self) -> bool:
        return self.env_password is not None

    def ensure(self) -> Optional[str]:
        """启动时调用。没有密码就生成一个，写哈希、在日志里显示一次，并返回它（给测试用）。"""
        with self._lock:
            if self.from_env:
                if len(self.env_password) < MIN_PASSWORD:
                    raise PasswordError("env_password_too_short")
                self._record = hash_password(self.env_password)
                log.info("登录密码来自环境变量 COLASEEDGROUP_PASSWORD（面板里不能改）")
                return None
            record = read_json(self.path)
            if record and _well_formed(record):
                self._record = record
                return None
            if record is not None:
                log.warning("%s 损坏，重新生成登录密码", AUTH_FILE)
            password = secrets.token_urlsafe(12)
            self._record = {**hash_password(password), "created_at": int(time.time())}
            write_private(self.path, self._record)
            self._sessions.clear()
            log.warning("初始登录密码：%s（只显示这一次；登录后在「设置 → 登录密码」里改。"
                        "忘了就删掉数据目录里的 %s 再重启容器）", password, AUTH_FILE)
            return password

    def check(self, password: str) -> bool:
        with self._lock:
            record = self._record
        if record is None or not isinstance(password, str) or len(password) > MAX_PASSWORD:
            return False
        return verify_password(password, record)

    # ---- 会话 ----
    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def new_session(self, now: Optional[float] = None) -> str:
        now = time.time() if now is None else now
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions = {k: v for k, v in self._sessions.items() if v > now}
            while len(self._sessions) >= MAX_SESSIONS:
                self._sessions.pop(min(self._sessions, key=self._sessions.get))
            self._sessions[self._key(token)] = now + SESSION_TTL
        return token

    def valid(self, token: Optional[str], now: Optional[float] = None) -> bool:
        if not token or len(token) > 128:
            return False
        now = time.time() if now is None else now
        key = self._key(token)
        with self._lock:
            expires = self._sessions.get(key)
            if expires is None:
                return False
            if expires <= now:
                self._sessions.pop(key, None)
                return False
            return True

    def end_session(self, token: Optional[str]) -> None:
        if token:
            with self._lock:
                self._sessions.pop(self._key(token), None)

    def change_password(self, current: str, new: str) -> str:
        """校验旧密码、写新哈希、作废全部会话；返回给当前客户端的新会话令牌。"""
        if self.from_env:
            raise PasswordError("password_from_env")
        if not isinstance(new, str) or len(new) < MIN_PASSWORD:
            raise PasswordError("too_short")
        if len(new) > MAX_PASSWORD:
            raise PasswordError("too_long")
        if not self.check(current):
            raise PasswordError("wrong_password")
        with self._lock:
            record = {**hash_password(new), "created_at": int(time.time())}
            write_private(self.path, record)
            self._record = record
            self._sessions.clear()
        log.info("登录密码已修改，其它会话已退出")
        return self.new_session()
