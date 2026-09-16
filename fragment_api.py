"""
Fragment.com Scraper — поиск +888 номеров и NFT юзернеймов.

Скрапит fragment.com для:
  - Виртуальных номеров +888 (листинг + страницы номеров)
  - NFT юзернеймов (отдельные страницы юзернеймов)
"""
import asyncio
import logging
import re
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

FRAGMENT_BASE = "https://fragment.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

# Лимиты
MAX_NUMBERS_PER_SCAN = 300
MAX_CONCURRENT = 20
MAX_USERNAMES_PER_SCAN = 200


# ── +888 Номера ──────────────────────────────


async def fetch_numbers_list(
    filter_type: str = "sold",
    session: aiohttp.ClientSession | None = None,
) -> list[dict]:
    """
    Скрапит список +888 номеров с fragment.com/numbers.
    filter_type: 'sold' | 'sale' | 'auction'
    Возвращает: [{'number': '88800001312', 'display': '+888 0000 1312', 'price': '123,456'}, ...]
    """
    close = False
    if session is None:
        session = aiohttp.ClientSession(headers=HEADERS)
        close = True

    try:
        url = f"{FRAGMENT_BASE}/numbers"
        params = {"filter": filter_type, "sort": "price"}
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                logger.error("Fragment numbers list error: %d", resp.status)
                return []
            html = await resp.text()

        # Парсим таблицу
        rows = re.findall(
            r'<tr[^>]*class="[^"]*tm-row[^"]*"[^>]*>.*?</tr>',
            html, re.DOTALL,
        )

        results = []
        for row in rows:
            # Номер
            num_match = re.findall(r'href="/number/(\d+)"', row)
            if not num_match:
                continue
            number = num_match[0]

            # Отображаемый номер
            display_match = re.findall(r'tm-value">([^<]+)<', row)
            display = display_match[0].strip() if display_match else f"+{number}"

            # Цена (TON)
            price_match = re.findall(r'icon-ton">([^<]+)<', row)
            price = price_match[0].strip() if price_match else ""

            results.append({
                "number": number,
                "display": display,
                "price": price,
            })

        return results[:MAX_NUMBERS_PER_SCAN]

    except Exception as e:
        logger.error("Fragment numbers list exception: %s", e)
        return []
    finally:
        if close:
            await session.close()


async def fetch_number_details(
    number: str,
    session: aiohttp.ClientSession | None = None,
) -> dict | None:
    """
    Скрапит страницу конкретного +888 номера.
    Возвращает: {
        'number': '88800001312',
        'display': '+888 0000 1312',
        'owner_wallet': 'EQ...',
        'owner_tme': 'username.t.me' (если есть),
        'owner_username': 'username' (извлечённый из .t.me),
        'price': '123,456',
    }
    """
    close = False
    if session is None:
        session = aiohttp.ClientSession(headers=HEADERS)
        close = True

    try:
        url = f"{FRAGMENT_BASE}/number/{number}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return None
            html = await resp.text()

        result: dict = {"number": number}

        # Отображаемый номер
        display_match = re.findall(r'class="tm-section-header-domain">\s*(.+?)\s*<', html, re.DOTALL)
        if display_match:
            d = re.sub(r'<[^>]+>', '', display_match[0]).strip()
            result["display"] = d
        else:
            result["display"] = f"+{number}"

        # Owner секция — первый кошелёк = текущий владелец
        owner_sec = re.findall(r'Owner.*?(?:Ownership|Latest|$)', html, re.DOTALL)
        if owner_sec:
            # Кошелёк владельца
            wallets = re.findall(r'tonviewer\.com/([^"]+)', owner_sec[0])
            if wallets:
                result["owner_wallet"] = wallets[0]

            # .t.me имя (TG username в формате кошелька)
            tme_names = re.findall(r'class="short">([^<]+\.t\.me)', owner_sec[0])
            if tme_names:
                result["owner_tme"] = tme_names[0]
                # Извлекаем username: "niga.t.me" → "niga"
                result["owner_username"] = tme_names[0].replace(".t.me", "")

        # Также ищем в истории (Ownership History) — последний покупатель
        hist = re.findall(r'Ownership History.*?</table>', html, re.DOTALL)
        if hist:
            hist_wallets = re.findall(r'tonviewer\.com/([^"]+)', hist[0])
            hist_tme = re.findall(r'class="short">([^<]+\.t\.me)', hist[0])

            if not result.get("owner_wallet") and hist_wallets:
                result["owner_wallet"] = hist_wallets[0]
            if not result.get("owner_tme") and hist_tme:
                result["owner_tme"] = hist_tme[0]
                result["owner_username"] = hist_tme[0].replace(".t.me", "")

            # Сохраняем всю историю
            history = []
            # Парсим пары: wallet + дата
            all_hist_wallets = hist_wallets
            all_hist_tme = hist_tme
            for w in all_hist_wallets[:5]:
                entry = {"wallet": w}
                # Если есть .t.me формат для этого кошелька
                for t in all_hist_tme:
                    if t not in [h.get("tme") for h in history]:
                        entry["tme"] = t
                        entry["username"] = t.replace(".t.me", "")
                        break
                history.append(entry)
            if history:
                result["history"] = history

        # Цена
        price_match = re.findall(r'Sale price.*?icon-ton[^>]*>([^<]+)<', html, re.DOTALL)
        if price_match:
            result["price"] = price_match[0].strip()

        return result

    except Exception as e:
        logger.error("Fragment number detail exception for %s: %s", number, e)
        return None
    finally:
        if close:
            await session.close()


async def scan_numbers(
    filter_type: str = "sold",
    max_details: int = 50,
    stop_event: asyncio.Event | None = None,
    progress_callback=None,
) -> list[dict]:
    """
    Скрапит номера +888 с деталями.
    1. Получаем список номеров
    2. Параллельно грузим детали каждого
    3. Возвращаем только номера с владельцем (wallet или tme)
    """
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        # 1. Список
        numbers = await fetch_numbers_list(filter_type, session)
        if not numbers:
            return []

        # Берём только max_details
        to_scan = numbers[:max_details]

        if progress_callback:
            await progress_callback(0, len(to_scan), "Загружаю детали номеров…")

        # 2. Параллельно грузим детали (с семафором)
        sem = asyncio.Semaphore(MAX_CONCURRENT)
        results = []
        done_count = [0]

        async def _fetch_one(num_info: dict):
            if stop_event and stop_event.is_set():
                return
            async with sem:
                detail = await fetch_number_details(num_info["number"], session)
                done_count[0] += 1
                if detail:
                    if not detail.get("price"):
                        detail["price"] = num_info.get("price", "")
                    results.append(detail)
                if progress_callback and done_count[0] % 10 == 0:
                    await progress_callback(done_count[0], len(to_scan), "")

        tasks = [_fetch_one(n) for n in to_scan]
        await asyncio.gather(*tasks)

        # 3. Фильтруем — только с владельцем
        with_owner = [
            r for r in results
            if r.get("owner_wallet") or r.get("owner_tme")
        ]

        return with_owner


# ── NFT Юзернеймы ───────────────────────────


async def fetch_username_details(
    username: str,
    session: aiohttp.ClientSession | None = None,
) -> dict | None:
    """
    Скрапит страницу NFT юзернейма на Fragment.
    Возвращает: {
        'username': 'crypto',
        'owner_wallet': 'EQ...',
        'owner_tme': 'dope.ton',
        'price': '153,000',
        'status': 'Taken',
    }
    """
    close = False
    if session is None:
        session = aiohttp.ClientSession(headers=HEADERS)
        close = True

    try:
        url = f"{FRAGMENT_BASE}/username/{username}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return None
            html = await resp.text()
            if len(html) < 100:
                return None

        result: dict = {"username": username}

        # Статус
        status_match = re.findall(r'tm-status[^"]*"[^>]*>([^<]+)<', html)
        if status_match:
            result["status"] = status_match[0].strip()

        # Owner секция
        owner_sec = re.findall(r'Owner.*?(?:Ownership|Latest|Subscribe|$)', html, re.DOTALL)
        if owner_sec:
            wallets = re.findall(r'tonviewer\.com/([^"]+)', owner_sec[0])
            if wallets:
                result["owner_wallet"] = wallets[0]

            tme = re.findall(r'class="short">([^<]+)', owner_sec[0])
            # Фильтруем даты (Mar 17, 2024 etc.)
            for t in tme:
                if ".ton" in t or ".t.me" in t or not any(c.isdigit() for c in t):
                    if "," not in t and "at" not in t:
                        result["owner_tme"] = t
                        break

        # Цена из Ownership History
        price_match = re.findall(r'icon-ton[^>]*>([^<]+)<', html)
        if price_match:
            result["price"] = price_match[0].strip()

        # Всего кошельков на странице (для проверки что страница рабочая)
        all_wallets = re.findall(r'tonviewer\.com/([^"]+)', html)
        if all_wallets and not result.get("owner_wallet"):
            result["owner_wallet"] = all_wallets[0]

        return result

    except Exception as e:
        logger.error("Fragment username detail exception for %s: %s", username, e)
        return None
    finally:
        if close:
            await session.close()


async def scan_usernames_batch(
    usernames: list[str],
    stop_event: asyncio.Event | None = None,
    progress_callback=None,
) -> list[dict]:
    """
    Проверяет список юзернеймов на Fragment.
    Возвращает те, у которых есть NFT (страница существует + есть владелец).
    """
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        sem = asyncio.Semaphore(MAX_CONCURRENT)
        results = []
        done_count = [0]

        async def _fetch_one(uname: str):
            if stop_event and stop_event.is_set():
                return
            async with sem:
                detail = await fetch_username_details(uname, session)
                done_count[0] += 1
                if detail and detail.get("owner_wallet"):
                    results.append(detail)
                if progress_callback and done_count[0] % 10 == 0:
                    await progress_callback(done_count[0], len(usernames), "")

        tasks = [_fetch_one(u) for u in usernames[:MAX_USERNAMES_PER_SCAN]]
        await asyncio.gather(*tasks)

        return results


# ── Утилиты ──────────────────────────────────

def format_wallet_short(wallet: str) -> str:
    """Сокращает адрес кошелька: 'EQA9jI...F5dnO' → 'EQA9…5dnO'."""
    if not wallet:
        return ""
    if ".t.me" in wallet or ".ton" in wallet:
        return wallet  # Это уже имя, не адрес
    if len(wallet) <= 12:
        return wallet
    return f"{wallet[:6]}…{wallet[-4:]}"


def extract_username_from_wallet(wallet: str) -> str | None:
    """Из формата 'username.t.me' или 'name.ton' извлекает username."""
    if not wallet:
        return None
    if wallet.endswith(".t.me"):
        return wallet[:-5]
    return None
