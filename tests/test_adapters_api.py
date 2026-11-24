"""直连下载器适配（与 MoviePilot 下载器模块同形）与面板接口。"""

import json
import os
import pathlib
import tempfile
import unittest

from support import KEY, LAN, QB_PASS, make_torrent

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

    def test_index_served(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("可乐保种组", r.text)


if __name__ == "__main__":
    unittest.main()
