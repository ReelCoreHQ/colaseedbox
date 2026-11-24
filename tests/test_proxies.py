"""受信任代理：登录限速按哪个客户端地址计数。"""

import os
import pathlib
import tempfile
import unittest

import support  # noqa: F401  （把仓库根目录放进 sys.path）

from fastapi.testclient import TestClient

import app.main as main
from app.auth import Auth
from app.proxies import Trusted, client_ip, parse_trusted

CADDY = "172.18.0.1"              # 同机 Caddy 经 docker 网桥进来的地址
CF_EDGE = "172.68.10.20"          # Cloudflare 边缘节点（172.64.0.0/13）
CF_EDGE6 = "2606:4700:10::1"


def peer_app(peer):
    """把请求的直连对端地址换成 peer（TestClient 默认是 "testclient"）。"""
    async def wrapped(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": (peer, 40000)}
        await main.app(scope, receive, send)
    return wrapped


def ip(peer, xff=None, trusted="", cf=None):
    headers = [xff] if isinstance(xff, str) else (xff or [])
    return client_ip(peer, headers, parse_trusted(trusted), cf)


class ParseTests(unittest.TestCase):
    def test_parse(self):
        t = parse_trusted(" 172.16.0.0/12 , ::1, 10.0.0.5 ,cloudflare,")
        self.assertTrue(t.cloudflare)
        self.assertGreater(len(t.networks), 3)
        self.assertFalse(parse_trusted(""))
        self.assertFalse(parse_trusted(None))
        for bad in ("nope", "300.1.1.1", "10.0.0.0/33", "cloud flare"):
            with self.assertRaises(ValueError):
                parse_trusted(bad)


class ClientIpTests(unittest.TestCase):
    def test_default_never_reads_xff(self):
        self.assertEqual(ip(CADDY, "1.2.3.4"), CADDY)
        self.assertEqual(ip(CADDY, "1.2.3.4", cf="5.6.7.8"), CADDY)

    def test_untrusted_peer_xff_ignored(self):
        self.assertEqual(ip("203.0.113.9", "1.2.3.4", trusted="172.16.0.0/12"), "203.0.113.9")

    def test_trusted_peer_single_hop(self):
        self.assertEqual(ip(CADDY, "198.51.100.7", trusted="172.16.0.0/12"), "198.51.100.7")

    def test_multi_hop_and_spoofed_leftmost_ignored(self):
        trusted = "172.16.0.0/12,10.0.0.0/8"
        # 客户端自己塞了 1.1.1.1、9.9.9.9；真正的客户端是最右边第一个不受信任的
        self.assertEqual(ip(CADDY, "1.1.1.1, 9.9.9.9, 198.51.100.7, 10.1.2.3", trusted=trusted), "198.51.100.7")
        # 多个 X-Forwarded-For 头按顺序拼起来
        self.assertEqual(ip(CADDY, ["1.1.1.1", "198.51.100.7, 10.1.2.3"], trusted=trusted), "198.51.100.7")

    def test_malformed_or_empty_falls_back_to_peer(self):
        trusted = "172.16.0.0/12"
        for xff in ("", "garbage", "198.51.100.7, not-an-ip", "198.51.100.7,,10.0.0.1", "1.2.3.4:5678",
                    "[2001:db8::1]"):
            self.assertEqual(ip(CADDY, xff, trusted=trusted), CADDY, xff)
        self.assertEqual(ip(CADDY, [], trusted=trusted), CADDY)

    def test_ipv6(self):
        self.assertEqual(ip("fd00::1", "2001:db8::42", trusted="fd00::/8"), "2001:db8::42")
        self.assertEqual(ip("fd00::1", "2001:db8::42, fd00::7", trusted="fd00::/8"), "2001:db8::42")
        self.assertEqual(ip("::ffff:172.18.0.1", "198.51.100.7", trusted="172.16.0.0/12"), "198.51.100.7")
        self.assertEqual(ip("2001:db8::9", "198.51.100.7", trusted="172.16.0.0/12"), "2001:db8::9")

    def test_all_trusted_returns_leftmost_hop(self):
        self.assertEqual(ip(CADDY, "10.0.0.1", trusted="172.16.0.0/12,10.0.0.0/8"), "10.0.0.1")
        self.assertEqual(ip(CADDY, "10.0.0.1", trusted="172.16.0.0/12,10.0.0.0/8", cf="198.51.100.7"), "10.0.0.1")

    def test_cloudflare_token(self):
        trusted = "172.16.0.0/12,cloudflare"
        # Cloudflare → Caddy（不信 Cloudflare，只写边缘地址）→ 本服务：用 CF-Connecting-IP
        self.assertEqual(ip(CADDY, CF_EDGE, trusted=trusted, cf="198.51.100.7"), "198.51.100.7")
        self.assertEqual(ip(CADDY, CF_EDGE6, trusted=trusted, cf="2001:db8::5"), "2001:db8::5")
        # Caddy 信 Cloudflare、把真实客户端也写进了 XFF：走 XFF
        self.assertEqual(ip(CADDY, f"198.51.100.7, {CF_EDGE}", trusted=trusted, cf="198.51.100.7"), "198.51.100.7")
        # 不是从 Cloudflare 来的，CF-Connecting-IP 是客户端伪造的：不信
        self.assertEqual(ip(CADDY, "203.0.113.5", trusted=trusted, cf="198.51.100.7"), "203.0.113.5")
        self.assertEqual(ip(CADDY, CF_EDGE, trusted="172.16.0.0/12", cf="198.51.100.7"), CF_EDGE)
        # 坏的 CF-Connecting-IP：用边缘地址
        self.assertEqual(ip(CADDY, CF_EDGE, trusted=trusted, cf="bad"), CF_EDGE)
        self.assertTrue(isinstance(parse_trusted("cloudflare"), Trusted))


class ThrottleKeyTests(unittest.TestCase):
    """接口层：配了受信任代理后，一个客户端连错被限速，不挡另一个客户端。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["COLASEEDGROUP_NO_SCHEDULER"] = "1"
        main.store.__init__(pathlib.Path(self.tmp.name))
        main.auth = Auth(pathlib.Path(self.tmp.name))
        self.password = main.auth.ensure()
        self.saved = main.trusted_proxies

    def tearDown(self):
        main.trusted_proxies = self.saved
        self.tmp.cleanup()

    def attempts(self, client, password, cf, n):
        return [client.post("/api/login", json={"password": password},
                            headers={"X-Forwarded-For": CF_EDGE, "CF-Connecting-IP": cf}).status_code
                for _ in range(n)]

    def test_throttle_keys_on_real_client(self):
        main.trusted_proxies = parse_trusted("172.16.0.0/12,cloudflare")
        client = TestClient(peer_app(CADDY))
        self.assertEqual(self.attempts(client, "wrong-pass", "198.51.100.7", 5)[-1], 429)
        self.assertEqual(self.attempts(client, self.password, "198.51.100.7", 1), [429])
        self.assertEqual(self.attempts(client, self.password, "203.0.113.8", 1), [200])

    def test_without_setting_everyone_behind_proxy_shares_a_key(self):
        main.trusted_proxies = parse_trusted("")
        client = TestClient(peer_app(CADDY))
        self.assertEqual(self.attempts(client, "wrong-pass", "198.51.100.7", 5)[-1], 429)
        self.assertEqual(self.attempts(client, self.password, "203.0.113.8", 1), [429])


if __name__ == "__main__":
    unittest.main()
