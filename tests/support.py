"""测试替身：假站点（requests.Session）、假 qBittorrent（qbittorrentapi.Client 的形状）、种子文件。

执行器本身（app/seedgroup/）的单元测试在插件仓库里；这里测的是独立版外壳：
配置、直连下载器适配、调度一轮、面板数据，以及「日志与数据里没有秘密」。
"""

import hashlib
import logging
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

KEY = "rcp_LIVE_KEY_ABCDEF0123456789"
QB_PASS = "qb-p@ss-very-secret"
LAN = "192.168.50.23"
SIGNED = "signed-SECRETTOKEN"
SITE = "https://c.example.com"


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())

    def text(self):
        return "\n".join(self.lines)


LOG = ListHandler()
logging.getLogger("colaseedgroup").addHandler(LOG)
logging.getLogger("colaseedgroup").setLevel(logging.DEBUG)


def bencode(value):
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, list):
        return b"l" + b"".join(bencode(v) for v in value) + b"e"
    if isinstance(value, dict):
        return b"d" + b"".join(bencode(k) + bencode(value[k]) for k in sorted(value)) + b"e"
    raise TypeError(value)


def make_torrent(name, length=1024):
    info = {"name": name, "length": length, "piece length": 16384, "pieces": b"\x00" * 20, "private": 1,
            "source": "[https://www.colapt.com] ColaPT"}
    data = bencode({"announce": "https://tracker.example/announce", "info": info})
    return data, hashlib.sha1(bencode(info), usedforsecurity=False).hexdigest()


class Rsp:
    def __init__(self, status=200, body=None, content=b""):
        self.status_code, self._body, self._content, self.headers = status, body, content, {}

    def json(self):
        if self._body is None:
            raise ValueError()
        return self._body

    def iter_content(self, n):
        yield self._content

    def close(self):
        pass


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def request(self, method, url, headers=None, **kw):
        self.calls.append((method, url, headers, kw))
        for suffix, rsp in self.routes.items():
            if url.endswith(suffix):
                return rsp() if callable(rsp) else rsp
        raise AssertionError(url)

    def posted(self):
        return [c[3]["json"] for c in self.calls if c[0] == "POST"]


def assignment(items, revision=1, minimum="0.7.0"):
    return {"revision": revision, "min_plugin_version": minimum, "capacity_gib": 2048,
            "assigned_bytes": str(sum(int(i["size_bytes"]) for i in items)),
            "server_time": "2026-11-24T00:00:00Z", "items": items}


def item(h, tid, action, size=1024, url=None):
    return {"info_hash": h, "torrent_id": tid, "size_bytes": str(size), "action": action,
            "download_url": url if url is not None else f"{SITE}/api/v1/dl/{tid}-{SIGNED}", "reason": None}


class FakeQbc:
    """qbittorrentapi.Client 的最小替身（只实现 QbInstance 与 clients.QbDownloader 用到的）。"""

    def __init__(self):
        self.rows = []
        self.deleted = []
        self.calls = []
        self.fail_list = False

    def torrents_info(self):
        if self.fail_list:
            raise ConnectionError(f"http://{LAN}:8080 refused (password={QB_PASS})")
        return [dict(r) for r in self.rows]

    def torrents_add(self, torrent_files, save_path, is_paused, tags, category, use_auto_torrent_management):
        from app.seedgroup.torrent import info_hash, torrent_name
        h, n = info_hash(torrent_files), torrent_name(torrent_files)
        if any(r["hash"] == h for r in self.rows):
            return "Fails."
        self.rows.append({"hash": h, "name": n, "save_path": save_path, "content_path": f"{save_path}/{n}",
                          "tags": ",".join(tags), "progress": 0, "state": "downloading", "size": 1024,
                          "uploaded": 0})
        return "Ok."

    def torrents_delete(self, delete_files, torrent_hashes):
        self.deleted.append((torrent_hashes, delete_files))
        self.rows = [r for r in self.rows if r["hash"] != torrent_hashes]

    def _row(self, h):
        return next(r for r in self.rows if r["hash"] == h)

    def torrents_start(self, torrent_hashes):
        self.calls.append(("start", torrent_hashes))
        self._row(torrent_hashes)["state"] = "downloading"

    def torrents_recheck(self, torrent_hashes):
        self.calls.append(("recheck", torrent_hashes))

    def torrents_add_tags(self, tags, torrent_hashes):
        self.calls.append(("tag", torrent_hashes, tuple(tags)))
        row = self._row(torrent_hashes)
        row["tags"] = ",".join([t for t in row["tags"].split(",") if t] + list(tags))

    def app_preferences(self):
        return {"up_limit": 0, "dl_limit": 0, "alt_up_limit": 0, "alt_dl_limit": 0}

    def transfer_speed_limits_mode(self):
        return 0


def fake_qb(name="qb1"):
    from app.downloaders import QbInstance
    inst = QbInstance.__new__(QbInstance)
    inst.name = name
    inst._url = f"http://{LAN}:8080"
    inst._username = "admin"
    inst._password = QB_PASS
    inst.qbc = FakeQbc()
    return inst
