"""Redis client wrapper around isales-common's factory."""

from __future__ import annotations

import os
from typing import Any

from isales_common.utils.redis import get_redis as _get_redis
from redis.asyncio import Redis


def get_redis(url: str | None = None, *, decode_responses: bool = True) -> Redis[Any]:
    """Build an async Redis client. URL falls back to ``ISALES_REDIS_URL``."""
    resolved = url or os.environ["ISALES_REDIS_URL"]
    return _get_redis(resolved, decode_responses=decode_responses)
