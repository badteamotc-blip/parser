"""
Сканер NFT подарков Telegram — V7.
Парсит t.me/nft/{slug}, определяет 🇨🇳 китайцев и 🇷🇺 русских.
Дополнительно: bio-проверка для русских через t.me/{username}.
"""
import asyncio
import random
import re
import logging
from typing import Callable, Optional

import aiohttp

from chinese_detector import (
    is_chinese_name, is_russian_name, is_russian_bio,
    detect_country, detect_country_full, COUNTRY_DETECTORS,
)
from config import (
    MAX_CONCURRENT_REQUESTS, REQUEST_TIMEOUT,
    RATING_FILTER_ENABLED, RATING_MAX_LEVEL,
    DELAY_BETWEEN_BATCHES,
    RANDOM_COLLECTIONS_COUNT, RANDOM_ITEMS_PER_COLLECTION,
)
import db
import rating_api

logger = logging.getLogger(__name__)

# ── Паттерны для парсинга HTML ────────────────
RE_OWNER_SECTION = re.compile(
    r'<th>Owner</th><td[^>]*>(.*?)</td>', re.DOTALL,
)
RE_OWNER_LINK = re.compile(r'href="https://t\.me/([^"]+)"')
RE_TABLE_ROW = re.compile(
    r'<th>(Model|Backdrop|Symbol|Quantity)</th><td[^>]*>(.*?)</td>', re.DOTALL,
)
RE_CLEAN_HTML = re.compile(r'<[^>]+>')
RE_QUANTITY = re.compile(r'([\d\s\u00a0]+)/([\d\s\u00a0]+)\s*issued')
RE_PERCENT_TAIL = re.compile(r'\s+[\d.]+%\s*$')
RE_BIO = re.compile(
    r'<div class="tgme_page_description[^"]*">(.*?)</div>', re.DOTALL,
)


# ── Парсинг одной страницы ────────────────────

def parse_nft_page(html: str, collection: str, item_number: int) -> Optional[dict]:
    owner_match = RE_OWNER_SECTION.search(html)
    if not owner_match:
        return None

    owner_html = owner_match.group(1)
    link = RE_OWNER_LINK.search(owner_html)
    username = link.group(1) if link else ""
    display_name = RE_CLEAN_HTML.sub('', owner_html).strip()

    model = backdrop = symbol = ""
    quantity_total = 0

    for m in RE_TABLE_ROW.finditer(html):
        key = m.group(1)
        raw = RE_CLEAN_HTML.sub(' ', m.group(2)).strip()
        val = RE_PERCENT_TAIL.sub('', raw).strip()
        if key == "Model":
            model = val
        elif key == "Backdrop":
            backdrop = val
        elif key == "Symbol":
            symbol = val
        elif key == "Quantity":
            qm = RE_QUANTITY.search(raw)
            if qm:
                quantity_total = int(
                    qm.group(2).replace(' ', '').replace('\u00a0', '')
                )

    return {
        "slug": f"{collection}-{item_number}",
        "collection": collection,
        "item_number": item_number,
        "display_name": display_name,
        "username": username,
        "model": model,
        "backdrop": backdrop,
        "symbol": symbol,
        "has_chinese": is_chinese_name(display_name),
        "has_russian": is_russian_name(display_name),
        "detected_country": detect_country(display_name),
        "quantity_total": quantity_total,
    }


async def _fetch_one(
    session: aiohttp.ClientSession,
    collection: str,
    item_number: int,
) -> Optional[dict]:
    url = f"https://t.me/nft/{collection}-{item_number}"
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                return None
            html = await resp.text()
            return parse_nft_page(html, collection, item_number)
    except Exception as exc:
        logger.debug("Ошибка загрузки %s-%d: %s", collection, item_number, exc)
        return None


# ── Bio-проверка для русских ──────────────────

async def _fetch_bio(
    session: aiohttp.ClientSession, username: str,
) -> Optional[str]:
    """Парсит bio с t.me/{username}."""
    if not username:
        return None
    url = f"https://t.me/{username}"
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status != 200:
                return None
            html = await resp.text()
            m = RE_BIO.search(html)
            if m:
                return RE_CLEAN_HTML.sub('', m.group(1)).strip()
    except Exception:
        pass
    return None


async def enrich_russian_from_bios(
    collection: str,
    progress_callback: Optional[Callable] = None,
):
    """
    Вторичная проверка: скрапит bio пользователей без кириллицы в имени.
    Если bio содержит кириллицу → помечает has_russian=1.
    """
    candidates = await db.get_non_russian_with_usernames(collection, limit=200)
    if not candidates:
        return 0

    enriched = 0
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    async def _check_one(session, item):
        nonlocal enriched
        async with semaphore:
            bio = await _fetch_bio(session, item["username"])
            if bio and is_russian_bio(bio):
                await db.update_russian_flag(item["slug"], 1)
                enriched += 1

    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_REQUESTS, ttl_dns_cache=300)
    async with aiohttp.ClientSession(
        headers={"User-Agent": "Mozilla/5.0"}, connector=connector,
    ) as session:
        batch_size = MAX_CONCURRENT_REQUESTS * 2
        for i in range(0, len(candidates), batch_size):
            batch = candidates[i:i + batch_size]
            await asyncio.gather(*[_check_one(session, c) for c in batch])
            if DELAY_BETWEEN_BATCHES > 0:
                await asyncio.sleep(DELAY_BETWEEN_BATCHES)

    if progress_callback:
        await progress_callback(f"Bio: +{enriched} 🇷🇺")
    return enriched


async def enrich_country_from_bios(
    collection: str,
    progress_callback: Optional[Callable] = None,
    limit: int = 200,
):
    """
    Универсальное bio-обогащение для ВСЕХ 12 стран.
    Берёт владельцев без определённой страны (имя нейтральное),
    скрапит bio с t.me/{username} и пытается определить страну по bio.
    Если получилось — проставляет detected_country. Максимальная точность.
    """
    candidates = await db.get_owners_no_country(collection, limit=limit)
    if not candidates:
        return 0

    enriched = 0
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    async def _check_one(session, item):
        nonlocal enriched
        async with semaphore:
            bio = await _fetch_bio(session, item["username"])
            if not bio:
                return
            # Имя + bio вместе — максимально точно
            country = detect_country_full(item.get("display_name", ""), bio)
            if country:
                await db.update_detected_country(item["slug"], country)
                enriched += 1

    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_REQUESTS, ttl_dns_cache=300)
    async with aiohttp.ClientSession(
        headers={"User-Agent": "Mozilla/5.0"}, connector=connector,
    ) as session:
        batch_size = MAX_CONCURRENT_REQUESTS * 2
        for i in range(0, len(candidates), batch_size):
            batch = candidates[i:i + batch_size]
            await asyncio.gather(*[_check_one(session, c) for c in batch])
            if DELAY_BETWEEN_BATCHES > 0:
                await asyncio.sleep(DELAY_BETWEEN_BATCHES)

    if progress_callback:
        await progress_callback(f"Bio: +{enriched} 🌍")
    return enriched


# ── Определение размера коллекции ─────────────

async def get_collection_size(collection: str) -> int:
    cached = await db.get_collection_size(collection)
    if cached:
        return cached

    async with aiohttp.ClientSession(
        headers={"User-Agent": "Mozilla/5.0"},
    ) as session:
        result = await _fetch_one(session, collection, 1)
        if result and result["quantity_total"] > 0:
            total = result["quantity_total"]
            await db.save_collection_size(collection, total)
            await db.save_nft_items_batch([result])
            return total

    return await _binary_search_size(collection)


async def _binary_search_size(collection: str) -> int:
    async with aiohttp.ClientSession(
        headers={"User-Agent": "Mozilla/5.0"},
    ) as session:
        low, high = 1, 300_000
        while low < high:
            mid = (low + high + 1) // 2
            result = await _fetch_one(session, collection, mid)
            if result:
                low = mid
            else:
                high = mid - 1
            await asyncio.sleep(0.1)
        return low


async def enrich_ratings_for_collection(
    collection: str,
    country: str,
    limit: int = 1000,
    progress_callback: Optional[Callable] = None,
) -> int:
    """Resolve Stars Rating for candidate owners and persist it by username.

    Only L0..L{RATING_MAX_LEVEL} are eligible when the filter is enabled.
    Unknown/unavailable ratings remain NULL and therefore never appear in results.
    """
    if not RATING_FILTER_ENABLED:
        return 0
    candidates = await db.get_rating_candidates(collection, country, limit=limit)
    if not candidates:
        return 0

    ratings = await rating_api.check_many([c["username"] for c in candidates])
    allowed = 0
    normalized_ratings = {}
    for candidate in candidates:
        username = candidate["username"]
        level = ratings.get(username.lower().lstrip("@"))
        normalized_ratings[username] = level
        if level is not None and level <= RATING_MAX_LEVEL:
            allowed += 1
    await db.update_ratings_batch(normalized_ratings)

    if progress_callback:
        await progress_callback(f"⭐ Рейтинг: L0–L{RATING_MAX_LEVEL} · {allowed}/{len(candidates)}")
    return allowed


# ── Основной скан ─────────────────────────────

async def scan_collection(
    collection: str,
    model_filter: Optional[str] = None,
    backdrop_filter: Optional[str] = None,
    progress_callback: Optional[Callable] = None,
    stop_event: Optional[asyncio.Event] = None,
    max_results: int = 200,
    max_scan_items: int = 0,
    country: str = "cn",
) -> list[dict]:
    """
    Сканирует коллекцию. Возвращает список владельцев по стране.
    """
    total = await get_collection_size(collection)
    if total == 0:
        return []

    # Детектор страны для подсчёта найденных (любая из 12 стран)
    _country_detector = COUNTRY_DETECTORS.get(country)

    def _is_match(result: dict) -> bool:
        if country == "cn":
            return bool(result.get("has_chinese"))
        if country == "ru":
            return bool(result.get("has_russian"))
        return result.get("detected_country") == country

    # Перед выдачей кэша обязательно подтверждаем Stars Rating.
    # Это также мигрирует старые записи, созданные до включения фильтра.
    if RATING_FILTER_ENABLED:
        await enrich_ratings_for_collection(collection, country, limit=max(1000, max_results * 5))

    # Кэшированные
    cached = await db.get_users_grouped(
        collection, country,
        model_filter, backdrop_filter, max_results,
    )
    if len(cached) >= max_results:
        if progress_callback:
            await progress_callback(total, total, len(cached))
        return cached[:max_results]

    # Что ещё не отсканировано
    scanned_nums = await db.get_scanned_items(collection)
    to_scan = [i for i in range(1, total + 1) if i not in scanned_nums]

    if max_scan_items > 0 and len(to_scan) > max_scan_items:
        to_scan = to_scan[:max_scan_items]

    if not to_scan:
        if progress_callback:
            await progress_callback(total, total, len(cached))
        return cached[:max_results]

    logger.info(
        "Коллекция %s: всего %d, в кэше %d, осталось %d",
        collection, total, len(scanned_nums), len(to_scan),
    )

    # ── Скан ──
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    processed = len(scanned_nums)
    batch_buffer: list[dict] = []
    found_count = len(cached)
    # Ранняя остановка: если нашли достаточно — стоп
    enough = max_results * 3  # запас на фильтрацию

    async def _scan_one(session, num):
        nonlocal processed, found_count
        async with semaphore:
            if stop_event and stop_event.is_set():
                return
            result = await _fetch_one(session, collection, num)
            if result:
                batch_buffer.append(result)
                if _is_match(result):
                    found_count += 1
            processed += 1

    connector = aiohttp.TCPConnector(
        limit=MAX_CONCURRENT_REQUESTS, ttl_dns_cache=300,
        force_close=False, enable_cleanup_closed=True,
        keepalive_timeout=30,
    )
    async with aiohttp.ClientSession(
        headers={"User-Agent": "Mozilla/5.0"}, connector=connector,
    ) as session:
        batch_size = MAX_CONCURRENT_REQUESTS * 4
        for batch_start in range(0, len(to_scan), batch_size):
            if stop_event and stop_event.is_set():
                break
            # Ранняя остановка
            if found_count >= enough:
                break
            batch = to_scan[batch_start:batch_start + batch_size]
            await asyncio.gather(*[_scan_one(session, n) for n in batch])

            if batch_buffer:
                await db.save_nft_items_batch(batch_buffer)
                batch_buffer.clear()

            if progress_callback:
                await progress_callback(processed, total, found_count)

            if DELAY_BETWEEN_BATCHES > 0:
                await asyncio.sleep(DELAY_BETWEEN_BATCHES)

    if batch_buffer:
        await db.save_nft_items_batch(batch_buffer)

    # Bio-обогащение: ru — по флагу, остальные страны — по detected_country
    if country == "ru":
        await enrich_russian_from_bios(collection)
    await enrich_country_from_bios(collection)

    # Строгая проверка Stars Rating: только L0..L2 (или заданный максимум).
    if RATING_FILTER_ENABLED:
        await enrich_ratings_for_collection(collection, country, limit=max(1000, max_results * 5))

    # Финальная выборка из БД
    results = await db.get_users_grouped(
        collection, country, model_filter, backdrop_filter, max_results,
    )

    if progress_callback:
        await progress_callback(processed, total, len(results))

    return results[:max_results]


# ── Рандомный скан ────────────────────────────

async def scan_random(
    collections: list[str],
    country: str = "cn",
    progress_callback: Optional[Callable] = None,
    stop_event: Optional[asyncio.Event] = None,
) -> tuple[list[dict], list[str]]:
    """
    Рандомный парсинг: выбирает случайные коллекции и элементы.
    Возвращает (results, scanned_collections).
    """
    chosen = random.sample(
        collections,
        min(RANDOM_COLLECTIONS_COUNT, len(collections)),
    )

    scanned_colls = []
    total_processed = 0
    total_to_scan = 0

    for coll in chosen:
        if stop_event and stop_event.is_set():
            break

        size = await get_collection_size(coll)
        if size == 0:
            continue
        scanned_colls.append(coll)

        scanned_nums = await db.get_scanned_items(coll)
        unscanned = [i for i in range(1, size + 1) if i not in scanned_nums]

        # Берём рандомные элементы
        count = min(RANDOM_ITEMS_PER_COLLECTION, len(unscanned))
        if count == 0:
            continue
        to_scan = random.sample(unscanned, count)
        total_to_scan += count

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        batch_buffer: list[dict] = []

        async def _scan_one(session, num, c=coll):
            nonlocal total_processed
            async with semaphore:
                if stop_event and stop_event.is_set():
                    return
                result = await _fetch_one(session, c, num)
                if result:
                    batch_buffer.append(result)
                total_processed += 1

        connector = aiohttp.TCPConnector(
            limit=MAX_CONCURRENT_REQUESTS, ttl_dns_cache=300,
            force_close=False, enable_cleanup_closed=True,
        )
        async with aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0"}, connector=connector,
        ) as session:
            batch_size = MAX_CONCURRENT_REQUESTS * 3
            for i in range(0, len(to_scan), batch_size):
                if stop_event and stop_event.is_set():
                    break
                batch = to_scan[i:i + batch_size]
                await asyncio.gather(*[_scan_one(session, n) for n in batch])

                if batch_buffer:
                    await db.save_nft_items_batch(batch_buffer)
                    batch_buffer.clear()

                if progress_callback:
                    await progress_callback(total_processed, total_to_scan, coll)

                if DELAY_BETWEEN_BATCHES > 0:
                    await asyncio.sleep(DELAY_BETWEEN_BATCHES)

        if batch_buffer:
            await db.save_nft_items_batch(batch_buffer)

        # Bio-обогащение для русских + по всем странам (точность)
        if country == "ru":
            await enrich_russian_from_bios(coll)
        await enrich_country_from_bios(coll)

    # Подтверждаем рейтинг всех кандидатов из выбранных коллекций до выдачи.
    if RATING_FILTER_ENABLED:
        for _coll in scanned_colls:
            await enrich_ratings_for_collection(_coll, country, limit=1000)

    # Собираем результаты из отсканированных коллекций
    results = await db.get_users_random_multi(scanned_colls, country, limit=200)
    return results, scanned_colls
