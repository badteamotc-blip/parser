"""
peek.tg API Client — поиск NFT подарков.

Base URL: https://server.peek.tg/api/nft
Авторизация: заголовок Referer: https://peek.tg/
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

BASE_URL = "https://server.peek.tg/api/nft"
HEADERS = {
    "Referer": "https://peek.tg/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

COOLDOWN_DAYS = 7  # дней кулдауна после улучшения / передачи

# Сетевые настройки (надёжность + скорость)
_MAX_RETRIES = 3            # повторов на временные ошибки (429/5xx/timeout)
_RETRY_BASE_DELAY = 0.4     # базовая задержка backoff, сек
_DEFAULT_CONCURRENCY = 6    # страниц одновременно в search_all_pages


def make_connector(limit: int = 24) -> "aiohttp.TCPConnector":
    """Тюнингованный коннектор: keep-alive + DNS-кэш → меньше TLS/handshake."""
    return aiohttp.TCPConnector(
        limit=limit,
        ttl_dns_cache=300,
        keepalive_timeout=30,
        enable_cleanup_closed=True,
    )


def make_session(limit: int = 24) -> "aiohttp.ClientSession":
    """Готовая сессия peek.tg с правильными заголовками и пулом соединений."""
    return aiohttp.ClientSession(headers=HEADERS, connector=make_connector(limit))


# ── Конвертация имён ─────────────────────────

def human_to_api(name: str) -> str:
    """'Genie Lamp' → 'GenieLamp', "Durov's Cap" → "DurovsCap"."""
    return name.replace(" ", "").replace("-", "").replace("'", "").replace("\u2019", "")


def _strip_tme(username: str) -> str:
    """'t.me/lucha' → 'lucha'."""
    if username.startswith("t.me/"):
        return username[5:]
    return username


# ── Вспомогательные ──────────────────────────

def _parse_dt(s: str | None) -> datetime | None:
    """Парсит ISO дату из API."""
    if not s:
        return None
    try:
        # Формат: "2025-06-01T12:34:56.789Z"
        s = s.rstrip("Z") + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _cooldown_status(item: dict) -> str:
    """
    Определяет статус кулдауна.
    Возвращает: 'free' | 'soon' | 'active' | 'unknown'
    """
    now = datetime.now(timezone.utc)

    # Дата последней передачи
    prev = item.get("previousOwner")
    changed_at = _parse_dt(prev.get("changedAt")) if prev else None

    # Дата улучшения (создания upgraded NFT)
    created_at = _parse_dt(item.get("createdAt"))

    # Берём самую позднюю из двух дат
    cd_start = None
    if changed_at and created_at:
        cd_start = max(changed_at, created_at)
    elif changed_at:
        cd_start = changed_at
    elif created_at:
        cd_start = created_at

    if cd_start is None:
        return "unknown"

    cd_end = cd_start + timedelta(days=COOLDOWN_DAYS)
    remaining = cd_end - now

    if remaining.total_seconds() <= 0:
        return "free"
    elif remaining.total_seconds() <= 2 * 86400:  # ≤ 2 дней
        return "soon"
    else:
        return "active"


def _cooldown_remaining_str(item: dict) -> str:
    """Человекочитаемая строка 'осталось X дней Y часов'."""
    now = datetime.now(timezone.utc)
    prev = item.get("previousOwner")
    changed_at = _parse_dt(prev.get("changedAt")) if prev else None
    created_at = _parse_dt(item.get("createdAt"))
    cd_start = None
    if changed_at and created_at:
        cd_start = max(changed_at, created_at)
    elif changed_at:
        cd_start = changed_at
    elif created_at:
        cd_start = created_at
    if cd_start is None:
        return "?"
    cd_end = cd_start + timedelta(days=COOLDOWN_DAYS)
    remaining = cd_end - now
    if remaining.total_seconds() <= 0:
        return "снят"
    days = remaining.days
    hours = remaining.seconds // 3600
    if days > 0:
        return f"{days}д {hours}ч"
    return f"{hours}ч"


# ── API вызовы ───────────────────────────────

async def fetch_gifts_list() -> list[str]:
    """Список всех коллекций (названий подарков)."""
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        async with session.get(f"{BASE_URL}/gifts", timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                logger.error("peek.tg /gifts error: %d", resp.status)
                return []
            data = await resp.json()
            names = sorted(set(data)) if isinstance(data, list) else []
            return names


async def search_gifts(
    name: str,
    market_only: bool = False,
    page: int = 1,
    sort_by: str = "giftNumber",
    sort_order: str = "asc",
    session: aiohttp.ClientSession | None = None,
) -> list[dict]:
    """
    Поиск NFT по названию коллекции.
    Возвращает список items (до 20 за страницу).
    """
    api_name = human_to_api(name)
    params = {
        "name": api_name,
        "page": str(page),
        "sortBy": sort_by,
        "sortOrder": sort_order,
    }
    if market_only:
        params["marketOnly"] = "true"

    close_session = False
    if session is None:
        session = make_session()
        close_session = True

    try:
        # Повторяем на временных ошибках (429 / 5xx / таймаут / разрыв связи),
        # чтобы случайный сбой не «съедал» целую страницу результатов.
        last_err = None
        for attempt in range(_MAX_RETRIES):
            try:
                async with session.get(
                    f"{BASE_URL}/gifts/search",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if isinstance(data, dict):
                            items = data.get("results", [])
                        elif isinstance(data, list):
                            items = data
                        else:
                            items = []
                        for item in items:
                            u = item.get("username", "")
                            if u:
                                item["username"] = _strip_tme(u)
                        return items
                    # 429 / 5xx — временная ошибка, повторяем с backoff
                    if resp.status == 429 or resp.status >= 500:
                        last_err = f"HTTP {resp.status}"
                        delay = _RETRY_BASE_DELAY * (2 ** attempt)
                        # уважаем Retry-After, если сервер прислал
                        ra = resp.headers.get("Retry-After")
                        if ra and ra.isdigit():
                            delay = max(delay, min(float(ra), 5.0))
                        await asyncio.sleep(delay)
                        continue
                    # 4xx (кроме 429) — не временная, нет смысла повторять
                    logger.error("peek.tg search error: %d for %s", resp.status, name)
                    return []
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_err = repr(e)
                await asyncio.sleep(_RETRY_BASE_DELAY * (2 ** attempt))
                continue
        logger.warning("peek.tg search failed after %d retries (%s): %s",
                       _MAX_RETRIES, last_err, name)
        return []
    except Exception as e:
        logger.error("peek.tg search exception: %s", e)
        return []
    finally:
        if close_session:
            await session.close()


async def search_all_pages(
    name: str,
    market_only: bool = False,
    max_pages: int = 200,
    stop_event: asyncio.Event | None = None,
    progress_callback=None,
    concurrent: int = _DEFAULT_CONCURRENCY,
) -> list[dict]:
    """
    Загружает все страницы результатов для коллекции.
    Параллельно по `concurrent` страниц одновременно (с retry внутри search_gifts).
    Останавливается, только когда встречает неполную/пустую страницу В КОНЦЕ
    батча — так временный сбой в середине не обрезает выдачу преждевременно.
    """
    all_items = []
    async with make_session() as session:
        page = 1
        while page <= max_pages:
            if stop_event and stop_event.is_set():
                break

            # Параллельный запрос нескольких страниц подряд
            last_page = min(page + concurrent, max_pages + 1)
            tasks = [
                search_gifts(name, market_only=market_only, page=p, session=session)
                for p in range(page, last_page)
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Конец достигнут, только если ПОСЛЕДНЯЯ страница батча неполная.
            # (search_gifts уже сделал retry, так что пустота = реальный конец.)
            reached_end = False
            for batch in results:
                if isinstance(batch, Exception) or not batch:
                    reached_end = True
                    continue
                all_items.extend(batch)
                if len(batch) < 20:
                    reached_end = True

            if progress_callback:
                await progress_callback(len(all_items), page)

            page += len(tasks)
            if reached_end:
                break
            await asyncio.sleep(0.03)
    return all_items


# ── Фильтры ──────────────────────────────────

def filter_market_telegram(items: list[dict]) -> list[dict]:
    """Только лоты на внутреннем маркете Telegram (за звёзды)."""
    result = []
    for item in items:
        market = item.get("market")
        if market and market.get("market") == "telegram":
            result.append(item)
    return result


def filter_original_owners(items: list[dict]) -> list[dict]:
    """Подарки которые никогда не передавались (нет previousOwner)."""
    return [item for item in items if not item.get("previousOwner")]


def filter_by_cooldown(items: list[dict], status: str) -> list[dict]:
    """
    Фильтр по кулдауну.
    status: 'free' | 'soon' | 'active'
    """
    result = []
    for item in items:
        s = _cooldown_status(item)
        if s == status:
            result.append(item)
    return result


# ── Извлечение данных из item ────────────────

def extract_owner_info(item: dict) -> dict:
    """Извлекает информацию о владельце из peek.tg item."""
    raw_uname = item.get("username", "")
    return {
        "display_name": item.get("owner", ""),
        "username": _strip_tme(raw_uname) if raw_uname else "",
        "user_id": item.get("userId"),
        "gift_name": item.get("giftName", item.get("title", "")),
        "gift_number": item.get("giftNumber"),
        "model": item.get("model", ""),
        "pattern": item.get("pattern", ""),
        "backdrop": item.get("backdrop", ""),
        "rarity_model": item.get("rarityModel"),
        "rarity_pattern": item.get("rarityPattern"),
        "rarity_backdrop": item.get("rarityBackdrop"),
        "created_at": item.get("createdAt"),
        "previous_owner": item.get("previousOwner"),
        "market": item.get("market"),
        "cooldown_status": _cooldown_status(item),
        "cooldown_remaining": _cooldown_remaining_str(item),
        "nft_link": f"https://t.me/nft/{item.get('giftName', '').replace(' ', '')}-{item.get('giftNumber', '')}",
    }
