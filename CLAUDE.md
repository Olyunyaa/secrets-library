# Проект: Секреты — Библиотека контента и Telegram-бот

> **Инструкция для Claude:** После каждого значимого изменения (новый деплой, изменение файлов, исправление багов, добавление фич) — обнови секции «Текущий статус» и «Что осталось сделать» в этом файле. В конце сессии — тоже обнови. Если добавляешь новый файл или меняешь роль существующего — обнови секцию «Ключевые файлы». Если принимаешь архитектурное решение — запиши в «Принятые решения».

## Что это за проект

Система для бизнес-клуба «Секреты» (Таня Меламори): Telegram-бот с онбордингом, персональными дорожными картами и мини-приложением — библиотекой контента. Бот задаёт пользователю вопросы, подбирает релевантные посты из базы знаний (496 постов), генерирует дорожную карту и отправляет материалы — сразу или порционно (drip).

## Ключевые файлы

### Бот (Salebot.pro интеграция)
- **`bot_salebot.py`** — **активный** бот (Flask webhook + Salebot API). Та же бизнес-логика: онбординг Q1-Q4 + CHAT, дорожные карты, drip-рассылка (APScheduler, 10:00 МСК), свободный чат с Claude, коррекция маршрута, кнопки «Больше про [тему]». Работает через Salebot.pro — все сообщения идут через бота Sailbot (управляемого Salebot). Env vars: `SALEBOT_API_KEY`, `WEBHOOK_SECRET`, `ANTHROPIC_API_KEY`, `DATA_DIR`.
- **`bot.py`** — **backup** (старый standalone Telegram-бот на polling, `python-telegram-bot`). Не используется в production.
- **`roadmap_all_pains_v4.json`** — 11 pain points с подобранными постами для каждого. Используется для формирования дорожных карт.
- **`user_roadmaps.json`** — сохранённые дорожные карты пользователей (sent_index, delivery_count, pause и т.д.). Ключ: Salebot client_id.
- **`user_states.json`** — состояния диалогов (state machine: q1→q2→q3→q4→chat). Ключ: Salebot client_id.
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
- **`Dockerfile`** — Python 3.11-slim, копирует bot_salebot.py + data files, запускает `python bot_salebot.py`
- **`fly.toml`** — конфиг Fly.io: регион ams, HTTP service на порту 8080 (для webhook), volume `/data`, min_machines_running=1
- **`requirements.txt`** — flask, requests, python-dotenv, pytz, anthropic, apscheduler
- **`.dockerignore`** — исключает .env, HTML, изображения, _from_desktop

### Прочее
- **`go.html`** — редирект на бота
- **`_from_desktop/`** — старые файлы, перемещены 10.03.2026

## Принятые решения

### Архитектура бота (Salebot.pro интеграция — 23.03.2026)
- **Flask webhook** (не polling) — Salebot шлёт outgoing webhook на наш endpoint
- **Salebot API** (`/api/{key}/message`) для отправки сообщений пользователям
- **APScheduler** вместо python-telegram-bot JobQueue для drip-рассылки
- **Синхронные** обработчики (не async) — Flask + requests
- **Salebot client_id** как первичный ключ пользователя (не Telegram user_id)
- **user_states.json** — persistent state machine (вместо ConversationHandler)
- **Multi-select UX**: каждый toggle отправляет НОВОЕ сообщение с [x]/[ ] маркерами (Salebot API не может редактировать сообщения)
- **Нажатие inline-кнопки**: Salebot отправляет текст кнопки как обычное сообщение → мы парсим текст и сопоставляем с опциями

### Старые решения (сохранены)
- **Claude Haiku 4.5** для генерации дорожных карт и подбора постов (баланс цена/качество)
- **Drip-рассылка**: batch по 2 поста, интервалы зависят от периода (7д→каждый день, 60д→каждые 4 дня)
- **Обратная связь**: каждые 3 батча спрашиваем "Как материалы?", при коррекции Claude перестраивает оставшийся маршрут
- **«Больше про...»**: кнопки показываются только при >1 pain point; 5 постов за нажатие; лимит 15 бонусных на тему; сначала из roadmap, потом Claude Haiku подбирает из общей базы
- **Persistent storage**: `DATA_DIR` env var → `/data` volume на Fly.io, локально — рядом с bot_salebot.py

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
- `SALEBOT_API_KEY` — API ключ Salebot.pro
- `WEBHOOK_SECRET` — секрет для URL webhook-а (часть URL: `/webhook/<secret>`)
- `ANTHROPIC_API_KEY` — ключ Anthropic API
- `DATA_DIR` — путь к папке данных (на Fly.io: `/data`, локально: не задан → `Path(__file__).parent`)
- `TELEGRAM_BOT_TOKEN` — ~~старый~~ токен бота (не используется в bot_salebot.py, нужен для bot.py backup)

## Команды

### Установить secrets перед первым деплоем Salebot-версии
```bash
~/.fly/bin/flyctl secrets set SALEBOT_API_KEY="6d4e8f02719f0033413928f783a7c2ba" --app secrets-library-bot
~/.fly/bin/flyctl secrets set WEBHOOK_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" --app secrets-library-bot
```

### Деплой бота на Fly.io
```bash
cd ~/Desktop/Ontri\ Проект\ для\ Секреты/
~/.fly/bin/flyctl deploy --app secrets-library-bot --yes
```

### Узнать WEBHOOK_SECRET (для передачи Вове)
```bash
~/.fly/bin/flyctl ssh console --app secrets-library-bot -C 'echo $WEBHOOK_SECRET'
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

## Текущий статус (обновлено 23.03.2026)

### Salebot.pro интеграция — 23.03.2026
- Создан **`bot_salebot.py`** — Flask webhook app с полной бизнес-логикой из bot.py
- Архитектура: Пользователь ↔ Telegram ↔ Salebot.pro ↔ Flask (Fly.io) ↔ Salebot API → пользователю
- Multi-select: [x]/[ ] маркеры в тексте кнопок (Salebot не может редактировать сообщения)
- State machine в `user_states.json` (вместо ConversationHandler)
- APScheduler для drip (вместо JobQueue)
- URL-кнопки (вместо WebAppInfo) — app.html НЕ использует Telegram.WebApp API
- Dockerfile и fly.toml обновлены для HTTP service
- requirements.txt обновлён: flask, requests, apscheduler (вместо python-telegram-bot)
- **Не задеплоено** — нужно: установить secrets, задеплоить, дать Вове webhook URL

### Предыдущие изменения
- База знаний: 496 постов (9 лишних удалены)
- `app.html` обновлён: убраны «материалы», подкатегория «Менторство у психолога»
- roadmap_all_pains_v4.json обновлён (pain_2, pain_3)
- Fly.io volume `/data` подключён

## Что осталось сделать

### Деплой Salebot-версии
- [ ] Установить Fly.io secrets: `SALEBOT_API_KEY`, `WEBHOOK_SECRET`
- [ ] Задеплоить bot_salebot.py на Fly.io
- [ ] Дать Вове webhook URL: `https://secrets-library-bot.fly.dev/webhook/<secret>`
- [ ] Вова: настроить webhook URL в Salebot.pro
- [ ] Вова: настроить Reply keyboard: "Онбординг и Библиотека", "Библиотека" (URL), "Моя дорожная карта"
- [ ] Вова: НЕ создавать funnels для текстов кнопок, отключить auto-response

### Тестирование
- [ ] Отправить "Онбординг и Библиотека" → должен прийти Q1
- [ ] Пройти весь онбординг Q1→Q2→Q3→Q4
- [ ] Проверить roadmap URL-кнопку
- [ ] Написать свободный текст → Claude подберёт посты
- [ ] Проверить drip delivery (дождаться 10:00 или тестовый запуск)
- [ ] Проверить "Больше про..." кнопки
- [ ] Проверить pause/resume/my_roadmap

### Прочее
- [ ] **Запушить статику** на GitHub Pages
- [ ] Добавить `.gitignore`
- [ ] Обновить library_v2.html — убрать «материалы»
