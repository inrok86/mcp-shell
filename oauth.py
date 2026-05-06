import os
import time
import httpx

_cache: dict[str, tuple[dict, float]] = {}
CACHE_TTL = int(os.getenv("TOKEN_CACHE_TTL", "3600"))  # 기본 1시간


async def get_user(token: str) -> dict | None:
    # 캐시 확인
    if token in _cache:
        user, expires_at = _cache[token]
        if time.time() < expires_at:
            return user
        del _cache[token]

    introspect_url = os.environ["AUTHENTIK_INTROSPECT_URL"]
    userinfo_url = os.environ["AUTHENTIK_USERINFO_URL"]
    client_id = os.environ["CLIENT_ID"]
    client_secret = os.environ["CLIENT_SECRET"]

    async with httpx.AsyncClient() as client:
        r = await client.post(
            introspect_url,
            data={"token": token},
            auth=(client_id, client_secret),
        )
        if not r.is_success or not r.json().get("active"):
            return None

        r = await client.get(userinfo_url, headers={"Authorization": f"Bearer {token}"})
        if not r.is_success:
            return None

        info = r.json()
        if not info.get("preferred_username"):
            return None

    _cache[token] = (info, time.time() + CACHE_TTL)
    return info
