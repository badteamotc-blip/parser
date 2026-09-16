"""Telegram Stars Rating checker.

Uses a user MTProto session and users.getFullUser() to read UserFull.stars_rating.
Only confirmed ratings are accepted by the scanner; unknown/unavailable ratings are rejected.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

from telethon import TelegramClient
from telethon.tl.functions.users import GetFullUserRequest

from config import (
    TELEGRAM_API_ID,
    TELEGRAM_API_HASH,
    TELEGRAM_SESSION_FILE,
    RATING_CACHE_TTL,
    RATING_CONCURRENCY,
    RATING_MAX_LEVEL,
    RATING_FILTER_ENABLED,
)

logger = logging.getLogger(__name__)

_client: TelegramClient | None = None
_lock = asyncio.Lock()
_sem = asyncio.Semaphore(max(1, RATING_CONCURRENCY))
_cache: dict[str, tuple[float, Optional[int]]] = {}


def _key(username: str) -> str:
    return (username or "").strip().lstrip("@").lower()


async def start() -> None:
    """Connect the MTProto client once at bot startup."""
    global _client
    if not RATING_FILTER_ENABLED:
        logger.warning("Stars Rating filter disabled")
        return

    session_path = TELEGRAM_SESSION_FILE
    if not os.path.exists(session_path):
        raise RuntimeError(
            f"Telegram session not found: {session_path}. "
            "Upload my_session.session to the project or set TELEGRAM_SESSION_FILE."
        )

    async with _lock:
        if _client is not None and _client.is_connected():
            return
        client = TelegramClient(session_path, TELEGRAM_API_ID, TELEGRAM_API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError(
                f"Telegram session '{session_path}' is not authorized. "
                "Create/upload a valid user session."
            )
        _client = client
        me = await client.get_me()
        logger.info(
            "Stars Rating checker connected as @%s (id=%s)",
            getattr(me, "username", None) or "-", getattr(me, "id", "-")
        )


async def stop() -> None:
    global _client
    async with _lock:
        if _client is not None:
            try:
                await _client.disconnect()
            finally:
                _client = None


def _extract_level(full) -> Optional[int]:
    rating = getattr(getattr(full, "full_user", None), "stars_rating", None)
    if rating is None:
        return None
    level = getattr(rating, "level", None)
    return int(level) if level is not None else None


async def get_rating(username: str, *, force: bool = False) -> Optional[int]:
    """Return confirmed Stars Rating level, or None if it cannot be confirmed."""
    key = _key(username)
    if not key:
        return None

    now = time.monotonic()
    cached = _cache.get(key)
    if not force and cached and now - cached[0] < RATING_CACHE_TTL:
        return cached[1]

    await start()
    client = _client
    if client is None:
        return None

    async with _sem:
        try:
            full = await client(GetFullUserRequest(key))
            level = _extract_level(full)
            _cache[key] = (time.monotonic(), level)
            return level
        except Exception as exc:
            # Do not treat an API error as an allowed rating.
            logger.debug("Stars Rating check failed for @%s: %s", key, exc)
            _cache[key] = (time.monotonic(), None)
            return None


async def is_allowed(username: str) -> tuple[bool, Optional[int]]:
    if not RATING_FILTER_ENABLED:
        return True, None
    level = await get_rating(username)
    return level is not None and level <= RATING_MAX_LEVEL, level


async def check_many(usernames: list[str]) -> dict[str, Optional[int]]:
    """Check unique usernames concurrently within the configured limit."""
    unique = list(dict.fromkeys(_key(u) for u in usernames if _key(u)))
    if not unique:
        return {}
    values = await asyncio.gather(*(get_rating(u) for u in unique), return_exceptions=True)
    result: dict[str, Optional[int]] = {}
    for username, value in zip(unique, values):
        result[username] = value if isinstance(value, int) else None
    return result
