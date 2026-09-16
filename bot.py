"""
NFT Scanner V8 — Telegram-бот для поиска владельцев NFT-подарков.

Фичи: 🇨🇳 Китай / 🇷🇺 Россия, 👩 девушки, 🛒 маркет TG,
       кулдаун, оригинальные владельцы, мини-апп, зеркала,
       TGP эмодзи, peek.tg API, дедупликация, шаблоны.
"""
import asyncio
import csv
import html
import io
import json
import logging
import math
import os
import re
import time
import urllib.parse
from difflib import get_close_matches
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardButton, InlineKeyboardMarkup,
    
    BufferedInputFile, WebAppInfo, ContentType,
)
from aiogram.enums import ParseMode
from aiohttp import web

from config import (
    BOT_TOKEN, BOT_USERNAME, ADMIN_IDS,
    REQUIRED_CHANNEL_ID, REQUIRED_CHANNEL_LINK,
    GIFTS_PER_PAGE, MODELS_PER_PAGE, BACKDROPS_PER_PAGE,
    PROGRESS_UPDATE_INTERVAL, TOTAL_RESULTS, RESULTS_PER_PAGE,
    NFT_COUNT_RANGES, DEFAULT_NFT_RANGE, MAX_NFT_HARD_CAP,
    CSV_DIR, TELEGRAM_API_SERVER, WEBAPP_URL,
)
from gifts_constants import ALL_COLLECTIONS, COLLECTION_MODELS, COLLECTION_BACKDROPS
from emoji_ids import (
    E_WELCOME, E_MINIAPP, E_TEMPLATES, E_MIRROR,
    E_PARSING, E_COUNT, E_GIFT, E_MENU, E_EXPORT, E_FOUND,
    E_BLOCKQUOTE,
)
import db
import scanner as nft_scanner
import peek_api
import gender_api
import nationality_api
import rating_api
from chinese_detector import (
    is_chinese_name, is_russian_name, is_bot_or_shop,
    COUNTRY_DETECTORS, COUNTRY_FLAGS, COUNTRY_LABELS,
    detect_country,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Bot init ──
if TELEGRAM_API_SERVER:
    _session = AiohttpSession(api=TelegramAPIServer.from_base(TELEGRAM_API_SERVER))
    bot = Bot(token=BOT_TOKEN, session=_session)
    logger.info("Custom API server: %s", TELEGRAM_API_SERVER)
else:
    bot = Bot(token=BOT_TOKEN)

dp = Dispatcher()
router = Router()
dp.include_router(router)

# ── Глобальное состояние ──
active_scans: dict[int, asyncio.Event] = {}
user_state: dict[int, dict] = {}
# token → Bot  (основной + зеркала)
mirror_bots: dict[str, Bot] = {}
mirror_tasks: dict[str, asyncio.Task] = {}  # token → polling task


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  TGP Emoji helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def tge(emoji_id: str, fallback: str = "✨") -> str:
    """Telegram Premium emoji tag."""
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Утилиты
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_RE_CAMEL = re.compile(r'(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])')


def _d(collection: str) -> str:
    """CamelCase → Human Readable."""
    return _RE_CAMEL.sub(' ', collection)


def _bar(cur: int, tot: int, w: int = 14) -> str:
    if tot <= 0:
        return "░" * w
    f = min(w, int(w * cur / tot))
    return "▓" * f + "░" * (w - f)


def _btn(text: str, data: str, icon: str = None) -> InlineKeyboardButton:
    kwargs = dict(text=text, callback_data=data)
    if icon:
        kwargs["icon_custom_emoji_id"] = icon
    return InlineKeyboardButton(**kwargs)


def _url_btn(text: str, url: str, icon: str = None) -> InlineKeyboardButton:
    kwargs = dict(text=text, url=url)
    if icon:
        kwargs["icon_custom_emoji_id"] = icon
    return InlineKeyboardButton(**kwargs)


def _fuzzy(query: str, options: list[str], n: int = 20) -> list[str]:
    q = query.lower().strip()
    if not q:
        return options[:n]
    sub = [o for o in options if q in o.lower() or q in _d(o).lower()]
    if sub:
        return sub[:n]
    dm = {_d(o).lower(): o for o in options}
    dm.update({o.lower(): o for o in options})
    close = get_close_matches(q, list(dm.keys()), n=n, cutoff=0.35)
    return [dm[c] for c in close]


def _st(cid: int) -> dict:
    """Получить / создать состояние юзера."""
    if cid not in user_state:
        user_state[cid] = {}
    return user_state[cid]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Подписка на канал
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _check_sub(user_id: int, cur_bot: Bot | None = None) -> bool:
    """Проверяет подписку пользователя на канал."""
    try:
        b = cur_bot or bot
        member = await b.get_chat_member(REQUIRED_CHANNEL_ID, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return True


def _kb_subscribe() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_url_btn("Подписаться на канал", REQUIRED_CHANNEL_LINK)],
        [_btn("Я подписался", "sub_check")],
    ])


MSG_SUBSCRIBE = (
    "🔒 <b>Подпишись на канал</b>\n"
    "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
    "Для доступа к боту нужна подписка 👇"
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Клавиатуры
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def kb_main_inline(bot_username: str = BOT_USERNAME) -> InlineKeyboardMarkup:
    """Inline-кнопки (парсинг, мини апп, шаблоны, справка)."""
    webapp_url = f"{WEBAPP_URL}?bot={bot_username}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("🔍 Парсинг", "parsing_menu")],
        [InlineKeyboardButton(
            text="🎁 Открыть мини апп",
            web_app=WebAppInfo(url=webapp_url),
        )],
        [_btn("Шаблоны", "templates", icon=E_TEMPLATES),
         _btn("Справка", "help")],
        [_btn("Создать зеркало", "mirror", icon=E_MIRROR)],
    ])


_bot_username_cache: dict[str, str] = {}

async def _get_bot_username(cur_bot: Bot) -> str:
    """Получить username текущего бота (основной или зеркало), с кэшем."""
    token = cur_bot.token
    if token in _bot_username_cache:
        return _bot_username_cache[token]
    try:
        me = await cur_bot.get_me()
        uname = me.username or BOT_USERNAME
        _bot_username_cache[token] = uname
        return uname
    except Exception:
        return BOT_USERNAME


def kb_country() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("🌍 Все", "cnt:all")],
        [_btn("🇨🇳 Китай", "cnt:cn"), _btn("🇷🇺 Россия", "cnt:ru")],
        [_btn("🇺🇿 Узбекистан", "cnt:uz"), _btn("🇰🇿 Казахстан", "cnt:kz")],
        [_btn("🇰🇬 Киргизия", "cnt:kg"), _btn("🇹🇯 Таджикистан", "cnt:tj")],
        [_btn("🇮🇳 Индия", "cnt:in"), _btn("🇸🇦 Арабы", "cnt:ar")],
        [_btn("🇮🇩 Индонезия", "cnt:id"), _btn("🇯🇵 Япония", "cnt:jp")],
        [_btn("🇰🇷 Корея", "cnt:kr"), _btn("🇹🇷 Турция", "cnt:tr")],
        [_btn("Назад", "home")],
    ])


def kb_nft_count() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row = []
    for code, (_, _, label) in NFT_COUNT_RANGES.items():
        row.append(_btn(label, f"nft:{code}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([_btn("Назад", "back_bd"), _btn("В Меню", "home", icon=E_MENU)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_gifts(page: int = 0, query: str = "") -> InlineKeyboardMarkup:
    colls = _fuzzy(query, ALL_COLLECTIONS) if query else ALL_COLLECTIONS
    pages = max(1, math.ceil(len(colls) / GIFTS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    chunk = colls[page * GIFTS_PER_PAGE:(page + 1) * GIFTS_PER_PAGE]

    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(chunk), 2):
        row = [_btn(f"🎁 {_d(chunk[i])}", f"g:{chunk[i]}")]
        if i + 1 < len(chunk):
            row.append(_btn(f"🎁 {_d(chunk[i+1])}", f"g:{chunk[i+1]}"))
        rows.append(row)
    nav = []
    if page > 0:
        nav.append(_btn("◀️", f"gp:{page-1}"))
    nav.append(_btn(f"· {page+1}/{pages} ·", "noop"))
    if page < pages - 1:
        nav.append(_btn("▶️", f"gp:{page+1}"))
    rows.append(nav)
    rows.append([_btn("Искать коллекцию", "srch:g")])
    rows.append([_btn("В Меню", "home", icon=E_MENU)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_models(coll: str, page: int = 0, query: str = "") -> InlineKeyboardMarkup:
    models = _fuzzy(query, COLLECTION_MODELS.get(coll, [])) if query else COLLECTION_MODELS.get(coll, [])
    pages = max(1, math.ceil(len(models) / MODELS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    chunk = models[page * MODELS_PER_PAGE:(page + 1) * MODELS_PER_PAGE]

    rows: list[list[InlineKeyboardButton]] = []
    if page == 0 and not query:
        rows.append([_btn("✦ Любая модель", f"m:{coll}:*")])
    for i in range(0, len(chunk), 2):
        row = [_btn(chunk[i], f"m:{coll}:{chunk[i]}")]
        if i + 1 < len(chunk):
            row.append(_btn(chunk[i+1], f"m:{coll}:{chunk[i+1]}"))
        rows.append(row)
    if pages > 1 and not query:
        nav = []
        if page > 0:
            nav.append(_btn("◀️", f"mp:{coll}:{page-1}"))
        nav.append(_btn(f"· {page+1}/{pages} ·", "noop"))
        if page < pages - 1:
            nav.append(_btn("▶️", f"mp:{coll}:{page+1}"))
        rows.append(nav)
    rows.append([_btn("Искать модель", f"srch:m:{coll}")])
    rows.append([_btn("Назад", "back_coll")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_backdrops(coll: str, model: str, page: int = 0, query: str = "") -> InlineKeyboardMarkup:
    bds = _fuzzy(query, COLLECTION_BACKDROPS.get(coll, [])) if query else COLLECTION_BACKDROPS.get(coll, [])
    pages = max(1, math.ceil(len(bds) / BACKDROPS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    chunk = bds[page * BACKDROPS_PER_PAGE:(page + 1) * BACKDROPS_PER_PAGE]

    rows: list[list[InlineKeyboardButton]] = []
    if page == 0 and not query:
        rows.append([_btn("✦ Любой фон", f"b:{coll}:{model}:*")])
    for i in range(0, len(chunk), 2):
        row = [_btn(chunk[i], f"b:{coll}:{model}:{chunk[i]}")]
        if i + 1 < len(chunk):
            row.append(_btn(chunk[i+1], f"b:{coll}:{model}:{chunk[i+1]}"))
        rows.append(row)
    if pages > 1 and not query:
        nav = []
        if page > 0:
            nav.append(_btn("◀️", f"bp:{coll}:{model}:{page-1}"))
        nav.append(_btn(f"· {page+1}/{pages} ·", "noop"))
        if page < pages - 1:
            nav.append(_btn("▶️", f"bp:{coll}:{model}:{page+1}"))
        rows.append(nav)
    rows.append([_btn("Искать фон", f"srch:b:{coll}:{model}")])
    rows.append([_btn("Назад", f"g:{coll}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_results(page: int, total_pages: int) -> InlineKeyboardMarkup:
    nav = []
    if page > 0:
        nav.append(_btn("◀️", f"pg:{page-1}"))
    nav.append(_btn(f"{page+1}/{total_pages}", "noop"))
    if page < total_pages - 1:
        nav.append(_btn("▶️", f"pg:{page+1}"))
    return InlineKeyboardMarkup(inline_keyboard=[
        nav,
        [_btn("Экспорт", "export", icon=E_EXPORT)],
        [_btn("Обновить", "refresh"), _btn("В Меню", "home", icon=E_MENU)],
    ])


def kb_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("В Меню", "home", icon=E_MENU)]])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Тексты
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def msg_welcome(user_name: str = "") -> str:
    return (
        f'{tge(E_WELCOME, "👋")} <b>Привет{", " + html.escape(user_name) if user_name else ""}!</b>\n\n'
        f'<blockquote>{tge(E_BLOCKQUOTE, "🎁")}Это бот для парсинга нфт подарков '
        'здесь много различных функций и удобный интерфейс, '
        'приятного использования!</blockquote>\n\n'
        'Выберите действие 👇'
    )


def msg_results_page(
    results: list[dict], page: int,
    total_found: int, total_pages: int,
    hidden_viewed: int, hidden_bots: int,
    template_text: str = "",
) -> str:
    start = page * RESULTS_PER_PAGE
    chunk = results[start:start + RESULTS_PER_PAGE]

    header = (
        f'{tge(E_FOUND, "✅")} <b>Найдено {total_found} NFT</b>\n\n'
    )

    lines = []
    for i, r in enumerate(chunk, start + 1):
        username = html.escape(r.get("username", ""))
        display = html.escape(r.get("display_name", "?"))
        slug = r.get("first_slug", r.get("nft_link", ""))

        # NFT link
        if slug and slug.startswith("http"):
            nft_link = slug
        elif slug:
            nft_link = f"https://t.me/nft/{slug}"
        else:
            nft_link = ""

        # Компактный формат: @username | NFT | Написать
        if username:
            user_str = f"@{username}"
        else:
            user_str = display

        nft_str = f'<a href="{nft_link}">NFT</a>' if nft_link else "NFT"

        write_link = ""
        if username:
            if template_text:
                encoded = urllib.parse.quote(template_text)
                write_link = f' | <a href="https://t.me/{username}?text={encoded}">Написать</a>'
            else:
                write_link = f' | <a href="https://t.me/{username}">Написать</a>'

        lines.append(f"<b>{i}.</b> {user_str} | {nft_str}{write_link}")

    body = "\n".join(lines)

    footer_parts = []
    if hidden_viewed > 0:
        footer_parts.append(f"👁 <i>Скрыто: {hidden_viewed} просм.</i>")
    if hidden_bots > 0:
        footer_parts.append(f"🤖 <i>Скрыто: {hidden_bots} ботов</i>")
    footer = "  ".join(footer_parts)
    if footer:
        footer = "\n\n" + footer

    return header + body + footer


def _format_numbers_page(
    results: list[dict], page: int, total_pages: int,
) -> str:
    """Красивое форматирование страницы +888 номеров."""
    start = page * RESULTS_PER_PAGE
    chunk = results[start:start + RESULTS_PER_PAGE]

    header = f'{tge(E_FOUND, "✅")} <b>Найдено {len(results)} номеров +888</b>\n\n'

    lines = []
    for i, r in enumerate(chunk, start + 1):
        display = html.escape(r.get("display_name", ""))
        wallet = r.get("wallet_short", "")
        username = r.get("username", "")
        tme = r.get("tme_name", "")
        price = r.get("price", "")
        number = r.get("number", "")

        # Номер + цена
        line = f"<b>{i}.</b> 📞 <b>{display}</b>"
        if price:
            line += f"  ·  {price} TON"

        # Владелец (юзернейм)
        if username:
            line += f"\n     👤 <a href=\"https://t.me/{html.escape(username)}\">@{html.escape(username)}</a>"
        elif tme and not tme.endswith(".ton"):
            clean_tme = tme.replace(".t.me", "")
            line += f"\n     👤 <a href=\"https://t.me/{html.escape(clean_tme)}\">@{html.escape(clean_tme)}</a>"

        # Кошелёк
        if wallet:
            line += f"\n     💎 <code>{html.escape(wallet)}</code>"

        # Ссылка на Fragment
        line += f"\n     🔗 <a href=\"https://fragment.com/number/{number}\">Fragment</a>"

        lines.append(line)

    body = "\n\n".join(lines)  # Пустая строка между записями

    page_info = ""
    if total_pages > 1:
        page_info = f"\n\n📄 Стр. {page + 1}/{total_pages}"

    return header + body + page_info


def _format_nft_usernames_page(
    results: list[dict], page: int, total_pages: int,
) -> str:
    """Красивое форматирование страницы NFT юзернеймов."""
    start = page * RESULTS_PER_PAGE
    chunk = results[start:start + RESULTS_PER_PAGE]

    header = (
        f'{tge(E_FOUND, "✅")} <b>Найдено {len(results)} NFT юзернеймов</b>\n'
        '🏷 ≤30 TON · привязанные к TG\n\n'
    )

    lines = []
    for i, r in enumerate(chunk, start + 1):
        uname = html.escape(r.get("username", ""))
        wallet = r.get("wallet_short", "")
        tme = r.get("tme_name", "")
        price = r.get("price_ton")
        marketplace = r.get("marketplace", "")

        # Юзернейм + цена
        line = f"<b>{i}.</b> 🏷 <b>@{uname}</b>"
        if price is not None:
            line += f"  ·  {price} TON"
        if marketplace:
            line += f" ({html.escape(marketplace)})"

        # Владелец
        if tme:
            clean = tme.replace(".t.me", "")
            line += f"\n     👤 <a href=\"https://t.me/{html.escape(clean)}\">{html.escape(clean)}</a>"

        # Кошелёк
        if wallet:
            line += f"\n     💎 <code>{html.escape(wallet)}</code>"

        # Ссылка на Fragment
        line += f'\n     🔗 <a href="https://fragment.com/username/{uname}">Fragment</a>'

        lines.append(line)

    body = "\n\n".join(lines)

    page_info = ""
    if total_pages > 1:
        page_info = f"\n\n📄 Стр. {page + 1}/{total_pages}"

    return header + body + page_info


def _format_non_upgraded_page(
    results: list[dict], page: int, total_pages: int,
) -> str:
    """Красивое форматирование страницы неулучшенных подарков."""
    start = page * RESULTS_PER_PAGE
    chunk = results[start:start + RESULTS_PER_PAGE]

    header = f'{tge(E_FOUND, "✅")} <b>Найдено {len(results)} с неулучшенными</b>\n\n'

    lines = []
    for i, r in enumerate(chunk, start + 1):
        uname = html.escape(r.get("username", ""))
        display = html.escape(r.get("display_name", ""))
        nug_count = r.get("non_upgraded_count", 0)
        total_g = r.get("total_gifts", 0)

        line = f"<b>{i}.</b> 🎁 <b>@{uname}</b>"
        if display and display != uname:
            line += f" ({display})"
        line += f"\n     📦 Неулучш: <b>{nug_count}</b> / Всего: {total_g}"
        line += f'\n     ✉️ <a href="https://t.me/{uname}">Написать</a>'

        lines.append(line)

    body = "\n\n".join(lines)

    page_info = ""
    if total_pages > 1:
        page_info = f"\n\n📄 Стр. {page + 1}/{total_pages}"

    return header + body + page_info


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Команды
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.message(CommandStart())
async def cmd_start(message: Message):
    cid = message.chat.id

    # Регистрация юзера
    cur_bot: Bot = message.bot
    await db.register_user(
        cid,
        cur_bot.token,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )

    # ── Deep link от мини аппа ──
    text = message.text or ""
    parts = text.split(maxsplit=1)
    deep_link = parts[1].strip() if len(parts) > 1 else ""

    if deep_link:
        if not await _check_sub(message.from_user.id, message.bot):
            await message.answer(MSG_SUBSCRIBE, reply_markup=_kb_subscribe(), parse_mode=ParseMode.HTML)
            return
        await _handle_deep_link(message, deep_link)
        return

    # ── Обычный /start ──
    user_state.pop(cid, None)

    if not await _check_sub(message.from_user.id, message.bot):
        await message.answer(MSG_SUBSCRIBE, reply_markup=_kb_subscribe(), parse_mode=ParseMode.HTML)
        return

    name = message.from_user.first_name or ""
    uname = await _get_bot_username(message.bot)
    await message.answer(
        msg_welcome(name), reply_markup=kb_main_inline(uname), parse_mode=ParseMode.HTML,
    )


def _parse_action_modifiers(action: str) -> tuple[str, str | None, bool]:
    """
    Парсит модификаторы действия.
    'girlscn' → ('girls', 'chinese', False)
    'cdfreeru' → ('cdfree', 'russian', False)
    'randomorig' → ('random', None, True)
    'randomcnorig' → ('random', 'chinese', True)
    """
    orig = False
    if action.endswith("orig"):
        orig = True
        action = action[:-4]

    country = None
    # Проверяем все 2-буквенные коды стран (cn, ru, jp, kr, id, in, ar, uz, kz, kg, tj, tr)
    _country_codes = {"cn": "cn", "ru": "ru", "jp": "jp", "kr": "kr",
                      "id": "id", "in": "in", "ar": "ar", "uz": "uz",
                      "kz": "kz", "kg": "kg", "tj": "tj", "tr": "tr"}
    if len(action) >= 2 and action[-2:] in _country_codes:
        country = action[-2:]
        action = action[:-2]
    elif action.endswith("all"):
        country = None
        action = action[:-3]

    return action, country, orig


async def _handle_deep_link(message: Message, dl: str):
    """Обработка deep link от мини аппа: action__GiftApiName."""
    cid = message.chat.id

    # Сохраняем для кнопки «Обновить»
    _st(cid)["_peek_dl"] = dl

    # Спец-действия без коллекции
    if dl in ("reset_viewed", "reset"):
        await db.clear_viewed(cid)
        await message.answer(
            "✅ <b>Просмотренные сброшены!</b>",
            parse_mode=ParseMode.HTML, reply_markup=kb_home(),
        )
        return

    # random (может быть с модификаторами: randomcn, randomorig, randomcnorig)
    if dl.startswith("random") and "__" not in dl:
        _, country, orig = _parse_action_modifiers(dl)
        await _do_peek_random_scan_all(message, country=country, original_only=orig)
        return

    # Комбо-поиск из мини-аппа: combo__<country>_<flags>__Gift1-Gift2-...
    #   country: 2-буквенный код или 'xx' (любая)
    #   flags: любые из g(irls)/o(rig)/m(arket), либо '0'
    if dl.startswith("combo__"):
        await _handle_combo_deep_link(message, dl)
        return

    # +888 номера (из мини-аппа)
    if dl == "numbers":
        await _do_fragment_numbers_scan(message)
        return

    # NFT юзернеймы (из мини-аппа) — автопарсинг
    if dl == "nft_usernames":
        await _do_nft_usernames_auto_scan(message)
        return

    # Формат: action__GiftApiName
    if "__" not in dl:
        await message.answer("❌ Неизвестная команда.")
        return

    raw_action, gift = dl.split("__", 1)
    action, country, orig = _parse_action_modifiers(raw_action)

    if action in ("chinese", "russian"):
        await _do_peek_country_scan(message, gift, action)
    elif action == "country" and country:
        await _do_peek_country_scan(message, gift, country)
    elif action == "girls":
        await _do_peek_girls_scan(message, gift, country=country)
    elif action == "original":
        await _do_peek_original_scan(message, gift)
    elif action in ("market", "marketall"):
        await _do_peek_market_scan(message, gift)
    elif action.startswith("market") and country:
        await _do_peek_market_country_scan(message, gift, country)
    elif action == "marketchinese":
        await _do_peek_market_country_scan(message, gift, "cn")
    elif action == "marketrussian":
        await _do_peek_market_country_scan(message, gift, "ru")
    elif action in ("cdfree",):
        await _do_peek_cooldown_scan(message, gift, "free", country=country)
    elif action in ("cdsoon",):
        await _do_peek_cooldown_scan(message, gift, "soon", country=country)
    elif action in ("cdactive",):
        await _do_peek_cooldown_scan(message, gift, "active", country=country)
    elif action == "nonupgraded":
        await _do_non_upgraded_scan(message, gift)
    else:
        await message.answer(f"❌ Неизвестное действие: {action}")


async def _handle_combo_deep_link(message: Message, dl: str):
    """Парсит combo-ссылку из мини-аппа и запускает комбо-скан.

    Формат: combo__<country>_<flags>__Gift1-Gift2-...
      <country> — 2 буквы (cn/ru/uz/…) или 'xx' = любая
      <flags>   — буквы g(irls)/o(rig)/m(arket) в любом порядке, или '0'
    """
    body = dl[len("combo__"):]
    if "__" not in body:
        await message.answer("❌ Пустой комбо-запрос.")
        return
    meta, gift_str = body.split("__", 1)
    parts = meta.split("_")
    raw_country = parts[0] if parts and parts[0] else "xx"
    flags = parts[1] if len(parts) > 1 else ""

    _codes = {"cn", "ru", "jp", "kr", "id", "in", "ar", "uz", "kz", "kg", "tj", "tr"}
    country = raw_country if raw_country in _codes else "any"

    girls = "g" in flags
    original = "o" in flags
    market = "m" in flags

    gifts = [g for g in gift_str.split("-") if g.strip()]
    if not gifts:
        await message.answer("❌ Не выбрано ни одной коллекции.")
        return

    await _do_peek_combo_scan(
        message, gifts=gifts, country=country,
        girls=girls, original=original, market=market,
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        '<b>Справка</b>\n'
        '┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n'
        f'{tge(E_GIFT, "📌")} <b>Команды</b>\n'
        '  /start — меню  ·  /stop — стоп скан\n'
        '  /reset — сброс просмотренных\n'
        '  /cancel — отмена ввода\n\n'
        '<b>Как пользоваться</b>\n'
        '  1. Открой <b>мини апп</b> из меню\n'
        '  2. Выбери коллекцию\n'
        '  3. Выбери фильтр (страна, девушки, маркет, кулдаун…)\n'
        '  4. Результаты придут в чат!\n\n'
        '<b>Фишки</b>\n'
        '  • Просмотренные не повторяются\n'
        '  • Боты и магазины фильтруются\n'
        f'  • {tge(E_MIRROR, "🪞")} Создай зеркало — свой бот!\n'
        f'  • {tge(E_EXPORT, "📥")} Экспорт в CSV'
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    stats = await db.get_cache_stats()
    if not stats:
        await message.answer(
            "📊 <b>Кэш пуст</b>\n\nЗапустите первый поиск!",
            parse_mode=ParseMode.HTML,
        )
        return
    lines = ["📊 <b>Статистика кэша</b>\n┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"]
    ta = cc = rc = 0
    for coll, d in sorted(stats.items()):
        ta += d["total_scanned"]
        cc += d["chinese_found"]
        rc += d["russian_found"]
        lines.append(
            f"🎁 {_d(coll)}\n"
            f"    📦 {d['total_scanned']:,}  🇨🇳 {d['chinese_found']}  🇷🇺 {d['russian_found']}"
        )
    lines.append(f"\n┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n📦 Итого: {ta:,} NFT · 🇨🇳 {cc} · 🇷🇺 {rc}")
    await message.answer(
        "\n".join(lines).replace(",", " "),
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("stop"))
async def cmd_stop(message: Message):
    ev = active_scans.get(message.chat.id)
    if ev:
        ev.set()
        await message.answer("⏹ Останавливаю…")
    else:
        await message.answer("Нет активного сканирования.")


@router.message(Command("reset"))
async def cmd_reset(message: Message):
    """Сброс просмотренных юзеров."""
    await db.clear_viewed(message.chat.id)
    await message.answer(
        "✅ <b>Просмотренные сброшены!</b>\n\n"
        "Теперь все юзеры снова будут показываться в результатах.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_home(),
    )


@router.message(Command("favs"))
async def cmd_favs(message: Message):
    await _show_favorites(message.chat.id, message)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Админ-панель
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def kb_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("Стата", "adm:stats"), _btn("Юзеры", "adm:users")],
        [_btn("Рассылка", "adm:broadcast"), _btn("Зеркала", "adm:mirrors")],
        [_btn("Просмотр.", "adm:clear_viewed"), _btn("Кэш", "adm:clear_cache")],
        [_btn("В Меню", "home", icon=E_MENU)],
    ])


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not _is_admin(message.from_user.id):
        await message.answer("⛔")
        return

    await message.answer(
        "🛠 <b>Админ-панель</b>",
        reply_markup=kb_admin(),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data == "adm:stats")
async def cb_adm_stats(cq: CallbackQuery):
    if not _is_admin(cq.from_user.id):
        await cq.answer("⛔", show_alert=True)
        return

    s = await db.get_global_stats()
    text = (
        "📊 <b>Статистика</b>\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
        f"📦 NFT в кэше  <b>{s['total_nfts']:,}</b>\n"
        f"🇨🇳 Китайцев  <b>{s['cn_users']:,}</b>\n"
        f"🇷🇺 Русских  <b>{s['ru_users']:,}</b>\n"
        f"👥 Юзеров  <b>{s['bot_users']}</b>\n"
        f"🪞 Зеркал  <b>{s['total_mirrors']}</b>"
    ).replace(",", " ")

    await cq.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_btn("Назад", "adm:back")],
        ]),
        parse_mode=ParseMode.HTML,
    )
    await cq.answer()


@router.callback_query(F.data == "adm:users")
async def cb_adm_users(cq: CallbackQuery):
    if not _is_admin(cq.from_user.id):
        await cq.answer("⛔", show_alert=True)
        return

    users = await db.get_all_users_admin()
    text = f"👥 <b>Юзеры</b>  ·  {len(users)}\n┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
    if users:
        for i, user in enumerate(users[:30], 1):
            cid = user.get("chat_id")
            username = (user.get("username") or "").strip().lstrip("@")
            display_name = html.escape(user.get("display_name") or "")
            if username:
                identity = f"@{html.escape(username)}"
            elif display_name:
                identity = display_name
            else:
                identity = "без username"
            text += f"<b>{i}.</b> {identity}  <code>{cid}</code>\n"
        if len(users) > 30:
            text += f"\n<i>+{len(users) - 30} ещё</i>"

    await cq.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_btn("Назад", "adm:back")],
        ]),
        parse_mode=ParseMode.HTML,
    )
    await cq.answer()


@router.callback_query(F.data == "adm:broadcast")
async def cb_adm_broadcast(cq: CallbackQuery):
    if not _is_admin(cq.from_user.id):
        await cq.answer("⛔ Нет доступа", show_alert=True)
        return

    st = _st(cq.message.chat.id)
    st["search_action"] = "admin_broadcast"

    await cq.message.edit_text(
        "📢 <b>Рассылка</b>\n┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
        "Введи текст (HTML).\n"
        "Отправится <b>всем</b> юзерам <b>всех</b> ботов.\n\n"
        "<i>/cancel — отмена</i>",
        parse_mode=ParseMode.HTML,
    )
    await cq.answer()


@router.callback_query(F.data == "adm:clear_viewed")
async def cb_adm_clear_viewed(cq: CallbackQuery):
    if not _is_admin(cq.from_user.id):
        await cq.answer("⛔ Нет доступа", show_alert=True)
        return

    await cq.message.edit_text(
        "🗑 <b>Очистить просмотренных?</b>\n\n"
        "Сброс для <b>всех</b> юзеров бота.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_btn("Да, очистить", "adm:clear_viewed_ok")],
            [_btn("Отмена", "adm:back")],
        ]),
        parse_mode=ParseMode.HTML,
    )
    await cq.answer()


@router.callback_query(F.data == "adm:clear_viewed_ok")
async def cb_adm_clear_viewed_ok(cq: CallbackQuery):
    if not _is_admin(cq.from_user.id):
        await cq.answer("⛔ Нет доступа", show_alert=True)
        return

    await db.clear_all_viewed()
    await cq.answer("✅ Просмотренные очищены!", show_alert=True)
    await cq.message.edit_text(
        "✅ Просмотренные сброшены.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_btn("Назад", "adm:back")],
        ]),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data == "adm:clear_cache")
async def cb_adm_clear_cache(cq: CallbackQuery):
    if not _is_admin(cq.from_user.id):
        await cq.answer("⛔ Нет доступа", show_alert=True)
        return

    await cq.message.edit_text(
        "💣 <b>Очистить кэш?</b>\n\n"
        "Удалит все NFT из кэша.\n"
        "Скан загрузит заново.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_btn("Да, очистить", "adm:clear_cache_ok")],
            [_btn("Отмена", "adm:back")],
        ]),
        parse_mode=ParseMode.HTML,
    )
    await cq.answer()


@router.callback_query(F.data == "adm:clear_cache_ok")
async def cb_adm_clear_cache_ok(cq: CallbackQuery):
    if not _is_admin(cq.from_user.id):
        await cq.answer("⛔ Нет доступа", show_alert=True)
        return

    await db.clear_cache()
    await cq.answer("✅ Кэш очищен!", show_alert=True)
    await cq.message.edit_text(
        "✅ Кэш очищен.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_btn("Назад", "adm:back")],
        ]),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data == "adm:back")
async def cb_adm_back(cq: CallbackQuery):
    if not _is_admin(cq.from_user.id):
        await cq.answer("⛔ Нет доступа", show_alert=True)
        return

    await cq.message.edit_text(
        "🛠 <b>Админ-панель</b>",
        reply_markup=kb_admin(),
        parse_mode=ParseMode.HTML,
    )
    await cq.answer()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Callback: подписка
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.callback_query(F.data == "sub_check")
async def cb_sub_check(cq: CallbackQuery):
    if not await _check_sub(cq.from_user.id, cq.bot):
        await cq.answer("❌ Вы ещё не подписались!", show_alert=True)
        return
    await cq.answer("✅ Подписка подтверждена!")
    name = cq.from_user.first_name or ""
    uname = await _get_bot_username(cq.bot)
    try:
        await cq.message.edit_text(
            msg_welcome(name), reply_markup=kb_main_inline(uname), parse_mode=ParseMode.HTML,
        )
    except Exception:
        await cq.message.answer(
            msg_welcome(name), reply_markup=kb_main_inline(uname), parse_mode=ParseMode.HTML,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Callback: навигация
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.callback_query(F.data == "noop")
async def cb_noop(cq: CallbackQuery):
    await cq.answer()


@router.callback_query(F.data == "home")
async def cb_home(cq: CallbackQuery):
    cid = cq.message.chat.id
    user_state.pop(cid, None)
    if not await _check_sub(cq.from_user.id, cq.bot):
        await cq.message.edit_text(
            MSG_SUBSCRIBE, reply_markup=_kb_subscribe(), parse_mode=ParseMode.HTML,
        )
        await cq.answer()
        return
    name = cq.from_user.first_name or ""
    uname = await _get_bot_username(cq.bot)
    try:
        await cq.message.edit_text(
            msg_welcome(name), reply_markup=kb_main_inline(uname), parse_mode=ParseMode.HTML,
        )
    except Exception:
        await cq.message.answer(
            msg_welcome(name), reply_markup=kb_main_inline(uname), parse_mode=ParseMode.HTML,
        )
    await cq.answer()


@router.callback_query(F.data == "help")
async def cb_help(cq: CallbackQuery):
    await cmd_help(cq.message)
    await cq.answer()


@router.callback_query(F.data == "stats")
async def cb_stats(cq: CallbackQuery):
    await cmd_stats(cq.message)
    await cq.answer()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Callback: Меню «Парсинг» (все виды)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def kb_parsing_menu() -> InlineKeyboardMarkup:
    """Подменю парсинга со всеми типами."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("🎛 Комбо-поиск", "kombo")],
        [_btn("🌍 По стране", "pmenu:country")],
        [_btn("👩 Девушки", "pmenu:girls"), _btn("🎲 Случайный", "pmenu:random")],
        [_btn("🆕 Оригинальные", "pmenu:original")],
        [_btn("🛒 Маркет TG", "pmenu:market"), _btn("⏰ Кулдаун", "pmenu:cooldown")],
        [_btn("📞 +888 номера", "pmenu:numbers"), _btn("🏷 NFT юзернеймы", "pmenu:nft_usernames")],
        [_btn("В Меню", "home", icon=E_MENU)],
    ])


@router.callback_query(F.data == "parsing_menu")
async def cb_parsing_menu(cq: CallbackQuery):
    if not await _check_sub(cq.from_user.id, cq.bot):
        await cq.message.edit_text(MSG_SUBSCRIBE, reply_markup=_kb_subscribe(), parse_mode=ParseMode.HTML)
        await cq.answer()
        return

    await cq.message.edit_text(
        "🔍 <b>Парсинг</b>\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
        "Выбери тип парсинга 👇\n\n"
        "🌍 — по стране владельца (12 стран)\n"
        "👩 — AI-определение пола\n"
        "🎲 — случайные из всех коллекций\n"
        "🆕 — никогда не передавались\n"
        "🛒 — выставлены на маркете TG\n"
        "⏰ — фильтр по кулдауну\n"
        "📞 — владельцы +888 номеров (Fragment)\n"
        "🏷 — NFT юзернеймы (TON блокчейн)",
        reply_markup=kb_parsing_menu(),
        parse_mode=ParseMode.HTML,
    )
    await cq.answer()


@router.callback_query(F.data.startswith("pmenu:"))
async def cb_pmenu(cq: CallbackQuery):
    """Обработка выбора типа парсинга из подменю."""
    action = cq.data.split(":")[1]
    st = _st(cq.message.chat.id)

    if action == "country":
        st["mode"] = "reg"
        await cq.message.edit_text(
            "🌍 <b>Выберите страну</b>\n"
            "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
            "Бот найдёт владельцев из выбранной страны",
            reply_markup=kb_country(),
            parse_mode=ParseMode.HTML,
        )
    elif action in ("chinese", "russian"):
        # Legacy — для обратной совместимости
        st["mode"] = "reg"
        st["country"] = "cn" if action == "chinese" else "ru"
        flag = "🇨🇳" if action == "chinese" else "🇷🇺"
        await cq.message.edit_text(
            f"🔍 <b>Выберите коллекцию</b>  {flag}\n"
            "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
            "🎁 Нажмите или 🔍 найдите по названию",
            reply_markup=kb_gifts(0),
            parse_mode=ParseMode.HTML,
        )
    elif action == "girls":
        st["_pmenu_action"] = "girls"
        await cq.message.edit_text(
            "👩 <b>AI-поиск девушек</b>\n"
            "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
            "🎁 Выберите коллекцию",
            reply_markup=kb_gifts_quick("girls"),
            parse_mode=ParseMode.HTML,
        )
    elif action == "random":
        # Рандомный — без выбора коллекции, сразу скан
        await _do_peek_random_scan_all(cq.message)
        await cq.answer()
        return
    elif action == "original":
        st["_pmenu_action"] = "original"
        await cq.message.edit_text(
            "🆕 <b>Оригинальные владельцы</b>\n"
            "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
            "Подарки, которые никогда не передавались\n\n"
            "🎁 Выберите коллекцию",
            reply_markup=kb_gifts_quick("original"),
            parse_mode=ParseMode.HTML,
        )
    elif action == "market":
        st["_pmenu_action"] = "market"
        await cq.message.edit_text(
            "🛒 <b>Маркет Telegram</b>\n"
            "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
            "Подарки выставленные на продажу за ⭐\n\n"
            "🎁 Выберите коллекцию",
            reply_markup=kb_gifts_quick("market"),
            parse_mode=ParseMode.HTML,
        )
    elif action == "cooldown":
        await cq.message.edit_text(
            "⏰ <b>Кулдаун-фильтр</b>\n"
            "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
            "Выберите статус кулдауна:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_btn("✅ Без кулдауна", "cd_menu:free")],
                [_btn("⏰ Скоро снятие", "cd_menu:soon")],
                [_btn("🔒 На кулдауне", "cd_menu:active")],
                [_btn("Назад", "parsing_menu")],
            ]),
            parse_mode=ParseMode.HTML,
        )
    elif action == "numbers":
        # +888 номера — сразу запускаем скан Fragment
        await _do_fragment_numbers_scan(cq.message)
        await cq.answer()
        return
    elif action == "nft_usernames":
        # NFT юзернеймы — автопарсинг из TON блокчейна
        await _do_nft_usernames_auto_scan(cq.message)
        await cq.answer()
        return
    elif action == "non_upgraded":
        # Неулучшенные — нужно сначала выбрать коллекцию
        await cq.message.edit_text(
            "🎁 <b>Неулучшенные подарки</b>\n"
            "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
            "Бот найдёт юзеров, у которых есть\n"
            "неулучшенные (обычные) подарки.\n\n"
            "Выбери коллекцию 👇",
            reply_markup=kb_gifts_quick("non_upgraded"),
            parse_mode=ParseMode.HTML,
        )
        await cq.answer()
        return

    await cq.answer()


def kb_gifts_quick(action: str) -> InlineKeyboardMarkup:
    """Клавиатура выбора коллекции для быстрого парсинга (из подменю)."""
    # Показываем первую страницу коллекций с callback-ом для быстрого действия
    chunk = ALL_COLLECTIONS[:GIFTS_PER_PAGE]
    pages = max(1, math.ceil(len(ALL_COLLECTIONS) / GIFTS_PER_PAGE))

    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(chunk), 2):
        row = [_btn(f"🎁 {_d(chunk[i])}", f"qg:{action}:{chunk[i]}")]
        if i + 1 < len(chunk):
            row.append(_btn(f"🎁 {_d(chunk[i+1])}", f"qg:{action}:{chunk[i+1]}"))
        rows.append(row)
    nav = [_btn(f"· 1/{pages} ·", "noop")]
    if pages > 1:
        nav.append(_btn("▶️", f"qgp:{action}:1"))
    rows.append(nav)
    rows.append([_btn("🔍 Искать", f"qgs:{action}")])
    rows.append([_btn("Назад", "parsing_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("qgp:"))
async def cb_qgp(cq: CallbackQuery):
    """Пагинация коллекций для быстрого парсинга."""
    parts = cq.data.split(":")
    action = parts[1]
    page = int(parts[2])
    pages = max(1, math.ceil(len(ALL_COLLECTIONS) / GIFTS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    chunk = ALL_COLLECTIONS[page * GIFTS_PER_PAGE:(page + 1) * GIFTS_PER_PAGE]

    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(chunk), 2):
        row = [_btn(f"🎁 {_d(chunk[i])}", f"qg:{action}:{chunk[i]}")]
        if i + 1 < len(chunk):
            row.append(_btn(f"🎁 {_d(chunk[i+1])}", f"qg:{action}:{chunk[i+1]}"))
        rows.append(row)
    nav = []
    if page > 0:
        nav.append(_btn("◀️", f"qgp:{action}:{page-1}"))
    nav.append(_btn(f"· {page+1}/{pages} ·", "noop"))
    if page < pages - 1:
        nav.append(_btn("▶️", f"qgp:{action}:{page+1}"))
    rows.append(nav)
    rows.append([_btn("🔍 Искать", f"qgs:{action}")])
    rows.append([_btn("Назад", "parsing_menu")])

    await cq.message.edit_reply_markup(
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await cq.answer()


@router.callback_query(F.data.startswith("qgs:"))
async def cb_qgs(cq: CallbackQuery):
    """Поиск коллекции для быстрого парсинга."""
    action = cq.data.split(":")[1]
    st = _st(cq.message.chat.id)
    st["search_action"] = f"qsearch_{action}"
    await cq.message.edit_text(
        "🔍 Введите название коллекции:",
        parse_mode=ParseMode.HTML,
    )
    await cq.answer()


@router.callback_query(F.data.startswith("qg:"))
async def cb_qg(cq: CallbackQuery):
    """Выбор коллекции → сразу запуск быстрого парсинга."""
    parts = cq.data.split(":", 2)
    action = parts[1]
    gift = parts[2]

    if action == "girls":
        await _do_peek_girls_scan(cq.message, gift)
    elif action == "original":
        await _do_peek_original_scan(cq.message, gift)
    elif action == "market":
        await _do_peek_market_scan(cq.message, gift)
    elif action == "non_upgraded":
        await _do_non_upgraded_scan(cq.message, gift)
    elif action.startswith("cd_"):
        cd_status = action.replace("cd_", "")
        await _do_peek_cooldown_scan(cq.message, gift, cd_status)
    await cq.answer()


@router.callback_query(F.data.startswith("cd_menu:"))
async def cb_cd_menu(cq: CallbackQuery):
    """Выбор статуса кулдауна → выбор коллекции."""
    cd_status = cq.data.split(":")[1]
    st = _st(cq.message.chat.id)
    st["_pmenu_action"] = f"cd_{cd_status}"
    labels = {"free": "✅ Без кулдауна", "soon": "⏰ Скоро снятие", "active": "🔒 На кулдауне"}
    await cq.message.edit_text(
        f"⏰ <b>{labels.get(cd_status, cd_status)}</b>\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
        "🎁 Выберите коллекцию",
        reply_markup=kb_gifts_quick(f"cd_{cd_status}"),
        parse_mode=ParseMode.HTML,
    )
    await cq.answer()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fragment: +888 номера
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

import fragment_api
import ton_api


# ══════════════════════════════════════════════
#  🎛 КОМБО-ПОИСК (мультивыбор NFT + несколько фильтров)
# ══════════════════════════════════════════════

_COMBO_COUNTRY_BTNS = [
    ("any", "🌍 Любая"), ("cn", "🇨🇳 Китай"), ("ru", "🇷🇺 Россия"),
    ("uz", "🇺🇿 Узбекистан"), ("kz", "🇰🇿 Казахстан"), ("kg", "🇰🇬 Киргизия"),
    ("tj", "🇹🇯 Таджикистан"), ("in", "🇮🇳 Индия"), ("ar", "🇸🇦 Арабы"),
    ("id", "🇮🇩 Индонезия"), ("jp", "🇯🇵 Япония"), ("kr", "🇰🇷 Корея"),
    ("tr", "🇹🇷 Турция"),
]


def _combo_state(cid: int) -> dict:
    st = _st(cid)
    if "combo" not in st or not isinstance(st.get("combo"), dict):
        st["combo"] = {"gifts": [], "country": "any", "girls": False,
                       "original": False, "market": False}
    return st["combo"]


def _combo_text(c: dict) -> str:
    gifts = c.get("gifts", [])
    country = c.get("country", "any")
    cflag = dict(_COMBO_COUNTRY_BTNS).get(country, "🌍 Любая")
    filt = []
    if c.get("girls"):
        filt.append("👩 девушки")
    if country and country != "any":
        filt.append(cflag)
    if c.get("original"):
        filt.append("🆕 не передавались")
    if c.get("market"):
        filt.append("🛒 на маркете")
    filt_str = ", ".join(filt) if filt else "—"
    if gifts:
        gl = ", ".join(_d(g) for g in gifts[:6])
        if len(gifts) > 6:
            gl += f" +{len(gifts) - 6}"
    else:
        gl = "—"
    return (
        "🎛 <b>Комбо-поиск</b>\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
        "Выбери <b>несколько NFT</b> и <b>несколько фильтров</b> — "
        "бот найдёт владельцев, подходящих под ВСЕ условия сразу.\n\n"
        f"🎁 <b>Коллекции ({len(gifts)}):</b> {html.escape(gl)}\n"
        f"🎯 <b>Фильтры:</b> {html.escape(filt_str)}\n\n"
        "Настрой ниже 👇"
    )


def kb_combo(c: dict) -> InlineKeyboardMarkup:
    def chk(on: bool) -> str:
        return "✅" if on else "▫️"
    country = c.get("country", "any")
    cflag = dict(_COMBO_COUNTRY_BTNS).get(country, "🌍 Любая")
    rows = [
        [_btn(f"🎁 Коллекции ({len(c.get('gifts', []))})", "kgift:open")],
        [_btn(f"{chk(c.get('girls'))} 👩 Девушки", "kf:girls")],
        [_btn(f"🌍 Страна: {cflag}", "kcnt:open")],
        [_btn(f"{chk(c.get('original'))} 🆕 Не передавались", "kf:original")],
        [_btn(f"{chk(c.get('market'))} 🛒 На маркете", "kf:market")],
        [_btn("🔍 Искать", "krun")],
        [_btn("Назад", "parsing_menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_combo_country() -> InlineKeyboardMarkup:
    rows, row = [], []
    for code, label in _COMBO_COUNTRY_BTNS:
        row.append(_btn(label, f"kcnt:{code}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([_btn("Назад", "kombo")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_combo_gifts(selected: list[str], page: int = 0) -> InlineKeyboardMarkup:
    pages = max(1, math.ceil(len(ALL_COLLECTIONS) / GIFTS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    chunk = ALL_COLLECTIONS[page * GIFTS_PER_PAGE:(page + 1) * GIFTS_PER_PAGE]
    sel = set(selected)
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(chunk), 2):
        row = []
        for coll in chunk[i:i + 2]:
            mark = "✅ " if coll in sel else ""
            row.append(_btn(f"{mark}{_d(coll)}", f"kgift:{coll}"))
        rows.append(row)
    nav = []
    if page > 0:
        nav.append(_btn("◀️", f"kgp:{page-1}"))
    nav.append(_btn(f"· {page+1}/{pages} ·", "noop"))
    if page < pages - 1:
        nav.append(_btn("▶️", f"kgp:{page+1}"))
    rows.append(nav)
    rows.append([_btn(f"✔️ Готово ({len(selected)})", "kombo")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "kombo")
async def cb_kombo(cq: CallbackQuery):
    if not await _check_sub(cq.from_user.id, cq.bot):
        await cq.message.edit_text(MSG_SUBSCRIBE, reply_markup=_kb_subscribe(), parse_mode=ParseMode.HTML)
        await cq.answer()
        return
    c = _combo_state(cq.message.chat.id)
    await cq.message.edit_text(_combo_text(c), reply_markup=kb_combo(c), parse_mode=ParseMode.HTML)
    await cq.answer()


@router.callback_query(F.data.startswith("kf:"))
async def cb_kombo_filter(cq: CallbackQuery):
    key = cq.data.split(":")[1]
    c = _combo_state(cq.message.chat.id)
    if key in ("girls", "original", "market"):
        c[key] = not c.get(key)
    await cq.message.edit_text(_combo_text(c), reply_markup=kb_combo(c), parse_mode=ParseMode.HTML)
    await cq.answer()


@router.callback_query(F.data.startswith("kcnt:"))
async def cb_kombo_country(cq: CallbackQuery):
    val = cq.data.split(":")[1]
    c = _combo_state(cq.message.chat.id)
    if val == "open":
        await cq.message.edit_text(
            "🌍 <b>Выбери страну для комбо</b>\n\n"
            "Будут найдены только владельцы из этой страны.",
            reply_markup=kb_combo_country(), parse_mode=ParseMode.HTML,
        )
        await cq.answer()
        return
    c["country"] = val
    await cq.message.edit_text(_combo_text(c), reply_markup=kb_combo(c), parse_mode=ParseMode.HTML)
    await cq.answer()


@router.callback_query(F.data.startswith("kgift:"))
async def cb_kombo_gift(cq: CallbackQuery):
    val = cq.data.split(":", 1)[1]
    c = _combo_state(cq.message.chat.id)
    if val == "open":
        await cq.message.edit_text(
            "🎁 <b>Выбери NFT-коллекции</b>\n"
            "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
            "Жми, чтобы добавить/убрать (✅). Можно несколько.\n"
            "Когда закончишь — «✔️ Готово».",
            reply_markup=kb_combo_gifts(c.get("gifts", []), 0), parse_mode=ParseMode.HTML,
        )
        await cq.answer()
        return
    gifts = c.setdefault("gifts", [])
    if val in gifts:
        gifts.remove(val)
    else:
        gifts.append(val)
    # перерисовываем текущую страницу (определяем по позиции выбранного)
    try:
        idx = ALL_COLLECTIONS.index(val)
        page = idx // GIFTS_PER_PAGE
    except ValueError:
        page = 0
    await cq.message.edit_reply_markup(reply_markup=kb_combo_gifts(gifts, page))
    await cq.answer(f"Выбрано: {len(gifts)}")


@router.callback_query(F.data.startswith("kgp:"))
async def cb_kombo_gift_page(cq: CallbackQuery):
    page = int(cq.data.split(":")[1])
    c = _combo_state(cq.message.chat.id)
    await cq.message.edit_reply_markup(reply_markup=kb_combo_gifts(c.get("gifts", []), page))
    await cq.answer()


@router.callback_query(F.data == "krun")
async def cb_kombo_run(cq: CallbackQuery):
    c = _combo_state(cq.message.chat.id)
    if not c.get("gifts"):
        await cq.answer("Сначала выбери хотя бы одну коллекцию 🎁", show_alert=True)
        return
    await cq.answer()
    await _do_peek_combo_scan(
        cq.message,
        gifts=list(c.get("gifts", [])),
        country=c.get("country", "any"),
        girls=bool(c.get("girls")),
        original=bool(c.get("original")),
        market=bool(c.get("market")),
    )


async def _do_peek_combo_scan(message: Message, gifts: list[str], country: str = "any",
                              girls: bool = False, original: bool = False,
                              market: bool = False):
    """Комбо-скан: несколько коллекций + несколько фильтров (И/AND)."""
    cid = message.chat.id
    if cid in active_scans:
        await message.answer("⏳ Уже идёт скан! /stop")
        return

    _country = None if country in (None, "any", "all") else country
    tpl = await db.get_template(cid, _country) or ""

    tags = []
    if girls:
        tags.append("👩")
    if _country:
        tags.append(COUNTRY_FLAGS.get(_country, "🌍"))
    if original:
        tags.append("🆕")
    if market:
        tags.append("🛒")
    tag_str = " ".join(tags) if tags else "🎁"

    prog_msg = await message.answer(
        f'{tge(E_PARSING, "⏳")} <b>Комбо-парсинг…</b> {tag_str}\n'
        f'{tge(E_GIFT, "🎁")} Коллекций: {len(gifts)}\n'
        f'<code>{_bar(0, len(gifts))}</code> 0%\n\n'
        '<i>/stop чтобы остановить</i>',
        parse_mode=ParseMode.HTML,
    )

    stop_ev = asyncio.Event()
    active_scans[cid] = stop_ev
    det = COUNTRY_DETECTORS.get(_country) if _country else None

    try:
        merged: dict[str, dict] = {}

        def _process(items):
            """Применяет AND-фильтры к одной коллекции и сливает в merged."""
            if det:
                items = [it for it in items if det(it.get("owner", ""))]
            if original:
                items = peek_api.filter_original_owners(items)
            if market:
                items = [it for it in items if it.get("market")]
            items = [
                it for it in items
                if it.get("username") and not is_bot_or_shop(it.get("username", ""), it.get("owner", ""))
            ]
            for it in items:
                u = it.get("username")
                if u and u not in merged:
                    merged[u] = it

        # Грузим коллекции ПАРАЛЛЕЛЬНО (до 3 одновременно) — кратно быстрее,
        # чем по одной. Прогресс обновляем по мере готовности.
        sem = asyncio.Semaphore(3)

        async def _load(gift):
            async with sem:
                if stop_ev.is_set():
                    return []
                return await peek_api.search_all_pages(gift, max_pages=200, stop_event=stop_ev)

        tasks = [asyncio.create_task(_load(g)) for g in gifts]
        done_n = 0
        for fut in asyncio.as_completed(tasks):
            try:
                items = await fut
            except Exception:
                items = []
            _process(items)
            done_n += 1
            try:
                pct = int(done_n / max(1, len(gifts)) * 100)
                await prog_msg.edit_text(
                    f'{tge(E_PARSING, "⏳")} <b>Комбо-парсинг…</b> {tag_str}\n'
                    f'{tge(E_GIFT, "🎁")} {done_n}/{len(gifts)} · найдено {len(merged)}\n'
                    f'<code>{_bar(done_n, len(gifts))}</code> {pct}%\n\n'
                    '<i>/stop чтобы остановить</i>',
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

        all_items = list(merged.values())

        if girls and all_items:
            gender_api.reset_api_counters()
            try:
                await prog_msg.edit_text(
                    f'{tge(E_PARSING, "⏳")} <b>Определяю пол…</b> {tag_str}\n'
                    f'👥 Кандидатов: {len(all_items)}',
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            all_items = await gender_api.filter_female_owners(all_items)

        results = _build_peek_results(all_items, cid, include_market=market)
        results, hidden_viewed, hidden_bots = await _filter_peek_results(results, cid)

        owner_keys = [r["owner_key"] for r in results]
        await db.mark_viewed(cid, owner_keys)

        st = _st(cid)
        st["results"] = results
        st["page"] = 0
        st["hidden_viewed"] = hidden_viewed
        st["hidden_bots"] = hidden_bots
        st["_template"] = tpl

        total_pages = max(1, math.ceil(len(results) / RESULTS_PER_PAGE))
        if not results:
            await prog_msg.edit_text(
                f"📭 <b>Никого не нашёл по комбо</b> {tag_str}\n\n"
                f"🎁 Коллекций: {len(gifts)}\n"
                f"👁 Скрыто просмотренных: {hidden_viewed}\n\n"
                "💡 <i>Попробуй ослабить фильтры или добавить коллекций</i>",
                reply_markup=kb_home(), parse_mode=ParseMode.HTML,
            )
        else:
            text = msg_results_page(
                results, 0, len(results), total_pages,
                hidden_viewed, hidden_bots, tpl,
            )
            await prog_msg.edit_text(
                text, reply_markup=kb_results(0, total_pages),
                parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            )

    except Exception as exc:
        logger.error("Combo scan error: %s", exc, exc_info=True)
        try:
            await prog_msg.edit_text(
                f"❌ <b>Ошибка</b>\n\n<code>{html.escape(str(exc))}</code>",
                reply_markup=kb_home(), parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    finally:
        active_scans.pop(cid, None)


async def _do_fragment_numbers_scan(message: Message):
    """Скан +888 номеров с Fragment."""
    cid = message.chat.id

    if cid in active_scans:
        await message.answer("⏳ Уже идёт скан! /stop")
        return

    prog_msg = await message.answer(
        f'{tge(E_PARSING, "⏳")} <b>Загружаю +888 номера…</b>\n'
        '📞 Fragment.com\n'
        f'<code>{_bar(0, 1)}</code> 0%\n\n'
        '<i>/stop чтобы остановить</i>',
        parse_mode=ParseMode.HTML,
    )

    stop_ev = asyncio.Event()
    active_scans[cid] = stop_ev

    try:
        async def _progress(done, total, msg):
            try:
                await prog_msg.edit_text(
                    f'{tge(E_PARSING, "⏳")} <b>Загружаю +888 номера…</b>\n'
                    f'📞 Fragment.com\n'
                    f'<code>{_bar(done, total)}</code> {done}/{total}\n\n'
                    '<i>/stop чтобы остановить</i>',
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

        results = await fragment_api.scan_numbers(
            filter_type="sold",
            max_details=80,
            stop_event=stop_ev,
            progress_callback=_progress,
        )

        if not results:
            await prog_msg.edit_text(
                "📭 <b>Номера не найдены</b>\n\n"
                "Попробуйте позже.",
                reply_markup=kb_home(),
                parse_mode=ParseMode.HTML,
            )
            return

        # Нормализуем результаты для пагинации
        clean_results = []
        for r in results[:TOTAL_RESULTS]:
            number = r.get("number", "")
            raw_display = r.get("display", f"+{number}")
            # Чистим отображение номера: "+888 8 777" → "+888 8777"
            # Убираем лишние пробелы внутри номера, оставляем +888 XX XX XX XX
            digits = re.sub(r'\D', '', raw_display)  # Только цифры
            if len(digits) >= 11:
                # Формат: +888 XXXX XXXX
                fmt = f"+{digits[0:3]} {digits[3:7]} {digits[7:]}"
            elif len(digits) >= 7:
                fmt = f"+{digits[0:3]} {digits[3:7]} {digits[7:]}"
            else:
                fmt = raw_display
            fmt = fmt.strip()

            wallet = r.get("owner_wallet", "")
            username = r.get("owner_username", "")
            tme = r.get("owner_tme", "")
            price = r.get("price", "")

            # Если username — это бот или канал, убираем
            if username and is_bot_or_shop(username, ""):
                username = ""

            clean_results.append({
                "display_name": fmt,
                "username": username,
                "owner_key": f"frag_{number}",
                "wallet": wallet,
                "wallet_short": fragment_api.format_wallet_short(wallet),
                "tme_name": tme,
                "price": price,
                "number": number,
                "first_slug": f"https://fragment.com/number/{number}",
            })

        if not clean_results:
            await prog_msg.edit_text(
                "📭 <b>Номера не найдены</b>\n\n"
                "Попробуйте позже.",
                reply_markup=kb_home(),
                parse_mode=ParseMode.HTML,
            )
            return

        # Сохраняем в state для пагинации
        st = _st(cid)
        st["results"] = clean_results
        st["page"] = 0
        st["result_type"] = "numbers"
        st["hidden_viewed"] = 0
        st["hidden_bots"] = 0

        total_pages = max(1, math.ceil(len(clean_results) / RESULTS_PER_PAGE))

        text = _format_numbers_page(clean_results, 0, total_pages)
        await prog_msg.edit_text(
            text,
            reply_markup=kb_results(0, total_pages),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    except Exception as exc:
        logger.error("Fragment numbers scan error: %s", exc, exc_info=True)
        try:
            await prog_msg.edit_text(
                f"❌ <b>Ошибка</b>\n\n<code>{html.escape(str(exc))}</code>",
                reply_markup=kb_home(),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    finally:
        active_scans.pop(cid, None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  NFT юзернеймы — автопарсинг из TON блокчейна
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _do_nft_usernames_auto_scan(message: Message):
    """
    Автопарсинг NFT юзернеймов из TON блокчейна.
    Фильтры: цена ≤ 30 TON (~$100), привязан к TG аккаунту.
    """
    cid = message.chat.id

    if cid in active_scans:
        await message.answer("⏳ Уже идёт скан! /stop")
        return

    prog_msg = await message.answer(
        f'{tge(E_PARSING, "⏳")} <b>Парсинг NFT юзернеймов…</b>\n'
        '🏷 TON Blockchain · ≤30 TON · привязанные к TG\n'
        f'<code>{_bar(0, 1)}</code> 0 / ?\n\n'
        '<i>/stop чтобы остановить</i>',
        parse_mode=ParseMode.HTML,
    )

    stop_ev = asyncio.Event()
    active_scans[cid] = stop_ev
    last_upd = [time.time()]

    try:
        viewed = await db.get_viewed_keys(cid)

        async def _progress(scanned, found):
            now = time.time()
            if now - last_upd[0] < PROGRESS_UPDATE_INTERVAL:
                return
            last_upd[0] = now
            try:
                await prog_msg.edit_text(
                    f'{tge(E_PARSING, "⏳")} <b>Парсинг NFT юзернеймов…</b>\n'
                    f'🏷 TON Blockchain · ≤30 TON · привязанные к TG\n'
                    f'<code>{_bar(found, TOTAL_RESULTS)}</code> {found}/{TOTAL_RESULTS}\n'
                    f'📦 Просканировано: {scanned:,}\n\n'.replace(",", " ") +
                    '<i>/stop чтобы остановить</i>',
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

        results = await ton_api.scan_nft_usernames(
            max_results=TOTAL_RESULTS,
            max_price_ton=30.0,       # ≤ ~$100
            only_with_tg=True,        # привязанные к TG
            progress_callback=_progress,
            stop_event=stop_ev,
            viewed_keys=viewed,
        )

        if not results:
            await prog_msg.edit_text(
                "📭 <b>NFT юзернеймы не найдены</b>\n\n"
                "Юзернеймы ≤30 TON на продаже не найдены, "
                "либо все уже были показаны.\n"
                "Используй /reset чтобы сбросить историю.",
                reply_markup=kb_home(),
                parse_mode=ParseMode.HTML,
            )
            return

        # Помечаем просмотренными
        await db.mark_viewed(cid, [r["owner_key"] for r in results])

        # Сохраняем в state для пагинации
        st = _st(cid)
        st["results"] = results
        st["page"] = 0
        st["result_type"] = "nft_usernames"
        st["hidden_viewed"] = 0
        st["hidden_bots"] = 0

        total_pages = max(1, math.ceil(len(results) / RESULTS_PER_PAGE))
        text = _format_nft_usernames_page(results, 0, total_pages)

        await prog_msg.edit_text(
            text,
            reply_markup=kb_results(0, total_pages),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    except Exception as exc:
        logger.error("NFT usernames auto-scan error: %s", exc, exc_info=True)
        try:
            await prog_msg.edit_text(
                f"❌ <b>Ошибка</b>\n\n<code>{html.escape(str(exc))}</code>",
                reply_markup=kb_home(),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    finally:
        active_scans.pop(cid, None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Неулучшенные подарки — через getUserGifts
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _do_non_upgraded_scan(message: Message, gift_name: str):
    """
    Глубокий поиск владельцев неулучшенных подарков:
    1. Берём ВСЕХ юзеров из peek.tg (текущие + предыдущие владельцы)
    2. Дополнительно: сканируем t.me/nft/{slug} для поиска новых владельцев
    3. Для каждого вызываем Bot API getUserGifts
    4. Фильтруем подарки без upgrade
    """
    cid = message.chat.id

    if cid in active_scans:
        await message.answer("⏳ Уже идёт скан! /stop")
        return

    prog_msg = await message.answer(
        f'{tge(E_PARSING, "⏳")} <b>Глубокий поиск неулучшенных…</b>\n'
        f'🎁 {_d(gift_name)}\n'
        f'<code>{_bar(0, 1)}</code> 0%\n\n'
        '<i>/stop чтобы остановить</i>',
        parse_mode=ParseMode.HTML,
    )

    stop_ev = asyncio.Event()
    active_scans[cid] = stop_ev
    last_upd = [time.time()]
    viewed = await db.get_viewed_keys(cid)

    try:
        # ── Фаза 1: Собираем userId из peek.tg ──
        await prog_msg.edit_text(
            f'{tge(E_PARSING, "⏳")} <b>Фаза 1/3: Загрузка из peek.tg…</b>\n'
            f'🎁 {_d(gift_name)}\n\n'
            '📡 Загружаю владельцев (текущие + предыдущие)…\n\n'
            '<i>/stop чтобы остановить</i>',
            parse_mode=ParseMode.HTML,
        )

        peek_results = await peek_api.search_all_pages(
            gift_name, max_pages=200, stop_event=stop_ev,
        )

        # Собираем ВСЕХ уникальных юзеров: текущие + previousOwner
        users_with_id = {}
        for r in peek_results:
            # Текущий владелец
            uid = r.get("userId")
            uname = r.get("username", "")
            if uid and uname and f"nug_{uid}" not in viewed:
                if uid not in users_with_id:
                    users_with_id[uid] = {
                        "user_id": uid,
                        "username": uname.replace("t.me/", ""),
                        "display_name": r.get("owner", ""),
                    }
            # Предыдущий владелец (тоже имеет userId!)
            prev = r.get("previousOwner")
            if prev:
                prev_uid = prev.get("userId")
                prev_uname = prev.get("username", "")
                if prev_uid and prev_uname and f"nug_{prev_uid}" not in viewed:
                    if prev_uid not in users_with_id:
                        users_with_id[prev_uid] = {
                            "user_id": prev_uid,
                            "username": prev_uname.replace("t.me/", ""),
                            "display_name": prev.get("displayName", ""),
                        }

        # ── Фаза 2: Дополнительный скан через t.me/nft ──
        if not stop_ev.is_set() and len(users_with_id) < 1000:
            try:
                await prog_msg.edit_text(
                    f'{tge(E_PARSING, "⏳")} <b>Фаза 2/3: Доп. скан t.me/nft…</b>\n'
                    f'🎁 {_d(gift_name)}\n\n'
                    f'📡 Из peek.tg: {len(users_with_id)} юзеров\n'
                    '🔍 Ищу ещё через t.me/nft…\n\n'
                    '<i>/stop чтобы остановить</i>',
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

            # Получаем размер коллекции и сканируем случайные номера
            api_name = peek_api.human_to_api(gift_name)
            try:
                coll_size = await nft_scanner.get_collection_size(api_name)
            except Exception:
                coll_size = 0

            if coll_size > 0:
                import random as _rnd
                scanned_nums = await db.get_scanned_items(api_name)
                unscanned = [i for i in range(1, min(coll_size + 1, 50001)) if i not in scanned_nums]
                sample_size = min(2000, len(unscanned))
                if sample_size > 0:
                    sample = _rnd.sample(unscanned, sample_size)

                    import aiohttp as _aio
                    sem = asyncio.Semaphore(100)
                    new_items = []

                    async def _fetch_nft(session, num):
                        async with sem:
                            if stop_ev.is_set():
                                return
                            return await nft_scanner._fetch_one(session, api_name, num)

                    connector = _aio.TCPConnector(limit=100, ttl_dns_cache=300)
                    async with _aio.ClientSession(
                        headers={"User-Agent": "Mozilla/5.0"}, connector=connector,
                    ) as session:
                        batch_size = 400
                        for bs in range(0, len(sample), batch_size):
                            if stop_ev.is_set():
                                break
                            batch = sample[bs:bs + batch_size]
                            results_batch = await asyncio.gather(
                                *[_fetch_nft(session, n) for n in batch],
                                return_exceptions=True,
                            )
                            for res in results_batch:
                                if isinstance(res, dict) and res.get("username"):
                                    new_items.append(res)

                            try:
                                await prog_msg.edit_text(
                                    f'{tge(E_PARSING, "⏳")} <b>Фаза 2/3: Доп. скан…</b>\n'
                                    f'🎁 {_d(gift_name)}\n\n'
                                    f'📦 Просканировано: {min(bs + batch_size, len(sample))}/{len(sample)}\n'
                                    f'👤 Новых юзеров: {len(new_items)}\n\n'
                                    '<i>/stop чтобы остановить</i>',
                                    parse_mode=ParseMode.HTML,
                                )
                            except Exception:
                                pass

                    # Сохраняем в БД
                    if new_items:
                        await db.save_nft_items_batch(new_items)

                    # Для новых юзеров пытаемся получить userId через bot
                    for item in new_items:
                        uname = item.get("username", "")
                        if not uname or stop_ev.is_set():
                            continue
                        try:
                            chat = await message.bot.get_chat(chat_id=f"@{uname}")
                            uid = chat.id
                            if uid and f"nug_{uid}" not in viewed and uid not in users_with_id:
                                users_with_id[uid] = {
                                    "user_id": uid,
                                    "username": uname,
                                    "display_name": item.get("display_name", ""),
                                }
                        except Exception:
                            pass
                        await asyncio.sleep(0.03)

        if not users_with_id:
            await prog_msg.edit_text(
                f"📭 <b>Нет юзеров для проверки</b>\n\n"
                f"🎁 {_d(gift_name)}\n"
                "Все владельцы уже проверены или не имеют userId.\n"
                "Попробуй /reset для сброса.",
                reply_markup=kb_home(),
                parse_mode=ParseMode.HTML,
            )
            return

        # ── Фаза 3: Проверяем getUserGifts ──
        total_users = len(users_with_id)
        checked = 0
        hidden = 0
        results = []
        bot_inst = message.bot

        try:
            await prog_msg.edit_text(
                f'{tge(E_PARSING, "⏳")} <b>Фаза 3/3: Проверка подарков…</b>\n'
                f'🎁 {_d(gift_name)}\n\n'
                f'👤 Всего юзеров: {total_users}\n'
                f'<code>{_bar(0, total_users)}</code> 0%\n\n'
                '<i>/stop чтобы остановить</i>',
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

        for uid, udata in users_with_id.items():
            if stop_ev.is_set():
                break
            if len(results) >= TOTAL_RESULTS:
                break

            checked += 1

            try:
                user_gifts = await bot_inst.get_user_gifts(user_id=int(uid))
                gifts_list = user_gifts.gifts if user_gifts else []

                non_upgraded = []
                for g in gifts_list:
                    if hasattr(g, 'is_upgraded') and not g.is_upgraded:
                        non_upgraded.append(g)
                    elif hasattr(g, 'gift') and hasattr(g.gift, 'sticker'):
                        if not getattr(g, 'is_upgraded', False):
                            non_upgraded.append(g)

                if non_upgraded:
                    results.append({
                        "username": udata["username"],
                        "display_name": udata["display_name"],
                        "user_id": uid,
                        "non_upgraded_count": len(non_upgraded),
                        "total_gifts": len(gifts_list),
                        "owner_key": f"nug_{uid}",
                    })

            except Exception as exc:
                hidden += 1
                logger.debug("getUserGifts(%s) error: %s", uid, exc)

            # Прогресс
            now = time.time()
            if now - last_upd[0] >= PROGRESS_UPDATE_INTERVAL:
                last_upd[0] = now
                pct = int(100 * checked / total_users)
                try:
                    await prog_msg.edit_text(
                        f'{tge(E_PARSING, "⏳")} <b>Фаза 3/3: Проверка подарков…</b>\n'
                        f'🎁 {_d(gift_name)}\n\n'
                        f'<code>{_bar(checked, total_users)}</code> {pct}%\n'
                        f'👤 Проверено: {checked}/{total_users}\n'
                        f'🔒 Скрытые: {hidden}\n'
                        f'✅ С неулучш.: {len(results)}\n\n'
                        '<i>/stop чтобы остановить</i>',
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass

            await asyncio.sleep(0.05)

        if not results:
            await prog_msg.edit_text(
                f"📭 <b>Неулучшенные не найдены</b>\n\n"
                f"🎁 {_d(gift_name)}\n"
                f"👤 Проверено: {checked}\n"
                f"🔒 Скрытые профили: {hidden}\n\n"
                f"У остальных все подарки улучшены.\n"
                "💡 Попробуй другую коллекцию.",
                reply_markup=kb_home(),
                parse_mode=ParseMode.HTML,
            )
            return

        await db.mark_viewed(cid, [r["owner_key"] for r in results])

        st = _st(cid)
        st["results"] = results
        st["page"] = 0
        st["result_type"] = "non_upgraded"
        st["hidden_viewed"] = 0
        st["hidden_bots"] = 0

        total_pages = max(1, math.ceil(len(results) / RESULTS_PER_PAGE))
        text = _format_non_upgraded_page(results, 0, total_pages)

        await prog_msg.edit_text(
            text,
            reply_markup=kb_results(0, total_pages),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    except Exception as exc:
        logger.error("Non-upgraded scan error: %s", exc, exc_info=True)
        try:
            await prog_msg.edit_text(
                f"❌ <b>Ошибка</b>\n\n<code>{html.escape(str(exc))}</code>",
                reply_markup=kb_home(),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    finally:
        active_scans.pop(cid, None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Callback: выбор режима → страна  (legacy inline flow)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.callback_query(F.data.startswith("mode:"))
async def cb_mode(cq: CallbackQuery):
    if not await _check_sub(cq.from_user.id, cq.bot):
        await cq.message.edit_text(MSG_SUBSCRIBE, reply_markup=_kb_subscribe(), parse_mode=ParseMode.HTML)
        await cq.answer()
        return

    mode = cq.data.split(":")[1]
    st = _st(cq.message.chat.id)
    st["mode"] = mode
    st.pop("search_action", None)

    labels = {"reg": "🔍 Парсинг", "rnd": "🎲 Рандомный парсинг", "raw": "🎁 Неулучшенные подарки"}
    label = labels.get(mode, "🔍 Парсинг")
    await cq.message.edit_text(
        f"{label}\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
        "🌍 <b>Выберите регион поиска</b>",
        reply_markup=kb_country(),
        parse_mode=ParseMode.HTML,
    )
    await cq.answer()


@router.callback_query(F.data.startswith("cnt:"))
async def cb_country(cq: CallbackQuery):
    country = cq.data.split(":")[1]
    st = _st(cq.message.chat.id)
    st["country"] = country
    flag = COUNTRY_FLAGS.get(country, "🌍")

    mode = st.get("mode", "reg")

    if mode == "rnd":
        await cq.message.edit_text(
            f"🎲 <b>Рандомный парсинг</b>  {flag}\n"
            "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
            "📊 <b>Фильтр по кол-ву NFT</b>\n\n"
            "<i>Сколько всего NFT должно быть\n"
            "у владельца? (по всем коллекциям)</i>",
            reply_markup=kb_nft_count(),
            parse_mode=ParseMode.HTML,
        )
    elif mode == "raw":
        await cq.message.edit_text(
            f"🎁 <b>Неулучшенные подарки</b>  {flag}\n"
            "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
            "Подарки, которые ещё не улучшили\n\n"
            "🎁 <b>Выберите коллекцию</b>",
            reply_markup=kb_gifts(0),
            parse_mode=ParseMode.HTML,
        )
    else:
        await cq.message.edit_text(
            f"🔍 <b>Выберите коллекцию</b>  {flag}\n"
            "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
            "🎁 Нажмите или 🔍 найдите по названию",
            reply_markup=kb_gifts(0),
            parse_mode=ParseMode.HTML,
        )
    await cq.answer()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Callback: навигация коллекций / моделей / фонов
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.callback_query(F.data.startswith("gp:"))
async def cb_gp(cq: CallbackQuery):
    p = int(cq.data.split(":")[1])
    await cq.message.edit_reply_markup(reply_markup=kb_gifts(p))
    await cq.answer()


@router.callback_query(F.data.startswith("g:"))
async def cb_gift(cq: CallbackQuery):
    coll = cq.data.split(":", 1)[1]
    st = _st(cq.message.chat.id)
    st["collection"] = coll
    flag = COUNTRY_FLAGS.get(st.get("country", "cn"), "🌍")

    if st.get("mode") == "raw":
        st["model"] = "*"
        st["backdrop"] = "*"
        await cq.message.edit_text(
            f"🎁 <b>{_d(coll)}</b>  {flag}  ·  неулучш.\n"
            "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
            "📊 <b>Фильтр по кол-ву подарков</b>\n\n"
            "<i>Сколько неулучшенных подарков\n"
            "должно быть у владельца?</i>",
            reply_markup=kb_nft_count(),
            parse_mode=ParseMode.HTML,
        )
        await cq.answer()
        return

    if not COLLECTION_MODELS.get(coll):
        st["model"] = "*"
        await cq.message.edit_text(
            f"🎁 <b>{_d(coll)}</b>  {flag}\n"
            "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
            "🖼 Модель: <b>любая</b>\n"
            "🎨 <b>Выберите фон</b>",
            reply_markup=kb_backdrops(coll, "*", 0),
            parse_mode=ParseMode.HTML,
        )
    else:
        await cq.message.edit_text(
            f"🎁 <b>{_d(coll)}</b>  {flag}\n"
            "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
            "🖼 <b>Выберите модель</b>",
            reply_markup=kb_models(coll, 0),
            parse_mode=ParseMode.HTML,
        )
    await cq.answer()


@router.callback_query(F.data == "back_coll")
async def cb_back_coll(cq: CallbackQuery):
    st = _st(cq.message.chat.id)
    flag = COUNTRY_FLAGS.get(st.get("country", "cn"), "🌍")
    await cq.message.edit_text(
        f"🔍 <b>Выберите коллекцию</b>  {flag}\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
        "🎁 Нажмите или 🔍 найдите по названию",
        reply_markup=kb_gifts(0),
        parse_mode=ParseMode.HTML,
    )
    await cq.answer()


@router.callback_query(F.data.startswith("mp:"))
async def cb_mp(cq: CallbackQuery):
    _, c, p = cq.data.split(":")
    await cq.message.edit_reply_markup(reply_markup=kb_models(c, int(p)))
    await cq.answer()


@router.callback_query(F.data.startswith("m:"))
async def cb_mdl(cq: CallbackQuery):
    _, c, m = cq.data.split(":", 2)
    st = _st(cq.message.chat.id)
    st["model"] = m
    flag = COUNTRY_FLAGS.get(st.get("country", "cn"), "🌍")
    m_d = "любая" if m == "*" else m
    await cq.message.edit_text(
        f"🎁 <b>{_d(c)}</b>  {flag}\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
        f"🖼 Модель: <b>{m_d}</b>\n"
        "🎨 <b>Выберите фон</b>",
        reply_markup=kb_backdrops(c, m, 0),
        parse_mode=ParseMode.HTML,
    )
    await cq.answer()


@router.callback_query(F.data.startswith("bp:"))
async def cb_bp(cq: CallbackQuery):
    parts = cq.data.split(":")
    await cq.message.edit_reply_markup(
        reply_markup=kb_backdrops(parts[1], parts[2], int(parts[3])),
    )
    await cq.answer()


@router.callback_query(F.data == "back_bd")
async def cb_back_bd(cq: CallbackQuery):
    st = _st(cq.message.chat.id)
    coll = st.get("collection", "")
    m = st.get("model", "*")
    flag = COUNTRY_FLAGS.get(st.get("country", "cn"), "🌍")
    m_d = "любая" if m == "*" else m
    await cq.message.edit_text(
        f"🎁 <b>{_d(coll)}</b>  {flag}\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
        f"🖼 Модель: <b>{m_d}</b>\n"
        "🎨 <b>Выберите фон</b>",
        reply_markup=kb_backdrops(coll, m, 0),
        parse_mode=ParseMode.HTML,
    )
    await cq.answer()


@router.callback_query(F.data.startswith("b:"))
async def cb_bd(cq: CallbackQuery):
    parts = cq.data.split(":")
    c, m, b = parts[1], parts[2], parts[3]
    st = _st(cq.message.chat.id)
    st["backdrop"] = b
    flag = COUNTRY_FLAGS.get(st.get("country", "cn"), "🌍")
    m_d = "любая" if m == "*" else m
    b_d = "любой" if b == "*" else b

    await cq.message.edit_text(
        f"🎁 <b>{_d(c)}</b>  {flag}\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
        f"🖼 {m_d}  ·  🎨 {b_d}\n\n"
        "📊 <b>Фильтр по кол-ву NFT</b>\n\n"
        "<i>Сколько всего NFT должно быть\n"
        "у владельца? (по всем коллекциям)</i>",
        reply_markup=kb_nft_count(),
        parse_mode=ParseMode.HTML,
    )
    await cq.answer()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Callback: поиск текстом
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.callback_query(F.data.startswith("srch:"))
async def cb_search(cq: CallbackQuery):
    parts = cq.data.split(":")
    t = parts[1]
    st = _st(cq.message.chat.id)
    st["search_action"] = f"search_{t}"
    if t == "m" and len(parts) > 2:
        st["_srch_coll"] = parts[2]
    elif t == "b" and len(parts) > 3:
        st["_srch_coll"] = parts[2]
        st["_srch_model"] = parts[3]
    labels = {"g": "коллекцию", "m": "модель", "b": "фон"}
    await cq.message.edit_text(
        f"🔍 <b>Поиск</b>\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
        f"Введите название ({labels.get(t, 'коллекцию')}).\n"
        "Можно часть слова — подберу похожие.\n\n"
        "<i>Например: cat, rose, green…</i>",
        parse_mode=ParseMode.HTML,
    )
    await cq.answer()


@router.message(Command("cancel"))
async def cmd_cancel(message: Message):
    st = _st(message.chat.id)
    st.pop("search_action", None)
    await message.answer("❌ Отменено.", reply_markup=kb_home())


async def _do_broadcast(message: Message, text: str):
    """Рассылка сообщения всем юзерам всех ботов (основной + зеркала)."""
    all_bots_map: dict[str, Bot] = {bot.token: bot}
    all_bots_map.update(mirror_bots)

    pairs: list[tuple[Bot, int]] = []
    seen_cids: set[int] = set()
    for token, b in all_bots_map.items():
        cids = await db.get_all_chat_ids_by_token(token)
        for cid in cids:
            if cid not in seen_cids:
                pairs.append((b, cid))
                seen_cids.add(cid)

    if not pairs:
        await message.answer("📢 Нет юзеров.")
        return

    total = len(pairs)
    status = await message.answer(f"📢 Рассылка: 0/{total}…")
    sent = 0
    failed = 0
    for b, cid in pairs:
        try:
            await b.send_message(cid, text, parse_mode=ParseMode.HTML)
            sent += 1
        except Exception:
            failed += 1
        if (sent + failed) % 10 == 0:
            try:
                await status.edit_text(
                    f"📢 {sent + failed}/{total}  ✅ {sent}  ❌ {failed}",
                )
            except Exception:
                pass
        await asyncio.sleep(0.05)

    await status.edit_text(
        f"📢 <b>Готово!</b>  ✅ {sent}  ❌ {failed}  📊 {total}",
        parse_mode=ParseMode.HTML,
    )


@router.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: Message):
    cid = message.chat.id
    st = _st(cid)
    action = st.pop("search_action", None)
    q = message.text.strip()

    # Админ: рассылка
    if action == "admin_broadcast":
        if not _is_admin(message.from_user.id):
            await message.answer("⛔ Нет доступа.")
            return
        await _do_broadcast(message, q)
        return

    # Шаблон: сохранение
    if action and action.startswith("tpl_edit_"):
        country = action.replace("tpl_edit_", "")
        if len(q) > 500:
            await message.answer("❌ Слишком длинный текст (макс. 500 символов).")
            return
        await db.set_template(cid, country, q)
        flag = "🇷🇺" if country == "ru" else "🇨🇳"
        await message.answer(
            f"✅ Шаблон для {flag} сохранён!\n\n"
            f"<i>{html.escape(q)}</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_btn("Шаблоны", "templates", icon=E_TEMPLATES), _btn("В Меню", "home", icon=E_MENU)],
            ]),
        )
        return

    if action == "mirror_token":
        q = q.strip().replace("\n", "").replace("\r", "").replace(" ", "")
        if ":" not in q or len(q) < 30:
            await message.answer(
                "❌ Неверный токен.\nОтправь токен от @BotFather.",
                reply_markup=kb_home(),
            )
            return

        status_msg = await message.answer("🔄 Проверяю токен…")
        info = None
        for _attempt in range(2):
            try:
                if TELEGRAM_API_SERVER:
                    _s = AiohttpSession(api=TelegramAPIServer.from_base(TELEGRAM_API_SERVER))
                    test_bot = Bot(token=q, session=_s)
                else:
                    test_bot = Bot(token=q)
                info = await test_bot.get_me()
                await test_bot.session.close()
                break
            except Exception:
                try:
                    await test_bot.session.close()
                except Exception:
                    pass
                if _attempt == 0:
                    await asyncio.sleep(1)
        if info is None:
            await status_msg.edit_text(
                "❌ Токен не работает.\nПроверь и попробуй снова.",
                reply_markup=kb_home(),
            )
            return

        existing = await db.get_mirror_by_token(q)
        if existing:
            await status_msg.edit_text(
                f"⚠️ @{info.username} уже зеркало.",
                reply_markup=kb_home(),
            )
            return

        await db.add_mirror(message.from_user.id, q, info.username)
        ok = await _activate_mirror(q)
        if ok:
            await status_msg.edit_text(
                f"✅ <b>Зеркало создано!</b>\n\n"
                f"🤖 @{info.username}\n"
                f"Бот уже работает — отправь /start",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_home(),
            )
        else:
            await status_msg.edit_text(
                "❌ Не удалось запустить. Попробуй позже.",
                reply_markup=kb_home(),
            )
        return

    # Быстрый поиск коллекции для парсинга (из подменю)
    if action and action.startswith("qsearch_"):
        sub_action = action.replace("qsearch_", "")
        r = _fuzzy(q, ALL_COLLECTIONS)
        if not r:
            await message.answer(f'❌ «{q}» не найдено.')
            return
        # Показываем результаты с кнопками быстрого парсинга
        rows: list[list[InlineKeyboardButton]] = []
        for i in range(0, min(len(r), 10), 2):
            row = [_btn(f"🎁 {_d(r[i])}", f"qg:{sub_action}:{r[i]}")]
            if i + 1 < len(r):
                row.append(_btn(f"🎁 {_d(r[i+1])}", f"qg:{sub_action}:{r[i+1]}"))
            rows.append(row)
        rows.append([_btn("Назад", "parsing_menu")])
        await message.answer(
            f'🔍 По «{q}»:',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        return

    if action == "search_g":
        r = _fuzzy(q, ALL_COLLECTIONS)
        if not r:
            await message.answer(f'❌ «{q}» не найдено. /start')
            return
        await message.answer(
            f'🔍 По «{q}»:',
            reply_markup=kb_gifts(0, query=q),
        )
    elif action == "search_m":
        c = st.get("_srch_coll", st.get("collection", ""))
        r = _fuzzy(q, COLLECTION_MODELS.get(c, []))
        if not r:
            await message.answer(f'❌ «{q}» не найдено.')
            return
        await message.answer(
            f'🔍 Модели по «{q}»:',
            reply_markup=kb_models(c, 0, query=q),
        )
    elif action == "search_b":
        c = st.get("_srch_coll", st.get("collection", ""))
        m = st.get("_srch_model", st.get("model", "*"))
        r = _fuzzy(q, COLLECTION_BACKDROPS.get(c, []))
        if not r:
            await message.answer(f'❌ «{q}» не найдено.')
            return
        await message.answer(
            f'🔍 Фоны по «{q}»:',
            reply_markup=kb_backdrops(c, m, 0, query=q),
        )
    else:
        r = _fuzzy(q, ALL_COLLECTIONS)
        if not r:
            await message.answer(f'❌ «{q}» не найдено. /start')
            return
        await message.answer(
            f'🔍 По «{q}»:',
            reply_markup=kb_gifts(0, query=q),
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Mini App: web_app_data handler
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.message(F.web_app_data)
async def handle_web_app_data(message: Message):
    """Обработка данных от мини аппа."""
    cid = message.chat.id

    try:
        data = json.loads(message.web_app_data.data)
    except (json.JSONDecodeError, AttributeError):
        await message.answer("❌ Ошибка данных из мини аппа.")
        return

    # Desktop fallback: мини апп отправляет deeplink через sendData
    deeplink = data.get("deeplink")
    if deeplink:
        logger.info("WebApp deeplink from %d: %s", cid, deeplink)
        _st(cid)["_peek_dl"] = deeplink
        await _handle_deep_link(message, deeplink)
        return

    action = data.get("action", "")
    gift = data.get("gift", "")

    logger.info("WebApp data from %d: action=%s gift=%s", cid, action, gift)

    # Сброс просмотренных
    if action == "reset":
        await db.clear_viewed(cid)
        await message.answer(
            "✅ <b>Просмотренные сброшены!</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_home(),
        )
        return

    if not gift:
        await message.answer("❌ Не выбрана коллекция.")
        return

    # Маршрутизация по action
    if action in ("chinese", "russian"):
        await _do_peek_country_scan(message, gift, action)
    elif action in COUNTRY_DETECTORS:
        # Прямой код страны (cn, ru, jp, etc.)
        await _do_peek_country_scan(message, gift, action)
    elif action == "girls":
        await _do_peek_girls_scan(message, gift)
    elif action == "random":
        await _do_peek_random_scan(message, gift)
    elif action == "original":
        await _do_peek_original_scan(message, gift)
    elif action in ("market", "market_all"):
        await _do_peek_market_scan(message, gift)
    elif action == "market_chinese":
        await _do_peek_market_country_scan(message, gift, "cn")
    elif action == "market_russian":
        await _do_peek_market_country_scan(message, gift, "ru")
    elif action in ("cd_free", "cd_soon", "cd_active"):
        await _do_peek_cooldown_scan(message, gift, action.replace("cd_", ""))
    elif action == "reset_viewed":
        await db.clear_viewed(cid)
        await message.answer(
            "✅ <b>Просмотренные сброшены!</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_home(),
        )
    else:
        await message.answer(f"❌ Неизвестное действие: {action}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Гибрид: peek.tg + scanner (t.me/nft)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Минимальный порог: если peek дал ≤ этого — дополняем сканером
HYBRID_THRESHOLD = 5
# Максимум элементов для быстрого скана сканером
SCANNER_QUICK_LIMIT = 600
# Минимальный номер NFT — подарки с номером ниже этого скрываются
# (1-99 = очень дорогие / ранние, владельцы не подходят для работы)
MIN_NFT_NUMBER = 100


def _scanner_to_peek_format(scanner_rows: list[dict], gift_name: str = "") -> list[dict]:
    """Конвертирует строки из scanner DB в формат peek items."""
    items = []
    for r in scanner_rows:
        slug = r.get("first_slug", r.get("slug", ""))
        username = r.get("username", "")
        # Без юзернейма — пропускаем (нельзя написать)
        if not username:
            continue
        items.append({
            "owner": r.get("display_name", ""),
            "username": username,
            "giftName": gift_name or r.get("collection", ""),
            "giftNumber": "",
            "first_slug": slug,
            "nft_link": f"https://t.me/nft/{slug}" if slug and not slug.startswith("http") else slug,
            "collection": r.get("collection", gift_name),
            "owner_key": username or r.get("owner_key", r.get("display_name", "")),
            "display_name": r.get("display_name", ""),
            "nft_count": r.get("nft_count", 1),
            "_source": "scanner",
        })
    return items


async def _get_scanner_cached(gift: str, country: str | None = None, limit: int = 500) -> list[dict]:
    """Быстро достать кэш из scanner DB (если был сканирован раньше)."""
    _legacy = {"chinese": "cn", "russian": "ru"}
    db_country = _legacy.get(country, country)

    if db_country and db_country in COUNTRY_DETECTORS:
        rows = await db.get_users_grouped(gift, db_country, None, None, limit)
    else:
        # Без фильтра по стране — достаём все страны + объединяем
        seen = set()
        rows = []
        for code in COUNTRY_DETECTORS:
            part = await db.get_users_grouped(gift, code, None, None, limit)
            for r in part:
                k = r.get("owner_key", "")
                if k not in seen:
                    seen.add(k)
                    rows.append(r)
    return rows


async def _scanner_quick_scan(
    gift: str, country: str = "cn",
    stop_event: asyncio.Event | None = None,
    max_items: int = SCANNER_QUICK_LIMIT,
) -> list[dict]:
    """Запускает быстрый скан через scanner (t.me/nft) и возвращает результаты."""
    try:
        await nft_scanner.scan_collection(
            collection=gift, max_results=200, country=country,
            stop_event=stop_event, max_scan_items=max_items,
        )
        return await db.get_users_grouped(gift, country, None, None, 500)
    except Exception as exc:
        logger.warning("Scanner quick scan failed for %s: %s", gift, exc)
        return []


# Страны с УНИКАЛЬНЫМ письмом (иероглифы/кана/хангыль/арабица) — определяются
# по буквам на 100%, как Китай. Для них AI-нейросеть НЕ нужна (она только
# тащит мусор: латинские имена, ошибочно отнесённые к стране) и фильтр строгий:
# оставляем ТОЛЬКО тех, у кого реально есть письмо этой страны.
SCRIPT_ONLY_COUNTRIES = {"cn", "jp", "kr", "ar"}


def _drop_wrong_country(results: list[dict], target: str) -> list[dict]:
    """
    Финальный жёсткий фильтр для поиска по стране.

    - Для стран с уникальным письмом (cn/jp/kr/ar) — СТРОГО: оставляем только
      тех, у кого имя реально проходит детектор по буквам (как Китай → 0 русских).
    - Для остальных (латиница/смешанные: in, uz, kz…) — выкидываем только тех,
      кто ОДНОЗНАЧНО другой страны; имена-загадки (None) оставляем.
    """
    if not target or target not in COUNTRY_DETECTORS:
        return results

    # Строгий режим для уникального письма — как у Китая.
    if target in SCRIPT_ONLY_COUNTRIES:
        detector = COUNTRY_DETECTORS[target]
        return [
            r for r in results
            if detector(r.get("display_name") or r.get("owner") or "")
        ]

    # Мягкий режim для латиницы/смешанных.
    kept = []
    for r in results:
        name = r.get("display_name") or r.get("owner") or ""
        det = detect_country(name)
        if det is None or det == target:
            kept.append(r)
    return kept


def _merge_peek_and_scanner(
    peek_results: list[dict],
    scanner_rows: list[dict],
    gift_name: str = "",
) -> list[dict]:
    """Объединяет результаты из peek и scanner, дедуплицируя по username."""
    seen_keys = set()
    merged = []

    # Сначала peek (обычно свежее)
    for r in peek_results:
        key = r.get("owner_key", r.get("username", ""))
        if key and key not in seen_keys:
            seen_keys.add(key)
            merged.append(r)

    # Потом scanner
    for r in scanner_rows:
        key = r.get("owner_key", r.get("username", ""))
        if key and key not in seen_keys:
            seen_keys.add(key)
            merged.append(r)

    return merged


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  peek.tg сканеры (через мини апп) — гибрид
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _do_peek_country_scan(message: Message, gift: str, country: str):
    """Парсинг через peek.tg: по стране."""
    cid = message.chat.id
    _legacy = {"chinese": "cn", "russian": "ru"}
    db_country = _legacy.get(country, country)
    flag = COUNTRY_FLAGS.get(db_country, "🌍")
    country_label = COUNTRY_LABELS.get(db_country, db_country)
    detector = COUNTRY_DETECTORS.get(db_country, is_chinese_name)
    tpl = await db.get_template(cid, db_country) or ""

    if cid in active_scans:
        await message.answer("⏳ Уже идёт скан! /stop")
        return

    prog_msg = await message.answer(
        f'{tge(E_PARSING, "⏳")} <b>Идет парсинг…</b> {flag} {country_label}\n'
        f'{tge(E_COUNT, "📊")} Любое кол-во\n'
        f'{tge(E_GIFT, "🎁")} {html.escape(gift)}\n'
        f'<code>{_bar(0, 1)}</code> 0%\n\n'
        '<i>/stop чтобы остановить</i>',
        parse_mode=ParseMode.HTML,
    )

    stop_ev = asyncio.Event()
    active_scans[cid] = stop_ev
    last_upd = [time.time()]

    try:
        all_items = []
        found = []
        page = 0
        max_pages = 300  # сканируем глубже
        _scan_start = time.time()
        _TIME_BUDGET = 70  # сек на фазу peek — чтобы редкие страны не висли 10 мин

        async with peek_api.make_session() as session:
            while page < max_pages:
                if stop_ev.is_set():
                    break
                # Бюджет по времени: для редких стран (Индия и т.п.) не сканируем
                # бесконечно — лучше отдать что есть, чем висеть 10 минут.
                if time.time() - _scan_start > _TIME_BUDGET:
                    break
                page += 1

                # Параллельный запрос 6 страниц одновременно
                page_tasks = []
                for p_off in range(6):
                    cur_p = page + p_off
                    if cur_p > max_pages:
                        break
                    page_tasks.append(peek_api.search_gifts(gift, page=cur_p, session=session))
                page += len(page_tasks) - 1  # скорректировать page

                batch_results = await asyncio.gather(*page_tasks, return_exceptions=True)
                empty_count = 0
                for batch in batch_results:
                    if isinstance(batch, Exception) or not batch:
                        empty_count += 1
                        continue
                    all_items.extend(batch)
                    for item in batch:
                        owner_name = item.get("owner", "")
                        if detector(owner_name):
                            found.append(item)
                    if len(batch) < 20:
                        empty_count += 1

                # Обновляем прогресс
                now = time.time()
                if now - last_upd[0] >= PROGRESS_UPDATE_INTERVAL:
                    last_upd[0] = now
                    try:
                        await prog_msg.edit_text(
                            f'{tge(E_PARSING, "⏳")} <b>Идет парсинг…</b> {flag} {country_label}\n'
                            f'{tge(E_COUNT, "📊")} Любое кол-во\n'
                            f'{tge(E_GIFT, "🎁")} {html.escape(gift)}\n'
                            f'<code>{_bar(len(all_items), len(all_items) + 20)}</code> стр. {page}\n'
                            f'📦 Просмотрено: {len(all_items)} · Найдено: {len(found)}\n\n'
                            '<i>/stop чтобы остановить</i>',
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception:
                        pass

                if empty_count >= len(page_tasks):
                    break
                if len(found) >= TOTAL_RESULTS * 3:
                    break
                await asyncio.sleep(0.05)  # быстрее между батчами

        # ── AI-уточнение национальности (нейросеть nationalize.io) ──
        # Словарь/скрипт ловит явные случаи; нейросеть добирает латинские/
        # неоднозначные имена, которые словарь пропустил → точнее.
        # НО для стран с уникальным письмом (cn/jp/kr/ar) AI НЕ запускаем —
        # там определение по буквам 100% точное, а нейросеть тащит мусор
        # (русских/латиницу, ошибочно отнесённых к стране).
        if not stop_ev.is_set() and all_items and db_country not in SCRIPT_ONLY_COUNTRIES:
            try:
                nationality_api.reset_api_counters()
                already = {it.get("username") or it.get("owner", "") for it in found}
                try:
                    await prog_msg.edit_text(
                        f'{tge(E_PARSING, "⏳")} <b>AI-уточнение национальности…</b> {flag} {country_label}\n'
                        f'{tge(E_GIFT, "🎁")} {html.escape(gift)}\n'
                        f'🧠 Нейросеть проверяет неоднозначные имена…\n'
                        f'📦 Найдено словарём: {len(found)}',
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
                ai_extra = await nationality_api.ai_refine_country(
                    all_items, db_country, already, stop_event=stop_ev,
                )
                if ai_extra:
                    found.extend(ai_extra)
                    logger.info("AI nationality: +%d для %s", len(ai_extra), db_country)
            except Exception as exc:
                logger.debug("AI nationality refine error: %s", exc)

        # Дедупликация и фильтрация (peek)
        peek_results = _build_peek_results(found, cid)

        # ── Гибрид: дополняем из scanner DB ──
        scanner_rows = await _get_scanner_cached(gift, country)
        scanner_formatted = _scanner_to_peek_format(scanner_rows, gift)

        # Если мало — запускаем быстрый скан (но только если ещё есть время;
        # t.me/nft-скан медленный, для редких стран он и вешал бота на ~10 мин).
        _elapsed = time.time() - _scan_start
        if (len(peek_results) + len(scanner_formatted) < HYBRID_THRESHOLD
                and not stop_ev.is_set() and _elapsed < _TIME_BUDGET):
            try:
                await prog_msg.edit_text(
                    f'{tge(E_PARSING, "⏳")} <b>Дополняю сканером…</b> {flag}\n'
                    f'{tge(E_GIFT, "🎁")} {html.escape(gift)}\n'
                    f'📡 peek.tg: {len(peek_results)} · Запускаю t.me/nft…',
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            extra = await _scanner_quick_scan(gift, db_country, stop_ev)
            scanner_formatted = _scanner_to_peek_format(extra, gift)

        # Объединяем
        results = _merge_peek_and_scanner(peek_results, scanner_formatted, gift)
        # ── Жёсткий фильтр чужаков: убираем явных «не наша страна»
        #    (например, русские имена в выдаче по Индии). ──
        before_guard = len(results)
        results = _drop_wrong_country(results, db_country)
        if before_guard != len(results):
            logger.info("Country guard %s: убрано %d чужаков",
                        db_country, before_guard - len(results))
        results, hidden_viewed, hidden_bots = await _filter_peek_results(results, cid)

        # Перемешиваем, чтобы выдача не была по алфавиту / по номеру
        import random as _rng_c
        _rng_c.shuffle(results)

        # Помечаем просмотренными
        owner_keys = [r["owner_key"] for r in results]
        await db.mark_viewed(cid, owner_keys)

        # Сохраняем в состояние
        st = _st(cid)
        st["results"] = results
        st["page"] = 0
        st["hidden_viewed"] = hidden_viewed
        st["hidden_bots"] = hidden_bots
        st["_template"] = tpl

        total_pages = max(1, math.ceil(len(results) / RESULTS_PER_PAGE))
        src_info = f"peek: {len(peek_results)} · scan: {len(scanner_formatted)}"
        if not results:
            await prog_msg.edit_text(
                f"📭 <b>Ничего не найдено</b>  {flag}\n\n"
                f"🎁 {html.escape(gift)}\n"
                f"👁 <i>Скрыто: {hidden_viewed} просм. · {hidden_bots} ботов</i>\n\n"
                "💡 <i>Попробуй другую коллекцию</i>",
                reply_markup=kb_home(),
                parse_mode=ParseMode.HTML,
            )
        else:
            text = msg_results_page(
                results, 0, len(results), total_pages,
                hidden_viewed, hidden_bots, tpl,
            )
            await prog_msg.edit_text(
                text,
                reply_markup=kb_results(0, total_pages),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

    except Exception as exc:
        logger.error("Peek country scan error: %s", exc, exc_info=True)
        try:
            await prog_msg.edit_text(
                f"❌ <b>Ошибка</b>\n\n<code>{html.escape(str(exc))}</code>",
                reply_markup=kb_home(),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    finally:
        active_scans.pop(cid, None)


async def _do_peek_girls_scan(message: Message, gift: str, country: str | None = None):
    """Парсинг через peek.tg: поиск девушек."""
    cid = message.chat.id
    db_country_tpl = {"chinese": "cn", "russian": "ru"}.get(country, country)
    tpl = await db.get_template(cid, db_country_tpl) or ""

    if cid in active_scans:
        await message.answer("⏳ Уже идёт скан! /stop")
        return

    country_label = COUNTRY_FLAGS.get({"chinese": "cn", "russian": "ru"}.get(country, country), "🌍")
    prog_msg = await message.answer(
        f'{tge(E_PARSING, "⏳")} <b>Идет парсинг…</b> 👩 Девушки {country_label}\n'
        f'{tge(E_GIFT, "🎁")} {html.escape(gift)}\n'
        f'<code>{_bar(0, 1)}</code> 0%\n\n'
        '<i>/stop чтобы остановить</i>',
        parse_mode=ParseMode.HTML,
    )

    stop_ev = asyncio.Event()
    active_scans[cid] = stop_ev

    try:
        # Загружаем все NFT коллекции
        all_items = await peek_api.search_all_pages(
            gift, max_pages=200, stop_event=stop_ev,
        )

        # Фильтр по стране (до гендера — так быстрее)
        _lc = {"chinese": "cn", "russian": "ru"}.get(country, country)
        _det = COUNTRY_DETECTORS.get(_lc)
        if _det:
            all_items = [it for it in all_items if _det(it.get("owner", ""))]

        try:
            await prog_msg.edit_text(
                f'{tge(E_PARSING, "⏳")} <b>Анализ...</b> 👩 Девушки {country_label}\n'
                f'{tge(E_GIFT, "🎁")} {html.escape(gift)}\n'
                f'📦 Загружено: {len(all_items)}\n'
                f'🔍 Определяю пол владельцев…',
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

        # Сброс API лимитов (авторесет по дню)
        gender_api.reset_api_counters()

        # Фильтруем каналы/ботов ДО гендера
        all_items = [it for it in all_items if it.get("username") and not is_bot_or_shop(it.get("username", ""), it.get("owner", ""))]

        try:
            await prog_msg.edit_text(
                f'{tge(E_PARSING, "⏳")} <b>Анализ...</b> 👩 Девушки {country_label}\n'
                f'{tge(E_GIFT, "🎁")} {html.escape(gift)}\n'
                f'📦 Юзеров для проверки: {len(all_items)}\n'
                f'🔍 Определяю пол…',
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

        # Фильтруем по полу
        female_items = await gender_api.filter_female_owners(all_items)

        peek_results = _build_peek_results(female_items, cid)

        # ── Гибрид: дополняем из scanner DB (для любой страны, в т.ч. «Все») ──
        scanner_rows = await _get_scanner_cached(gift, country)
        # Для scanner тоже нужна гендерная фильтрация
        if scanner_rows:
            scanner_items = _scanner_to_peek_format(scanner_rows, gift)
            scanner_items = [it for it in scanner_items if it.get("username") and not is_bot_or_shop(it.get("username", ""), it.get("display_name", ""))]
            scanner_female = await gender_api.filter_female_owners(
                [{"owner": it["display_name"], "username": it["username"]} for it in scanner_items]
            )
            female_usernames = {it.get("username") for it in scanner_female}
            scanner_items = [it for it in scanner_items if it.get("username") in female_usernames]
        else:
            scanner_items = []

        results = _merge_peek_and_scanner(peek_results, scanner_items, gift)
        results, hidden_viewed, hidden_bots = await _filter_peek_results(results, cid)

        owner_keys = [r["owner_key"] for r in results]
        await db.mark_viewed(cid, owner_keys)

        st = _st(cid)
        st["results"] = results
        st["page"] = 0
        st["hidden_viewed"] = hidden_viewed
        st["hidden_bots"] = hidden_bots
        st["_template"] = tpl

        total_pages = max(1, math.ceil(len(results) / RESULTS_PER_PAGE))
        if not results:
            api_status = "✅" if not gender_api.GenderizeAPI.exhausted else f"⚠️ лимит ({gender_api.GenderizeAPI.used})"
            await prog_msg.edit_text(
                f"📭 <b>Девушки не найдены</b>\n\n"
                f"🎁 {html.escape(gift)}\n"
                f"📦 Загружено: {len(all_items)} юзеров\n"
                f"👩 Девушек: {len(female_items)}\n"
                f"👁 Скрыто просм.: {hidden_viewed}\n"
                f"🤖 Gender API: {api_status}\n\n"
                "💡 <i>Попробуй другую коллекцию</i>",
                reply_markup=kb_home(),
                parse_mode=ParseMode.HTML,
            )
        else:
            text = msg_results_page(
                results, 0, len(results), total_pages,
                hidden_viewed, hidden_bots, tpl,
            )
            await prog_msg.edit_text(
                text,
                reply_markup=kb_results(0, total_pages),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

    except Exception as exc:
        logger.error("Peek girls scan error: %s", exc, exc_info=True)
        try:
            await prog_msg.edit_text(
                f"❌ <b>Ошибка</b>\n\n<code>{html.escape(str(exc))}</code>",
                reply_markup=kb_home(),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    finally:
        active_scans.pop(cid, None)


async def _do_peek_market_scan(message: Message, gift: str):
    """Парсинг через peek.tg: маркет Telegram."""
    cid = message.chat.id
    tpl = await db.get_template(cid, "ru") or ""

    if cid in active_scans:
        await message.answer("⏳ Уже идёт скан! /stop")
        return

    prog_msg = await message.answer(
        f'{tge(E_PARSING, "⏳")} <b>Поиск на маркете TG…</b>\n'
        f'{tge(E_GIFT, "🎁")} {html.escape(gift)}\n'
        f'<code>{_bar(0, 1)}</code> 0%\n\n'
        '<i>/stop чтобы остановить</i>',
        parse_mode=ParseMode.HTML,
    )

    stop_ev = asyncio.Event()
    active_scans[cid] = stop_ev

    try:
        all_items = await peek_api.search_all_pages(
            gift, market_only=True, max_pages=200, stop_event=stop_ev,
        )

        # Фильтруем — только внутренний маркет TG (за звёзды)
        tg_market = peek_api.filter_market_telegram(all_items)

        results = _build_peek_results(tg_market, cid, include_market=True)
        results, hidden_viewed, hidden_bots = await _filter_peek_results(results, cid)

        owner_keys = [r["owner_key"] for r in results]
        await db.mark_viewed(cid, owner_keys)

        st = _st(cid)
        st["results"] = results
        st["page"] = 0
        st["hidden_viewed"] = hidden_viewed
        st["hidden_bots"] = hidden_bots
        st["_template"] = tpl

        total_pages = max(1, math.ceil(len(results) / RESULTS_PER_PAGE))
        if not results:
            await prog_msg.edit_text(
                "📭 <b>На маркете TG не найдено</b>\n\n"
                f"🎁 {html.escape(gift)}\n\n"
                "💡 <i>Попробуй другую коллекцию</i>",
                reply_markup=kb_home(),
                parse_mode=ParseMode.HTML,
            )
        else:
            text = msg_results_page(
                results, 0, len(results), total_pages,
                hidden_viewed, hidden_bots, tpl,
            )
            await prog_msg.edit_text(
                text,
                reply_markup=kb_results(0, total_pages),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

    except Exception as exc:
        logger.error("Peek market scan error: %s", exc, exc_info=True)
        try:
            await prog_msg.edit_text(
                f"❌ <b>Ошибка</b>\n\n<code>{html.escape(str(exc))}</code>",
                reply_markup=kb_home(),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    finally:
        active_scans.pop(cid, None)


async def _do_peek_market_country_scan(message: Message, gift: str, country: str):
    """Парсинг через peek.tg: маркет Telegram + фильтр по стране."""
    cid = message.chat.id
    _legacy = {"chinese": "cn", "russian": "ru"}
    db_country = _legacy.get(country, country)
    flag = COUNTRY_FLAGS.get(db_country, "🌍")
    country_label = COUNTRY_LABELS.get(db_country, db_country)
    detector = COUNTRY_DETECTORS.get(db_country, is_chinese_name)
    tpl = await db.get_template(cid, db_country) or ""

    if cid in active_scans:
        await message.answer("⏳ Уже идёт скан! /stop")
        return

    prog_msg = await message.answer(
        f'{tge(E_PARSING, "⏳")} <b>Маркет TG…</b> {flag} {country_label}\n'
        f'{tge(E_GIFT, "🎁")} {html.escape(gift)}\n'
        f'<code>{_bar(0, 1)}</code> 0%\n\n'
        '<i>/stop чтобы остановить</i>',
        parse_mode=ParseMode.HTML,
    )

    stop_ev = asyncio.Event()
    active_scans[cid] = stop_ev

    try:
        all_items = await peek_api.search_all_pages(
            gift, market_only=True, max_pages=200, stop_event=stop_ev,
        )

        # Фильтруем: маркет TG + страна
        tg_market = peek_api.filter_market_telegram(all_items)
        country_filtered = [
            item for item in tg_market
            if detector(item.get("owner", ""))
        ]

        # ── AI-уточнение национальности (нейросеть) ──
        # Для уникального письма (cn/jp/kr/ar) AI не нужен — буквы точнее.
        if not stop_ev.is_set() and tg_market and db_country not in SCRIPT_ONLY_COUNTRIES:
            try:
                nationality_api.reset_api_counters()
                already = {it.get("username") or it.get("owner", "") for it in country_filtered}
                ai_extra = await nationality_api.ai_refine_country(
                    tg_market, db_country, already, stop_event=stop_ev,
                )
                if ai_extra:
                    country_filtered.extend(ai_extra)
            except Exception as exc:
                logger.debug("AI nationality (market) error: %s", exc)

        results = _build_peek_results(country_filtered, cid, include_market=True)
        results = _drop_wrong_country(results, db_country)
        results, hidden_viewed, hidden_bots = await _filter_peek_results(results, cid)

        import random as _rng_mc
        _rng_mc.shuffle(results)

        owner_keys = [r["owner_key"] for r in results]
        await db.mark_viewed(cid, owner_keys)

        st = _st(cid)
        st["results"] = results
        st["page"] = 0
        st["hidden_viewed"] = hidden_viewed
        st["hidden_bots"] = hidden_bots
        st["_template"] = tpl

        total_pages = max(1, math.ceil(len(results) / RESULTS_PER_PAGE))
        if not results:
            await prog_msg.edit_text(
                f"📭 <b>Не найдено на маркете</b> {flag}\n\n"
                f"🎁 {html.escape(gift)}\n\n"
                "💡 <i>Попробуй другую коллекцию</i>",
                reply_markup=kb_home(),
                parse_mode=ParseMode.HTML,
            )
        else:
            text = msg_results_page(
                results, 0, len(results), total_pages,
                hidden_viewed, hidden_bots, tpl,
            )
            await prog_msg.edit_text(
                text,
                reply_markup=kb_results(0, total_pages),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

    except Exception as exc:
        logger.error("Peek market country scan error: %s", exc, exc_info=True)
        try:
            await prog_msg.edit_text(
                f"❌ <b>Ошибка</b>\n\n<code>{html.escape(str(exc))}</code>",
                reply_markup=kb_home(),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    finally:
        active_scans.pop(cid, None)


async def _do_peek_cooldown_scan(message: Message, gift: str, cd_status: str, country: str | None = None):
    """Парсинг через peek.tg: фильтр по кулдауну."""
    cid = message.chat.id
    db_country_tpl = {"chinese": "cn", "russian": "ru"}.get(country, country)
    tpl = await db.get_template(cid, db_country_tpl) or ""
    labels = {"free": "Без кулдауна ✅", "soon": "Скоро снятие ⏰", "active": "На кулдауне 🔒"}
    label = labels.get(cd_status, cd_status)
    country_label = COUNTRY_FLAGS.get({"chinese": "cn", "russian": "ru"}.get(country, country), "🌍")

    if cid in active_scans:
        await message.answer("⏳ Уже идёт скан! /stop")
        return

    prog_msg = await message.answer(
        f'{tge(E_PARSING, "⏳")} <b>Поиск…</b> {label} {country_label}\n'
        f'{tge(E_GIFT, "🎁")} {html.escape(gift)}\n'
        f'<code>{_bar(0, 1)}</code> 0%\n\n'
        '<i>/stop чтобы остановить</i>',
        parse_mode=ParseMode.HTML,
    )

    stop_ev = asyncio.Event()
    active_scans[cid] = stop_ev

    try:
        all_items = await peek_api.search_all_pages(
            gift, max_pages=200, stop_event=stop_ev,
        )

        # Фильтр по стране
        _lc2 = {"chinese": "cn", "russian": "ru"}.get(country, country)
        _det2 = COUNTRY_DETECTORS.get(_lc2)
        if _det2:
            all_items = [it for it in all_items if _det2(it.get("owner", ""))]

        filtered = peek_api.filter_by_cooldown(all_items, cd_status)

        results = _build_peek_results(filtered, cid, include_cooldown=True)
        results, hidden_viewed, hidden_bots = await _filter_peek_results(results, cid)

        owner_keys = [r["owner_key"] for r in results]
        await db.mark_viewed(cid, owner_keys)

        st = _st(cid)
        st["results"] = results
        st["page"] = 0
        st["hidden_viewed"] = hidden_viewed
        st["hidden_bots"] = hidden_bots
        st["_template"] = tpl

        total_pages = max(1, math.ceil(len(results) / RESULTS_PER_PAGE))
        if not results:
            await prog_msg.edit_text(
                f"📭 <b>Не найдено</b> · {label}\n\n"
                f"🎁 {html.escape(gift)}\n"
                f"📦 Загружено: {len(all_items)} юзеров\n"
                f"🕐 С фильтром «{label}»: {len(filtered)}\n"
                f"👁 Скрыто просм.: {hidden_viewed}\n\n"
                "💡 <i>Попробуй другую коллекцию</i>",
                reply_markup=kb_home(),
                parse_mode=ParseMode.HTML,
            )
        else:
            text = msg_results_page(
                results, 0, len(results), total_pages,
                hidden_viewed, hidden_bots, tpl,
            )
            await prog_msg.edit_text(
                text,
                reply_markup=kb_results(0, total_pages),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

    except Exception as exc:
        logger.error("Peek cooldown scan error: %s", exc, exc_info=True)
        try:
            await prog_msg.edit_text(
                f"❌ <b>Ошибка</b>\n\n<code>{html.escape(str(exc))}</code>",
                reply_markup=kb_home(),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    finally:
        active_scans.pop(cid, None)


async def _do_peek_original_scan(message: Message, gift: str):
    """Парсинг через peek.tg: оригинальные владельцы (не передавались)."""
    cid = message.chat.id
    tpl = await db.get_template(cid, "ru") or ""

    if cid in active_scans:
        await message.answer("⏳ Уже идёт скан! /stop")
        return

    prog_msg = await message.answer(
        f'{tge(E_PARSING, "⏳")} <b>Поиск оригинальных владельцев…</b>\n'
        f'{tge(E_GIFT, "🎁")} {html.escape(gift)}\n'
        f'<code>{_bar(0, 1)}</code> 0%\n\n'
        '<i>/stop чтобы остановить</i>',
        parse_mode=ParseMode.HTML,
    )

    stop_ev = asyncio.Event()
    active_scans[cid] = stop_ev

    try:
        all_items = await peek_api.search_all_pages(
            gift, max_pages=200, stop_event=stop_ev,
        )

        original = peek_api.filter_original_owners(all_items)

        results = _build_peek_results(original, cid)
        results, hidden_viewed, hidden_bots = await _filter_peek_results(results, cid)

        owner_keys = [r["owner_key"] for r in results]
        await db.mark_viewed(cid, owner_keys)

        st = _st(cid)
        st["results"] = results
        st["page"] = 0
        st["hidden_viewed"] = hidden_viewed
        st["hidden_bots"] = hidden_bots
        st["_template"] = tpl

        total_pages = max(1, math.ceil(len(results) / RESULTS_PER_PAGE))
        if not results:
            await prog_msg.edit_text(
                "📭 <b>Оригинальные владельцы не найдены</b>\n\n"
                f"🎁 {html.escape(gift)}\n\n"
                "💡 <i>Попробуй другую коллекцию</i>",
                reply_markup=kb_home(),
                parse_mode=ParseMode.HTML,
            )
        else:
            text = msg_results_page(
                results, 0, len(results), total_pages,
                hidden_viewed, hidden_bots, tpl,
            )
            await prog_msg.edit_text(
                text,
                reply_markup=kb_results(0, total_pages),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

    except Exception as exc:
        logger.error("Peek original scan error: %s", exc, exc_info=True)
        try:
            await prog_msg.edit_text(
                f"❌ <b>Ошибка</b>\n\n<code>{html.escape(str(exc))}</code>",
                reply_markup=kb_home(),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    finally:
        active_scans.pop(cid, None)


async def _do_peek_random_scan_all(message: Message, country: str | None = None, original_only: bool = False):
    """Рандомный поиск по ВСЕМ коллекциям (без выбора)."""
    cid = message.chat.id
    db_country_tpl = {"chinese": "cn", "russian": "ru"}.get(country, country)
    tpl = await db.get_template(cid, db_country_tpl) or ""

    if cid in active_scans:
        await message.answer("⏳ Уже идёт скан! /stop")
        return

    tags = []
    _lc4 = {"chinese": "cn", "russian": "ru"}.get(country, country) if country else None
    if _lc4 and _lc4 in COUNTRY_FLAGS:
        tags.append(COUNTRY_FLAGS[_lc4])
    if original_only: tags.append("🆕")
    tag_str = " ".join(tags)

    prog_msg = await message.answer(
        f'{tge(E_PARSING, "⏳")} <b>Рандомный поиск…</b> {tag_str}\n'
        f'{tge(E_GIFT, "🎁")} Все коллекции\n'
        f'<code>{_bar(0, 1)}</code> 0%\n\n'
        '<i>/stop чтобы остановить</i>',
        parse_mode=ParseMode.HTML,
    )

    stop_ev = asyncio.Event()
    active_scans[cid] = stop_ev

    try:
        import random as rng_mod
        # Получаем список всех коллекций
        all_gifts = await peek_api.fetch_gifts_list()
        if not all_gifts:
            await prog_msg.edit_text(
                "❌ <b>Не удалось загрузить список коллекций</b>",
                reply_markup=kb_home(), parse_mode=ParseMode.HTML,
            )
            return

        rng_mod.shuffle(all_gifts)
        selected = all_gifts[:8]  # 8 рандомных коллекций

        all_items = []
        # Тянем все 8 коллекций ПАРАЛЛЕЛЬНО (по случайной странице) — быстро.
        async with peek_api.make_session() as session:
            async def _grab(gname):
                if stop_ev.is_set():
                    return []
                page = rng_mod.randint(1, 30)
                return await peek_api.search_gifts(gname, page=page, session=session)

            batches = await asyncio.gather(
                *[_grab(g) for g in selected], return_exceptions=True
            )
            for batch in batches:
                if isinstance(batch, Exception) or not batch:
                    continue
                all_items.extend(batch)
        try:
            await prog_msg.edit_text(
                f'{tge(E_PARSING, "⏳")} <b>Рандомный поиск…</b>\n'
                f'{tge(E_GIFT, "🎁")} Все коллекции\n'
                f'<code>{_bar(1, 1)}</code> готово\n'
                f'📦 Собрано: {len(all_items)} · обрабатываю…\n',
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

        # Исключаем Fragment
        regular = [
            item for item in all_items
            if not (item.get("market") and item["market"].get("market") == "fragment")
        ]

        # Фильтр по стране
        _lc3 = {"chinese": "cn", "russian": "ru"}.get(country, country)
        _det3 = COUNTRY_DETECTORS.get(_lc3)
        if _det3:
            regular = [it for it in regular if _det3(it.get("owner", ""))]

        # Никогда не передавались
        if original_only:
            regular = peek_api.filter_original_owners(regular)

        rng_mod.shuffle(regular)
        peek_results = _build_peek_results(regular, cid)

        # ── Гибрид: дополняем из scanner DB ──
        db_country = {"chinese": "cn", "russian": "ru"}.get(country, country)
        scanner_rows = []
        for sel_gift in selected[:4]:
            rows = await _get_scanner_cached(sel_gift, country)
            scanner_rows.extend(_scanner_to_peek_format(rows, sel_gift))
        rng_mod.shuffle(scanner_rows)

        results = _merge_peek_and_scanner(peek_results, scanner_rows)
        # Жёсткий фильтр чужаков, если задана страна (рандом по стране)
        if country:
            _lc_g = {"chinese": "cn", "russian": "ru"}.get(country, country)
            results = _drop_wrong_country(results, _lc_g)
        results, hidden_viewed, hidden_bots = await _filter_peek_results(results, cid)

        owner_keys = [r["owner_key"] for r in results]
        await db.mark_viewed(cid, owner_keys)

        st = _st(cid)
        st["results"] = results
        st["page"] = 0
        st["hidden_viewed"] = hidden_viewed
        st["hidden_bots"] = hidden_bots
        st["_template"] = tpl

        total_pages = max(1, math.ceil(len(results) / RESULTS_PER_PAGE))
        if not results:
            await prog_msg.edit_text(
                "📭 <b>Ничего не найдено</b>\n\n"
                f"👁 <i>Скрыто: {hidden_viewed} просм. · {hidden_bots} ботов</i>\n\n"
                "💡 <i>Попробуй снова или сбрось просмотренных</i>",
                reply_markup=kb_home(),
                parse_mode=ParseMode.HTML,
            )
        else:
            text = msg_results_page(
                results, 0, len(results), total_pages,
                hidden_viewed, hidden_bots, tpl,
            )
            await prog_msg.edit_text(
                text,
                reply_markup=kb_results(0, total_pages),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

    except Exception as exc:
        logger.error("Peek random-all scan error: %s", exc, exc_info=True)
        try:
            await prog_msg.edit_text(
                f"❌ <b>Ошибка</b>\n\n<code>{html.escape(str(exc))}</code>",
                reply_markup=kb_home(),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    finally:
        active_scans.pop(cid, None)


async def _do_peek_random_scan(message: Message, gift: str):
    """Парсинг через peek.tg: рандомные обычные люди (конкретная коллекция)."""
    cid = message.chat.id
    tpl = await db.get_template(cid, "ru") or ""

    if cid in active_scans:
        await message.answer("⏳ Уже идёт скан! /stop")
        return

    prog_msg = await message.answer(
        f'{tge(E_PARSING, "⏳")} <b>Рандомный поиск…</b>\n'
        f'{tge(E_GIFT, "🎁")} {html.escape(gift)}\n'
        f'<code>{_bar(0, 1)}</code> 0%\n\n'
        '<i>/stop чтобы остановить</i>',
        parse_mode=ParseMode.HTML,
    )

    stop_ev = asyncio.Event()
    active_scans[cid] = stop_ev

    try:
        # Загружаем несколько рандомных страниц
        import random as rng_mod
        all_items = []
        import aiohttp
        async with peek_api.make_session() as session:
            # Пробуем рандомные страницы
            pages = list(range(1, 100))
            rng_mod.shuffle(pages)
            for p in pages[:15]:
                if stop_ev.is_set():
                    break
                items = await peek_api.search_gifts(gift, page=p, session=session)
                if items:
                    all_items.extend(items)
                if len(all_items) >= TOTAL_RESULTS * 5:
                    break
                await asyncio.sleep(0.15)

        # Не фильтруем по стране — берём обычных людей
        # Исключаем тех кто выставлен на Fragment (богатые)
        regular = [
            item for item in all_items
            if not (item.get("market") and item["market"].get("market") == "fragment")
        ]

        rng_mod.shuffle(regular)
        results = _build_peek_results(regular, cid)
        results, hidden_viewed, hidden_bots = await _filter_peek_results(results, cid)

        owner_keys = [r["owner_key"] for r in results]
        await db.mark_viewed(cid, owner_keys)

        st = _st(cid)
        st["results"] = results
        st["page"] = 0
        st["hidden_viewed"] = hidden_viewed
        st["hidden_bots"] = hidden_bots
        st["_template"] = tpl

        total_pages = max(1, math.ceil(len(results) / RESULTS_PER_PAGE))
        if not results:
            await prog_msg.edit_text(
                "📭 <b>Ничего не найдено</b>\n\n"
                f"🎁 {html.escape(gift)}\n\n"
                "💡 <i>Попробуй другую коллекцию</i>",
                reply_markup=kb_home(),
                parse_mode=ParseMode.HTML,
            )
        else:
            text = msg_results_page(
                results, 0, len(results), total_pages,
                hidden_viewed, hidden_bots, tpl,
            )
            await prog_msg.edit_text(
                text,
                reply_markup=kb_results(0, total_pages),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

    except Exception as exc:
        logger.error("Peek random scan error: %s", exc, exc_info=True)
        try:
            await prog_msg.edit_text(
                f"❌ <b>Ошибка</b>\n\n<code>{html.escape(str(exc))}</code>",
                reply_markup=kb_home(),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    finally:
        active_scans.pop(cid, None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Построение и фильтрация результатов peek.tg
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _build_peek_results(
    items: list[dict], chat_id: int,
    include_market: bool = False,
    include_cooldown: bool = False,
) -> list[dict]:
    """Преобразует peek.tg items в формат результатов бота."""
    seen_owners = set()
    results = []
    for item in items:
        username = item.get("username", "")
        display_name = item.get("owner", "")

        # Строго: без юзернейма — пропускаем (нельзя написать)
        if not username:
            continue

        # Пропускаем слишком дорогие NFT (номер < MIN_NFT_NUMBER)
        gift_number = item.get("giftNumber", "")
        try:
            if gift_number and int(gift_number) < MIN_NFT_NUMBER:
                continue
        except (ValueError, TypeError):
            pass

        owner_key = username
        if owner_key in seen_owners:
            continue
        seen_owners.add(owner_key)

        gift_name = item.get("giftName", item.get("title", ""))
        # Убираем пробелы из названия для ссылки
        slug_name = gift_name.replace(" ", "") if gift_name else ""
        nft_link = f"https://t.me/nft/{slug_name}-{gift_number}" if slug_name and gift_number else ""

        r = {
            "owner_key": owner_key,
            "display_name": display_name,
            "username": username,
            "collection": gift_name,
            "gift_name": gift_name,
            "first_slug": nft_link,
            "nft_link": nft_link,
            "nft_count": 1,
        }

        if include_market:
            market = item.get("market", {})
            if market:
                r["market_price"] = market.get("price", "?")
                r["market_type"] = market.get("market", "?")

        if include_cooldown:
            r["cooldown_status"] = peek_api._cooldown_status(item)
            r["cooldown_remaining"] = peek_api._cooldown_remaining_str(item)

        results.append(r)

    return results


async def _filter_peek_results(
    results: list[dict], chat_id: int,
) -> tuple[list[dict], int, int]:
    """
    Фильтрует результаты: убирает без юзернейма, ботов и просмотренных.
    """
    # Жёсткий фильтр: без юзернейма — не показываем нигде
    results = [r for r in results if r.get("username")]

    # Фильтр ботов / магазинов
    hidden_bots = 0
    clean = []
    for r in results:
        if is_bot_or_shop(r.get("username", ""), r.get("display_name", "")):
            hidden_bots += 1
        else:
            clean.append(r)

    # Дедупликация: скрыть ранее просмотренных
    viewed = await db.get_viewed_keys(chat_id)
    hidden_viewed = 0
    final = []
    for r in clean:
        if r["owner_key"] in viewed:
            hidden_viewed += 1
        else:
            final.append(r)

    return final[:TOTAL_RESULTS], hidden_viewed, hidden_bots


def _send_results(prog_msg, results, tpl, hidden_viewed, hidden_bots):
    """Placeholder — actual sending done inline."""
    pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Фильтрация и сортировка результатов (legacy t.me/nft скан)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _filter_results(
    raw: list[dict], country: str,
    nft_min: int, nft_max: int,
    chat_id: int,
) -> tuple[list[dict], int, int]:
    """
    Применяет фильтры и возвращает (results, hidden_viewed, hidden_bots).
    FIXED: НЕ отсекаем юзеров без username.
    FIXED: НЕ перепроверяем детектором (уже проверено при сканировании).
    """
    # Кросс-коллекционный подсчёт NFT
    owner_keys = [r["owner_key"] for r in raw]
    totals = await db.get_total_nft_counts(owner_keys)
    for r in raw:
        r["total_nft_count"] = totals.get(r["owner_key"], r.get("nft_count", 1))

    # Фильтр по общему NFT + жёсткий лимит
    hard_max = min(nft_max, MAX_NFT_HARD_CAP)
    raw = [r for r in raw if nft_min <= r["total_nft_count"] <= hard_max]

    # Фильтр ботов / магазинов
    hidden_bots = 0
    clean = []
    for r in raw:
        if is_bot_or_shop(r.get("username", ""), r.get("display_name", "")):
            hidden_bots += 1
        else:
            clean.append(r)

    # Дедупликация: скрыть ранее просмотренных
    viewed = await db.get_viewed_keys(chat_id)
    hidden_viewed = 0
    final = []
    for r in clean:
        if r["owner_key"] in viewed:
            hidden_viewed += 1
        else:
            final.append(r)

    return final[:TOTAL_RESULTS], hidden_viewed, hidden_bots


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Сканирование (обычное) — legacy inline flow
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.callback_query(F.data.startswith("nft:"))
async def cb_nft_range(cq: CallbackQuery):
    rng = cq.data.split(":")[1]
    st = _st(cq.message.chat.id)
    st["nft_range"] = rng
    cid = cq.message.chat.id

    mode = st.get("mode", "reg")

    if mode == "rnd":
        await _do_random_scan(cq, st)
    elif mode == "raw":
        await _do_raw_scan(cq, st)
    else:
        await _do_regular_scan(cq, st)


async def _do_regular_scan(cq: CallbackQuery, st: dict):
    cid = cq.message.chat.id
    coll = st.get("collection", "")
    model_raw = st.get("model", "*")
    bd_raw = st.get("backdrop", "*")
    country = st.get("country", "cn")
    tpl = await db.get_template(cid, country) or ""
    st["_template"] = tpl
    rng = st.get("nft_range", DEFAULT_NFT_RANGE)

    model = None if model_raw == "*" else model_raw
    backdrop = None if bd_raw == "*" else bd_raw
    nft_min, nft_max, rng_lbl = NFT_COUNT_RANGES.get(rng, NFT_COUNT_RANGES[DEFAULT_NFT_RANGE])
    flag = COUNTRY_FLAGS.get(country, "🌍")

    if cid in active_scans:
        await cq.answer("⏳ Уже идёт скан! /stop")
        return

    m_d = "любая" if model_raw == "*" else model_raw
    b_d = "любой" if bd_raw == "*" else bd_raw

    prog_msg = await cq.message.edit_text(
        f'{tge(E_PARSING, "⏳")} <b>Сканирование…</b>  {flag}\n'
        f'{tge(E_GIFT, "🎁")} {_d(coll)}\n'
        f'{tge(E_COUNT, "📊")} {rng_lbl}\n\n'
        f'<code>{_bar(0,1)}</code>  0%\n'
        f'📦 0/?\n\n'
        '<i>/stop чтобы остановить</i>',
        parse_mode=ParseMode.HTML,
    )
    await cq.answer("🚀 Поехали!")

    stop_ev = asyncio.Event()
    active_scans[cid] = stop_ev
    last_upd = [time.time()]

    async def on_progress(done, total, found):
        now = time.time()
        if now - last_upd[0] < PROGRESS_UPDATE_INTERVAL and done < total:
            return
        last_upd[0] = now
        pct = int(100 * done / total) if total > 0 else 0
        try:
            await prog_msg.edit_text(
                f'{tge(E_PARSING, "⏳")} <b>Сканирование…</b>  {flag}\n'
                f'{tge(E_GIFT, "🎁")} {_d(coll)}\n'
                f'{tge(E_COUNT, "📊")} {rng_lbl}\n\n'
                f'<code>{_bar(done, total)}</code>  {pct}%\n'
                f'📦 {done:,}/{total:,}\n\n'
                '<i>/stop чтобы остановить</i>'.replace(",", " "),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    try:
        await nft_scanner.scan_collection(
            collection=coll, model_filter=model, backdrop_filter=backdrop,
            progress_callback=on_progress, stop_event=stop_ev,
            max_results=TOTAL_RESULTS * 5, country=country,
        )

        raw = await db.get_users_grouped(
            coll, country, model, backdrop, TOTAL_RESULTS * 5,
        )

        results, hidden_viewed, hidden_bots = await _filter_results(
            raw, country, nft_min, nft_max, cid,
        )

        await db.mark_viewed(cid, [r["owner_key"] for r in results])

        st["results"] = results
        st["page"] = 0
        st["hidden_viewed"] = hidden_viewed
        st["hidden_bots"] = hidden_bots

        total_pages = max(1, math.ceil(len(results) / RESULTS_PER_PAGE))

        if not results:
            await prog_msg.edit_text(
                f"📭 <b>Ничего не найдено</b>  {flag}\n\n"
                f"🎁 {_d(coll)}  ·  🖼 {m_d}  ·  🎨 {b_d}\n\n"
                f"👁 <i>Скрыто: {hidden_viewed} просм. · {hidden_bots} ботов</i>\n\n"
                "💡 <i>Попробуй другую коллекцию</i>",
                reply_markup=kb_home(),
                parse_mode=ParseMode.HTML,
            )
        else:
            text = msg_results_page(
                results, 0, len(results), total_pages,
                hidden_viewed, hidden_bots, tpl,
            )
            await prog_msg.edit_text(
                text,
                reply_markup=kb_results(0, total_pages),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

    except Exception as exc:
        logger.error("Scan error: %s", exc, exc_info=True)
        try:
            await prog_msg.edit_text(
                f"❌ <b>Ошибка</b>\n\n<code>{html.escape(str(exc))}</code>",
                reply_markup=kb_home(),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    finally:
        active_scans.pop(cid, None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Рандомный скан (legacy)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _do_random_scan(cq: CallbackQuery, st: dict):
    cid = cq.message.chat.id
    country = st.get("country", "cn")
    rng = st.get("nft_range", DEFAULT_NFT_RANGE)
    nft_min, nft_max, rng_lbl = NFT_COUNT_RANGES.get(rng, NFT_COUNT_RANGES[DEFAULT_NFT_RANGE])
    flag = COUNTRY_FLAGS.get(country, "🌍")
    tpl = await db.get_template(cid, country) or ""
    st["_template"] = tpl

    if cid in active_scans:
        await cq.answer("⏳ Уже идёт скан! /stop")
        return

    prog_msg = await cq.message.edit_text(
        f'{tge(E_PARSING, "⏳")} <b>Рандомный скан…</b>  {flag}\n'
        f'{tge(E_COUNT, "📊")} {rng_lbl}\n\n'
        f'<code>{_bar(0,1)}</code>  0%\n\n'
        '<i>/stop чтобы остановить</i>',
        parse_mode=ParseMode.HTML,
    )
    await cq.answer("🎲 Поехали!")

    stop_ev = asyncio.Event()
    active_scans[cid] = stop_ev
    last_upd = [time.time()]

    async def on_progress(done, total, current_coll):
        now = time.time()
        if now - last_upd[0] < PROGRESS_UPDATE_INTERVAL:
            return
        last_upd[0] = now
        pct = int(100 * done / total) if total > 0 else 0
        try:
            await prog_msg.edit_text(
                f'{tge(E_PARSING, "⏳")} <b>Рандомный скан…</b>  {flag}\n'
                f'{tge(E_COUNT, "📊")} {rng_lbl}\n'
                f'{tge(E_GIFT, "🎁")} {_d(current_coll)}\n\n'
                f'<code>{_bar(done, total)}</code>  {pct}%\n\n'
                '<i>/stop чтобы остановить</i>',
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    try:
        raw, scanned_colls = await nft_scanner.scan_random(
            ALL_COLLECTIONS, country, on_progress, stop_ev,
        )

        results, hidden_viewed, hidden_bots = await _filter_results(
            raw, country, nft_min, nft_max, cid,
        )

        await db.mark_viewed(cid, [r["owner_key"] for r in results])

        st["results"] = results
        st["page"] = 0
        st["hidden_viewed"] = hidden_viewed
        st["hidden_bots"] = hidden_bots

        total_pages = max(1, math.ceil(len(results) / RESULTS_PER_PAGE))

        if not results:
            colls_str = ", ".join(_d(c) for c in scanned_colls[:5])
            await prog_msg.edit_text(
                f"📭 <b>Ничего не найдено</b>  {flag}\n\n"
                f"🎁 {colls_str}\n\n"
                f"👁 <i>Скрыто: {hidden_viewed} просм. · {hidden_bots} ботов</i>\n\n"
                "💡 <i>Попробуй ещё раз</i>",
                reply_markup=kb_home(),
                parse_mode=ParseMode.HTML,
            )
        else:
            text = msg_results_page(
                results, 0, len(results), total_pages,
                hidden_viewed, hidden_bots, tpl,
            )
            await prog_msg.edit_text(
                text,
                reply_markup=kb_results(0, total_pages),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

    except Exception as exc:
        logger.error("Random scan error: %s", exc, exc_info=True)
        try:
            await prog_msg.edit_text(
                f"❌ <b>Ошибка</b>\n\n<code>{html.escape(str(exc))}</code>",
                reply_markup=kb_home(),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    finally:
        active_scans.pop(cid, None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Пагинация результатов
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.callback_query(F.data.startswith("pg:"))
async def cb_page(cq: CallbackQuery):
    page = int(cq.data.split(":")[1])
    st = _st(cq.message.chat.id)
    results = st.get("results", [])
    if not results:
        await cq.answer("Нет результатов")
        return

    total_pages = max(1, math.ceil(len(results) / RESULTS_PER_PAGE))
    page = max(0, min(page, total_pages - 1))
    st["page"] = page

    result_type = st.get("result_type", "nft")

    if result_type == "numbers":
        text = _format_numbers_page(results, page, total_pages)
    elif result_type == "nft_usernames":
        text = _format_nft_usernames_page(results, page, total_pages)
    elif result_type == "non_upgraded":
        text = _format_non_upgraded_page(results, page, total_pages)
    else:
        tpl = st.get("_template", "")
        text = msg_results_page(
            results, page, len(results), total_pages,
            st.get("hidden_viewed", 0), st.get("hidden_bots", 0), tpl,
        )
    try:
        await cq.message.edit_text(
            text,
            reply_markup=kb_results(page, total_pages),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception:
        pass
    await cq.answer()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Обновить (повторный скан)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.callback_query(F.data == "refresh")
async def cb_refresh(cq: CallbackQuery):
    st = _st(cq.message.chat.id)

    # Peek deep link — повторяем последний скан
    peek_dl = st.get("_peek_dl")
    if peek_dl:
        await cq.answer("🔄 Обновляю…")
        await _handle_deep_link(cq.message, peek_dl)
        return

    mode = st.get("mode", "reg")
    if mode == "rnd":
        await _do_random_scan(cq, st)
    elif mode == "raw":
        if st.get("collection"):
            await _do_raw_scan(cq, st)
        else:
            await cq.answer("Нет параметров для обновления. /start")
    else:
        if st.get("collection"):
            await _do_regular_scan(cq, st)
        else:
            await cq.answer("Нет параметров для обновления. /start")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Экспорт CSV
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.callback_query(F.data == "export")
async def cb_export(cq: CallbackQuery):
    st = _st(cq.message.chat.id)
    results = st.get("results", [])
    if not results:
        await cq.answer("Нет результатов для экспорта")
        return

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["#", "Username", "Имя", "NFT_count", "Коллекция", "Ссылка"])

    for i, r in enumerate(results, 1):
        nft_link = r.get("nft_link", r.get("first_slug", ""))
        if nft_link and not nft_link.startswith("http"):
            nft_link = f"https://t.me/nft/{nft_link}"
        writer.writerow([
            i,
            r.get("username", ""),
            r.get("display_name", ""),
            r.get("total_nft_count", r.get("nft_count", 1)),
            r.get("collection", r.get("gift_name", "")),
            nft_link,
        ])

    content = buf.getvalue().encode("utf-8-sig")
    fname = f"nft_scan_{int(time.time())}.csv"
    doc = BufferedInputFile(content, filename=fname)
    await cq.message.answer_document(
        doc, caption=f"📥 {len(results)} юзеров",
    )
    await cq.answer("📥 Готово!")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Избранное
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.callback_query(F.data == "fav_add")
async def cb_fav_add(cq: CallbackQuery):
    st = _st(cq.message.chat.id)
    results = st.get("results", [])
    if not results:
        await cq.answer("Нет результатов")
        return
    added = 0
    for r in results:
        ok = await db.add_favorite(
            cq.message.chat.id,
            r.get("display_name", ""),
            r.get("username", ""),
            r.get("collection", ""),
            r.get("first_slug", ""),
        )
        if ok:
            added += 1
    await cq.answer(f"⭐ Добавлено {added} в избранное!")


@router.callback_query(F.data == "favs")
async def cb_favs(cq: CallbackQuery):
    await _show_favorites(cq.message.chat.id, cq.message)
    await cq.answer()


@router.callback_query(F.data.startswith("fav_del:"))
async def cb_fav_del(cq: CallbackQuery):
    fav_id = int(cq.data.split(":")[1])
    await db.remove_favorite(cq.message.chat.id, fav_id)
    await _show_favorites(cq.message.chat.id, cq.message, edit=True)
    await cq.answer("Удалено")


async def _show_favorites(chat_id: int, message: Message, edit: bool = False):
    favs = await db.get_favorites(chat_id)
    if not favs:
        text = (
            "⭐ <b>Избранное пусто</b>\n"
            "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
            "Добавляйте юзеров после поиска."
        )
        kb = kb_home()
    else:
        lines = [f"⭐ <b>Избранное</b>  ·  {len(favs)}\n┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"]
        rows = []
        for i, f in enumerate(favs[:20], 1):
            name = html.escape(f.get("display_name", "?"))
            user = html.escape(f.get("username", ""))
            slug = f.get("slug", "")
            u_str = f"@{user}" if user else name
            nft_link = f'<a href="https://t.me/nft/{slug}">{_d(slug.rsplit("-",1)[0])}</a>' if slug else ""
            lines.append(f"  {i}. {u_str}\n       └ {nft_link}")
            rows.append([_btn(f"❌ {(user or name)[:20]}", f"fav_del:{f['id']}")])
        rows.append([_btn("В Меню", "home", icon=E_MENU)])
        text = "\n".join(lines)
        kb = InlineKeyboardMarkup(inline_keyboard=rows)

    if edit:
        try:
            await message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception:
            await message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    else:
        await message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Скан неулучшенных подарков (legacy)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _do_raw_scan(cq: CallbackQuery, st: dict):
    """Скан неулучшенных подарков (model/backdrop пустые)."""
    cid = cq.message.chat.id
    coll = st.get("collection", "")
    country = st.get("country", "cn")
    rng = st.get("nft_range", DEFAULT_NFT_RANGE)
    nft_min, nft_max, rng_lbl = NFT_COUNT_RANGES.get(rng, NFT_COUNT_RANGES[DEFAULT_NFT_RANGE])
    flag = COUNTRY_FLAGS.get(country, "🌍")
    tpl = await db.get_template(cid, country) or ""
    st["_template"] = tpl

    if cid in active_scans:
        await cq.answer("⏳ Уже идёт скан! /stop")
        return

    prog_msg = await cq.message.edit_text(
        f'{tge(E_PARSING, "⏳")} <b>Скан неулучшенных…</b>  {flag}\n'
        f'{tge(E_GIFT, "🎁")} {_d(coll)}\n'
        f'{tge(E_COUNT, "📊")} {rng_lbl}\n\n'
        f'<code>{_bar(0,1)}</code>  0%\n'
        f'📦 0/?\n\n'
        '<i>/stop чтобы остановить</i>',
        parse_mode=ParseMode.HTML,
    )
    await cq.answer("🚀 Поехали!")

    stop_ev = asyncio.Event()
    active_scans[cid] = stop_ev
    last_upd = [time.time()]

    async def on_progress(done, total, found):
        now = time.time()
        if now - last_upd[0] < PROGRESS_UPDATE_INTERVAL and done < total:
            return
        last_upd[0] = now
        pct = int(100 * done / total) if total > 0 else 0
        try:
            await prog_msg.edit_text(
                f'{tge(E_PARSING, "⏳")} <b>Скан неулучшенных…</b>  {flag}\n'
                f'{tge(E_GIFT, "🎁")} {_d(coll)}\n'
                f'{tge(E_COUNT, "📊")} {rng_lbl}\n\n'
                f'<code>{_bar(done, total)}</code>  {pct}%\n'
                f'📦 {done:,}/{total:,}\n\n'
                '<i>/stop чтобы остановить</i>'.replace(",", " "),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    try:
        await nft_scanner.scan_collection(
            collection=coll, model_filter=None, backdrop_filter=None,
            progress_callback=on_progress, stop_event=stop_ev,
            max_results=TOTAL_RESULTS * 5, country=country,
        )

        raw = await db.get_non_upgraded_users(coll, country, TOTAL_RESULTS * 5)

        results, hidden_viewed, hidden_bots = await _filter_results(
            raw, country, nft_min, nft_max, cid,
        )

        await db.mark_viewed(cid, [r["owner_key"] for r in results])

        st["results"] = results
        st["page"] = 0
        st["hidden_viewed"] = hidden_viewed
        st["hidden_bots"] = hidden_bots

        total_pages = max(1, math.ceil(len(results) / RESULTS_PER_PAGE))

        if not results:
            await prog_msg.edit_text(
                f"📭 <b>Ничего не найдено</b>  {flag}\n\n"
                f"🎁 {_d(coll)}  ·  неулучшенные\n\n"
                f"👁 <i>Скрыто: {hidden_viewed} просм. · {hidden_bots} ботов</i>\n\n"
                "💡 <i>Попробуй другую коллекцию</i>",
                reply_markup=kb_home(),
                parse_mode=ParseMode.HTML,
            )
        else:
            text = msg_results_page(
                results, 0, len(results), total_pages,
                hidden_viewed, hidden_bots, tpl,
            )
            await prog_msg.edit_text(
                text,
                reply_markup=kb_results(0, total_pages),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

    except Exception as exc:
        logger.error("Raw scan error: %s", exc, exc_info=True)
        try:
            await prog_msg.edit_text(
                f"❌ <b>Ошибка</b>\n\n<code>{html.escape(str(exc))}</code>",
                reply_markup=kb_home(),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    finally:
        active_scans.pop(cid, None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Сброс просмотренных (callback)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.callback_query(F.data == "reset_viewed")
async def cb_reset_viewed(cq: CallbackQuery):
    await db.clear_viewed(cq.message.chat.id)
    await cq.answer("✅ Просмотренные сброшены!", show_alert=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Шаблоны сообщений
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.callback_query(F.data == "templates")
async def cb_templates(cq: CallbackQuery):
    if not await _check_sub(cq.from_user.id, cq.bot):
        await cq.message.edit_text(MSG_SUBSCRIBE, reply_markup=_kb_subscribe(), parse_mode=ParseMode.HTML)
        await cq.answer()
        return

    cid = cq.message.chat.id
    tpl_ru = await db.get_template(cid, "ru")
    tpl_cn = await db.get_template(cid, "cn")

    ru_status = f"✅ <i>{html.escape(tpl_ru[:40])}{'…' if len(tpl_ru) > 40 else ''}</i>" if tpl_ru else "❌ <i>не задан</i>"
    cn_status = f"✅ <i>{html.escape(tpl_cn[:40])}{'…' if len(tpl_cn) > 40 else ''}</i>" if tpl_cn else "❌ <i>не задан</i>"

    text = (
        "✉️ <b>Шаблоны сообщений</b>\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
        "Шаблон — это текст, который будет\n"
        "вставлен в поле ввода при нажатии\n"
        "«Написать» рядом с юзером.\n\n"
        "Вам останется лишь нажать «Отправить».\n\n"
        f"🇷🇺 <b>Россия:</b>  {ru_status}\n"
        f"🇨🇳 <b>Китай:</b>  {cn_status}\n"
    )

    rows = []
    if tpl_ru:
        rows.append([_btn("🇷🇺 Изменить", "tpl_edit:ru"), _btn("🗑 Удалить", "tpl_del:ru")])
    else:
        rows.append([_btn("🇷🇺 Добавить шаблон", "tpl_edit:ru")])
    if tpl_cn:
        rows.append([_btn("🇨🇳 Изменить", "tpl_edit:cn"), _btn("🗑 Удалить", "tpl_del:cn")])
    else:
        rows.append([_btn("🇨🇳 Добавить шаблон", "tpl_edit:cn")])
    rows.append([_btn("В Меню", "home", icon=E_MENU)])

    await cq.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode=ParseMode.HTML,
    )
    await cq.answer()


@router.callback_query(F.data.startswith("tpl_edit:"))
async def cb_tpl_edit(cq: CallbackQuery):
    country = cq.data.split(":")[1]
    st = _st(cq.message.chat.id)
    st["search_action"] = f"tpl_edit_{country}"

    flag = "🇷🇺" if country == "ru" else "🇨🇳"
    await cq.message.edit_text(
        f"✉️ <b>Шаблон для {flag}</b>\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
        "Введите текст шаблона.\n\n"
        "Этот текст будет вставлен в поле\n"
        "ввода при нажатии «Написать».\n\n"
        "<i>Пример: Привет! Меня интересует\n"
        "ваш NFT-подарок. Готовы обсудить?</i>\n\n"
        "<i>/cancel — отмена</i>",
        parse_mode=ParseMode.HTML,
    )
    await cq.answer()


@router.callback_query(F.data.startswith("tpl_del:"))
async def cb_tpl_del(cq: CallbackQuery):
    country = cq.data.split(":")[1]
    await db.delete_template(cq.message.chat.id, country)
    await cq.answer("🗑 Шаблон удалён!", show_alert=True)
    cq.data = "templates"
    await cb_templates(cq)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Зеркала
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.callback_query(F.data == "mirror")
async def cb_mirror(cq: CallbackQuery):
    text = (
        "🪞 <b>Создать зеркало</b>\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\n"
        "Зеркало — твоя копия бота.\n"
        "Все функции работают, база общая.\n\n"
        "<b>Инструкция:</b>\n"
        "  1. Открой @BotFather\n"
        "  2. /newbot → задай имя и юзернейм\n"
        "  3. Скопируй <b>токен</b>\n"
        "  4. Отправь токен сюда 👇\n\n"
        "<i>/cancel — отмена</i>"
    )
    st = _st(cq.message.chat.id)
    st["search_action"] = "mirror_token"
    await cq.message.edit_text(text, parse_mode=ParseMode.HTML)
    await cq.answer()


async def _mirror_polling_loop(m_bot: Bot, token: str):
    """Ручной polling для одного зеркала."""
    offset = 0
    try:
        old = await m_bot.get_updates(offset=-1, timeout=1)
        if old:
            offset = old[-1].update_id + 1
    except Exception:
        pass
    while True:
        try:
            updates = await m_bot.get_updates(offset=offset, timeout=30)
            for upd in updates:
                offset = upd.update_id + 1
                try:
                    await dp.feed_update(m_bot, upd)
                except Exception as e:
                    logger.error("Mirror feed error: %s", e)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Mirror polling error (%s): %s", token[:20], e)
            await asyncio.sleep(5)


async def _activate_mirror(token: str) -> Bot | None:
    """Создаёт Bot-объект и запускает polling для зеркала."""
    try:
        if TELEGRAM_API_SERVER:
            _s = AiohttpSession(api=TelegramAPIServer.from_base(TELEGRAM_API_SERVER))
            new_bot = Bot(token=token, session=_s)
        else:
            new_bot = Bot(token=token)
        info = await new_bot.get_me()
        logger.info("Зеркало активировано: @%s", info.username)
        mirror_bots[token] = new_bot
        task = asyncio.create_task(_mirror_polling_loop(new_bot, token))
        mirror_tasks[token] = task
        return new_bot
    except Exception as e:
        logger.error("Ошибка активации зеркала: %s", e)
        return None


@router.callback_query(F.data == "adm:mirrors")
async def cb_adm_mirrors(cq: CallbackQuery):
    if not _is_admin(cq.from_user.id):
        await cq.answer("⛔", show_alert=True)
        return

    mirrors = await db.get_all_mirrors()
    if not mirrors:
        text = "🪞 <b>Зеркала</b>\n┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n\nНет зеркал."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [_btn("Назад", "adm:back")],
        ])
    else:
        lines = [f"🪞 <b>Зеркала</b>  ·  {len(mirrors)}\n┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"]
        rows = []
        for m in mirrors:
            uname = m.get("bot_username") or "?"
            oid = m.get("owner_id", 0)
            active = "🟢" if m["bot_token"] in mirror_bots else "🔴"
            lines.append(f"{active} @{uname}  ·  owner: <code>{oid}</code>")
            rows.append([_btn(f"❌ @{uname}", f"adm:mirdel:{m['id']}")])
        rows.append([_btn("Назад", "adm:back")])
        text = "\n".join(lines)
        kb = InlineKeyboardMarkup(inline_keyboard=rows)

    await cq.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await cq.answer()


@router.callback_query(F.data.startswith("adm:mirdel:"))
async def cb_adm_mirror_del(cq: CallbackQuery):
    if not _is_admin(cq.from_user.id):
        await cq.answer("⛔", show_alert=True)
        return

    mid = int(cq.data.split(":")[2])
    mirrors = await db.get_all_mirrors()
    target = next((m for m in mirrors if m["id"] == mid), None)
    if target:
        token = target["bot_token"]
        if token in mirror_tasks:
            mirror_tasks[token].cancel()
            mirror_tasks.pop(token, None)
        if token in mirror_bots:
            try:
                await mirror_bots[token].session.close()
            except Exception:
                pass
            mirror_bots.pop(token, None)
        await db.remove_mirror(mid)

    await cq.answer("🗑 Удалено")
    await cb_adm_mirrors(cq)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Запуск
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _start_mirrors():
    """Запуск всех зеркал из БД."""
    mirrors = await db.get_all_mirrors()
    for m in mirrors:
        token = m["bot_token"]
        try:
            await _activate_mirror(token)
        except Exception as e:
            logger.warning("Зеркало %s не запустилось: %s", m.get("bot_username"), e)


async def _health(_request: web.Request):
    return web.json_response({"ok": True, "service": "nft-parser"})


async def _start_web_server():
    """Tiny HTTP server required by Render Web Service health/port detection."""
    port = int(os.getenv("PORT", "10000"))
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Render health server listening on 0.0.0.0:%d", port)
    return runner


async def main():
    # Start the HTTP health server first so Render can detect the port
    # even while the database and Telegram clients are initializing.
    web_runner = await _start_web_server()

    logger.info("Инициализация БД…")
    await db.init_db()
    os.makedirs(CSV_DIR, exist_ok=True)

    # Stars Rating checker uses the uploaded my_session.session.
    await rating_api.start()

    # Запуск зеркал
    await _start_mirrors()

    max_retries = 5
    try:
        for attempt in range(1, max_retries + 1):
            try:
                logger.info("Запуск бота (попытка %d/%d)…", attempt, max_retries)
                await dp.start_polling(bot, skip_updates=True)
                break
            except Exception as e:
                logger.error("Polling упал: %s", e)
                if attempt < max_retries:
                    wait = min(30 * attempt, 120)
                    logger.info("Повтор через %d сек…", wait)
                    await asyncio.sleep(wait)
                else:
                    logger.critical("Все попытки исчерпаны, выход.")
                    raise
    finally:
        await rating_api.stop()
        await web_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
