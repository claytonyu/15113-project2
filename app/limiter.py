"""Per-IP rate limiting (in-memory; fine for a single Render instance).

The client IP is NOT taken from the leftmost X-Forwarded-For value, which the client controls.
Each proxy appends the address it saw to the right end of the header, so with
TRUSTED_PROXY_HOPS proxies in front of the app, the real client is that many entries from the
right. With 0 hops, or too few entries, the socket address is used.
"""
from fastapi import Request
from slowapi import Limiter

from .config import get_config


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    hops = get_config().trusted_proxy_hops
    if hops <= 0:
        return peer
    entries = [part.strip() for value in request.headers.getlist("x-forwarded-for") for part in value.split(",")]
    entries = [e for e in entries if e]
    if len(entries) < hops:
        return peer
    return entries[-hops]


limiter = Limiter(key_func=client_ip)
