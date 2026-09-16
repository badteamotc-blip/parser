"""
Детектор национальности по имени / bio.

Стратегия (по убыванию надёжности):
  1. Скрипт-уникальные системы письма (определяются на 100 %):
       cjk  → cn   (китайские иероглифы без каны/хангыля)
       kana → jp   (хирагана / катакана)
       hangul → kr (корейский хангыль)
       arabic → ar (арабское письмо)
       indic  → in (деванагари, бенгали, тамильский …)
  2. Спец-буквы языков:
       turkish letters → tr   (ğ ü ş ı ö ç)
       kazakh letters  → kz   (ә ғ қ ң ө ұ ү һ і)
  3. Словари имён (проверяются ВСЕ токены имени, латиница + кириллица):
       uz, kz, kg, tj, id, in (латиница), tr (латиница)
  4. Fallback: любая кириллица → ru (если не подошла Средняя Азия)

ВАЖНО: порядок проверки в detect_country критичен — узкие детекторы
(Средняя Азия по спец-буквам / именам) идут ДО общего русского, иначе
все кириллические имена попадают в «русские».
"""
import unicodedata
import re as _re

from names_extra import (
    KAZAKH_EXTRA, UZBEK_EXTRA, KYRGYZ_EXTRA, TAJIK_EXTRA,
    INDONESIAN_EXTRA, INDIAN_EXTRA, TURKISH_EXTRA,
)

# ── Unicode-диапазоны ────────────────────────

_CJK_RANGES = [
    (0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x20000, 0x2A6DF),
    (0x2A700, 0x2B73F), (0x2B740, 0x2B81F), (0xF900, 0xFAFF),
]
_HIRAGANA = (0x3040, 0x309F)
_KATAKANA = (0x30A0, 0x30FF)
_KATAKANA_EXT = (0x31F0, 0x31FF)
_HANGUL_RANGES = [(0xAC00, 0xD7AF), (0x1100, 0x11FF), (0x3130, 0x318F)]
_CYRILLIC_RANGES = [(0x0400, 0x04FF), (0x0500, 0x052F)]
_LATIN_RANGES = [(0x0041, 0x024F)]
_ARABIC_RANGES = [(0x0600, 0x06FF), (0x0750, 0x077F), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF)]
_DEVANAGARI = (0x0900, 0x097F)
_BENGALI = (0x0980, 0x09FF)
_TAMIL = (0x0B80, 0x0BFF)
_TELUGU = (0x0C00, 0x0C7F)
_KANNADA = (0x0C80, 0x0CFF)
_GURMUKHI = (0x0A00, 0x0A7F)
_GUJARATI = (0x0A80, 0x0AFF)
_MALAYALAM = (0x0D00, 0x0D7F)
_ORIYA = (0x0B00, 0x0B7F)


def _in(cp: int, ranges) -> bool:
    return any(lo <= cp <= hi for lo, hi in ranges)


def _count_script(text: str) -> dict:
    """Считает символы по скриптам (буквы, без эмодзи/пунктуации)."""
    c = {"cjk": 0, "kana": 0, "hangul": 0, "cyrillic": 0,
         "latin": 0, "arabic": 0, "indic": 0, "text": 0}
    for ch in text:
        cp = ord(ch)
        cat = unicodedata.category(ch)
        if cat[0] in ("S", "P", "Z", "C", "M", "N"):
            continue
        c["text"] += 1
        if _in(cp, _CJK_RANGES):
            c["cjk"] += 1
        elif (_HIRAGANA[0] <= cp <= _HIRAGANA[1]
              or _KATAKANA[0] <= cp <= _KATAKANA[1]
              or _KATAKANA_EXT[0] <= cp <= _KATAKANA_EXT[1]):
            c["kana"] += 1
        elif _in(cp, _HANGUL_RANGES):
            c["hangul"] += 1
        elif _in(cp, _CYRILLIC_RANGES):
            c["cyrillic"] += 1
        elif _in(cp, _LATIN_RANGES):
            c["latin"] += 1
        elif _in(cp, _ARABIC_RANGES):
            c["arabic"] += 1
        elif any((_DEVANAGARI[0] <= cp <= _DEVANAGARI[1],
                  _BENGALI[0] <= cp <= _BENGALI[1],
                  _TAMIL[0] <= cp <= _TAMIL[1],
                  _TELUGU[0] <= cp <= _TELUGU[1],
                  _KANNADA[0] <= cp <= _KANNADA[1],
                  _GURMUKHI[0] <= cp <= _GURMUKHI[1],
                  _GUJARATI[0] <= cp <= _GUJARATI[1],
                  _MALAYALAM[0] <= cp <= _MALAYALAM[1],
                  _ORIYA[0] <= cp <= _ORIYA[1])):
            c["indic"] += 1
    return c


def _has_cyrillic(text: str) -> bool:
    return any(_in(ord(ch), _CYRILLIC_RANGES) for ch in (text or ""))


def _tokens(name: str) -> list[str]:
    """Все слова имени (lower), латиница + кириллица. 'Ali | Trader' → ['ali','trader']."""
    clean = _re.sub(r"[^\w\s]", " ", name or "", flags=_re.UNICODE)
    return [t.lower() for t in clean.split() if t]


def _match_any(name: str, nameset: set) -> bool:
    """True если ЛЮБОЙ токен имени есть в словаре."""
    return any(t in nameset for t in _tokens(name))


# ── Спец-буквы языков (как иероглифы — определяют страну по алфавиту) ──
#
# Принцип: у каждого языка Средней Азии есть СВОИ буквы, которых нет в
# русском. Делим на «сильные» (уникальны для ОДНОГО языка → 100% точность)
# и «общие» (встречаются в нескольких → даём несколько кандидатов).
#
# ВАЖНО: і/І НЕ казахская метка — это украинская/белорусская буква.
# Из-за неё раньше украинцы попадали в «казахов». Убрано.

_TURKISH_CHARS = set("ğşıİĞŞ")           # без ü/ö/ç — они есть в нем./фр.

# Сильные (уникальные для одного языка среди наших 12):
_KZ_STRONG = set("әұһӘҰҺ")               # казахские: ә, ұ, һ
_TJ_STRONG = set("ӣӯҷӢӮҶ")               # таджикские: ӣ, ӯ, ҷ
_UZ_STRONG = set("ўЎ")                    # узбекская кириллица: ў
# Узбекская латиница: oʻ / gʻ (модиф. запятая ʻ U+02BB, ʼ U+02BC, ', `)
_UZ_LATIN_MARKERS = ("oʻ", "gʻ", "oʼ", "gʼ", "o'", "g'", "o`", "g`", "oʽ", "gʽ")

# Общие буквы (несколько языков):
_SHARED_HA = set("ҳҲ")                    # ҳ → таджик / узбек
_SHARED_QGH = set("қғҚҒ")                 # қ, ғ → казах / таджик / узбек
_SHARED_NOU = set("ңөүҢӨҮ")              # ң, ө, ү → казах / киргиз


def _has_turkish_chars(name: str) -> bool:
    return any(ch in _TURKISH_CHARS for ch in (name or ""))


def _uz_latin_marker(name: str) -> bool:
    low = (name or "").lower()
    return any(m in low for m in _UZ_LATIN_MARKERS)


def _ca_char_codes(name: str) -> set:
    """
    Определяет страну(ы) Средней Азии по СПЕЦ-БУКВАМ алфавита (как иероглифы).
    Возвращает множество кодов-кандидатов. Сильные буквы → один точный код;
    общие буквы → несколько кандидатов (буква встречается в нескольких языках).
    """
    if not name:
        return set()
    chars = set(name)
    cand: set = set()
    strong: set = set()

    if chars & _KZ_STRONG:
        strong.add("kz")
    if chars & _TJ_STRONG:
        strong.add("tj")
    if (chars & _UZ_STRONG) or _uz_latin_marker(name):
        strong.add("uz")
    cand |= strong

    # ҳ → таджик/узбек (если сильных меток tj/uz ещё нет)
    if chars & _SHARED_HA and not (strong & {"tj", "uz"}):
        cand |= {"tj", "uz"}
    # қ, ғ → казах/таджик/узбек
    if chars & _SHARED_QGH and not (strong & {"kz", "tj", "uz"}):
        cand |= {"kz", "tj", "uz"}
    # ң, ө, ү → казах/киргиз
    if chars & _SHARED_NOU and "kz" not in strong:
        cand |= {"kz", "kg"}

    return cand


def _has_ca_chars(name: str) -> bool:
    """Есть ли в имени хоть одна спец-буква Средней Азии (любого языка)."""
    return bool(_ca_char_codes(name))


# ══════════════════════════════════════════════
#  Скрипт-детекторы (надёжные на 100 %)
# ══════════════════════════════════════════════

def is_chinese_name(name: str) -> bool:
    """≥ 1 CJK, нет каны/хангыля/кириллицы, латиница не доминирует."""
    if not name:
        return False
    s = _count_script(name)
    if s["cjk"] < 1:
        return False
    if s["kana"] > 0 or s["hangul"] > 0 or s["cyrillic"] > 0:
        return False
    if s["text"] > 0 and s["latin"] / s["text"] > 0.6:
        return False
    return True


def is_japanese_name(name: str) -> bool:
    """Есть хирагана или катакана."""
    if not name:
        return False
    return _count_script(name)["kana"] >= 1


def is_korean_name(name: str) -> bool:
    """Есть хангыль."""
    if not name:
        return False
    return _count_script(name)["hangul"] >= 1


def is_arab_name(name: str) -> bool:
    """Есть арабское письмо."""
    if not name:
        return False
    return _count_script(name)["arabic"] >= 1


def is_indian_name(name: str) -> bool:
    """Индийские скрипты ИЛИ типичное индийское имя (латиница)."""
    if not name:
        return False
    if _count_script(name)["indic"] >= 1:
        return True
    return _match_any(name, _INDIAN_NAMES)


# ══════════════════════════════════════════════
#  Спец-буквы + словари
# ══════════════════════════════════════════════

def is_turkish_name(name: str) -> bool:
    if not name:
        return False
    if _has_turkish_chars(name):
        return True
    return _match_any(name, _TURKISH_NAMES)


def is_uzbek_name(name: str) -> bool:
    if not name:
        return False
    if "uz" in _ca_char_codes(name):     # спец-буквы алфавита (ў, oʻ, gʻ …)
        return True
    return _match_any(name, _UZBEK_NAMES)


def is_kazakh_name(name: str) -> bool:
    if not name:
        return False
    if "kz" in _ca_char_codes(name):     # казахские буквы (ә, ұ, һ, қ, ғ, ң, ө, ү)
        return True
    return _match_any(name, _KAZAKH_NAMES)


def is_kyrgyz_name(name: str) -> bool:
    if not name:
        return False
    if "kg" in _ca_char_codes(name):     # киргизские буквы (ң, ө, ү)
        return True
    return _match_any(name, _KYRGYZ_NAMES)


def is_tajik_name(name: str) -> bool:
    if not name:
        return False
    if "tj" in _ca_char_codes(name):     # таджикские буквы (ӣ, ӯ, ҷ, ҳ, қ, ғ)
        return True
    return _match_any(name, _TAJIK_NAMES)


def is_indonesian_name(name: str) -> bool:
    return bool(name) and _match_any(name, _INDONESIAN_NAMES)


def is_russian_name(name: str) -> bool:
    """
    Кириллица в имени, НО не относится к Средней Азии.
    Это убирает узбеков/казахов/киргизов/таджиков из «русских».
    """
    if not name or not _has_cyrillic(name):
        return False
    # Исключаем явных среднеазиатов: у них свои буквы алфавита
    if _has_ca_chars(name):
        return False
    if _match_any(name, _CENTRAL_ASIA_ALL):
        return False
    return True


def is_russian_bio(bio: str) -> bool:
    return bool(bio) and _has_cyrillic(bio)


def detect_country_full(name: str, bio: str = "", extra: str = "") -> str | None:
    """
    Максимально точное определение страны по нескольким сигналам:
    имя → bio → доп.текст (например, последние посты/подпись).

    Логика: сначала по имени (самый надёжный сигнал). Если имя ничего
    не дало — пробуем bio, затем extra. Так не теряем точность имени,
    но добираем тех, у кого имя нейтральное (латиница без словаря),
    зато в bio видно язык/страну.
    """
    # 1) Имя — приоритет
    c = detect_country(name or "")
    if c:
        return c
    # 2) Bio
    if bio:
        c = detect_country(bio)
        if c:
            return c
    # 3) Доп. текст (посты, подпись и т.п.)
    if extra:
        c = detect_country(extra)
        if c:
            return c
    return None


# ── Первое слово (для обратной совместимости) ──

def _first_word(name: str) -> str:
    toks = _tokens(name)
    return toks[0] if toks else ""


# ══════════════════════════════════════════════
#  Словари имён
# ══════════════════════════════════════════════

_INDONESIAN_NAMES = {
    "agus", "ahmad", "andi", "ari", "budi", "dedi", "deni", "eko",
    "fajar", "hadi", "hendra", "irfan", "joko", "made", "muhammad",
    "rizki", "rizky", "rudi", "surya", "wahyu", "wayan", "yusuf", "putu",
    "ketut", "nyoman", "komang", "gede", "kadek", "bagus", "iman",
    "dwi", "tri", "nugroho", "setiawan", "kurniawan", "hidayat",
    "firmansyah", "ramadhan", "gunawan", "saputra", "pratama",
    "arif", "dian", "fikri", "gilang", "ilham", "fauzi", "rizal",
    "bayu", "yoga", "angga", "dimas", "galih", "reza", "farid",
    "adi", "agung", "bambang", "cahyo", "dani", "edi", "guntur",
    "haris", "indra", "krisna", "lukman", "maman", "naufal", "okta",
    "panji", "rangga", "satria", "teguh", "umar", "wisnu", "yudha",
    "dewi", "fitri", "indah", "lestari", "maya", "ningsih", "putri",
    "ratna", "sari", "sri", "siti", "wati", "ayu", "mega", "nisa",
    "nurul", "rina", "tuti", "yuli", "ani", "ida", "rini", "nita",
    "wulan", "intan", "laras", "melati", "citra", "anggi", "bunga",
    "cinta", "diah", "eka", "fani", "gita", "hesti", "kartika",
    "lia", "mira", "novi", "rahma", "sinta", "vina", "yanti",
}

_INDIAN_NAMES = {
    "aakash", "aarav", "abhishek", "aditya", "ajay", "akash", "amar",
    "amit", "anand", "anil", "arjun", "ashish", "deepak", "gaurav",
    "gopal", "hari", "harsh", "hemant", "karan", "krishna", "kumar",
    "lalit", "manoj", "mohit", "neeraj", "nikhil", "pankaj", "pradeep",
    "prakash", "praveen", "rahul", "raj", "rajesh", "raju", "rakesh",
    "ram", "ramesh", "ravi", "rohit", "sachin", "sandeep", "sanjay",
    "shiv", "sunil", "suresh", "vijay", "vikram", "vinod", "vishal",
    "vivek", "yash", "abhay", "ankit", "ankur", "arun", "ashok",
    "bhavesh", "chetan", "dinesh", "ganesh", "girish", "jatin",
    "kapil", "mahesh", "naveen", "nitin", "pawan", "rishi", "rohan",
    "saurabh", "shyam", "tarun", "umesh", "varun", "yogesh",
    "ananya", "anjali", "divya", "kavita", "komal", "madhuri", "meena",
    "neha", "nisha", "pallavi", "pooja", "pragya", "preeti", "priya",
    "radha", "rashmi", "rekha", "ritu", "sarita", "shanti", "shreya",
    "sita", "sneha", "sonia", "sunita", "swati", "tanya", "vaishali",
    "vandana", "aishwarya", "deepika", "isha", "jyoti", "kiran",
    "lakshmi", "manju", "nidhi", "payal", "ruchi", "shilpa", "simran",
    "singh", "sharma", "patel", "gupta", "verma", "jain", "chauhan",
    "thakur", "yadav", "khan", "mishra", "pandey", "dubey", "tiwari",
    "sahu", "joshi", "reddy", "nair", "iyer", "menon", "agarwal",
    "bansal", "chopra", "desai", "kapoor", "malhotra", "mehta",
    "rao", "shah", "sinha", "trivedi",
}

_TURKISH_NAMES = {
    "ahmet", "mehmet", "mustafa", "huseyin", "hasan", "ibrahim",
    "ismail", "yusuf", "osman", "murat", "omer", "burak", "emre",
    "kadir", "serkan", "kerem", "fatih", "selim", "baris", "can",
    "cem", "deniz", "enes", "eren", "furkan", "gokhan", "halil",
    "kemal", "onur", "recep", "tuncay", "ugur", "volkan", "yasin",
    "yilmaz", "caglar", "sahin", "berk", "alp", "arda", "tolga",
    "umut", "kaan", "ali", "veli", "suleyman", "ramazan", "abdullah",
    "okan", "ozan", "taha", "yigit", "bora", "cihan", "ferhat",
    "hakan", "ilker", "koray", "levent", "metin", "nuri", "polat",
    "sefa", "tarik", "ufuk", "vedat", "yavuz", "zeki",
    "ayse", "fatma", "emine", "hatice", "zeynep", "elif", "merve",
    "busra", "esra", "derya", "selin", "gul", "naz", "ecem", "irem",
    "yagmur", "cansu", "melis", "tugba", "ozge", "pinar", "sibel",
    "burcu", "gamze", "hulya", "nur", "seyma", "ceren", "dilek",
    "ebru", "leyla", "neslihan", "aysel", "bahar", "damla", "feride",
    "gizem", "kubra", "nilay", "ozlem", "rabia", "sevgi", "tugce",
    "yildiz", "yilmaz", "demir", "kaya", "celik", "sahin", "ozturk",
    "aydin", "ozdemir", "arslan", "dogan", "kilic",
}

_UZBEK_NAMES = {
    "abdulla", "abdullah", "alisher", "anvar", "aziz", "aziz",
    "bahrom", "bakhtiyor", "bobur", "botir", "dilshod", "eldor",
    "farhod", "hamid", "islom", "jamshid", "jasur", "kamoliddin",
    "laziz", "mansur", "mirzo", "muzaffar", "nodir", "obid", "odil",
    "otabek", "rustam", "said", "sardor", "sherzod", "temur",
    "ulugbek", "umid", "zafar", "shoxrux", "shohruh", "javohir",
    "doston", "diyor", "sanjar", "behruz", "akmal", "ravshan",
    "shavkat", "ulugbek", "fazliddin", "asadbek", "jahongir",
    "абдулла", "алишер", "анвар", "бахтиёр", "ботир", "дилшод",
    "жамшид", "жасур", "лазиз", "отабек", "сардор", "шерзод",
    "темур", "улугбек", "умид", "нодир", "шохрух", "жавохир",
    "достон", "санжар", "беҳруз", "акмал", "равшан", "шавкат",
    "barno", "dilfuza", "dilnoza", "feruza", "gavhar", "gulnara",
    "hulkar", "iroda", "kamola", "lola", "madina", "malika",
    "mohira", "muazzam", "nafisa", "nasiba", "nilufar", "nozima",
    "shahlo", "shoira", "zulfiya", "sevara", "dildora", "gozal",
    "munisa", "rayhona", "sabina", "umida", "zarina", "zebo",
    "дилноза", "гулнара", "лола", "малика", "мадина", "нафиса",
    "нилуфар", "шахло", "зулфия", "севара", "дилдора", "гузал",
    "муниса", "райхона", "сабина", "умида",
}

_KAZAKH_NAMES = {
    "abay", "adilet", "aibek", "akim", "almas", "arman", "asan",
    "askhat", "bakytzhan", "baurzhan", "berik", "bolat", "darhan",
    "dastan", "duman", "erlan", "ermek", "zhandos", "kanat",
    "kuanysh", "maksat", "marat", "nurzhan", "nurlan", "rakhat",
    "saken", "serik", "talgat", "temirlan", "timur", "yerlan",
    "daniyar", "dias", "alibek", "ablai", "sanzhar", "olzhas",
    "rustem", "azamat", "kairat", "nurbol", "samat", "yerbol",
    "абай", "адилет", "айбек", "аким", "алмас", "арман", "асан",
    "асхат", "бакытжан", "бауыржан", "берик", "болат", "дархан",
    "дастан", "думан", "ерлан", "ермек", "жандос", "канат",
    "куаныш", "максат", "марат", "нуржан", "нурлан", "рахат",
    "сакен", "серик", "талгат", "темирлан", "данияр", "диас",
    "алибек", "аблай", "санжар", "олжас", "рустем", "азамат",
    "кайрат", "нурбол", "самат", "ербол",
    "aigul", "aizhan", "aliya", "asem", "ayazhan", "botagoz",
    "gulmira", "danagul", "dina", "zhanar", "zhansaya", "zarina",
    "inzhu", "kamila", "madina", "nazgul", "nurgul", "saniya",
    "tomiris", "ainur", "akbota", "balzhan", "dana", "saule",
    "айгуль", "айжан", "алия", "асем", "аяжан", "ботагоз",
    "гульмира", "данагуль", "дина", "жанар", "жансая", "зарина",
    "инжу", "камила", "мадина", "назгуль", "нургуль", "сания",
    "томирис", "айнур", "акбота", "балжан", "дана", "сауле",
}

_KYRGYZ_NAMES = {
    "aibek", "akbar", "akylbek", "almazbek", "askar", "bakyt",
    "bekbolot", "zhanybek", "zhoomart", "kubat", "kutman", "manas",
    "meder", "mirbek", "mirlan", "nurbek", "nurdin", "syimyk",
    "tilek", "ulan", "chyngyz", "azamat", "baktiyar", "ermek",
    "kanybek", "maksat", "nursultan", "ruslan", "talant", "urmat",
    "айбек", "акбар", "акылбек", "алмазбек", "аскар", "бакыт",
    "бекболот", "жаныбек", "жоомарт", "кубат", "кутман", "манас",
    "медер", "мирбек", "мирлан", "нурбек", "нурдин", "сыймык",
    "тилек", "улан", "чынгыз", "каныбек", "нурсултан", "талант",
    "урмат", "бактияр",
    "aigerim", "aiperi", "altynai", "anara", "burul", "gulai",
    "gulzat", "zhypar", "nurgul", "cholpon", "aizada", "begaim",
    "айгерим", "айпери", "алтынai", "анара", "бурул", "гулай",
    "гулзат", "жыпар", "нургуль", "чолпон", "айзада", "бегайм",
    "айзат", "нуржан",
}

_TAJIK_NAMES = {
    "abdullo", "anvar", "bakhrom", "daler", "dodo", "ismoil",
    "komil", "maruf", "mirzo", "navruz", "parviz", "rustam",
    "somon", "firdavs", "khurshed", "sharif", "farrukh", "jamshed",
    "suhrob", "behruz", "shahriyor", "umed", "said", "mahmud",
    "абдулло", "анвар", "бахром", "далер", "додо", "исмоил",
    "комил", "маъруф", "мирзо", "навруз", "наврӯз", "парвиз",
    "рустам", "сомон", "фирдавс", "хуршед", "шариф", "фаррух",
    "ҷамшед", "сухроб", "беҳруз", "шаҳриёр", "умед", "саид",
    "dilorom", "zebo", "zulaykho", "malika", "mehrangez", "nigina",
    "nigora", "parvina", "rukhshona", "shahlo", "shirin", "sitora",
    "farzona", "gulnoza", "manija", "nasiba",
    "дилором", "зебо", "зулайхо", "малика", "меҳрангез", "нигина",
    "нигора", "парвина", "рухшона", "шахло", "ширин", "ситора",
    "фарзона", "гулноза", "манижа", "насиба",
}

# Доливаем большие расширения в словари Средней Азии (kz/uz/kg/tj).
# Это ГЛАВНЫЙ способ ловить тех, кто пишет имя обычными русскими буквами
# (без спец-букв алфавита) — их по символам не отличить.
_KAZAKH_NAMES = _KAZAKH_NAMES | KAZAKH_EXTRA
_UZBEK_NAMES = _UZBEK_NAMES | UZBEK_EXTRA
_KYRGYZ_NAMES = _KYRGYZ_NAMES | KYRGYZ_EXTRA
_TAJIK_NAMES = _TAJIK_NAMES | TAJIK_EXTRA

_CENTRAL_ASIA_ALL = (
    _UZBEK_NAMES | _KAZAKH_NAMES | _KYRGYZ_NAMES | _TAJIK_NAMES
)

# Доливаем расширения в существующие словари (id, in, tr)
_INDONESIAN_NAMES = _INDONESIAN_NAMES | INDONESIAN_EXTRA
_INDIAN_NAMES = _INDIAN_NAMES | INDIAN_EXTRA
_TURKISH_NAMES = _TURKISH_NAMES | TURKISH_EXTRA


# ══════════════════════════════════════════════
#  Маппинги код → детектор / флаг / название
# ══════════════════════════════════════════════

COUNTRY_DETECTORS = {
    "cn": is_chinese_name,
    "ru": is_russian_name,
    "jp": is_japanese_name,
    "kr": is_korean_name,
    "id": is_indonesian_name,
    "in": is_indian_name,
    "ar": is_arab_name,
    "uz": is_uzbek_name,
    "kz": is_kazakh_name,
    "kg": is_kyrgyz_name,
    "tj": is_tajik_name,
    "tr": is_turkish_name,
}

COUNTRY_FLAGS = {
    "cn": "🇨🇳", "ru": "🇷🇺", "jp": "🇯🇵", "kr": "🇰🇷",
    "id": "🇮🇩", "in": "🇮🇳", "ar": "🇸🇦", "uz": "🇺🇿",
    "kz": "🇰🇿", "kg": "🇰🇬", "tj": "🇹🇯", "tr": "🇹🇷",
}

COUNTRY_LABELS = {
    "cn": "Китай", "ru": "Россия", "jp": "Япония", "kr": "Корея",
    "id": "Индонезия", "in": "Индия", "ar": "Арабы", "uz": "Узбекистан",
    "kz": "Казахстан", "kg": "Киргизия", "tj": "Таджикистан", "tr": "Турция",
}

# Порядок проверки: узкие/надёжные детекторы ПЕРВЫМИ, общий русский — последним.
_DETECT_ORDER = [
    "cn", "jp", "kr", "ar", "in",   # уникальные скрипты
    "kz", "tr",                      # спец-буквы + имена
    "uz", "kg", "tj",                # имена Средней Азии
    "id",                            # индонезийские имена
    "ru",                            # fallback: любая кириллица
]


# Гео-ключевики: страны/города/языки в bio дают сильный сигнал.
# Проверяются ПЕРВЫМИ в detect_country (точнее любых словарей имён).
_GEO_KEYWORDS: dict[str, tuple[str, ...]] = {
    "uz": ("узбекистан", "ўзбекистон", "o'zbekiston", "ozbekiston", "uzbekistan",
           "ташкент", "тошкент", "toshkent", "tashkent", "самарканд", "samarqand",
           "бухара", "buxoro", "andijon", "андижан", "fergana", "farg'ona"),
    "kz": ("казахстан", "қазақстан", "qazaqstan", "kazakhstan", "алматы", "almaty",
           "астана", "astana", "нур-султан", "nur-sultan", "shymkent", "шымкент",
           "караганда", "qaraganda", "kz"),
    "kg": ("киргизия", "кыргызстан", "кыргыз", "kyrgyzstan", "kyrgyz", "бишкек",
           "bishkek", "ош ", "osh", "jalal-abad"),
    "tj": ("таджикистан", "тоҷикистон", "tojikiston", "tajikistan", "душанбе",
           "dushanbe", "худжанд", "khujand", "хучанд"),
    "tr": ("türkiye", "turkiye", "turkey", "турция", "istanbul", "стамбул",
           "ankara", "анкара", "izmir", "antalya"),
    "cn": ("中国", "中華", "北京", "上海", "广州", "深圳", "china", "китай",
           "beijing", "shanghai"),
    "jp": ("日本", "東京", "japan", "япония", "tokyo", "osaka", "大阪"),
    "kr": ("한국", "대한민국", "서울", "korea", "корея", "seoul", "сеул"),
    "ar": ("السعودية", "الإمارات", "مصر", "دبي", "dubai", "saudi", "uae", "qatar",
           "kuwait", "egypt", "دبي"),
    "in": ("भारत", "india", "индия", "delhi", "mumbai", "дели", "мумбаи",
           "bharat", "hindustan"),
    "id": ("indonesia", "индонезия", "jakarta", "джакарта", "bandung",
           "surabaya", "nusantara"),
    "ru": ("россия", "russia", "москва", "moscow", "санкт-петербург",
           "питер", "spb", "екатеринбург", "новосибиск"),
}

# Порядок проверки гео — узкие первыми, ru последним
_GEO_ORDER = ["uz", "kz", "kg", "tj", "tr", "cn", "jp", "kr", "ar", "in", "id", "ru"]


def detect_geo_keyword(text: str) -> str | None:
    """Определяет страну по гео-ключевикам (название страны/города/языка)."""
    if not text:
        return None
    t = text.lower()
    for code in _GEO_ORDER:
        for kw in _GEO_KEYWORDS[code]:
            if kw in t:
                return code
    return None


def detect_country(name: str) -> str | None:
    """Определяет страну по имени/тексту. Возвращает код или None.
    Гео-ключевики (страна/город в тексте) имеют приоритет над словарями имён."""
    geo = detect_geo_keyword(name)
    if geo:
        return geo
    for code in _DETECT_ORDER:
        if COUNTRY_DETECTORS[code](name):
            return code
    return None


# ══════════════════════════════════════════════
#  Фильтр ботов / магазинов / каналов
# ══════════════════════════════════════════════

_WALLET_RE = _re.compile(r'^(UQ|EQ|0:)[A-Za-z0-9_\-]{20,}')
_TON_DOMAIN_RE = _re.compile(r'^[a-z0-9\-]+\.t\.me$|^[a-z0-9\-]+\.ton$', _re.IGNORECASE)


def is_wallet_address(name: str) -> bool:
    return bool(name and _WALLET_RE.match(name.strip()))


def is_channel_or_junk(username: str, display_name: str) -> bool:
    """True если это канал, домен .ton/.t.me, или мусорное имя."""
    d = (display_name or "").strip()

    if is_wallet_address(d):
        return True
    if _TON_DOMAIN_RE.match(d):
        return True
    if len(d) <= 1:
        return True
    if len(d) > 30 and ('|' in d or '+' in d or '$' in d):
        return True

    d_lower = d.lower()
    _channel_words = (
        "channel", "канал", "news", "новости", "shop", "магазин",
        "store", "market", "маркет", "crypto", "крипто",
        "museum", "музей", "gallery", "обмен", "exchange",
        "casino", "казино", "premium", "official",
    )
    for cw in _channel_words:
        if cw in d_lower:
            return True

    clean = _re.sub(r'[^a-zA-Zа-яА-ЯёЁ\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF\u0600-\u06FF\u0900-\u097F]', '', d)
    if len(clean) < 2:
        return True

    return False


def is_bot_or_shop(username: str, display_name: str) -> bool:
    """Эвристика: Telegram-бот, магазин, канал или мусор."""
    u = (username or "").lower().strip()
    if u.endswith("bot"):
        return True

    _shop_prefixes = (
        "shop_", "store_", "market_", "news_", "crypto_",
        "nft_", "ton_", "official_", "museum_", "gallery_",
        "club_", "team_",
    )
    _shop_suffixes = (
        "_shop", "_store", "_market", "_bot", "_nft", "_ton",
        "_news", "_crypto", "_channel", "_official",
        "_trade", "_finance", "_transfer", "_media",
    )
    _skip_contains = (
        "relay", "swap", "exchange", "casino", "airdrop",
        "mining", "staking", "premium", "promo", "museum",
        "gallery", "studio", "invest", "trading",
    )

    for p in _shop_prefixes:
        if u.startswith(p):
            return True
    for s in _shop_suffixes:
        if u.endswith(s):
            return True
    for c in _skip_contains:
        if c in u:
            return True
    if is_channel_or_junk(u, display_name):
        return True
    return False
