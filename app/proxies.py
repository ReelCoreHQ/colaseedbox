"""受信任的反向代理：只用于找出登录限速要按哪个客户端地址计数（地址不写日志）。

``COLASEEDGROUP_TRUSTED_PROXIES``：逗号分隔的 IP / CIDR（IPv4、IPv6 都行），另可写 ``cloudflare``
展开成 Cloudflare 公布的地址段。默认空 = 只看直连对端地址（X-Forwarded-For 一概不信）。

直连对端在信任列表里时，从右往左走 X-Forwarded-For，跳过受信任的地址，第一个不受信任的就是客户端。
X-Forwarded-For 有格式不对的项 → 退回直连对端地址。
所有项都受信任、且最左边那一跳是 Cloudflare 的地址（例如 Caddy 不信 Cloudflare、只把边缘节点地址写进
X-Forwarded-For）时，用 Cloudflare 自己写的 ``CF-Connecting-IP``——这个头由 Cloudflare 覆盖，客户端伪造不了。
"""

import ipaddress
from typing import Iterable, List, Optional, Sequence, Union

Network = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]
Address = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]

# Cloudflare 公布的地址段，取自 https://www.cloudflare.com/ips-v4 与 /ips-v6（2026-11-24 抄录）。
# Cloudflare 很少改这份列表，但改了就要在这里更新。
CLOUDFLARE_RANGES = (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "141.101.64.0/18",
    "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22", "198.41.128.0/17",
    "162.158.0.0/15", "104.16.0.0/13", "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32", "2405:8100::/32",
    "2a06:98c0::/29", "2c0f:f248::/32",
)
_CLOUDFLARE = tuple(ipaddress.ip_network(r) for r in CLOUDFLARE_RANGES)
MAX_HOPS = 32


class Trusted:
    def __init__(self, networks: Sequence[Network] = (), cloudflare: bool = False):
        self.cloudflare = cloudflare
        self.networks = tuple(networks) + (_CLOUDFLARE if cloudflare else ())

    def __bool__(self) -> bool:
        return bool(self.networks)

    def contains(self, addr: Address) -> bool:
        return any(addr.version == net.version and addr in net for net in self.networks)


def parse_trusted(value: Optional[str]) -> Trusted:
    """解析环境变量；有不合法的项抛 ValueError（启动时报错退出，不静默忽略）。"""
    networks: List[Network] = []
    cloudflare = False
    for raw in (value or "").split(","):
        item = raw.strip()
        if not item:
            continue
        if item.lower() == "cloudflare":
            cloudflare = True
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            raise ValueError(f"COLASEEDGROUP_TRUSTED_PROXIES 里有不合法的项：{item!r}") from None
    return Trusted(networks, cloudflare)


def _addr(value: Optional[str]) -> Optional[Address]:
    try:
        addr = ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None
    if addr.version == 6 and addr.ipv4_mapped:
        return addr.ipv4_mapped
    return addr


def _is_cloudflare(addr: Address) -> bool:
    return any(addr.version == net.version and addr in net for net in _CLOUDFLARE)


def client_ip(peer: Optional[str], forwarded_for: Iterable[str], trusted: Trusted,
              cf_connecting_ip: Optional[str] = None) -> str:
    """限速用的客户端地址。``forwarded_for`` 是请求里所有 X-Forwarded-For 头（可能多个）。"""
    peer_addr = _addr(peer)
    if peer_addr is None:
        return peer or "-"
    fallback = str(peer_addr)
    if not trusted or not trusted.contains(peer_addr):
        return fallback                         # 对端不受信任：X-Forwarded-For 一概不看
    entries = [e.strip() for header in forwarded_for for e in header.split(",")]
    if not entries or any(not e for e in entries):
        return fallback
    hop = peer_addr
    for entry in reversed(entries[-MAX_HOPS:]):
        addr = _addr(entry)
        if addr is None:
            return fallback                     # 格式不对：退回对端
        if not trusted.contains(addr):
            return str(addr)
        hop = addr
    if trusted.cloudflare and _is_cloudflare(hop):
        cf = _addr(cf_connecting_ip)
        if cf is not None:
            return str(cf)
    return str(hop)
