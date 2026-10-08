"""Per-IP rate limiting (in-memory; fine for a single Render instance).

Behind Render's proxy, start uvicorn with `--proxy-headers --forwarded-allow-ips='*'` so the
client address comes from X-Forwarded-For instead of the proxy's own address.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
