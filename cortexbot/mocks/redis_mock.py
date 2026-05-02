"""
cortexbot/mocks/redis_mock.py

Returns a fakeredis instance that behaves like aioredis but keeps
all data in process memory. pip install fakeredis handles the rest.
"""
from __future__ import annotations
from typing import Optional

_instance: Optional[object] = None


async def get_fake_redis():
    global _instance
    if _instance is None:
        try:
            import fakeredis.aio as fakeredis
        except ImportError:
            from fakeredis import aioredis as fakeredis
        _instance = fakeredis.FakeRedis(decode_responses=True)
    return _instance
