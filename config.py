"""
Конфигурация NFT Scanner V8.
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Telegram ──────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "8997450266:AAHiM9b2Tkwfh9uHZn1JrmB-_XpvzSzVkQc")
BOT_USERNAME = os.getenv("BOT_USERNAME", "bad_pars_robot")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "8794223703").split(",") if x.strip()]

# ── Telegram MTProto / Stars Rating ───────────
# API ID/hash are used by the user session to read Stars Rating via users.getFullUser.
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "36435539"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "d05d86373e313e86aed7d051eed9c97c")
TELEGRAM_SESSION_FILE = os.getenv("TELEGRAM_SESSION_FILE", "my_session.session")

# Strict account rating filter: allow L0, L1 and L2 only.
RATING_FILTER_ENABLED = os.getenv("RATING_FILTER_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
RATING_MAX_LEVEL = int(os.getenv("RATING_MAX_LEVEL", "2"))
RATING_CACHE_TTL = int(os.getenv("RATING_CACHE_TTL", "600"))
RATING_CONCURRENCY = int(os.getenv("RATING_CONCURRENCY", "3"))
TELEGRAM_API_SERVER = os.getenv("TELEGRAM_API_SERVER", "")

# ── Mini App ──────────────────────────────────
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://badteamotc-blip.github.io/parser/")

# ── Обязательная подписка ─────────────────────
REQUIRED_CHANNEL_ID = -1003984671410
REQUIRED_CHANNEL_LINK = "https://t.me/bad_team_ton"

# ── БД ────────────────────────────────────────
DB_PATH = os.path.join(BASE_DIR, "data", "nft_cache.db")

# ── Скан ──────────────────────────────────────
MAX_CONCURRENT_REQUESTS = 150
REQUEST_TIMEOUT = 5
DELAY_BETWEEN_BATCHES = 0
PROGRESS_UPDATE_INTERVAL = 2  # секунд

# ── Рандомный парсинг ─────────────────────────
RANDOM_COLLECTIONS_COUNT = 5
RANDOM_ITEMS_PER_COLLECTION = 100

# ── Пагинация ────────────────────────────────
GIFTS_PER_PAGE = 8
MODELS_PER_PAGE = 10
BACKDROPS_PER_PAGE = 10
RESULTS_PER_PAGE = 10        # юзеров на страницу (компактно, влезает в TG)
TOTAL_RESULTS = 200          # макс. результатов за скан

# ── Фильтры NFT ──────────────────────────────
#  код: (min, max, label)
NFT_COUNT_RANGES = {
    "1-3":  (1, 3,   "1–3 NFT"),
    "4-10": (4, 10,  "4–10 NFT"),
    "10+":  (10, 999999, "10+ NFT"),
    "any":  (0, 999999, "Любое кол-во"),
}
DEFAULT_NFT_RANGE = "any"
MAX_NFT_HARD_CAP = 999999

# ── Экспорт ──────────────────────────────────
CSV_DIR = os.path.join(BASE_DIR, "data", "exports")
