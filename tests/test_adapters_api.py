"""直连下载器适配（与 MoviePilot 下载器模块同形）与面板接口。"""

import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

import qbittorrentapi
import transmission_rpc
from support import KEY, LAN, LOG, QB_PASS, make_torrent

from app.downloaders import TrInstance, _parse_add
from app.seedgroup.clients import TrDownloader


class FakeTrc:
    def __init__(self, response):
        self.response = response
        self.queries = []

    def _http_query(self, query, timeout=None):
        self.queries.append(query)
        return json.dumps(self.response)


def fake_tr(response):
    inst = TrInstance.__new__(TrInstance)
    inst.name = "tr1"
    inst.trc = FakeTrc(response)
    return inst


class AdapterTests(unittest.TestCase):
    def test_qb_add_responses(self):
        self.assertEqual(_parse_add("Ok."), (True, []))
        self.assertEqual(_parse_add("Fails."), (False, []))
        self.assertEqual(_parse_add({"success_count": 1, "added_torrent_ids": ["abc"]}), (True, ["abc"]))
        self.assertEqual(_parse_add({"success_count": 0, "failure_count": 1}), (False, []))
        self.assertEqual(_parse_add(None), (False, []))

    def test_qb_async_add_waits_until_visible(self):
        from support import fake_qb

        from app.seedgroup.clients import wrap
        qb = fake_qb()
        qb.qbc.async_add = True               # qB 的 add 回成功时种子还不在列表里
        data, h = make_torrent("Async")
        ok, _ = qb.add_torrent(content=data, download_dir="/d", tag=["t"])
        self.assertTrue(ok)
        self.assertEqual([t.info_hash for t in wrap("qb", "qbittorrent", qb).list_torrents()], [h])

    def test_tr_added_vs_duplicate(self):
        data, h = make_torrent("Film")
        inst = fake_tr({"result": "success", "arguments": {"torrent-added": {"hashString": h, "id": 1}}})
        res = TrDownloader("tr1", inst).add(data, "/downloads/sg", "tag")
        self.assertTrue(res.ok)
        args = inst.trc.queries[0]["arguments"]
        self.assertEqual((args["download-dir"], args["labels"], args["paused"]), ("/downloads/sg", ["tag"], False))
        inst = fake_tr({"result": "success", "arguments": {"torrent-duplicate": {"hashString": h, "id": 1}}})
        res = TrDownloader("tr1", inst).add(data, "/downloads/sg", "tag")
        self.assertEqual((res.ok, res.duplicate), (False, True))
        inst = fake_tr({"result": "invalid or corrupt torrent file"})
        self.assertFalse(TrDownloader("tr1", inst).add(data, "/downloads/sg", "tag").ok)

    def test_tr_url_parsing(self):
        orig = TrInstance.reconnect
        TrInstance.reconnect = lambda self: None
        try:
            inst = TrInstance("tr1", f"http://{LAN}:9091", "u", "p")
            self.assertEqual((inst._protocol, inst._host, inst._port, inst._path), ("http", LAN, 9091, "/transmission/rpc"))
            inst = TrInstance("tr1", "https://tr.example/custom/rpc", "u", "p")
            self.assertEqual((inst._port, inst._path), (443, "/custom/rpc"))
            self.assertNotIn(LAN, repr(TrInstance("tr1", f"http://{LAN}:9091", "u", "p")))
        finally:
            TrInstance.reconnect = orig


class FreeSpaceTrc:
    """transmission_rpc.Client.free_space 的替身：目录不存在时 Transmission 回错误。"""

    def __init__(self, exists):
        self.exists, self.asked = exists, []

    def free_space(self, path):
        self.asked.append(path)
        if path not in self.exists:
            raise transmission_rpc.TransmissionError(f"http://{LAN}:9091 No such file (password={QB_PASS})")
        return 7 * 1024 ** 3


class FreeSpaceTests(unittest.TestCase):
    def test_tr_free_space_walks_up_to_an_existing_dir(self):
        inst = TrInstance.__new__(TrInstance)
        inst.name, inst.trc = "tr1", FreeSpaceTrc({"/downloads"})
        self.assertEqual(inst.free_space("/downloads/colaseedgroup"), 7 * 1024 ** 3)
        self.assertEqual(inst.trc.asked, ["/downloads/colaseedgroup", "/downloads"])
        inst.trc = FreeSpaceTrc(set())
        self.assertIsNone(inst.free_space("/a/b/c/d/e"))
        self.assertEqual(len(inst.trc.asked), 4)   # 最多往上三层
        inst.trc = None
        self.assertIsNone(inst.free_space("/downloads"))

    def test_qb_free_space_from_maindata(self):
        from support import fake_qb
        qb = fake_qb()
        self.assertEqual(qb.free_space("/ignored"), 3 * 1024 ** 4)
        qb.qbc.sync_maindata = lambda: {"server_state": {}}
        self.assertIsNone(qb.free_space("/x"))
        qb.qbc.sync_maindata = lambda: {"server_state": {"free_space_on_disk": True}}
        self.assertIsNone(qb.free_space("/x"))


class FakeQbClient:
    """qbittorrentapi.Client 的替身：按地址 / 密码模拟连不上、密码不对、正常。异常文本故意带上地址和密码。"""

    made = []

    def __init__(self, host, username=None, password=None, **_kw):
        self.host, self.username, self.password = host, username, password
        FakeQbClient.made.append(self)

    def auth_log_in(self):
        if "10.9.9.9" in self.host:
            raise qbittorrentapi.APIConnectionError(f"Failed to connect to {self.host} (password={self.password})")
        if self.password != QB_PASS:
            raise qbittorrentapi.LoginFailed(f"{self.username}:{self.password}@{self.host} rejected")

    def torrents_info(self, torrent_hashes=None):
        return [{"hash": "a" * 40}, {"hash": "b" * 40}]

    def sync_maindata(self):
        return {"server_state": {"free_space_on_disk": 3 * 1024 ** 4}}


class FakeTrClient:
    def __init__(self, *, password=None, **_kw):
        if password != QB_PASS:
            raise transmission_rpc.TransmissionAuthError(f"401 at http://{LAN}:9091 (password={password})")

    def get_torrents(self, arguments=None):
        return []

    def free_space(self, path):
        return None


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["COLASEEDGROUP_DATA"] = cls.tmp.name
        os.environ["COLASEEDGROUP_NO_SCHEDULER"] = "1"
        from fastapi.testclient import TestClient

        import app.main as main
        from app.auth import Auth
        main.store.__init__(pathlib.Path(cls.tmp.name))
        main.auth = Auth(pathlib.Path(cls.tmp.name))
        password = main.auth.ensure()
        cls.client = TestClient(main.app)
        r = cls.client.post("/api/login", json={"password": password})
        assert r.status_code == 200, r.text

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_health(self):
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["service"], "colaseedgroup-docker")

    def test_config_roundtrip_hides_secrets_and_requires_json(self):
        body = {"api_key": KEY, "site_url": "https://c.example.com",
                "downloaders": [{"name": "qb1", "type": "qbittorrent", "url": f"http://{LAN}:8080",
                                 "username": "admin", "password": QB_PASS}]}
        r = self.client.post("/api/config", content=json.dumps(body), headers={"Content-Type": "text/plain"})
        self.assertEqual(r.status_code, 415)
        r = self.client.post("/api/config", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        for resp in (r, self.client.get("/api/config"), self.client.get("/api/status")):
            self.assertNotIn(KEY, resp.text)
            self.assertNotIn(QB_PASS, resp.text)
        got = self.client.get("/api/config").json()
        self.assertTrue(got["api_key_set"])
        self.assertTrue(got["downloaders"][0]["password_set"])
        r = self.client.post("/api/config", json={"save_path": "/"})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"], {"code": "too_broad", "field": "save_path"})

    def test_run_requires_json(self):
        self.assertEqual(self.client.post("/api/run").status_code, 415)

    def test_action_endpoint_validates(self):
        self.assertEqual(self.client.post("/api/action", content="{}", headers={"Content-Type": "text/plain"}).status_code,
                         415)
        for body in ({"action": "nuke", "torrent_id": 1}, {"action": "resume"}, {"action": "resume", "torrent_id": "1"},
                     {"action": "takeover", "torrent_id": 1}, {"action": "takeover", "torrent_id": 1, "confirm": "yes"}):
            self.assertEqual(self.client.post("/api/action", json=body).status_code, 422, body)
        r = self.client.post("/api/action", json={"action": "retry"})
        self.assertIn(r.status_code, (200, 409))
        self.assertIn("message", r.json())

    # ---------------- 设置里的「测试连接」 ----------------
    def save_qb(self):
        r = self.client.post("/api/config", json={
            "api_key": KEY, "downloaders": [{"name": "qb1", "type": "qbittorrent", "url": f"http://{LAN}:8080",
                                             "username": "admin", "password": QB_PASS}]})
        self.assertEqual(r.status_code, 200, r.text)

    def probe(self, **row):
        body = {"name": "qb1", "type": "qbittorrent", "url": f"http://{LAN}:8080", "username": "admin", "password": ""}
        body.update(row)
        with mock.patch.object(qbittorrentapi, "Client", FakeQbClient), \
             mock.patch.object(transmission_rpc, "Client", FakeTrClient):
            return self.client.post("/api/downloaders/test", json=body)

    def assert_no_secrets(self, *responses):
        blob = LOG.text() + "".join(r.text for r in responses)
        for secret in (QB_PASS, LAN, "10.9.9.9", "wrong-pass", "admin"):
            self.assertNotIn(secret, blob)

    def test_test_connection_results(self):
        self.save_qb()
        LOG.lines.clear()
        FakeQbClient.made.clear()
        ok = self.probe(password=QB_PASS)
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json(), {"result": "ok", "torrents": 2, "free_bytes": 3 * 1024 ** 4, "free_text": "3.0 TiB"})
        bad = self.probe(password="wrong-pass")
        self.assertEqual(bad.json(), {"result": "auth_failed", "free_text": ""})
        down = self.probe(url="http://10.9.9.9:8080", password=QB_PASS)
        self.assertEqual(down.json(), {"result": "unreachable", "free_text": ""})
        tr = self.probe(name="tr1", type="transmission", url=f"http://{LAN}:9091", password="wrong-pass")
        self.assertEqual(tr.json()["result"], "auth_failed")
        tr = self.probe(name="tr1", type="transmission", url=f"http://{LAN}:9091", password=QB_PASS)
        self.assertEqual(tr.json(), {"result": "ok", "torrents": 0, "free_bytes": None, "free_text": ""})
        self.assert_no_secrets(ok, bad, down, tr)
        # 只读：测试连接不改配置
        self.assertEqual(self.client.get("/api/config").json()["downloaders"][0]["url"], f"http://{LAN}:8080")

    def test_test_connection_blank_password_uses_saved_only_for_same_target(self):
        self.save_qb()
        LOG.lines.clear()
        FakeQbClient.made.clear()
        same = self.probe()                                            # 密码留空 = 用已保存的
        self.assertEqual(same.json()["result"], "ok")
        self.assertEqual(FakeQbClient.made[-1].password, QB_PASS)
        moved = self.probe(url="http://10.9.9.9:8080")                 # 改了地址：已保存的密码不跟过去
        self.assertEqual(moved.json(), {"result": "password_required"})
        renamed_user = self.probe(username="root")
        self.assertEqual(renamed_user.json(), {"result": "password_required"})
        self.assertEqual(len(FakeQbClient.made), 1)                    # 后两次根本没去连
        fresh = self.probe(name="", url=f"http://{LAN}:8081")          # 新的一行、没填名字、没填密码
        self.assertEqual(fresh.json()["result"], "auth_failed")
        self.assertIsNone(FakeQbClient.made[-1].password)
        self.assert_no_secrets(same, moved, renamed_user, fresh)

    def test_test_connection_validates_and_requires_session(self):
        r = self.client.post("/api/downloaders/test", content="{}", headers={"Content-Type": "text/plain"})
        self.assertEqual(r.status_code, 415)
        r = self.probe(type="deluge")
        self.assertEqual((r.status_code, r.json()["error"]["field"]), (422, "downloaders.type"))
        r = self.probe(url="http://u:p@host:8080")
        self.assertEqual((r.status_code, r.json()["error"]["field"]), (422, "downloaders.url"))
        from fastapi.testclient import TestClient

        import app.main as main
        anon = TestClient(main.app).post("/api/downloaders/test", json={"name": "qb1"})
        self.assertEqual(anon.status_code, 401)

    def test_panel_css_is_public(self):
        from fastapi.testclient import TestClient

        import app.main as main
        r = TestClient(main.app).get("/panel.css")
        self.assertEqual(r.status_code, 200)
        self.assertIn("--background", r.text)
        self.assertTrue(r.headers["content-type"].startswith("text/css"))

    def test_index_served(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("可乐保种组", r.text)


if __name__ == "__main__":
    unittest.main()
