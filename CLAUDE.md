# Проект: Секреты — Библиотека контента и Telegram-бот

> **Инструкция для Claude:** После каждого значимого изменения (новый деплой, изменение файлов, исправление багов, добавление фич) — обнови секции «Текущий статус» и «Что осталось сделать» в этом файле. В конце сессии — тоже обнови. Если добавляешь новый файл или меняешь роль существующего — обнови секцию «Ключевые файлы». Если принимаешь архитектурное решение — запиши в «Принятые решения».

## Что это за проект

Система для бизнес-клуба «Секреты» (Таня Меламори): Telegram-бот с онбордингом, персональными дорожными картами и мини-приложением — библиотекой контента. Бот задаёт пользователю вопросы, подбирает релевантные посты из базы знаний (496 постов), генерирует дорожную карту и отправляет материалы — сразу или порционно (drip).

## Ключевые файлы

### Telegram-бот
- **`bot.py`** — основной бот. Онбординг (5 вопросов: Q1-Q4 + CHAT), дорожные карты на основе roadmap JSON, drip-рассылка (JobQueue, ежедневно в 10:00 МСК) с реальным днём карты («День X из Y»), команда `/my_roadmap`, свободный чат с Claude для подбора постов, коррекция маршрута по обратной связи (Claude перестраивает оставшиеся посты). Env vars: `TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY`, `DATA_DIR`.
- **`roadmap_all_pains_v4.json`** — 11 pain points с подобранными постами для каждого. Используется для формирования дорожных карт.
- **`user_roadmaps.json`** — сохранённые дорожные карты пользователей (sent_index, delivery_count, pause и т.д.)
- **`onboarding_log.csv`** — лог ответов пользователей на онбординг

### Библиотеки (две активные версии!)
- **`app.html`** (Mini App для бота) — открывается из Telegram через WebAppInfo. Поддерживает `?ids=` для фильтрации по дорожной карте, roadmap-баннер "Ваша персональная дорожная карта", loading spinner + error handling для медленных соединений. Типы: текст/эфир (материалы убраны). Подкатегории: «Путь Тани» по годам, «Менторство у психолога» под «Рост личности». URL: `https://olyunyaa.github.io/secrets-library/app.html`
- **`library_v2.html`** (Версия библиотеки с тезисами) — десктопная версия, каждый пост показан с ключевыми тезисами (`key_theses`) и сводкой (`summary`), ссылки ведут в Telegram-канал. Поиск по заголовку, теме, тезисам. Фильтры: категория, тип (текст/эфир), подкатегории (Путь Тани по годам). URL: `https://olyunyaa.github.io/secrets-library/library_v2.html`
- **`index.html`** — другая версия библиотеки (с hero-секцией, избранным, dropdown категорий), но без тезисов. Стоит на корне GitHub Pages.
- **`library_secrets.html`** — более ранняя версия (не используется)

### Данные для библиотек
- **`knowledge_base.js`** (2.3 MB) — `const POSTS_DATA = [...]`, все 496 постов с метаданными. Используется обеими библиотеками через `<script defer>`.
- **`knowledge_base_enriched.json`** — обогащённая база (категории, теги, generated_title). Источник для генерации knowledge_base.js.
- **`photos/`** — фотографии к постам

### Парсинг и обогащение данных
- **`fetch_new_posts.py`** — загрузка новых постов из Telegram API (Telethon)
- **`enrich_posts.py`** — обогащение постов через Claude (категории, теги, сводки)
- **`backfill_photos.py`** — загрузка фотографий для постов
- **`update_efir_links.py`** — обновление ссылок на эфиры
- **`update_roadmap.py`** — генерация/обновление roadmap JSON

### Анализ
- **`analyze_categories.py`** / **`comprehensive_audit.py`** — аудит категорий и качества данных
- **`CATEGORY_AUDIT_FULL_REPORT.txt`** — результаты аудита

### Деплой (Fly.io)
- **`Dockerfile`** — Python 3.11-slim, копирует bot.py + data files, запускает `python bot.py`
- **`fly.toml`** — конфиг Fly.io: регион fra, volume `/data` для persistent storage
- **`requirements.txt`** — `python-telegram-bot[job-queue]==21.*`, python-dotenv, pytz, anthropic
- **`Procfile`** — `worker: python bot.py` (legacy, не используется на Fly.io)
- **`.dockerignore`** — исключает .env, HTML, изображения, _from_desktop

### Прочее
- **`go.html`** — редирект на бота
- **`_from_desktop/`** — старые файлы, перемещены 10.03.2026

## Принятые решения

### Архитектура бота
- **Polling** (не webhook) — проще для single-instance бота
- **Claude Haiku 4.5** для генерации дорожных карт и подбора постов (баланс цена/качество)
- **Drip-рассылка**: batch по 2 поста, интервалы зависят от периода (7д→каждый день, 90д→каждые 5 дней)
- **Обратная связь**: каждые 3 батча спрашиваем "Как материалы?", при коррекции Claude перестраивает оставшийся маршрут
- **Persistent storage**: `DATA_DIR` env var → `/data` volume на Fly.io, локально — рядом с bot.py

### Хостинг
- **Fly.io** (бесплатный tier) — выбран после того, как Railway оказался платным. Машина во Франкфурте (fra), 1GB encrypted volume для данных.
- **GitHub Pages** — для статики (app.html, knowledge_base.js) на olyunyaa.github.io/secrets-library/

### Данные
- **knowledge_base.js** — формат для прямой загрузки в браузер (`const POSTS_DATA = [...]`)
- **roadmap_all_pains_v4.json** — финальная версия (v1-v3 были итерациями)
- Посты подбираются по Q2-ответам → pain points → готовые списки постов (не через Claude каждый раз)

## Репозиторий и URL-ы
- **GitHub**: `git@github.com:Olyunyaa/secrets-library.git`
- **Fly.io app**: `secrets-library-bot` (дашборд: https://fly.io/apps/secrets-library-bot)

### GitHub Pages URL-ы
- `/` → `index.html` (версия библиотеки с hero, избранным, без тезисов)
- `/library_v2.html` → библиотека с тезисами (десктопная, актуальная)
- `/app.html` → Mini App для бота (с `?ids=` фильтрацией)

## Env vars (в .env и Fly.io secrets)
- `TELEGRAM_BOT_TOKEN` — токен бота
- `ANTHROPIC_API_KEY` — ключ Anthropic API
- `DATA_DIR` — путь к папке данных (на Fly.io: `/data`, локально: не задан → `Path(__file__).parent`)

## Команды

### Деплой бота на Fly.io
```bash
cd ~/Desktop/Ontri\ Проект\ для\ Секреты/
~/.fly/bin/flyctl deploy --app secrets-library-bot --yes
```

### Логи бота
```bash
~/.fly/bin/flyctl logs --app secrets-library-bot --no-tail
```

### Статус бота
```bash
~/.fly/bin/flyctl status --app secrets-library-bot
```

### Пуш статики на GitHub Pages
```bash
cd ~/Desktop/Ontri\ Проект\ для\ Секреты/
git add app.html knowledge_base.js photos/
git commit -m "Update library"
git push
```

## Текущий статус (обновлено 20.03.2026)

- Бот **задеплоен и работает** на Fly.io (Франкфурт)
- bot.py обновлён: 5 вопросов онбординга (Q1-Q4 + CHAT), drip с реальным днём карты, `/my_roadmap`, коррекция маршрута
- `app.html` обновлён: убраны «материалы», добавлена подкатегория «Менторство у психолога» под «Рост личности»
- База знаний: 496 постов (9 лишних постов удалены)
- Fly.io volume `/data` подключён, user_roadmaps.json и onboarding_log.csv создадутся при первом использовании

## Что осталось сделать

- [ ] Протестировать бота end-to-end: `/start` → онбординг → дорожная карта → drip
- [ ] Проверить drip-рассылку (дождаться следующего дня или протестировать вручную)
- [ ] Добавить `.gitignore` для исключения временных/личных файлов из репо
- [ ] Рассмотреть webhook вместо polling для экономии ресурсов Fly.io (polling держит машину active 24/7)
- [ ] Обновить library_v2.html — убрать «материалы» аналогично app.html
