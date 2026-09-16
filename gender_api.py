"""
Определение пола по имени — улучшенная AI-эвристика + API fallback.

Приоритет:
1. Проверка username на бот/канал/мусор (отсев)
2. Проверка display_name на бот/канал/мусор (отсев)
3. Мужские/женские имена из расширенных словарей
4. Эвристика по окончаниям (кириллица)
5. genderize.io API (fallback, 1000/день)

Принцип: консервативный — если не уверены что женщина, пропускаем.
"""
import asyncio
import logging
import re
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


# ── Фильтр каналов/ботов/мусора по username ──

_CHANNEL_USERNAME_SUFFIXES = (
    "bot", "_bot", "_nft", "_ton", "_news", "_shop", "_store",
    "_market", "_crypto", "_trade", "_finance", "_transfer",
    "_official", "_channel", "_group", "_chat", "_pro",
    "_team", "_dao", "_labs", "_studio", "_media",
    "_agency", "_service", "_support", "_help",
)

_CHANNEL_USERNAME_PREFIXES = (
    "shop_", "store_", "market_", "news_", "crypto_",
    "nft_", "ton_", "tg_", "the_", "official_",
    "museum_", "gallery_", "club_", "team_",
)

_CHANNEL_USERNAME_CONTAINS = (
    "relay", "swap", "exchange", "bridge", "vault",
    "airdrop", "giveaway", "claim", "faucet",
    "casino", "betting", "lottery", "jackpot",
    "mining", "staking", "defi", "yield",
    "premium", "promo", "advert",
    "museum", "gallery", "studio",
)


def _is_channel_username(username: str) -> bool:
    """Проверка username на паттерны каналов/ботов."""
    u = (username or "").lower().strip().lstrip("@")
    if not u:
        return False

    # Суффиксы
    for s in _CHANNEL_USERNAME_SUFFIXES:
        if u.endswith(s):
            return True

    # Префиксы
    for p in _CHANNEL_USERNAME_PREFIXES:
        if u.startswith(p):
            return True

    # Содержит
    for c in _CHANNEL_USERNAME_CONTAINS:
        if c in u:
            return True

    # Только цифры или слишком короткий
    clean = re.sub(r'[_\d]', '', u)
    if len(clean) < 2:
        return True

    return False


# ── Эвристика по display_name ────────────────

_FEMALE_ENDINGS_RU = ("а", "я", "ия", "ья")
_MALE_ENDINGS_RU = ("й", "н", "р", "в", "д", "г", "к", "с", "м")

# Мужские имена — расширенный список
_MALE_NAMES_RU = {
    # Полные
    "александр", "алексей", "андрей", "антон", "артём", "артем",
    "борис", "вадим", "валентин", "валерий", "василий", "виктор",
    "виталий", "владимир", "владислав", "вячеслав", "геннадий",
    "георгий", "григорий", "даниил", "данил", "денис", "дмитрий",
    "евгений", "егор", "иван", "игорь", "илья", "кирилл",
    "константин", "леонид", "максим", "матвей", "михаил", "никита",
    "николай", "олег", "павел", "пётр", "петр", "роман",
    "руслан", "сергей", "станислав", "степан", "тимофей",
    "тимур", "фёдор", "федор", "филипп", "эдуард", "юрий", "ярослав",
    "марк", "лев", "арсений", "глеб", "семён", "семен", "захар",
    "артур", "давид", "адам", "платон", "мирон", "савелий",
    "аркадий", "анатолий", "богдан", "вениамин", "всеволод",
    "герман", "демьян", "ефим", "зиновий", "иннокентий",
    "клим", "лаврентий", "макар", "мстислав", "назар",
    "олесь", "прохор", "ростислав", "святослав", "трофим",
    "устин", "харитон", "эмиль", "ян",
    "ислам", "рамазан", "рамзан", "магомед", "мурат", "ахмед",
    "рустам", "ринат", "ренат", "рафаэль", "равиль", "ильдар",
    "ильнар", "наиль", "нурлан", "азамат", "айрат", "булат",
    # Сокращённые
    "миша", "дима", "коля", "саша", "лёша", "леша", "алёша", "алеша",
    "ваня", "серёжа", "сережа", "петя", "вова", "витя",
    "толя", "стёпа", "степа", "гриша", "паша", "костя",
    "гена", "жора", "федя", "лёня", "леня", "вася",
    "тима", "кеша", "боря", "слава", "тёма", "тема",
    "рома", "сеня", "гоша", "яша", "юра", "митя",
    "кирюша", "данила", "данька", "илюша", "никиша",
    "женя", "валера", "эдик", "игорёша",
    # Латинские транслит
    "misha", "dima", "kolya", "sasha", "vanya", "petya",
    "pasha", "kostya", "roma", "tema", "gosha", "grisha",
    "artem", "dmitry", "dmitri", "sergey", "sergei",
    "nikolay", "nikolai", "andrey", "andrei", "maxim",
    "ruslan", "timur", "oleg", "igor", "kirill",
    "vlad", "vladislav", "vladimir", "bogdan", "vadim",
}

_MALE_NAMES_EN = {
    "james", "john", "robert", "michael", "david", "william",
    "richard", "joseph", "thomas", "charles", "christopher",
    "daniel", "matthew", "anthony", "mark", "donald", "steven",
    "paul", "andrew", "joshua", "kenneth", "kevin", "brian",
    "george", "timothy", "ronald", "edward", "jason", "jeffrey",
    "ryan", "jacob", "gary", "nicholas", "eric", "jonathan",
    "stephen", "larry", "justin", "scott", "brandon", "benjamin",
    "samuel", "raymond", "gregory", "frank", "alexander", "patrick",
    "jack", "dennis", "jerry", "tyler", "aaron", "henry",
    "peter", "adam", "nathan", "douglas", "zachary", "kyle",
    "noah", "ethan", "jeremy", "walter", "christian", "keith",
    "roger", "terry", "austin", "sean", "gerald", "carl",
    "dylan", "jesse", "jordan", "bryan", "billy", "joe",
    "bruce", "gabriel", "logan", "albert", "willie", "alan",
    "eugene", "russell", "bobby", "vincent", "philip", "harry",
    "ralph", "roy", "randy", "johnny", "howard",
    "carlos", "alex", "max", "leo", "ivan", "igor",
    "sergei", "dmitri", "nikolai", "andrei", "viktor", "vlad",
    "mike", "tom", "bob", "bill", "jim", "dan", "ben", "sam",
    "chris", "matt", "nick", "tony", "dave", "steve", "jeff",
    "greg", "ted", "ed", "rob", "rick", "ray", "don",
    "ahmed", "mohamed", "ali", "omar", "hassan", "hussein",
    "muhammad", "mustafa", "khalid", "tariq", "hamid",
}

_FEMALE_NAMES_RU = {
    "аня", "анна", "даша", "дарья", "маша", "мария",
    "настя", "анастасия", "катя", "екатерина", "оля", "ольга",
    "юля", "юлия", "лена", "елена", "алина", "алёна", "алена",
    "вика", "виктория", "ира", "ирина", "наташа", "наталья",
    "таня", "татьяна", "света", "светлана", "оксана", "ксения",
    "полина", "валерия", "кристина", "софья", "софия", "диана",
    "марина", "галина", "нина", "людмила", "вера", "надежда",
    "лиза", "елизавета", "женя", "евгения", "тоня", "антонина",
    "зоя", "инна", "рита", "лара", "лариса", "роза", "рузиля",
    "карина", "камилла", "милана", "варвара", "ева", "алиса",
    "ника", "арина", "ульяна", "яна", "злата", "эмилия",
    "ангелина", "регина", "дина", "альбина", "фатима", "лилия",
    "амина", "аида", "зарина", "сабина", "руслана", "снежана",
    "олеся", "ксюша", "надя", "люба", "люда", "тома", "зина",
    "нелли", "алла", "нина", "рая", "рая", "валя", "шура",
    "лида", "клава", "тоня", "нюра", "зоя",
    "милена", "виолетта", "маргарита", "вероника", "василиса",
    "мила", "есения", "стефания", "анжела", "анжелика",
    "лейла", "айгуль", "гульнара", "динара", "эльвира",
    "венера", "азиза", "замира", "мадина", "патимат",
}

_FEMALE_NAMES_EN = {
    "anna", "mary", "maria", "emma", "sophia", "olivia", "ava",
    "isabella", "mia", "charlotte", "amelia", "harper", "evelyn",
    "abigail", "emily", "elizabeth", "sofia", "ella", "madison",
    "scarlett", "victoria", "aria", "grace", "chloe", "lily",
    "natasha", "natalia", "elena", "diana", "lena", "irina",
    "katya", "kate", "dasha", "masha", "julia", "alina",
    "kristina", "karina", "marina", "nina", "vera", "alice",
    "jessica", "jennifer", "sarah", "lisa", "ashley",
    "nicole", "amanda", "stephanie", "michelle",
    "linda", "donna", "pamela", "sandra", "helen", "amy",
    "angela", "brenda", "cheryl", "nancy", "betty", "carol",
    "diana", "eva", "hannah", "kelly", "laura", "margaret",
    "rachel", "rebecca", "samantha", "susan", "tiffany",
    "vanessa", "wendy", "zoe", "sophie",
    "anastasia", "daria", "ekaterina", "olga", "tatiana",
    "svetlana", "polina", "ksenia", "valeria",
}

# Женские иероглифические символы (CJK)
_FEMALE_CJK_CHARS = set("美丽花莲芳婷婉娜娟玲珍珠琳瑶蓉蕾薇馨静雪雅露颖玉兰芬萍蝶媛惠敏慧")


def _extract_first_name(display_name: str) -> str:
    """Извлекает первое слово (имя) из display name."""
    clean = re.sub(r'[^\w\s]', ' ', display_name, flags=re.UNICODE)
    clean = clean.strip()
    parts = clean.split()
    if parts:
        return parts[0].lower()
    return ""


def _extract_all_words(display_name: str) -> list[str]:
    """Извлекает все слова для проверки."""
    clean = re.sub(r'[^\w\s]', ' ', display_name, flags=re.UNICODE)
    return [w.lower() for w in clean.split() if len(w) >= 2]


def _heuristic_check(display_name: str, username: str = "") -> Optional[str]:
    """
    Эвристика определения пола.
    Проверяет display_name + username.
    Возвращает: 'female' | 'male' | None (неизвестно)
    """
    if not display_name:
        return None

    first = _extract_first_name(display_name)
    all_words = _extract_all_words(display_name)

    # Также проверяем username (может содержать имя: @dima_crypto → dima)
    uname_parts = re.sub(r'[_\d]', ' ', (username or "").lower()).split()
    all_check = set(all_words + uname_parts)

    if not first and not all_check:
        return None

    # 1. Проверяем мужские списки ПЕРВЫМИ (Миша, Дима, Коля → male)
    for word in all_check:
        if word in _MALE_NAMES_RU or word in _MALE_NAMES_EN:
            return "male"

    # 2. Проверяем женские списки
    for word in all_check:
        if word in _FEMALE_NAMES_RU or word in _FEMALE_NAMES_EN:
            return "female"

    # 3. CJK: проверяем наличие "женских" иероглифов
    cjk_female = sum(1 for ch in display_name if ch in _FEMALE_CJK_CHARS)
    if cjk_female >= 1:
        return "female"

    # 4. Русские окончания (после проверки списков — Миша уже отловлен)
    if first:
        has_cyrillic = any('\u0400' <= ch <= '\u04FF' for ch in first)
        if has_cyrillic and len(first) > 2:
            if first.endswith(_FEMALE_ENDINGS_RU):
                return "female"
            if first.endswith(_MALE_ENDINGS_RU):
                return "male"

    return None


# ── API клиенты ──────────────────────────────

class GenderizeAPI:
    """genderize.io — 1000 запросов/день бесплатно."""
    URL = "https://api.genderize.io"
    daily_limit = 1000
    used = 0
    exhausted = False
    _last_reset_day = 0
    # Кэш «имя → пол» на время жизни процесса — экономит лимит и ускоряет
    # (одни и те же имена встречаются в выдаче многократно).
    _cache: dict[str, Optional[str]] = {}

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
        cls._auto_reset()
        key = (name or "").strip().lower()
        if key in cls._cache:
            return cls._cache[key]
        if cls.exhausted or cls.used >= cls.daily_limit:
            cls.exhausted = True
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
                gender = data.get("gender")
                prob = data.get("probability", 0)
                result = gender if (gender and prob >= 0.7) else None
                cls._cache[key] = result
                return result
        except Exception as e:
            logger.debug("genderize.io error: %s", e)
            return None


# ── Основная функция ─────────────────────────

def _looks_like_channel(display_name: str, username: str = "") -> bool:
    """Эвристика: имя/username похоже на канал/бота, а не на человека."""
    d = display_name.strip()
    u = (username or "").lower().strip()

    if not d or len(d) <= 1:
        return True

    # Кошельки
    if re.match(r'^(UQ|EQ|0:)[A-Za-z0-9_\-]{20,}', d):
        return True

    # .ton / .t.me домены
    if re.match(r'^[a-z0-9\-]+\.(ton|t\.me)$', d, re.I):
        return True

    # Спам-каналы: длинные имена с |, +, $
    if len(d) > 30 and ('|' in d or '+' in d or '$' in d):
        return True

    # Нет букв вообще (только эмодзи, цифры, символы)
    clean = re.sub(r'[^a-zA-Zа-яА-ЯёЁ\u4E00-\u9FFF]', '', d)
    if len(clean) < 2:
        return True

    # Username-based channel detection
    if u and _is_channel_username(u):
        return True

    # Display name содержит канальные/рекламные слова
    d_lower = d.lower()
    channel_words = [
        "channel", "канал", "news", "новости", "shop", "магазин",
        "store", "market", "маркет", "crypto", "крипто", "nft",
        "transfer", "трансфер", "official", "bot", "бот",
        "museum", "музей", "gallery", "галерея", "studio",
        "обмен", "exchange", "casino", "казино", "club", "клуб",
        "premium", "premium", "airdrop", "mining",
        "invest", "инвест", "trading", "трейдинг",
    ]
    for cw in channel_words:
        if cw in d_lower:
            return True

    # Если display_name выглядит как организация (все слова с заглавных, > 3 слов)
    words = d.split()
    if len(words) >= 3 and all(w[0].isupper() for w in words if len(w) > 1 and w[0].isalpha()):
        # Но не если это "Анна Мария Ковалёва" (имена)
        first = words[0].lower()
        if first not in _FEMALE_NAMES_RU and first not in _FEMALE_NAMES_EN:
            if first not in _MALE_NAMES_RU and first not in _MALE_NAMES_EN:
                # Проверяем наличие НЕ-именных слов
                non_name_count = sum(1 for w in words
                                     if w.lower() not in _FEMALE_NAMES_RU
                                     and w.lower() not in _MALE_NAMES_RU
                                     and w.lower() not in _FEMALE_NAMES_EN
                                     and w.lower() not in _MALE_NAMES_EN)
                if non_name_count >= 2:
                    return True

    return False


async def is_female(display_name: str, session: aiohttp.ClientSession | None = None, username: str = "") -> bool:
    """
    Определяет, является ли владелец женщиной.
    Использует username + display_name → эвристику → API.
    Консервативный подход: если не уверены — False.
    """
    # Фильтр каналов / мусора
    if _looks_like_channel(display_name, username):
        return False

    first = _extract_first_name(display_name)
    if not first or len(first) < 2:
        return False

    # 1. Эвристика (быстрая, учитывает и username)
    h = _heuristic_check(display_name, username)
    if h == "female":
        return True
    if h == "male":
        return False

    # 2. API (если имя латинское или кириллическое)
    has_alpha = any(ch.isalpha() and ord(ch) < 0x4E00 for ch in first)
    if has_alpha and session:
        api_result = await GenderizeAPI.check(first, session)
        if api_result == "female":
            return True
        if api_result == "male":
            return False

    # Консервативно: если не уверены — не показываем
    return False


async def filter_female_owners(items: list[dict], max_concurrent: int = 20) -> list[dict]:
    """
    Фильтрует список items, оставляя только владелиц.
    items — список из peek_api с полями 'owner' (display_name), 'username'.
    Порядок сохраняется (важно для последующего перемешивания/выдачи).
    """
    semaphore = asyncio.Semaphore(max_concurrent)
    flags = [False] * len(items)

    async def _check(session, idx, item):
        async with semaphore:
            name = item.get("display_name") or item.get("owner", "")
            uname = item.get("username", "")
            try:
                flags[idx] = await is_female(name, session, username=uname)
            except Exception:
                flags[idx] = False

    connector = aiohttp.TCPConnector(limit=max_concurrent, ttl_dns_cache=300,
                                     keepalive_timeout=30, enable_cleanup_closed=True)
    async with aiohttp.ClientSession(connector=connector) as session:
        await asyncio.gather(*[_check(session, i, it) for i, it in enumerate(items)])

    return [it for it, ok in zip(items, flags) if ok]


def reset_api_counters():
    """Сброс счётчиков API."""
    GenderizeAPI.used = 0
    GenderizeAPI.exhausted = False
