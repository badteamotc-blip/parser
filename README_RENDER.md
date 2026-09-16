# NFT Parser — Render Web Service

## Что важно

- Фильтр Stars Rating включён.
- Допускаются **L0, L1 и L2**.
- **L3 и выше не выдаются**.
- Если рейтинг нельзя подтвердить, аккаунт не выдаётся.
- Рейтинг читается через MTProto `users.getFullUser` из `my_session.session`.
- HTTP `/health` слушает `0.0.0.0:$PORT`, поэтому проект подходит для Render Web Service.

## Перед загрузкой в GitHub

Положите рядом с `bot.py` файл:

```text
my_session.session
```

Это авторизованная пользовательская Telethon-сессия. Не используйте чужую сессию.

## Render

Можно использовать `render.yaml` или создать Web Service вручную:

- Build: `pip install -r requirements.txt`
- Start: `python bot.py`
- Health Check: `/health`

Переменные:

```text
BOT_TOKEN=...
ADMIN_IDS=8794223703
TELEGRAM_API_ID=36435539
TELEGRAM_API_HASH=...
TELEGRAM_SESSION_FILE=my_session.session
RATING_FILTER_ENABLED=true
RATING_MAX_LEVEL=2
RATING_CACHE_TTL=600
RATING_CONCURRENCY=3
DB_PATH=<project>/data/nft_cache.db
CSV_DIR=<project>/data/exports
```

`TELEGRAM_API_HASH`, `BOT_TOKEN` и `.session` являются секретами. Закрытый репозиторий лучше, чем публичный, но безопаснее хранить секреты в Render Environment Variables/Secret Files и не коммитить их в Git.
