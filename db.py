"""
Кэш-база SQLite — NFT Scanner V7.
Миграция: новые колонки добавляются без потери данных.
"""
from datetime import datetime, timezone

import aiosqlite
from config import DB_PATH

_db: aiosqlite.Connection | None = None


async def _get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        import os
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        _db = await aiosqlite.connect(DB_PATH)
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.execute("PRAGMA synchronous=NORMAL")
    return _db


# ── Миграции ─────────────────────────────────

async def _column_exists(db, table: str, column: str) -> bool:
    cur = await db.execute(f"PRAGMA table_info({table})")
    rows = await cur.fetchall()
    return any(r[1] == column for r in rows)


async def _add_column_safe(db, table: str, column: str, col_type: str, default):
    if not await _column_exists(db, table, column):
        await db.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {col_type} DEFAULT {default}"
        )


async def _check_columns(db, table: str, required: list[str]) -> bool:
    try:
        cur = await db.execute(f"PRAGMA table_info({table})")
        rows = await cur.fetchall()
        if not rows:
            return True
        existing = {r[1] for r in rows}
        if all(c in existing for c in required):
            return True
        await db.execute(f"DROP TABLE IF EXISTS {table}")
        return True
    except Exception:
        await db.execute(f"DROP TABLE IF EXISTS {table}")
        return True


async def init_db():
    db = await _get_db()

    # ── Основная таблица NFT ──
    await db.execute("""
        CREATE TABLE IF NOT EXISTS nft_owners (
            slug         TEXT PRIMARY KEY,
            collection   TEXT NOT NULL,
            item_number  INTEGER NOT NULL,
            display_name TEXT,
            username     TEXT,
            model        TEXT,
            backdrop     TEXT,
            symbol       TEXT,
            has_chinese  INTEGER DEFAULT 0,
            scanned_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Миграция: добавить has_russian если нет
    await _add_column_safe(db, "nft_owners", "has_russian", "INTEGER", 0)
    # Миграция: детектированная страна (cn/ru/jp/kr/ar/in/uz/kz/kg/tj/id/tr)
    await _add_column_safe(db, "nft_owners", "detected_country", "TEXT", None)
    await _add_column_safe(db, "nft_owners", "rating_level", "INTEGER", None)

    # ── Размеры коллекций ──
    await db.execute("""
        CREATE TABLE IF NOT EXISTS collection_sizes (
            collection TEXT PRIMARY KEY,
            total      INTEGER NOT NULL
        )
    """)

    # ── Избранное ──
    await _check_columns(db, "favorites", [
        "id", "chat_id", "display_name", "username", "collection", "slug",
    ])
    await db.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id      INTEGER NOT NULL,
            display_name TEXT,
            username     TEXT,
            collection   TEXT,
            slug         TEXT,
            note         TEXT DEFAULT '',
            added_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chat_id, slug)
        )
    """)

    # ── Зеркала ──
    await db.execute("""
        CREATE TABLE IF NOT EXISTS mirrors (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id     INTEGER NOT NULL,
            bot_token    TEXT NOT NULL UNIQUE,
            bot_username TEXT,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ── Юзеры бота (для рассылки) ──
    await db.execute("""
        CREATE TABLE IF NOT EXISTS bot_users (
            chat_id    INTEGER PRIMARY KEY,
            bot_token  TEXT DEFAULT '',
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await _add_column_safe(db, "bot_users", "username", "TEXT", "''")
    await _add_column_safe(db, "bot_users", "display_name", "TEXT", "''")

    # ── Просмотренные юзеры (дедупликация) ──
    await db.execute("""
        CREATE TABLE IF NOT EXISTS viewed_users (
            chat_id    INTEGER NOT NULL,
            owner_key  TEXT NOT NULL,
            viewed_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chat_id, owner_key)
        )
    """)

    # ── Шаблоны сообщений ──
    await db.execute("""
        CREATE TABLE IF NOT EXISTS user_templates (
            chat_id  INTEGER NOT NULL,
            country  TEXT NOT NULL,
            text     TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(chat_id, country)
        )
    """)

    # ── Индексы ──
    await db.execute("CREATE INDEX IF NOT EXISTS idx_coll ON nft_owners(collection)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_chinese ON nft_owners(collection, has_chinese)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_russian ON nft_owners(collection, has_russian)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_detcountry ON nft_owners(collection, detected_country)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_fav_chat ON favorites(chat_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_viewed ON viewed_users(chat_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_botusers ON bot_users(bot_token)")

    await db.commit()

    # ── Одноразовая миграция: обновить has_russian для старых записей ──
    await _migrate_russian_flags(db)


async def _migrate_russian_flags(db):
    """Одноразово проставляет has_russian для уже отсканированных записей."""
    from chinese_detector import is_russian_name
    cur = await db.execute(
        "SELECT slug, display_name FROM nft_owners WHERE has_russian = 0 AND display_name IS NOT NULL LIMIT 50000"
    )
    rows = await cur.fetchall()
    updates = []
    for r in rows:
        if is_russian_name(r["display_name"]):
            updates.append((1, r["slug"]))
    if updates:
        await db.executemany(
            "UPDATE nft_owners SET has_russian = ? WHERE slug = ?", updates
        )
        await db.commit()


# ── Запись NFT ───────────────────────────────

async def save_nft_items_batch(items: list[dict]):
    if not items:
        return
    db = await _get_db()
    keep = {
        "slug", "collection", "item_number", "display_name",
        "username", "model", "backdrop", "symbol", "has_chinese", "has_russian",
        "detected_country", "rating_level",
    }
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    clean = []
    for item in items:
        row = {k: v for k, v in item.items() if k in keep}
        row["has_chinese"] = int(bool(row.get("has_chinese", 0)))
        row["has_russian"] = int(bool(row.get("has_russian", 0)))
        row["detected_country"] = row.get("detected_country") or None
        row["rating_level"] = row.get("rating_level") if row.get("rating_level") is not None else None
        row["scanned_at"] = now
        clean.append(row)
    await db.executemany(
        """INSERT OR REPLACE INTO nft_owners
           (slug, collection, item_number, display_name, username,
            model, backdrop, symbol, has_chinese, has_russian,
            detected_country, rating_level, scanned_at)
           VALUES (:slug, :collection, :item_number, :display_name,
                   :username, :model, :backdrop, :symbol,
                   :has_chinese, :has_russian, :detected_country, :rating_level, :scanned_at)
        """,
        clean,
    )
    await db.commit()


async def save_collection_size(collection: str, total: int):
    db = await _get_db()
    await db.execute(
        "INSERT OR REPLACE INTO collection_sizes (collection, total) VALUES (?, ?)",
        (collection, total),
    )
    await db.commit()


# ── Чтение NFT ───────────────────────────────

async def get_collection_size(collection: str) -> int | None:
    db = await _get_db()
    cur = await db.execute(
        "SELECT total FROM collection_sizes WHERE collection = ?", (collection,),
    )
    row = await cur.fetchone()
    return row["total"] if row else None


async def get_scanned_items(collection: str) -> set[int]:
    db = await _get_db()
    cur = await db.execute(
        "SELECT item_number FROM nft_owners WHERE collection = ?", (collection,),
    )
    return {row["item_number"] async for row in cur}


async def get_scanned_count(collection: str) -> int:
    db = await _get_db()
    cur = await db.execute(
        "SELECT COUNT(*) as cnt FROM nft_owners WHERE collection = ?", (collection,),
    )
    row = await cur.fetchone()
    return row["cnt"] if row else 0


# ── Универсальный запрос пользователей ────────

async def get_users_grouped(
    collection: str,
    country: str = "cn",
    model_filter: str | None = None,
    backdrop_filter: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """
    Уникальные владельцы (по стране) с подсчётом NFT внутри коллекции.
    country: "cn" | "ru"
    """
    db = await _get_db()

    # Универсальный фильтр по стране: cn/ru работают и по старым флагам,
    # и по новому detected_country; остальные страны — только detected_country.
    if country == "cn":
        country_pred = "(has_chinese = 1 OR detected_country = 'cn')"
    elif country == "ru":
        country_pred = "(has_russian = 1 OR detected_country = 'ru')"
    else:
        country_pred = "detected_country = ?"

    where = ["collection = ?", country_pred]
    try:
        from config import RATING_FILTER_ENABLED, RATING_MAX_LEVEL
        if RATING_FILTER_ENABLED:
            where.append("rating_level IS NOT NULL AND rating_level <= ?")
    except Exception:
        RATING_FILTER_ENABLED, RATING_MAX_LEVEL = False, 2
    params: list = [collection]
    if country not in ("cn", "ru"):
        params.append(country)
    if RATING_FILTER_ENABLED:
        params.append(RATING_MAX_LEVEL)

    if model_filter:
        where.append("model = ?")
        params.append(model_filter)
    if backdrop_filter:
        where.append("backdrop = ?")
        params.append(backdrop_filter)

    where_sql = " AND ".join(where)

    sql = f"""
        SELECT
            CASE WHEN username != '' AND username IS NOT NULL
                 THEN username ELSE display_name END AS owner_key,
            display_name,
            username,
            COUNT(*) AS nft_count,
            GROUP_CONCAT(slug, ', ') AS slugs,
            MIN(slug) AS first_slug,
            model,
            backdrop,
            symbol
        FROM nft_owners
        WHERE {where_sql}
        GROUP BY owner_key
        ORDER BY RANDOM()
        LIMIT ?
    """
    params.append(limit)
    cur = await db.execute(sql, params)
    results = []
    async for row in cur:
        d = dict(row)
        slugs_list = d.get("slugs", "").split(", ")
        d["slugs_short"] = slugs_list[:3]
        d["slugs_total"] = len(slugs_list)
        results.append(d)
    return results


async def get_users_random_multi(
    collections: list[str],
    country: str = "cn",
    limit: int = 200,
) -> list[dict]:
    """Юзеры из нескольких коллекций (для рандомного парсинга)."""
    db = await _get_db()
    placeholders = ", ".join("?" for _ in collections)

    if country == "cn":
        country_pred = "(has_chinese = 1 OR detected_country = 'cn')"
        extra_param = None
    elif country == "ru":
        country_pred = "(has_russian = 1 OR detected_country = 'ru')"
        extra_param = None
    else:
        country_pred = "detected_country = ?"
        extra_param = country

    rating_clause = ""
    rating_params: list = []
    try:
        from config import RATING_FILTER_ENABLED, RATING_MAX_LEVEL
        if RATING_FILTER_ENABLED:
            rating_clause = " AND rating_level IS NOT NULL AND rating_level <= ?"
            rating_params.append(RATING_MAX_LEVEL)
    except Exception:
        pass

    sql = f"""
        SELECT
            CASE WHEN username != '' AND username IS NOT NULL
                 THEN username ELSE display_name END AS owner_key,
            display_name,
            username,
            COUNT(*) AS nft_count,
            GROUP_CONCAT(slug, ', ') AS slugs,
            MIN(slug) AS first_slug,
            collection,
            model,
            backdrop,
            symbol
        FROM nft_owners
        WHERE collection IN ({placeholders}) AND {country_pred}{rating_clause}
        GROUP BY owner_key
        ORDER BY RANDOM()
        LIMIT ?
    """
    params = list(collections)
    if extra_param is not None:
        params.append(extra_param)
    params.extend(rating_params)
    params.append(limit)
    cur = await db.execute(sql, params)
    results = []
    async for row in cur:
        d = dict(row)
        slugs_list = d.get("slugs", "").split(", ")
        d["slugs_short"] = slugs_list[:3]
        d["slugs_total"] = len(slugs_list)
        results.append(d)
    return results


# ── Кросс-коллекционный подсчёт NFT ──────────

async def get_total_nft_counts(owner_keys: list[str]) -> dict[str, int]:
    """Общее кол-во NFT по ВСЕМ коллекциям для каждого owner_key."""
    if not owner_keys:
        return {}
    db = await _get_db()
    placeholders = ", ".join("?" for _ in owner_keys)
    sql = f"""
        SELECT owner_key, COUNT(*) AS total
        FROM (
            SELECT CASE WHEN username != '' AND username IS NOT NULL
                        THEN username ELSE display_name END AS owner_key
            FROM nft_owners
        )
        WHERE owner_key IN ({placeholders})
        GROUP BY owner_key
    """
    cur = await db.execute(sql, owner_keys)
    result = {}
    async for row in cur:
        result[row["owner_key"]] = row["total"]
    return result


# ── Просмотренные юзеры (дедупликация) ───────

async def mark_viewed(chat_id: int, owner_keys: list[str]):
    """Помечает юзеров как просмотренных."""
    if not owner_keys:
        return
    db = await _get_db()
    await db.executemany(
        "INSERT OR IGNORE INTO viewed_users (chat_id, owner_key) VALUES (?, ?)",
        [(chat_id, k) for k in owner_keys],
    )
    await db.commit()


async def get_viewed_keys(chat_id: int) -> set[str]:
    """Множество owner_key уже просмотренных юзеров."""
    db = await _get_db()
    cur = await db.execute(
        "SELECT owner_key FROM viewed_users WHERE chat_id = ?", (chat_id,),
    )
    return {row["owner_key"] async for row in cur}


async def clear_viewed(chat_id: int):
    """Сброс просмотренных (для кнопки «Сбросить»)."""
    db = await _get_db()
    await db.execute("DELETE FROM viewed_users WHERE chat_id = ?", (chat_id,))
    await db.commit()


# ── Обновление has_russian через bio ─────────

async def update_russian_flag(slug: str, value: int = 1):
    db = await _get_db()
    await db.execute(
        "UPDATE nft_owners SET has_russian = ? WHERE slug = ?", (value, slug)
    )
    await db.commit()


async def update_detected_country(slug: str, country: str):
    """Проставить detected_country (bio-обогащение для любой страны)."""
    db = await _get_db()
    await db.execute(
        "UPDATE nft_owners SET detected_country = ? WHERE slug = ?", (country, slug)
    )
    await db.commit()


async def get_owners_no_country(collection: str, limit: int = 300) -> list[dict]:
    """Владельцы без определённой страны (detected_country пуст), с username —
    кандидаты для bio-обогащения по всем 12 странам."""
    db = await _get_db()
    cur = await db.execute(
        """SELECT DISTINCT username, display_name, MIN(slug) AS slug
           FROM nft_owners
           WHERE collection = ?
                 AND (detected_country IS NULL OR detected_country = '')
                 AND has_russian = 0 AND has_chinese = 0
                 AND username != '' AND username IS NOT NULL
           GROUP BY username
           LIMIT ?""",
        (collection, limit),
    )
    return [dict(row) async for row in cur]


async def get_non_russian_with_usernames(collection: str, limit: int = 300) -> list[dict]:
    """NFT-владельцы без русского флага, у которых есть username (для bio-проверки)."""
    db = await _get_db()
    cur = await db.execute(
        """SELECT DISTINCT username, display_name, MIN(slug) AS slug
           FROM nft_owners
           WHERE collection = ? AND has_russian = 0 AND has_chinese = 0
                 AND username != '' AND username IS NOT NULL
           GROUP BY username
           LIMIT ?""",
        (collection, limit),
    )
    return [dict(row) async for row in cur]


async def update_rating_level(slug: str, level: int | None):
    db = await _get_db()
    await db.execute(
        "UPDATE nft_owners SET rating_level = ? WHERE slug = ?",
        (level, slug),
    )
    await db.commit()


async def update_rating_for_username(username: str, level: int | None):
    db = await _get_db()
    await db.execute(
        "UPDATE nft_owners SET rating_level = ? WHERE LOWER(username) = LOWER(?)",
        (level, username),
    )
    await db.commit()


async def update_ratings_batch(ratings: dict[str, int | None]):
    if not ratings:
        return
    db = await _get_db()
    await db.executemany(
        "UPDATE nft_owners SET rating_level = ? WHERE LOWER(username) = LOWER(?)",
        [(level, username) for username, level in ratings.items()],
    )
    await db.commit()


async def get_rating_candidates(collection: str, country: str, limit: int = 1000) -> list[dict]:
    db = await _get_db()
    if country == "cn":
        pred = "(has_chinese = 1 OR detected_country = 'cn')"
        params = [collection]
    elif country == "ru":
        pred = "(has_russian = 1 OR detected_country = 'ru')"
        params = [collection]
    else:
        pred = "detected_country = ?"
        params = [collection, country]
    cur = await db.execute(
        f"""SELECT username, MIN(slug) AS slug
            FROM nft_owners
            WHERE collection = ? AND {pred}
              AND username IS NOT NULL AND username != ''
            GROUP BY username LIMIT ?""",
        (*params, limit),
    )
    return [dict(row) async for row in cur]


# ── Избранное ────────────────────────────────

async def add_favorite(
    chat_id: int, display_name: str, username: str,
    collection: str, slug: str,
) -> bool:
    db = await _get_db()
    try:
        await db.execute(
            """INSERT OR REPLACE INTO favorites
               (chat_id, display_name, username, collection, slug)
               VALUES (?, ?, ?, ?, ?)""",
            (chat_id, display_name, username or "", collection, slug),
        )
        await db.commit()
        return True
    except Exception:
        return False


async def remove_favorite(chat_id: int, fav_id: int) -> bool:
    db = await _get_db()
    await db.execute(
        "DELETE FROM favorites WHERE id = ? AND chat_id = ?",
        (fav_id, chat_id),
    )
    await db.commit()
    return True


async def get_favorites(chat_id: int) -> list[dict]:
    db = await _get_db()
    cur = await db.execute(
        "SELECT * FROM favorites WHERE chat_id = ? ORDER BY added_at DESC",
        (chat_id,),
    )
    return [dict(row) async for row in cur]


# ── Зеркала ──────────────────────────────────

async def add_mirror(owner_id: int, bot_token: str, bot_username: str) -> bool:
    db = await _get_db()
    try:
        await db.execute(
            "INSERT INTO mirrors (owner_id, bot_token, bot_username) VALUES (?, ?, ?)",
            (owner_id, bot_token, bot_username),
        )
        await db.commit()
        return True
    except Exception:
        return False


async def remove_mirror(mirror_id: int) -> bool:
    db = await _get_db()
    await db.execute("DELETE FROM mirrors WHERE id = ?", (mirror_id,))
    await db.commit()
    return True


async def get_all_mirrors() -> list[dict]:
    db = await _get_db()
    cur = await db.execute("SELECT * FROM mirrors ORDER BY created_at")
    return [dict(row) async for row in cur]


async def get_mirror_by_token(token: str) -> dict | None:
    db = await _get_db()
    cur = await db.execute("SELECT * FROM mirrors WHERE bot_token = ?", (token,))
    row = await cur.fetchone()
    return dict(row) if row else None


# ── Юзеры бота ───────────────────────────────

async def register_user(
    chat_id: int, bot_token: str = "", username: str = "", display_name: str = ""
):
    db = await _get_db()
    await db.execute(
        """INSERT INTO bot_users (chat_id, bot_token, username, display_name)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(chat_id) DO UPDATE SET
             bot_token = excluded.bot_token,
             username = excluded.username,
             display_name = excluded.display_name""",
        (chat_id, bot_token, username or "", display_name or ""),
    )
    await db.commit()


async def get_all_users_admin() -> list[dict]:
    db = await _get_db()
    cur = await db.execute(
        "SELECT chat_id, username, display_name, bot_token FROM bot_users ORDER BY rowid DESC"
    )
    return [dict(row) async for row in cur]


async def get_all_chat_ids_by_token(bot_token: str = "") -> list[int]:
    """Юзеры конкретного бота (зеркала)."""
    db = await _get_db()
    cur = await db.execute(
        "SELECT chat_id FROM bot_users WHERE bot_token = ?", (bot_token,),
    )
    return [row["chat_id"] async for row in cur]


async def get_all_chat_ids_global() -> list[int]:
    """Все юзеры всех ботов (для глобальной рассылки)."""
    db = await _get_db()
    cur = await db.execute("SELECT DISTINCT chat_id FROM bot_users")
    return [row["chat_id"] async for row in cur]


# ── Статистика ───────────────────────────────

async def get_cache_stats() -> dict:
    db = await _get_db()
    cur = await db.execute("""
        SELECT collection,
               COUNT(*) as total_scanned,
               SUM(has_chinese) as chinese_found,
               SUM(has_russian) as russian_found
        FROM nft_owners
        GROUP BY collection
    """)
    stats = {}
    async for row in cur:
        stats[row["collection"]] = {
            "total_scanned": row["total_scanned"],
            "chinese_found": row["chinese_found"] or 0,
            "russian_found": row["russian_found"] or 0,
        }
    return stats


# ── Админ-функции ──

async def get_all_chat_ids() -> list[int]:
    """Все юзеры бота (из bot_users + viewed_users)."""
    db = await _get_db()
    cur = await db.execute(
        "SELECT DISTINCT chat_id FROM bot_users "
        "UNION "
        "SELECT DISTINCT chat_id FROM viewed_users"
    )
    return [row["chat_id"] async for row in cur]


async def get_global_stats() -> dict:
    """Общая статистика для админки."""
    db = await _get_db()
    r1 = await db.execute("SELECT COUNT(*) AS cnt FROM nft_owners")
    total_nfts = (await r1.fetchone())["cnt"]

    r2 = await db.execute("""
        SELECT COUNT(*) AS cnt FROM (
            SELECT CASE WHEN username != '' AND username IS NOT NULL
                        THEN username ELSE display_name END AS ok
            FROM nft_owners WHERE has_chinese = 1 GROUP BY ok
        )
    """)
    cn_users = (await r2.fetchone())["cnt"]

    r3 = await db.execute("""
        SELECT COUNT(*) AS cnt FROM (
            SELECT CASE WHEN username != '' AND username IS NOT NULL
                        THEN username ELSE display_name END AS ok
            FROM nft_owners WHERE has_russian = 1 GROUP BY ok
        )
    """)
    ru_users = (await r3.fetchone())["cnt"]

    r4 = await db.execute("SELECT COUNT(*) AS cnt FROM bot_users")
    bot_users = (await r4.fetchone())["cnt"]

    r5 = await db.execute("SELECT COUNT(*) AS cnt FROM mirrors")
    total_mirrors = (await r5.fetchone())["cnt"]

    return {
        "total_nfts": total_nfts,
        "cn_users": cn_users,
        "ru_users": ru_users,
        "bot_users": bot_users,
        "total_mirrors": total_mirrors,
    }


async def clear_all_viewed():
    """Очистить ВСЕ просмотренные юзеры (для всех чатов)."""
    db = await _get_db()
    await db.execute("DELETE FROM viewed_users")
    await db.commit()


async def clear_cache():
    """Полная очистка кэша NFT."""
    db = await _get_db()
    await db.execute("DELETE FROM nft_owners")
    await db.execute("DELETE FROM collection_sizes")
    await db.commit()


# ── Шаблоны сообщений ────────────────────────

async def get_template(chat_id: int, country: str) -> str | None:
    """Получить шаблон сообщения для страны."""
    db = await _get_db()
    cur = await db.execute(
        "SELECT text FROM user_templates WHERE chat_id = ? AND country = ?",
        (chat_id, country),
    )
    row = await cur.fetchone()
    return row["text"] if row else None


async def set_template(chat_id: int, country: str, text: str) -> bool:
    """Сохранить / обновить шаблон."""
    db = await _get_db()
    await db.execute(
        "INSERT OR REPLACE INTO user_templates (chat_id, country, text) VALUES (?, ?, ?)",
        (chat_id, country, text),
    )
    await db.commit()
    return True


async def delete_template(chat_id: int, country: str):
    db = await _get_db()
    await db.execute(
        "DELETE FROM user_templates WHERE chat_id = ? AND country = ?",
        (chat_id, country),
    )
    await db.commit()


# ── Неулучшенные подарки ─────────────────────

async def get_non_upgraded_users(
    collection: str,
    country: str = "cn",
    limit: int = 200,
) -> list[dict]:
    """
    Владельцы неулучшенных подарков (model и backdrop пустые).
    """
    db = await _get_db()
    flag_col = "has_chinese" if country == "cn" else "has_russian"

    sql = f"""
        SELECT
            CASE WHEN username != '' AND username IS NOT NULL
                 THEN username ELSE display_name END AS owner_key,
            display_name,
            username,
            COUNT(*) AS nft_count,
            GROUP_CONCAT(slug, ', ') AS slugs,
            MIN(slug) AS first_slug,
            collection,
            model,
            backdrop,
            symbol
        FROM nft_owners
        WHERE collection = ? AND {flag_col} = 1
              AND (model = '' OR model IS NULL)
              AND (backdrop = '' OR backdrop IS NULL)
        GROUP BY owner_key
        ORDER BY nft_count ASC
        LIMIT ?
    """
    cur = await db.execute(sql, (collection, limit))
    results = []
    async for row in cur:
        d = dict(row)
        slugs_list = d.get("slugs", "").split(", ")
        d["slugs_short"] = slugs_list[:3]
        d["slugs_total"] = len(slugs_list)
        results.append(d)
    return results


async def get_non_upgraded_random_multi(
    collections: list[str],
    country: str = "cn",
    limit: int = 200,
) -> list[dict]:
    """Неулучшенные из нескольких коллекций (рандомный)."""
    db = await _get_db()
    flag_col = "has_chinese" if country == "cn" else "has_russian"
    placeholders = ", ".join("?" for _ in collections)

    sql = f"""
        SELECT
            CASE WHEN username != '' AND username IS NOT NULL
                 THEN username ELSE display_name END AS owner_key,
            display_name,
            username,
            COUNT(*) AS nft_count,
            GROUP_CONCAT(slug, ', ') AS slugs,
            MIN(slug) AS first_slug,
            collection,
            model,
            backdrop,
            symbol
        FROM nft_owners
        WHERE collection IN ({placeholders}) AND {flag_col} = 1
              AND (model = '' OR model IS NULL)
              AND (backdrop = '' OR backdrop IS NULL)
        GROUP BY owner_key
        ORDER BY RANDOM()
        LIMIT ?
    """
    params = list(collections) + [limit]
    cur = await db.execute(sql, params)
    results = []
    async for row in cur:
        d = dict(row)
        slugs_list = d.get("slugs", "").split(", ")
        d["slugs_short"] = slugs_list[:3]
        d["slugs_total"] = len(slugs_list)
        results.append(d)
    return results
