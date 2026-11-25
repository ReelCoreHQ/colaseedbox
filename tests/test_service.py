"""独立版外壳：配置、跑一轮（加种 / 收回 / 已存在不接管 / 出错不删）、面板数据、日志无秘密。"""

import json
import os
import pathlib
import stat
import tempfile
import unittest

from support import (KEY, LAN, LOG, QB_PASS, ROOT, SIGNED, SITE, FakeSession, Rsp, assignment, fake_qb, item,
                     make_torrent)

import app.seedgroup.site as site_mod
from app import view
from app.config import ConfigError, Store, merge_update, normalize, public_view
from app.runner import Runner
from app.seedgroup import VENDOR, VERSION
from app.seedgroup.clients import wrap
from app.seedgroup.engine import human_size

TAG = "可乐保种组"
PATH = "/downloads/colaseedgroup"


def base_config(**extra):
    cfg = {"enabled": True, "site_url": SITE, "api_key": KEY, "save_path": PATH, "tag": TAG,
           "downloaders": [{"name": "qb1", "type": "qbittorrent", "url": f"http://{LAN}:8080",
                            "username": "admin", "password": QB_PASS}]}
    cfg.update(extra)
    return cfg


class Box(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(pathlib.Path(self.tmp.name))
        self.store.save(base_config())
        self.qb = fake_qb()
        self.runner = Runner(self.store, build_downloaders=lambda confs: [wrap(c["name"], c["type"], self.qb)
                                                                           for c in confs])
        LOG.lines.clear()

    def session(self, routes):
        sess = FakeSession(routes)
        orig = site_mod.requests.Session
        site_mod.requests.Session = lambda: sess
        self.addCleanup(setattr, site_mod.requests, "Session", orig)
        return sess

    def own(self, name, tid):
        """先让 qB 里有一颗「本服务加的」种子并记账。"""
        data, h = make_torrent(name)
        self.qb.qbc.torrents_add(data, PATH, False, [TAG], None, False)
        state = self.store.load_state()
        state.setdefault("owned", {})[h] = {"downloader": "qb1", "torrent_id": tid, "size": 1024, "added_at": 1}
        self.store.save_state(state)
        return h

    def everything_written(self, sess):
        blob = LOG.text()
        for p in pathlib.Path(self.tmp.name).glob("*.json"):
            if p.name != "config.json":
                blob += p.read_text("utf-8")
        blob += json.dumps(sess.posted(), ensure_ascii=False)
        blob += json.dumps(self.status(), ensure_ascii=False)
        return blob

    def status(self):
        cfg = self.store.load()
        return view.status(cfg, self.store.load_state(), self.store.load_last_run(), version=VERSION,
                           running=False, next_run_at=None, missing=[])

    # ---------------- 一轮 ----------------
    def test_add_lands_in_dedicated_path_with_tag_and_no_secrets_anywhere(self):
        data, h = make_torrent("Film")
        sess = self.session({"/assignment": Rsp(200, assignment([item(h, 3, "add")])),
                             SIGNED: Rsp(200, content=data), "/state": Rsp(200, {"ok": True, "revision": 1})})
        report = self.runner.run_cycle("manual")
        self.assertEqual(report["status"], "ok", report)
        self.assertEqual(report["added"], [h])
        self.assertEqual(report["action_counts"], {"add": 1})
        row = self.qb.qbc.rows[0]
        self.assertEqual((row["save_path"], row["tags"]), (PATH, TAG))
        # 签名下载链接不带 Key；派单 / 上报带 Key 与实例 ID
        dl_call = [c for c in sess.calls if SIGNED in c[1]][0]
        self.assertNotIn("X-API-Key", dl_call[2])
        api_call = [c for c in sess.calls if c[1].endswith("/assignment")][0]
        self.assertEqual(api_call[2]["X-API-Key"], KEY)
        self.assertEqual(api_call[2]["X-Plugin-Instance"], self.store.load()["instance_id"])
        self.assertTrue(api_call[2]["User-Agent"].startswith("ColaSeedGroup/"))
        posted = sess.posted()[0]
        self.assertEqual(posted["plugin_version"], VERSION)
        self.assertEqual(posted["items"], [{"info_hash": h, "status": "downloading", "progress": 0.0,
                                            "external": False}])
        self.assertEqual(posted["clients"], [{"name": "qbittorrent#1", "readable": True, "up_kib": 0, "dl_kib": 0}])
        blob = self.everything_written(sess)
        for secret in (KEY, QB_PASS, LAN, SIGNED):
            self.assertNotIn(secret, blob)

    def test_preexisting_same_hash_untouched_and_reported_external(self):
        data, h = make_torrent("Theirs")
        self.qb.qbc.rows.append({"hash": h, "name": "Theirs", "save_path": "/downloads/mine",
                                 "content_path": "/downloads/mine/Theirs", "tags": "", "progress": 1,
                                 "state": "uploading"})
        sess = self.session({"/assignment": Rsp(200, assignment([item(h, 4, "add")])),
                             "/state": Rsp(200, {"ok": True})})
        report = self.runner.run_cycle()
        self.assertEqual(report["skipped"], {h: "external"})
        self.assertEqual(sess.posted()[0]["items"], [{"info_hash": h, "status": "seeding", "progress": 1.0,
                                                      "external": True}])
        # 站点随后收回它：不是本服务加的，不删
        sess.routes["/assignment"] = Rsp(200, assignment([item(h, 4, "remove")], revision=2))
        report = self.runner.run_cycle()
        self.assertEqual(report["skipped"], {h: "not_owned"})
        self.assertEqual(self.qb.qbc.deleted, [])
        self.assertEqual(len(self.qb.qbc.rows), 1)

    def test_release_removes_only_owned(self):
        mine = self.own("Mine", 7)
        _, other = make_torrent("Other")
        self.qb.qbc.rows.append({"hash": other, "name": "Other", "save_path": PATH,
                                 "content_path": f"{PATH}/Other", "tags": TAG, "progress": 1, "state": "uploading"})
        self.session({"/assignment": Rsp(200, assignment([item(mine, 7, "remove"), item(other, 8, "remove")])),
                      "/state": Rsp(200, {"ok": True})})
        report = self.runner.run_cycle()
        self.assertEqual(report["removed"], [mine])
        self.assertEqual(report["skipped"], {other: "not_owned"})
        self.assertEqual(self.qb.qbc.deleted, [(mine, True)])
        self.assertNotIn(mine, self.store.load_state()["owned"])

    def test_site_errors_and_empty_list_delete_nothing(self):
        mine = self.own("Mine", 7)
        cases = [Rsp(500, {"error": {"code": "internal", "message": "内部错误 SECRET-MESSAGE"}}),
                 Rsp(404, {"error": {"code": "not_found", "message": "x"}}),
                 Rsp(403, {"error": {"code": "api_key_scope_denied", "message": "x"}}),
                 Rsp(200, {"items": "nope"}),
                 Rsp(200, assignment([]))]
        for rsp in cases:
            self.session({"/assignment": rsp, "/state": Rsp(200, {"ok": True})})
            self.runner.run_cycle()
            self.assertEqual(self.qb.qbc.deleted, [], rsp.status_code)
            self.assertIn(mine, self.store.load_state()["owned"])
        st = self.status()
        self.assertNotIn("SECRET-MESSAGE", json.dumps(st, ensure_ascii=False))

    def test_error_reported_by_code(self):
        self.session({"/assignment": Rsp(403, {"error": {"code": "api_key_scope_denied", "message": "随便"}})})
        self.runner.run_cycle()
        st = self.status()
        self.assertEqual(st["last"]["status"], "site_error")
        self.assertEqual(st["last"]["errors"][0]["code"], "http_403:api_key_scope_denied")
        self.assertEqual(st["last"]["errors"][0]["text"], view.SITE_CODE_TEXT["api_key_scope_denied"])
        self.assertNotIn("随便", json.dumps(st, ensure_ascii=False))

    def test_downloader_down_blocks_adds_and_deletes(self):
        mine = self.own("Mine", 7)
        data, h = make_torrent("New")
        self.qb.qbc.fail_list = True
        self.session({"/assignment": Rsp(200, assignment([item(mine, 7, "remove"), item(h, 9, "add")])),
                      SIGNED: Rsp(200, content=data), "/state": Rsp(200, {"ok": True})})
        report = self.runner.run_cycle()
        self.assertFalse(report["deletes_allowed"])
        self.assertEqual(report["added"], [])
        self.assertEqual(self.qb.qbc.deleted, [])
        self.assertNotIn(LAN, LOG.text())
        self.assertNotIn(QB_PASS, LOG.text())

    def test_not_configured_does_nothing(self):
        self.store.save(base_config(api_key=""))
        self.assertEqual(self.runner.run_cycle()["status"], "not_configured")

    def test_stop_interrupts(self):
        self.runner.stop()
        self.assertEqual(self.runner.run_cycle()["status"], "stopped")

    def test_outdated_only_reports(self):
        mine = self.own("Mine", 7)
        sess = self.session({"/assignment": Rsp(200, assignment([item(mine, 7, "remove")], minimum="9.0.0")),
                             "/state": Rsp(200, {"ok": True})})
        report = self.runner.run_cycle()
        self.assertEqual(report["status"], "outdated")
        self.assertEqual(self.qb.qbc.deleted, [])
        self.assertEqual(len(sess.posted()), 1)

    # ---------------- 面板数据 ----------------
    def test_status_sizes_auto_scale(self):
        self.store.save_state({"owned": {"a" * 40: {"downloader": "qb1", "torrent_id": 1, "size": 50331648, "added_at": 1},
                                         "b" * 40: {"downloader": "qb1", "torrent_id": 2, "size": 67108864, "added_at": 2}}})
        self.store.save_last_run({"status": "ok", "capacity_gib": 2048, "items": 2, "finished_at": 1,
                                  "action_counts": {"keep": 2}, "skipped": {"c" * 40: "external"}})
        st = self.status()
        self.assertEqual((st["owned_count"], st["owned_total_text"]), (2, "112 MiB"))
        self.assertEqual(st["last"]["capacity_text"], "2.0 TiB")
        self.assertIsNone(st["groups"])   # 老版本留下的 last_run 没有快照
        # 老版本的 last_run 也没有下载器快照：配置里的下载器照样列出，标成「待同步」
        self.assertEqual(st["downloaders"], [{"name": "qb1", "type": "qbittorrent", "reachable": None,
                                              "free_bytes": None, "free_text": "", "low": False}])
        self.assertNotIn("skipped", st["last"])

    def test_status_lists_every_assignment_by_name_without_hash(self):
        mine = self.own("Mine.Film", 1)
        _, theirs = make_torrent("Theirs.Film")
        self.qb.qbc.rows.append({"hash": theirs, "name": "Theirs.Film", "save_path": "/downloads/mine",
                                 "content_path": "/downloads/mine/Theirs.Film", "tags": "", "progress": 1,
                                 "state": "uploading"})
        data, new = make_torrent("New.Film")
        _, gone = make_torrent("Gone.Film")
        _, later = make_torrent("Later.Film")
        items = [item(mine, 1, "keep"), item(theirs, 2, "keep"), item(new, 3, "add"), item(gone, 4, "remove"),
                 item(later, 5, "add", url="")]
        items[0]["name"] = "站点给的名字"
        sess = self.session({"/assignment": Rsp(200, assignment(items)), "3-" + SIGNED: Rsp(200, content=data),
                             "/state": Rsp(200, {"ok": True})})
        self.assertEqual(self.runner.run_cycle()["status"], "ok")
        st = self.status()
        self.assertEqual(st["assign_stats"], {"count": 4, "size_text": "4.0 KiB", "removing": 1, "problem": 0,
                                              "pending_count": 1, "pending_bytes": 1024, "pending_text": "1.0 KiB"})
        for gone in ("assignments", "filters", "default_filter", "assignment_summary_text"):
            self.assertNotIn(gone, st)
        g = st["groups"]
        self.assertEqual((g["count"], g["size_text"]), (5, "5.0 KiB"))
        secs = {x["key"]: x for x in g["sections"]}
        self.assertEqual(list(secs), ["active", "seeding", "removing"])          # 没有异常：不出现
        self.assertEqual([(v["name"], v["badge"], v["note"]) for v in secs["active"]["rows"]],
                         [("New.Film", "插件管理", "本轮已加"), ("站点给的名字", "插件管理", "")])
        self.assertEqual([(p["label"], p["count"]) for p in secs["active"]["pending"]], [("派单没有下载链接", 1)])
        theirs_row = secs["seeding"]["rows"][0]
        self.assertEqual((theirs_row["name"], theirs_row["badge"], theirs_row["primary"],
                          [m["action"] for m in theirs_row["more"]]), ("Theirs.Film", "非插件管理", None, ["takeover"]))
        self.assertIsNone(theirs_row["site"])                                     # 站点没给观测字段：不显示
        self.assertEqual([(v["name"], v["outcome_text"]) for v in secs["removing"]["rows"]], [("#4", "下载器里已没有")])
        self.assertEqual(secs["seeding"]["rows"][0]["size_text"], "1.0 KiB")
        self.assertIn("连同文件一起删除", st["takeover_warning"])
        blob = json.dumps(st, ensure_ascii=False)
        for h in (mine, theirs, new, gone, later):
            self.assertNotIn(h, blob)
        self.assertNotIn("http", blob)
        self.assertNotIn(SIGNED, self.everything_written(sess))

    # ---------------- 面板上的手动操作 ----------------
    def seed_actions(self):
        mine = self.own("Mine.Film", 1)
        self.qb.qbc._row(mine)["state"] = "pausedDL"
        _, theirs = make_torrent("Theirs.Film")
        self.qb.qbc.rows.append({"hash": theirs, "name": "Theirs.Film", "save_path": "/media", "tags": "",
                                 "content_path": "/media/Theirs.Film", "progress": 1, "state": "uploading"})
        data, failed = make_torrent("Failed.Film")
        sess = self.session({"/assignment": Rsp(200, assignment([item(mine, 1, "keep"), item(theirs, 2, "keep"),
                                                                 item(failed, 3, "add", url=f"{SITE}/gone")])),
                             "/gone": Rsp(404, {}), "/state": Rsp(200, {"ok": True})})
        self.assertEqual(self.runner.run_cycle()["status"], "ok")
        return mine, theirs, failed, sess

    def test_actions_resume_retry_and_refusals(self):
        mine, theirs, failed, _ = self.seed_actions()
        st = self.status()
        problem = st["groups"]["sections"][0]
        self.assertEqual(problem["key"], "problem")
        self.assertEqual([(c["label"], (c["fix"] or {}).get("action")) for c in problem["causes"]],
                         [("下载器里已暂停", "resume"), ("加种失败 · 站点返回 404", "retry")])
        rows = {v["name"]: v for sec in st["groups"]["sections"] for v in sec.get("rows", [])}
        self.assertEqual([m["action"] for m in rows["Theirs.Film"]["more"]], ["takeover"])
        r = self.runner.act("resume", 2)                       # 不是本服务的：拒绝，碰都不碰
        self.assertEqual((r["ok"], r["code"]), (False, "not_owned"))
        self.assertEqual(self.qb.qbc.calls, [])
        self.assertEqual(self.runner.act("resume", 1)["code"], "resumed")
        self.assertEqual(self.qb.qbc.calls, [("start", mine)])
        self.assertIn(failed, self.store.load_state()["backoff"])
        self.assertEqual(self.runner.act("retry", 3)["code"], "retried")
        self.assertNotIn(failed, self.store.load_state()["backoff"])
        self.assertEqual(self.runner.act("retry")["code"], "nothing_to_retry")
        r = self.runner.act("takeover", 1)                     # 已经是本服务的：什么都不做
        self.assertEqual((r["ok"], r["code"]), (True, "already_owned"))

    def test_takeover_then_release_deletes_respecting_shared_content(self):
        mine, theirs, failed, sess = self.seed_actions()
        # 同一内容还被别站的辅种用着
        self.qb.qbc.rows.append({"hash": "f" * 40, "name": "Theirs.Film", "save_path": "/media", "tags": "other",
                                 "content_path": "/media/Theirs.Film", "progress": 1, "state": "uploading"})
        self.assertEqual(self.runner.act("takeover", 2)["code"], "taken_over")
        rec = self.store.load_state()["owned"][theirs]
        self.assertTrue(rec["taken_over"])
        self.assertIn(("tag", theirs, (TAG,)), self.qb.qbc.calls)
        self.assertEqual(self.qb.qbc._row(theirs)["save_path"], "/media")
        sess.routes["/assignment"] = Rsp(200, assignment([item(theirs, 2, "remove")], revision=2))
        report = self.runner.run_cycle()
        self.assertEqual(report["removed"], [theirs])
        self.assertEqual(self.qb.qbc.deleted, [(theirs, False)])   # 共用内容：只删种子
        self.assertEqual(json.dumps(self.status(), ensure_ascii=False).count(theirs), 0)

    def test_actions_wait_for_running_sync(self):
        self.seed_actions()
        self.runner._lock.acquire()
        try:
            self.assertEqual(self.runner.act("retry")["code"], "busy")
        finally:
            self.runner._lock.release()

    def test_panel_renders_assignment_table(self):
        html = (ROOT / "templates" / "index.html").read_text("utf-8")
        for text in ("需要处理", "进行中", "做种中", "即将删除", 'role="progressbar"', 'popovertarget="row-menu"',
                     "/account/seed-group", "站点上的我的保种组 ↗", "const PAGE = 50", "showModal", "/api/action",
                     "aria-pressed", "aria-expanded", "torrent_ids", "/api/downloaders/test", 'role="switch"',
                     "trapFocus", "修改登录密码"):
            self.assertIn(text, html)
        for gone in ("info_hash", "本轮跳过", "删种闸门：开", "assignment_summary_text", ">站点要求<", "<table"):
            self.assertNotIn(gone, html)
        # 开关全用 input.switch：页面上没有别的复选框
        self.assertEqual(html.count('type="checkbox"'), html.count('type="checkbox" role="switch" class="switch"'))

    # ---------------- 自动接管（执行器同一份规则，这里测外壳把开关传进去） ----------------
    def test_auto_takeover_follows_setting(self):
        _, theirs = make_torrent("Theirs.Film")
        self.qb.qbc.rows.append({"hash": theirs, "name": "Theirs.Film", "save_path": "/media", "tags": "",
                                 "content_path": "/media/Theirs.Film", "progress": 1, "state": "stalledUP"})
        self.session({"/assignment": Rsp(200, assignment([item(theirs, 2, "keep")])), "/state": Rsp(200, {"ok": True})})
        self.assertFalse(self.store.load()["auto_takeover"])             # 默认关
        self.assertEqual(self.runner.run_cycle()["taken_over"], [])
        self.assertEqual(self.qb.qbc.calls, [])
        self.store.save(base_config(auto_takeover=True))
        report = self.runner.run_cycle()
        self.assertEqual(report["taken_over"], [theirs])
        self.assertIn(("tag", theirs, (TAG,)), self.qb.qbc.calls)
        self.assertTrue(self.store.load_state()["owned"][theirs]["taken_over"])
        st = self.status()
        self.assertEqual(st["last"]["taken_over"], 1)
        took = st["groups"]["sections"][0]["rows"][0]
        self.assertEqual((took["badge"], took["note"]), ("已接管", "本轮自动接管"))
        self.assertIn("自动接管 Theirs.Film", LOG.text())
        self.assertNotIn(theirs, json.dumps(st, ensure_ascii=False))

    # ---------------- 下载器剩余空间（每轮读一次，记进 last_run） ----------------
    def run_empty(self):
        self.session({"/assignment": Rsp(200, assignment([item(make_torrent("Big")[1], 9, "add", size=5 * 1024 ** 4,
                                                                url=f"{SITE}/gone")])),
                      "/gone": Rsp(404, {}), "/state": Rsp(200, {"ok": True})})
        return self.runner.run_cycle()

    def test_free_space_captured_once_per_run(self):
        calls = []
        orig = self.qb.qbc.sync_maindata
        self.qb.qbc.sync_maindata = lambda: calls.append(1) or orig()
        report = self.run_empty()
        self.assertEqual(calls, [1])
        self.assertEqual(report["downloaders"], [{"name": "qb1", "type": "qbittorrent", "reachable": True,
                                                  "free_bytes": 3 * 1024 ** 4}])
        self.assertEqual(self.store.load_last_run()["downloaders"], report["downloaders"])
        st = self.status()
        card = st["downloaders"][0]
        self.assertEqual((card["reachable"], card["free_text"]), (True, "3.0 TiB"))
        self.assertTrue(card["low"])           # 还要加 5 TiB，只剩 3 TiB
        self.assertEqual(st["assign_stats"]["pending_text"], "5.0 TiB")
        self.qb.qbc.free = 8 * 1024 ** 4
        self.run_empty()
        self.assertFalse(self.status()["downloaders"][0]["low"])

    def test_free_space_unreadable_and_unreachable(self):
        self.qb.qbc.fail_maindata = True
        report = self.run_empty()
        self.assertEqual(report["downloaders"][0], {"name": "qb1", "type": "qbittorrent", "reachable": True,
                                                    "free_bytes": None})
        card = self.status()["downloaders"][0]
        self.assertEqual((card["free_bytes"], card["free_text"], card["low"]), (None, "", False))
        self.qb.qbc = None                     # 登录失败：连不上，也不去读
        self.qb.reconnect = lambda: None       # 不真去连
        report = self.run_empty()
        self.assertEqual(report["downloaders"][0]["reachable"], False)
        self.assertIsNone(report["downloaders"][0]["free_bytes"])
        blob = LOG.text() + json.dumps(self.store.load_last_run(), ensure_ascii=False)
        self.assertNotIn(LAN, blob)
        self.assertNotIn(QB_PASS, blob)


class HumanSize(unittest.TestCase):
    def test_scales(self):
        cases = [(0, "0 B"), (1023, "1023 B"), (1024, "1.0 KiB"), (48 * 1024 ** 2, "48.0 MiB"),
                 (112 * 1024 ** 2, "112 MiB"), (2048 * 1024 ** 3, "2.0 TiB"), (1536 * 1024 ** 3, "1.5 TiB"),
                 (None, "-"), ("x", "-")]
        for value, want in cases:
            self.assertEqual(human_size(value), want, value)


class ConfigTests(unittest.TestCase):
    def test_auto_takeover_saved_and_merged(self):
        cfg, _ = merge_update(normalize(base_config()), {"auto_takeover": True})
        self.assertTrue(cfg["auto_takeover"])
        self.assertFalse(normalize(base_config())["auto_takeover"])
        self.assertTrue(public_view(cfg)["auto_takeover"])

    def test_limits_clamped(self):
        cfg = normalize({"max_deletes": 500, "max_adds": -3, "interval": 1})
        self.assertEqual((cfg["max_deletes"], cfg["max_adds"], cfg["interval"]), (20, 0, 10))

    def test_defaults_10_minutes_10_adds_saved_values_kept(self):
        cfg = normalize({})
        self.assertEqual((cfg["interval"], cfg["max_adds"], cfg["max_deletes"]), (10, 10, 5))
        cfg = normalize({"interval": 30, "max_adds": 5})          # 已保存的配置保持原值
        self.assertEqual((cfg["interval"], cfg["max_adds"]), (30, 5))

    def test_rejects_bad_values(self):
        for raw in ({"save_path": "/"}, {"tag": "a,b"}, {"site_url": "ftp://x"},
                    {"downloaders": [{"name": "192.168.1.5", "type": "qbittorrent", "url": "x:1"}]},
                    {"downloaders": [{"name": "qb", "type": "deluge", "url": "x:1"}]},
                    {"downloaders": [{"name": "qb", "type": "qbittorrent", "url": "http://u:p@x:1"}]}):
            with self.assertRaises(ConfigError, msg=raw):
                normalize(raw)

    def test_files_are_0600_and_key_never_returned(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(pathlib.Path(d))
            cfg = store.save(base_config())
            store.save_state({"owned": {}})
            store.save_last_run({"status": "ok"})
            for name in ("config.json", "state.json", "last_run.json"):
                mode = stat.S_IMODE(os.stat(pathlib.Path(d) / name).st_mode)
                self.assertEqual(mode, 0o600, name)
            os.chmod(pathlib.Path(d) / "config.json", 0o644)
            store.load()
            self.assertEqual(stat.S_IMODE(os.stat(pathlib.Path(d) / "config.json").st_mode), 0o600)
            view_ = json.dumps(public_view(cfg))
            self.assertNotIn(KEY, view_)
            self.assertNotIn(QB_PASS, view_)
            self.assertRegex(cfg["instance_id"], r"^[0-9a-f]{32}$")

    def test_site_change_clears_key_and_url_change_clears_password(self):
        cur = normalize(base_config())
        new, notes = merge_update(cur, {"site_url": "https://evil.example"})
        self.assertEqual(new["api_key"], "")
        self.assertIn("api_key_cleared_site_changed", notes)
        new, _ = merge_update(cur, {"site_url": "https://evil.example", "api_key": "other"})
        self.assertEqual(new["api_key"], "other")
        same, _ = merge_update(cur, {"site_url": SITE + "/"})
        self.assertEqual(same["api_key"], KEY)
        dl = dict(cur["downloaders"][0], password="")
        kept, _ = merge_update(cur, {"downloaders": [dl]})
        self.assertEqual(kept["downloaders"][0]["password"], QB_PASS)
        moved, notes = merge_update(cur, {"downloaders": [dict(dl, url="http://10.9.9.9:8080")]})
        self.assertEqual(moved["downloaders"][0]["password"], "")
        self.assertIn("password_cleared:qb1", notes)


class SourceRules(unittest.TestCase):
    def test_glue_never_writes_limits_or_shells_out(self):
        src = "".join(p.read_text("utf-8") for p in (ROOT / "app").glob("*.py"))
        for forbidden in ("set_speed_limit", "setPreferences", "app_set_preferences", "set_session",
                          "transfer_set", "subprocess", "curl", "os.system"):
            self.assertNotIn(forbidden, src)

    def test_version_is_the_vendored_plugin_version(self):
        self.assertEqual(VERSION, VENDOR["plugin_version"])
        self.assertRegex(VENDOR["commit"], r"^[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
