"""Keep page fetching on the public internet.

Product pages and the picture URLs inside them come from outside, so without
a check the app could be pointed at the router, other containers or itself.
The address is checked just before each request; a host that changes its DNS
answer between the check and the connection can still slip through.
"""
import asyncio
import ipaddress
import os
import socket
from urllib.parse import urlparse

# Opt-in for tracking shops hosted on your own network
ALLOW_PRIVATE = os.environ.get("ALLOW_PRIVATE_URLS", "false").lower() in ("1", "true", "yes")


class BlockedURL(Exception):
    pass


async def is_public(url: str) -> bool:
    """True if the URL is http(s) and every address its host resolves to is publicly routable."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return False
    addresses = {info[4][0].split("%")[0] for info in infos}
    return bool(addresses) and all(ipaddress.ip_address(a).is_global for a in addresses)


async def require_public(url: str) -> None:
    if not await is_public(url):
        host = urlparse(url).hostname or url
        raise BlockedURL(f"{host} is not a public address (set ALLOW_PRIVATE_URLS=true to allow your own network)")


async def httpx_guard(request) -> None:
    """httpx request hook. Runs for the first request and for every redirect it follows."""
    await require_public(str(request.url))
