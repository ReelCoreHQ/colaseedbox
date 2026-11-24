"""面板登录：密码哈希、首次启动生成、会话、限速、改密码、环境变量密码、重置。"""

import json
import os
import pathlib
import stat
import tempfile
import unittest

from support import LOG

from fastapi.testclient import TestClient

import app.main as main
from app.auth import AUTH_FILE, Auth, PasswordError, Throttle, hash_password, verify_password

ENV_PASS = "env-pass-1234"


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class HashTests(unittest.TestCase):
    def test_hash_and_verify(self):
        rec = hash_password("correct horse")
        self.assertTrue(verify_password("correct horse", rec))
        self.assertFalse(verify_password("correct hors", rec))
        self.assertFalse(verify_password("", rec))
        self.assertNotIn("correct horse", json.dumps(rec))
        self.assertEqual(rec["scheme"], "scrypt")

    def test_salt_is_random(self):
        a, b = hash_password("same"), hash_password("same")
        self.assertNotEqual(a["salt"], b["salt"])
        self.assertNotEqual(a["hash"], b["hash"])

    def test_broken_record_never_verifies(self):
        rec = hash_password("pw")
        for bad in ({**rec, "scheme": "plain"}, {**rec, "hash": "!!"}, {**rec, "n": 2 ** 30},
                    {k: v for k, v in rec.items() if k != "salt"}, {}):
            self.assertFalse(verify_password("pw", bad))


class FirstStartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        LOG.lines.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def test_generates_once_logs_once_and_stores_only_hash(self):
        password = Auth(self.dir).ensure()
        self.assertGreaterEqual(len(password), 12)
        path = self.dir / AUTH_FILE
        self.assertEqual(mode(path), 0o600)
        self.assertNotIn(password, path.read_text("utf-8"))
        self.assertEqual(LOG.text().count(password), 1)
        self.assertIn("初始登录密码", LOG.text())
        # 再启动：不重新生成、不再显示
        again = Auth(self.dir)
        self.assertIsNone(again.ensure())
        self.assertEqual(LOG.text().count(password), 1)
        self.assertEqual(LOG.text().count("初始登录密码"), 1)
        self.assertTrue(again.check(password))
        self.assertFalse(again.check(password + "x"))

    def test_reset_by_deleting_file(self):
        old = Auth(self.dir).ensure()
        (self.dir / AUTH_FILE).unlink()
        auth = Auth(self.dir)
        new = auth.ensure()
        self.assertTrue(new and new != old)
        self.assertTrue(auth.check(new))
        self.assertFalse(auth.check(old))
        self.assertEqual(LOG.text().count("初始登录密码"), 2)

    def test_corrupt_file_regenerates(self):
        (self.dir / AUTH_FILE).write_text("{not json")
        auth = Auth(self.dir)
        self.assertTrue(auth.check(auth.ensure()))

    def test_no_password_before_ensure(self):
        self.assertFalse(Auth(self.dir).check(""))


class ThrottleTests(unittest.TestCase):
    def test_per_client_exponential_backoff(self):
        t = Throttle(free=5, cap=900, global_max=1000)
        for _ in range(4):
            t.failed("a", now=100)
            self.assertEqual(t.retry_after("a", now=100), 0)
        waits = []
        for _ in range(5):
            t.failed("a", now=100)
            waits.append(t.retry_after("a", now=100))
        self.assertEqual(waits, [2, 4, 8, 16, 32])
        self.assertEqual(t.retry_after("b", now=100), 0)       # 别的地址不受影响
        self.assertEqual(t.retry_after("a", now=133), 0)       # 等够了就能再试
        for _ in range(20):
            t.failed("a", now=200)
        self.assertEqual(t.retry_after("a", now=200), 900)     # 封顶
        t.succeeded("a")
        self.assertEqual(t.retry_after("a", now=200), 0)

    def test_global_limit(self):
        t = Throttle(free=5, global_max=10, window=600)
        for i in range(10):
            t.failed(f"ip{i}", now=1000)
        self.assertEqual(t.retry_after("fresh", now=1000), 600)
        self.assertEqual(t.retry_after("fresh", now=1601), 0)


class ApiAuthBase(unittest.TestCase):
    env_password = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        os.environ["COLASEEDGROUP_NO_SCHEDULER"] = "1"
        main.store.__init__(self.dir)
        main.auth = Auth(self.dir, env_password=self.env_password)
        self.password = main.auth.ensure() or self.env_password
        self.client = TestClient(main.app)
        LOG.lines.clear()

    def tearDown(self):
        os.environ.pop("COLASEEDGROUP_COOKIE_SECURE", None)
        self.tmp.cleanup()

    def login(self, client=None, password=None):
        client = client or self.client
        r = client.post("/api/login", json={"password": password or self.password})
        self.assertEqual(r.status_code, 200, r.text)
        return r


class SessionTests(ApiAuthBase):
    def test_everything_but_health_needs_a_session(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        for method, path in (("get", "/api/status"), ("get", "/api/config"), ("post", "/api/config"),
                             ("post", "/api/run"), ("post", "/api/password"), ("get", "/api/nope")):
            r = self.client.request(method.upper(), path, json={})
            self.assertEqual(r.status_code, 401, path)
            self.assertEqual(r.json()["error"]["code"], "login_required")
        r = self.client.get("/", follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/login"))
        r = self.client.get("/login")
        self.assertEqual(r.status_code, 200)
        self.assertIn("登录密码", r.text)
        self.assertIn("auth.json", r.text)

    def test_login_cookie_flags_and_logout(self):
        self.assertEqual(self.client.post("/api/login", content="{}", headers={"Content-Type": "text/plain"}).status_code,
                         415)
        r = self.login()
        cookie = r.headers["set-cookie"]
        for part in ("colaseedgroup_session=", "HttpOnly", "SameSite=strict", "Max-Age=604800", "Path=/"):
            self.assertIn(part.lower(), cookie.lower())
        self.assertNotIn("secure", cookie.lower())             # http + auto：不加 Secure
        self.assertEqual(self.client.get("/api/status").status_code, 200)
        self.assertEqual(self.client.get("/", follow_redirects=False).status_code, 200)
        self.assertEqual(self.client.get("/login", follow_redirects=False).status_code, 303)
        token = self.client.cookies.get("colaseedgroup_session")
        self.assertNotIn(token, LOG.text())
        self.assertNotIn(self.password, LOG.text())
        self.assertEqual(self.client.post("/api/logout", json={}).status_code, 200)
        stale = TestClient(main.app, cookies={"colaseedgroup_session": token})
        self.assertEqual(stale.get("/api/status").status_code, 401)
        self.assertEqual(self.client.get("/api/status").status_code, 401)

    def test_secure_cookie_modes(self):
        https = TestClient(main.app, base_url="https://testserver")
        self.assertIn("secure", self.login(https).headers["set-cookie"].lower())
        os.environ["COLASEEDGROUP_COOKIE_SECURE"] = "1"
        self.assertIn("secure", self.login().headers["set-cookie"].lower())
        os.environ["COLASEEDGROUP_COOKIE_SECURE"] = "0"
        self.assertNotIn("secure", self.login(https).headers["set-cookie"].lower())

    def test_expired_session(self):
        token = main.auth.new_session(now=0)
        self.assertFalse(main.auth.valid(token))
        self.assertTrue(main.auth.valid(main.auth.new_session()))
        self.assertFalse(main.auth.valid("made-up"))

    def test_wrong_passwords_throttled(self):
        codes = [self.client.post("/api/login", json={"password": "wrong-one"}).status_code for _ in range(6)]
        self.assertEqual(codes[:4], [401] * 4)
        self.assertEqual(codes[4:], [429, 429])
        r = self.client.post("/api/login", json={"password": self.password})
        self.assertEqual(r.status_code, 429)                   # 等待期内连对的密码也不校验
        self.assertEqual(r.json()["error"]["code"], "too_many_attempts")
        self.assertGreater(int(r.headers["retry-after"]), 0)
        self.assertNotIn("testclient", LOG.text())             # 不记客户端地址
        self.assertNotIn("wrong-one", LOG.text())
        main.auth.throttle.succeeded("testclient")
        self.login()

    def test_change_password_invalidates_other_sessions(self):
        other = TestClient(main.app)
        self.login()
        self.login(other)
        bad = self.client.post("/api/password", json={"current": "nope-nope", "new": "brand-new-pass"})
        self.assertEqual((bad.status_code, bad.json()["error"]["code"]), (403, "wrong_password"))
        short = self.client.post("/api/password", json={"current": self.password, "new": "short"})
        self.assertEqual((short.status_code, short.json()["error"]["code"]), (422, "too_short"))
        self.assertEqual(self.client.post("/api/password", content="{}",
                                          headers={"Content-Type": "text/plain"}).status_code, 415)
        r = self.client.post("/api/password", json={"current": self.password, "new": "brand-new-pass"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.client.get("/api/status").status_code, 200)   # 改密码的这个浏览器换了新会话
        self.assertEqual(other.get("/api/status").status_code, 401)         # 其它会话全部作废
        self.assertEqual(TestClient(main.app).post("/api/login", json={"password": self.password}).status_code, 401)
        self.login(TestClient(main.app), "brand-new-pass")
        path = self.dir / AUTH_FILE
        self.assertEqual(mode(path), 0o600)
        self.assertNotIn("brand-new-pass", path.read_text("utf-8"))
        self.assertNotIn("brand-new-pass", LOG.text())
        restarted = Auth(self.dir)
        self.assertIsNone(restarted.ensure())
        self.assertTrue(restarted.check("brand-new-pass"))

    def test_config_says_password_is_changeable(self):
        self.login()
        self.assertFalse(self.client.get("/api/config").json()["password_from_env"])


class EnvPasswordTests(ApiAuthBase):
    env_password = ENV_PASS

    def test_env_password_mode(self):
        self.assertFalse((self.dir / AUTH_FILE).exists())
        self.assertNotIn("初始登录密码", LOG.text())
        self.assertEqual(self.client.post("/api/login", json={"password": "something-else"}).status_code, 401)
        self.login()
        self.assertTrue(self.client.get("/api/config").json()["password_from_env"])
        r = self.client.post("/api/password", json={"current": ENV_PASS, "new": "another-pass-1"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "password_from_env"))
        self.assertFalse((self.dir / AUTH_FILE).exists())
        self.assertNotIn(ENV_PASS, LOG.text())

    def test_short_env_password_refused(self):
        with self.assertRaises(PasswordError):
            Auth(self.dir, env_password="short").ensure()


if __name__ == "__main__":
    unittest.main()
