"""
AI-определение национальности по имени через нейросеть nationalize.io.

nationalize.io — ML-модель (та же команда, что genderize.io), предсказывает
страну по имени и возвращает вероятности. Используется как «нейронка» для
ТОЧНОГО определения национальности в поиске по странам:

  Словарь/скрипт (быстро, бесплатно, безлимит) → ловит явные случаи
  (кириллица, CJK, спец-буквы, гео-ключевики).
  nationalize.io (нейросеть) → добирает латинские/неоднозначные имена,
  которые словарь пропустил. Результат кэшируется в памяти + БД.

Принцип консервативный: берём страну только если вероятность достаточна.
"""
import asyncio
import logging
from typing import Optional

import aiohttp

from chinese_detector import detect_country, is_bot_or_shop

logger = logging.getLogger(__name__)

# ── Маппинг ISO country_id (alpha-2) → наши 12 кодов ──
# Арабские страны схлопываем в "ar".
_ARAB = {
    "SA", "AE", "EG", "IQ", "SY", "JO", "LB", "KW", "QA", "OM", "YE",
    "MA", "DZ", "TN", "LY", "SD", "BH", "PS", "MR",
}
_ISO_TO_CODE = {
    "CN": "cn", "TW": "cn", "HK": "cn",
    "RU": "ru", "BY": "ru",
    "JP": "jp",
    "KR": "kr", "KP": "kr",
    "IN": "in",
    "ID": "id",
    "UZ": "uz",
    "KZ": "kz",
    "KG": "kg",
    "TJ": "tj",
    "TR": "tr",
}
for _c in _ARAB:
    _ISO_TO_CODE[_c] = "ar"

# Минимальная вероятность, чтобы доверять предсказанию нейросети
MIN_PROBABILITY = 0.28

# Кэш «имя → код страны» на время жизни процесса (экономит лимит API)
_CACHE: dict[str, Optional[str]] = {}


class NationalizeAPI:
    """nationalize.io — нейросеть, предсказывает страну по имени."""
    URL = "https://api.nationalize.io"
    daily_limit = 1000
    used = 0
    exhausted = False
    _last_reset_day = 0

    @classmethod
    def _auto_reset(cls):
        import time as _time
        today = int(_time.time()) // 86400
        if cls._last_reset_day != today:
            cls._last_reset_day = today
            cls.used = 0
            cls.exhausted = False

    @classmethod
    async def check(cls, name: str, session: aiohttp.ClientSession) -> Optional[str]:
        """Возвращает код страны (наш формат) или None."""
        cls._auto_reset()
        if cls.exhausted or cls.used >= cls.daily_limit:
            cls.exhausted = True
            return None
        if not name:
            return None
        try:
            async with session.get(
                cls.URL,
                params={"name": name},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                cls.used += 1
                if resp.status == 429:
                    cls.exhausted = True
                    return None
                if resp.status != 200:
                    return None
                data = await resp.json()
                countries = data.get("country") or []
                if not countries:
                    return None
                top = countries[0]
                prob = top.get("probability", 0)
                iso = (top.get("country_id") or "").upper()
                if prob >= MIN_PROBABILITY and iso in _ISO_TO_CODE:
                    return _ISO_TO_CODE[iso]
                return None
        except Exception as e:
            logger.debug("nationalize.io error: %s", e)
            return None


def reset_api_counters():
    NationalizeAPI._auto_reset()


def _extract_name(item: dict) -> str:
    return item.get("owner") or item.get("display_name") or ""


def _has_latin(text: str) -> bool:
    return any("a" <= ch.lower() <= "z" for ch in text)


async def ai_detect_country(name: str, session: aiohttp.ClientSession) -> Optional[str]:
    """
    Точное определение страны: сначала словарь/скрипт/гео (бесплатно),
    затем нейросеть nationalize.io (для латинских/неоднозначных имён).
    """
    # 1) Быстрый детектор (кириллица, CJK, спец-буквы, гео-ключевики)
    c = detect_country(name)
    if c:
        return c
    # 2) Нейросеть — только для имён с латиницей (где словарь бессилен)
    if not _has_latin(name):
        return None
    if name in _CACHE:
        return _CACHE[name]
    res = await NationalizeAPI.check(name, session)
    _CACHE[name] = res
    return res


async def ai_refine_country(
    items: list[dict],
    target: str,
    already_keys: set[str],
    stop_event: asyncio.Event | None = None,
    budget: int = 120,
    max_concurrent: int = 10,
) -> list[dict]:
    """
    Догоняет владельцев нужной страны, которых словарь пропустил.
    Прогоняет через нейросеть кандидатов с латинским именем (НЕ из already_keys),
    возвращает тех, кого нейросеть отнесла к target. Ограничено budget вызовов.
    """
    seen_names: set[str] = set()
    candidates: list[dict] = []
    for it in items:
        name = _extract_name(it)
        uname = it.get("username", "")
        if not name or not uname:
            continue
        key = uname or name
        if key in already_keys:
            continue
        if not _has_latin(name):
            continue  # кириллицу/CJK словарь уже разобрал
        if is_bot_or_shop(uname, name):
            continue
        if name in seen_names:
            continue
        seen_names.add(name)
        candidates.append(it)

    if not candidates:
        return []

    candidates = candidates[:budget]
    matched: list[dict] = []
    semaphore = asyncio.Semaphore(max_concurrent)

    async def _one(session, it):
        if stop_event and stop_event.is_set():
            return
        async with semaphore:
            name = _extract_name(it)
            code = await ai_detect_country(name, session)
            if code == target:
                matched.append(it)

    connector = aiohttp.TCPConnector(limit=max_concurrent, ttl_dns_cache=300)
    async with aiohttp.ClientSession(
        headers={"User-Agent": "Mozilla/5.0"}, connector=connector,
    ) as session:
        for i in range(0, len(candidates), max_concurrent * 2):
            if stop_event and stop_event.is_set():
                break
            if NationalizeAPI.exhausted:
                break
            batch = candidates[i:i + max_concurrent * 2]
            await asyncio.gather(*[_one(session, c) for c in batch])

    return matched
