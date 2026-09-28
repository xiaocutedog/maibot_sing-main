"""外部下载地址安全校验。

音乐平台接口返回的音频直链在下载前必须校验：只允许 http/https，
且主机名解析出的地址不能是内网、环回、链路本地或保留地址。

平台侧一旦被污染、或返回指向内网/云元数据地址（如 169.254.169.254）的跳转，
未校验的下载就会让插件变成现成的出口，因此每个跳转落点都要过一遍这里。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any
from urllib.parse import urljoin, urlparse

# 允许的下载协议
ALLOWED_SCHEMES = ("http", "https")
# 跳转跟随上限
MAX_REDIRECTS = 5
# 需要跟随的 HTTP 跳转状态码
REDIRECT_STATUS = (301, 302, 303, 307, 308)


class ExternalURLBlocked(RuntimeError):
    """下载地址未通过安全校验。"""


def _blocked_reason(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> str:
    if ip.is_loopback:
        return "环回地址"
    if ip.is_link_local:
        return "链路本地地址"
    if ip.is_private:
        return "内网地址"
    if ip.is_reserved:
        return "保留地址"
    if ip.is_multicast:
        return "组播地址"
    if ip.is_unspecified:
        return "未指定地址"
    return ""


def _parse_ip(text: str) -> "ipaddress.IPv4Address | ipaddress.IPv6Address":
    """解析 IP 字面量，忽略 IPv6 的作用域后缀（fe80::1%eth0）。"""
    return ipaddress.ip_address(text.split("%", 1)[0])


def check_url_shape(url: str) -> str:
    """校验 URL 的协议与主机名形式，返回主机名。

    Raises:
        ExternalURLBlocked: 协议不受支持、缺少主机名，或主机名本身就是受限 IP 字面量。
    """
    parsed = urlparse(url or "")
    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ExternalURLBlocked(f"不支持的下载协议: {scheme or '空'}")
    host = parsed.hostname or ""
    if not host:
        raise ExternalURLBlocked("下载地址缺少主机名")
    if host.lower() == "localhost":
        raise ExternalURLBlocked("下载地址指向 localhost，已拒绝")
    try:
        literal = _parse_ip(host)
    except ValueError:
        return host
    reason = _blocked_reason(literal)
    if reason:
        raise ExternalURLBlocked(f"下载地址指向{reason}（{host}），已拒绝")
    return host


async def ensure_external_url(url: str) -> None:
    """校验下载地址：协议 http(s) 且解析结果不落在内网/环回/链路本地等受限网段。

    Raises:
        ExternalURLBlocked: 地址不合法或解析到受限地址。
    """
    host = check_url_shape(url)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ExternalURLBlocked(f"下载地址域名解析失败: {host}") from exc
    for info in infos:
        sockaddr: Any = info[4]
        try:
            ip = _parse_ip(str(sockaddr[0]))
        except ValueError:
            continue
        reason = _blocked_reason(ip)
        if reason:
            raise ExternalURLBlocked(f"下载地址解析到{reason}（{host} -> {ip}），已拒绝")


def resolve_redirect(current: str, location: str) -> str:
    """把跳转的 Location 解析为绝对地址（相对跳转按当前地址补全）。"""
    if not location:
        raise ExternalURLBlocked(f"下载地址跳转缺少 Location: {current}")
    return urljoin(current, location)
