"""ICE (STUN/TURN) servers for browser calls.

Hosts like Render accept no inbound UDP, so a browser can't reach the bot's WebRTC peer directly;
both sides have to relay through a TURN server. Two sources, first match wins:
- Cloudflare Realtime TURN: short-lived credentials minted from CLOUDFLARE_TURN_KEY_ID and
  CLOUDFLARE_TURN_API_TOKEN, cached until shortly before they expire.
- ICE_SERVERS: a static JSON list, e.g. [{"urls": "turn:host:3478", "username": "u", "credential": "p"}].
With neither set (local runs), no servers are configured and peers connect directly.
"""

import json
import time

import httpx
from loguru import logger

from app.config import settings

CF_URL = "https://rtc.live.cloudflare.com/v1/turn/keys/{key_id}/credentials/generate-ice-servers"
CF_TTL = 6 * 3600
_cf_cache: dict = {"servers": None, "expires": 0.0}


def _normalize(servers) -> list[dict]:
    """A list of {urls, username?, credential?}, minus port-53 URLs (browsers block them)."""
    if isinstance(servers, dict):
        servers = [servers]
    out = []
    for s in servers or []:
        urls = s.get("urls") or []
        urls = [urls] if isinstance(urls, str) else list(urls)
        urls = [u for u in urls if ":53?" not in u and not u.endswith(":53")]
        if not urls:
            continue
        entry = {"urls": urls}
        for k in ("username", "credential"):
            if s.get(k):
                entry[k] = s[k]
        out.append(entry)
    return out


async def _cloudflare() -> list[dict]:
    now = time.time()
    if _cf_cache["servers"] is not None and now < _cf_cache["expires"]:
        return _cf_cache["servers"]
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            CF_URL.format(key_id=settings.cloudflare_turn_key_id),
            headers={"Authorization": f"Bearer {settings.cloudflare_turn_api_token}"},
            json={"ttl": CF_TTL},
        )
        r.raise_for_status()
    servers = _normalize(r.json().get("iceServers"))
    _cf_cache.update(servers=servers, expires=now + CF_TTL - 600)
    return servers


async def ice_servers() -> list[dict]:
    if settings.cloudflare_turn_key_id and settings.cloudflare_turn_api_token:
        try:
            return await _cloudflare()
        except (httpx.HTTPError, ValueError, KeyError) as e:
            logger.error(f"Cloudflare TURN credentials failed: {e!r}")
            return []
    if settings.ice_servers:
        try:
            return _normalize(json.loads(settings.ice_servers))
        except (ValueError, AttributeError) as e:
            logger.error(f"ICE_SERVERS is not a valid JSON list: {e!r}")
    return []
