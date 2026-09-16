"""
TON API модуль — парсинг NFT юзернеймов и анонимных номеров
через tonapi.io (бесплатный, без ключа).
"""
import asyncio
import logging
from typing import Optional, Callable

import aiohttp

logger = logging.getLogger(__name__)

BASE_URL = "https://tonapi.io/v2"

# Коллекция "Telegram Usernames" на TON
USERNAMES_COLLECTION = (
    "0:80d78a35f955a14b679faa887ff4cd5bfc0f43b4a4eea2a7e6927f3701b273c2"
)

# Лимит на один запрос (макс 1000 у tonapi)
PAGE_SIZE = 100
# Макс параллельных запросов
MAX_CONCURRENT = 10
# Пауза между батчами (секунды) чтобы не ловить 429
BATCH_DELAY = 0.2

# ~$100 ≈ 30 TON (при курсе ~$3.3-3.5)
MAX_PRICE_TON = 30


def format_wallet_short(addr: str) -> str:
    """UQ... → UQ..ab12"""
    if not addr:
        return ""
    if len(addr) > 12:
        return addr[:6] + "…" + addr[-4:]
    return addr


async def fetch_username_items(
    session: aiohttp.ClientSession,
    offset: int = 0,
    limit: int = PAGE_SIZE,
) -> list[dict]:
    """Получить пачку NFT юзернеймов из коллекции."""
    url = f"{BASE_URL}/nfts/collections/{USERNAMES_COLLECTION}/items"
    params = {"limit": limit, "offset": offset}
    try:
        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status == 429:
                logger.warning("TON API rate limit at offset %d", offset)
                await asyncio.sleep(2)
                return []
            if resp.status != 200:
                logger.warning("TON API %d at offset %d", resp.status, offset)
                return []
            data = await resp.json()
            return data.get("nft_items", [])
    except Exception as exc:
        logger.error("TON API error at offset %d: %s", offset, exc)
        return []


def _parse_username_item(item: dict, max_price_ton: float = 0) -> dict | None:
    """
    Парсит один NFT item в удобный формат.

    Если max_price_ton > 0:
      - Пропускает items, которые НЕ на продаже (sale=None)
      - Пропускает items дороже max_price_ton
    Если max_price_ton == 0 — возвращает все.
    """
    meta = item.get("metadata", {})
    owner = item.get("owner", {})
    name_raw = meta.get("name", "")
    username = name_raw.lstrip("@") if name_raw.startswith("@") else name_raw

    if not username:
        return None

    # DNS — если привязан к TG аккаунту: "username.t.me"
    dns = item.get("dns", "")
    has_tg = bool(dns and dns.endswith(".t.me"))

    # Цена (если на продаже)
    sale = item.get("sale")
    price_ton = 0.0
    on_sale = False
    marketplace = ""
    if sale and sale.get("price"):
        on_sale = True
        price_nano = int(sale["price"].get("value", 0))
        price_ton = price_nano / 1e9
        market_info = sale.get("market", {})
        marketplace = market_info.get("name", "")

    # Фильтр по цене: если задан max_price, показываем только дешёвые на продаже
    if max_price_ton > 0:
        if not on_sale:
            return None
        if price_ton <= 0:
            return None  # Пропускаем бесплатные / ошибочные листинги
        if price_ton > max_price_ton:
            return None

    # Мягкий фильтр: только явные боты
    u_lower = username.lower()
    if u_lower.endswith("bot"):
        return None

    wallet = owner.get("address", "")
    tme_name = owner.get("name", "")  # e.g. "cryptoape.t.me"

    return {
        "username": username,
        "wallet": wallet,
        "wallet_short": format_wallet_short(wallet),
        "tme_name": tme_name,
        "dns": dns,
        "has_tg": has_tg,
        "on_sale": on_sale,
        "price_ton": round(price_ton, 1) if on_sale else None,
        "marketplace": marketplace,
        "owner_key": f"nftu_{username}",
    }


async def scan_nft_usernames(
    max_results: int = 200,
    max_price_ton: float = 0,
    only_with_tg: bool = False,
    progress_callback: Optional[Callable] = None,
    stop_event: Optional[asyncio.Event] = None,
    viewed_keys: Optional[set] = None,
) -> list[dict]:
    """
    Автопарсинг NFT юзернеймов из TON блокчейна.

    Args:
        max_results: макс кол-во результатов
        max_price_ton: если > 0 — только на продаже и дешевле N TON
        only_with_tg: если True — только привязанные к TG аккаунту
        progress_callback: async fn(scanned, found)
        stop_event: стоп-сигнал
        viewed_keys: уже просмотренные ключи
    """
    viewed = viewed_keys or set()
    results = []
    offset = 0
    total_scanned = 0
    empty_pages = 0
    rate_limit_retries = 0
    sem = asyncio.Semaphore(MAX_CONCURRENT)

    async def _fetch_batch(sess, off):
        async with sem:
            return off, await fetch_username_items(sess, offset=off, limit=PAGE_SIZE)

    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT * 2, ttl_dns_cache=300)
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

    async with aiohttp.ClientSession(
        headers=headers, connector=connector,
    ) as session:
        while len(results) < max_results:
            if stop_event and stop_event.is_set():
                break

            # Параллельный батч
            tasks = []
            for i in range(MAX_CONCURRENT):
                off = offset + i * PAGE_SIZE
                tasks.append(_fetch_batch(session, off))

            batch_results = await asyncio.gather(*tasks)
            batch_results.sort(key=lambda x: x[0])

            any_items = False
            for off, items in batch_results:
                if not items:
                    empty_pages += 1
                    if empty_pages >= 5:
                        break
                    continue

                any_items = True
                empty_pages = 0
                total_scanned += len(items)

                for item in items:
                    parsed = _parse_username_item(item, max_price_ton=max_price_ton)
                    if parsed is None:
                        continue
                    if only_with_tg and not parsed["has_tg"]:
                        continue
                    if parsed["owner_key"] in viewed:
                        continue
                    results.append(parsed)
                    if len(results) >= max_results:
                        break

                if len(results) >= max_results:
                    break

            if not any_items or empty_pages >= 5:
                # Если все пустые из-за rate limit — пробуем ещё раз
                if rate_limit_retries < 3:
                    rate_limit_retries += 1
                    await asyncio.sleep(3)
                    empty_pages = max(0, empty_pages - 2)
                    continue
                break

            offset += MAX_CONCURRENT * PAGE_SIZE

            if progress_callback:
                await progress_callback(total_scanned, len(results))

            await asyncio.sleep(BATCH_DELAY)

    if progress_callback:
        await progress_callback(total_scanned, len(results))

    return results[:max_results]
