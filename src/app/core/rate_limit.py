import redis.asyncio as redis

from app.config import settings

_KEY_PREFIX = "rate_limit:"

_client = redis.from_url(settings.redis_url, decode_responses=True)


async def is_rate_limited(key: str, limit: int, window_seconds: int) -> bool:
    """固定時間窗計數：同一 key 在 window_seconds 內第 limit+1 次起回傳 True。

    用於綁定碼核銷等安全敏感操作，Redis 無法連線時視為受限（fail closed），
    避免防護在 Redis 故障時整個失效。
    """
    redis_key = f"{_KEY_PREFIX}{key}"
    try:
        count = await _client.incr(redis_key)
        if count == 1:
            await _client.expire(redis_key, window_seconds)
    except Exception:
        return True
    return count > limit
