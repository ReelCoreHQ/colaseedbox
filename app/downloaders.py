"""直连 qBittorrent / Transmission 的最小实例，形状与 MoviePilot 的下载器模块一致。

搬来的 ``app/seedgroup/clients.py``（插件原文）包的是 MoviePilot 的 ``Qbittorrent`` / ``Transmission``
实例：只调用 ``qbc`` / ``trc``、``reconnect()``、``get_torrents()``、``add_torrent()`` /
``add_torrent_with_status()``、``delete_torrents()``，以及面板手动操作用的 ``start_torrents()`` /
``recheck_torrents()`` / ``set_torrents_tag()``（qB）/ ``set_torrent_tag()``（TR）这几样。这里用 MoviePilot 用的同一套库
（qbittorrent-api、transmission-rpc）实现同样的几样，执行器逻辑一行不改。

日志只写下载器的名字和异常类型：不写地址、用户名、密码，也不写异常原文（库的异常里常带 URL）。
"""

import base64
import json
import logging
from typing import Any, List, Optional, Tuple
from urllib.parse import urlsplit

import qbittorrentapi
import transmission_rpc

from .seedgroup.clients import wrap

log = logging.getLogger("colaseedgroup")

CONNECT_TIMEOUT = 5
READ_TIMEOUT = 60

# 库自己的日志可能带地址：只放行 ERROR 以上，且我们从不打开它们的调试输出
for _name in ("qbittorrentapi", "transmission-rpc", "urllib3"):
    logging.getLogger(_name).setLevel(logging.ERROR)


class QbInstance:
    """qbittorrent-api 客户端，接口同 MoviePilot ``app.modules.qbittorrent.Qbittorrent``。"""

    def __init__(self, name: str, url: str, username: str, password: str):
        self.name = name
        self._url = url
        self._username = username
        self._password = password
        self.qbc: Optional[qbittorrentapi.Client] = None
        self.reconnect()

    def __repr__(self) -> str:
        return f"QbInstance({self.name})"

    def reconnect(self) -> None:
        try:
            client = qbittorrentapi.Client(
                host=self._url, username=self._username or None, password=self._password or None,
                VERIFY_WEBUI_CERTIFICATE=False,  # 与 MoviePilot 一致：局域网 qB 常是自签证书
                REQUESTS_ARGS={"timeout": (CONNECT_TIMEOUT, READ_TIMEOUT)},
                HTTPADAPTER_ARGS={"max_retries": 0},
                FORCE_SCHEME_FROM_HOST=True,
                DISABLE_LOGGING_DEBUG_OUTPUT=True,
            )
            client.auth_log_in()
            self.qbc = client
        except Exception as err:
            self.qbc = None
            log.warning("下载器 %s（qBittorrent）连接失败：%s", self.name, type(err).__name__)

    def get_torrents(self) -> Tuple[List[Any], bool]:
        if not self.qbc:
            return [], True
        try:
            return list(self.qbc.torrents_info() or []), False
        except Exception as err:
            log.warning("下载器 %s 列种子失败：%s", self.name, type(err).__name__)
            return [], True

    def add_torrent(self, content: bytes, is_paused: bool = False, download_dir: Optional[str] = None,
                    tag: Any = None, category: Optional[str] = None, ignore_category_check: bool = True,
                    **_: Any) -> Tuple[bool, List[str]]:
        if not self.qbc or not content:
            return False, []
        try:
            ret = self.qbc.torrents_add(torrent_files=content, save_path=download_dir, is_paused=is_paused,
                                        tags=tag, category=category if ignore_category_check else None,
                                        use_auto_torrent_management=False)
        except Exception as err:
            log.warning("下载器 %s 加种失败：%s", self.name, type(err).__name__)
            return False, []
        return _parse_add(ret)

    def delete_torrents(self, delete_file: bool, ids: Any) -> bool:
        if not self.qbc or not ids:
            return False
        try:
            self.qbc.torrents_delete(delete_files=bool(delete_file), torrent_hashes=ids)
            return True
        except Exception as err:
            log.warning("下载器 %s 删种失败：%s", self.name, type(err).__name__)
            return False

    def _call(self, what: str, func: Any, **kwargs: Any) -> bool:
        if not self.qbc:
            return False
        try:
            func(**kwargs)
            return True
        except Exception as err:
            log.warning("下载器 %s %s失败：%s", self.name, what, type(err).__name__)
            return False

    def start_torrents(self, ids: Any) -> bool:
        if not self.qbc:
            return False
        # qB 5.x 叫 start，4.x 叫 resume；qbittorrent-api 两个名字都有
        func = getattr(self.qbc, "torrents_start", None) or self.qbc.torrents_resume
        return self._call("恢复种子", func, torrent_hashes=ids)

    def recheck_torrents(self, ids: Any) -> bool:
        return self._call("重新校验", self.qbc.torrents_recheck if self.qbc else None, torrent_hashes=ids)

    def set_torrents_tag(self, ids: Any, tags: List[str]) -> bool:
        """追加标签（qB 的 addTags 本来就保留原有标签）。"""
        return self._call("打标签", self.qbc.torrents_add_tags if self.qbc else None, tags=tags, torrent_hashes=ids)


def _parse_add(ret: Any) -> Tuple[bool, List[str]]:
    """qB 旧版回 "Ok." / "Fails."，5.x 回 {success_count, pending_count, added_torrent_ids, ...}。"""
    if not ret:
        return False, []
    if isinstance(ret, str):
        return "Ok" in ret, []
    get = ret.get if hasattr(ret, "get") else (lambda k, d=None: getattr(ret, k, d))
    ids = [str(x) for x in (get("added_torrent_ids") or []) if x]
    if ids:
        return True, ids
    if get("success_count") or get("pending_count"):
        return True, []
    return False, []


#: clients.TrDownloader 读的字段
TR_FIELDS = ["id", "hashString", "name", "status", "error", "labels", "downloadDir", "percentDone",
             "totalSize", "uploadedEver"]


class TrInstance:
    """transmission-rpc 客户端，接口同 MoviePilot ``app.modules.transmission.Transmission``。"""

    def __init__(self, name: str, url: str, username: str, password: str):
        self.name = name
        parts = urlsplit(url)
        self._protocol = parts.scheme or "http"
        self._host = parts.hostname or ""
        self._port = parts.port or (443 if self._protocol == "https" else 9091)
        self._path = parts.path if parts.path and parts.path != "/" else "/transmission/rpc"
        self._username = username
        self._password = password
        self.trc: Optional[transmission_rpc.Client] = None
        self.reconnect()

    def __repr__(self) -> str:
        return f"TrInstance({self.name})"

    def reconnect(self) -> None:
        try:
            self.trc = transmission_rpc.Client(
                protocol=self._protocol, host=self._host, port=self._port, path=self._path,
                username=self._username or None, password=self._password or None,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
        except Exception as err:
            self.trc = None
            log.warning("下载器 %s（Transmission）连接失败：%s", self.name, type(err).__name__)

    def get_torrents(self) -> Tuple[List[Any], bool]:
        if not self.trc:
            return [], True
        try:
            return list(self.trc.get_torrents(arguments=TR_FIELDS)), False
        except Exception as err:
            log.warning("下载器 %s 列种子失败：%s", self.name, type(err).__name__)
            return [], True

    def add_torrent_with_status(self, content: bytes, is_paused: bool = False,
                                download_dir: Optional[str] = None,
                                labels: Optional[List[str]] = None) -> Tuple[Optional[Any], bool]:
        """返回（种子, 是否本来就有）。transmission-rpc 的 add_torrent 会把 torrent-duplicate 当成成功，
        分不清是不是我们加的；这里和 MoviePilot 一样直接发 torrent-add，按响应里的键区分。"""
        if not self.trc:
            return None, False
        arguments = {"metainfo": base64.b64encode(content).decode("ascii"), "paused": bool(is_paused)}
        if download_dir:
            arguments["download-dir"] = download_dir
        if labels:
            arguments["labels"] = list(labels)
        try:
            rsp = json.loads(self.trc._http_query({"method": "torrent-add", "arguments": arguments}))
        except Exception as err:
            log.warning("下载器 %s 加种失败：%s", self.name, type(err).__name__)
            return None, False
        if rsp.get("result") != "success":
            log.warning("下载器 %s 加种被拒绝", self.name)
            return None, False
        args = rsp.get("arguments") or {}
        for key, existed in (("torrent-added", False), ("torrent-duplicate", True)):
            fields = args.get(key)
            if fields:
                return transmission_rpc.Torrent(fields=fields), existed
        return None, False

    def delete_torrents(self, delete_file: bool, ids: Any) -> bool:
        if not self.trc or not ids:
            return False
        try:
            self.trc.remove_torrent(ids, delete_data=bool(delete_file))
            return True
        except Exception as err:
            log.warning("下载器 %s 删种失败：%s", self.name, type(err).__name__)
            return False

    def _call(self, what: str, method: str, *args: Any, **kwargs: Any) -> bool:
        if not self.trc:
            return False
        try:
            getattr(self.trc, method)(*args, **kwargs)
            return True
        except Exception as err:
            log.warning("下载器 %s %s失败：%s", self.name, what, type(err).__name__)
            return False

    def start_torrents(self, ids: Any) -> bool:
        return self._call("恢复种子", "start_torrent", ids)

    def recheck_torrents(self, ids: Any) -> bool:
        return self._call("重新校验", "verify_torrent", ids)

    def set_torrent_tag(self, ids: Any, tags: List[str], org_tags: Optional[List[str]] = None) -> bool:
        """TR 的 labels 整组覆盖：与原有标签合并后写回（同 MoviePilot）。"""
        if not tags:
            return False
        return self._call("打标签", "change_torrent", ids, labels=sorted(set((org_tags or []) + list(tags))))


def build(downloaders: List[dict]) -> List[Any]:
    """按配置建好下载器，包成执行器认的 ``Downloader``（插件原文 clients.wrap）。
    连不上的也照样返回：执行器列种子时拿到 ``unavailable``，本轮就不加不删。"""
    out = []
    for conf in downloaders:
        cls = QbInstance if conf["type"] == "qbittorrent" else TrInstance
        inst = cls(conf["name"], conf["url"], conf.get("username", ""), conf.get("password", ""))
        dl = wrap(conf["name"], conf["type"], inst)
        if dl is not None:
            out.append(dl)
    return out
