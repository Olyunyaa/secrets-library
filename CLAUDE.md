# Проект: Секреты — Библиотека контента и Telegram-бот

> **Инструкция для Claude:** После каждого значимого изменения (новый деплой, изменение файлов, исправление багов, добавление фич) — обнови секции «Текущий статус» и «Что осталось сделать» в этом файле. В конце сессии — тоже обнови. Если добавляешь новый файл или меняешь роль существующего — обнови секцию «Ключевые файлы». Если принимаешь архитектурное решение — запиши в «Принятые решения».

## Что это за проект

Система для бизнес-клуба «Секреты» (Таня Меламори): Telegram-бот с онбордингом, персональными дорожными картами и мини-приложением — библиотекой контента. Бот задаёт пользователю вопросы, подбирает релевантные посты из базы знаний (517 постов), генерирует дорожную карту и отправляет материалы — сразу или порционно (drip).

## Ключевые файлы

### Бот (Salebot.pro интеграция)
- **`bot_salebot.py`** — **активный** бот (Flask webhook + Salebot API + Telegram Bot API). Онбординг Q1-Q4 + CHAT, дорожные карты, drip-рассылка (APScheduler, 10:00 МСК), свободный чат с Claude, коррекция маршрута, кнопки «Больше про [тему]». Работает через Salebot.pro (webhook + API) + прямые вызовы Telegram Bot API для клавиатур и callback-ответов. **Задеплоен на Fly.io** и принимает webhook-и. Env vars: `SALEBOT_API_KEY`, `WEBHOOK_SECRET`, `ANTHROPIC_API_KEY`, `DATA_DIR`, `TELEGRAM_BOT_TOKEN`.
- **`bot.py`** — **backup** (старый standalone Telegram-бот на polling, `python-telegram-bot`). Не используется в production.
- **`roadmap_all_pains_v4.json`** — 11 pain points с подобранными постами для каждого. Используется для формирования дорожных карт.
- **`user_roadmaps.json`** — сохранённые дорожные карты пользователей (sent_index, delivery_count, pause и т.д.). Ключ: Salebot client_id.
- **`user_states.json`** — состояния диалогов (state machine: q1→q2→q3→q4→chat). Ключ: Salebot client_id.
- **`onboarding_log.csv`** — лог ответов пользователей на онбординг
- **`analytics_log.csv`** — аналитика всех событий бота (start, toggle, done, chat_request, skip и т.д.). Формат: `timestamp, client_id, event, detail`. Хранится на `/data` volume.

### Библиотеки (две активные версии!)
- **`app.html`** (Mini App для бота) — открывается из Telegram через WebAppInfo. Поддерживает `?ids=` для фильтрации по дорожной карте, roadmap-баннер "Ваша персональная дорожная карта", loading spinner + error handling для медленных соединений. Типы: текст/эфир (материалы убраны). Подкатегории: «Путь Тани» по годам, «Менторство у психолога» под «Рост личности». URL: `https://olyunyaa.github.io/secrets-library/app.html`
- **`library_v2.html`** (Версия библиотеки с тезисами) — десктопная версия, каждый пост показан с ключевыми тезисами (`key_theses`) и сводкой (`summary`), ссылки ведут в Telegram-канал. Поиск по заголовку, теме, тезисам. Фильтры: категория, тип (текст/эфир), подкатегории (Путь Тани по годам). URL: `https://olyunyaa.github.io/secrets-library/library_v2.html`
- **`index.html`** — другая версия библиотеки (с hero-секцией, избранным, dropdown категорий), но без тезисов. Стоит на корне GitHub Pages.
- **`library_secrets.html`** — более ранняя версия (не используется)

### Данные для библиотек
- **`knowledge_base.js`** (2.3 MB) — `const POSTS_DATA = [...]`, все 517 постов с метаданными. Используется обеими библиотеками через `<script defer>`.
- **`knowledge_base_enriched.json`** — обогащённая база (категории, теги, generated_title). Источник для генерации knowledge_base.js.
- **`photos/`** — фотографии к постам

### Парсинг и обогащение данных
- **`fetch_new_posts.py`** — **основной пайплайн**: загрузка новых постов из Telegram (Telethon), фильтрация (мин. 50 символов), Claude Haiku для категоризации + Sonnet для enrichment (UID-based), серийная нормализация категорий, перестройка knowledge_base.js. Запускать для обновления библиотеки.
- **`recategorize_all.py`** — пере-категоризация всех постов с чекпоинтом/resume. Скипает PROTECTED_SUBCATEGORIES (Путь Тани и др.). Запускать только при изменении промпта категоризации.
- **`reauth_telegram.py`** — интерактивная переавторизация Telethon-сессии (запускать в терминале если сессия истекла).
- **`enrich_posts.py`** — старый скрипт обогащения (заменён логикой в fetch_new_posts.py)
- **`backfill_photos.py`** — загрузка фотографий для постов
- **`update_efir_links.py`** — обновление ссылок на эфиры
- **`update_roadmap.py`** — генерация/обновление roadmap JSON

### Анализ
- **`analyze_categories.py`** / **`comprehensive_audit.py`** — аудит категорий и качества данных
- **`CATEGORY_AUDIT_FULL_REPORT.txt`** — результаты аудита

### Аналитика участников и платежей (ключевые файлы)
- **`generate_monthly_report.py`** — **НОВЫЙ главный скрипт** (в разработке). Один запуск → вкладка в Google Sheet.
- **`known_matches.json`** — ручная таблица `stripe_email → @username`. Пополнять при новых непривязанных платежах.
- **`tanya_agreements.json`** — договорённости Тани: `username → {type, label, valid_until, email}`. Типы: `annual` (оплачен год), `free` (бесплатно), `team` (команда, без expiry). `valid_until` обязателен для `annual` и `free` — при истечении человек падает в красную секцию. Обновлять `valid_until` для `free` каждый месяц если договорённость продолжается.
- **`docs/plans/2026-04-29-monthly-report-design.md`** — утверждённый дизайн.
- **`docs/plans/2026-04-29-monthly-report-implementation-plan.md`** — план реализации (шаги 0–11).
- **`/Users/olgaperova/Desktop/secrets-club.json`** — service account key (не копировать в папку проекта, не коммитить). Путь прописать в `.env` как `GOOGLE_SA_KEY_PATH`.

### Аналитика участников и платежей

#### Пайплайн — НОВЫЙ (с 29.04.2026, в разработке)
```
python3 generate_monthly_report.py --season-start 2026-04-23 --month "Май 2026"
  → читает Stripe из Google Sheet (вкладка с CSV)
  → экспортирует участников 3 TG-групп (Telethon)
  → загружает список Вовы (Salebot API)
  → diff с предыдущим сводом (кто новый / кто вышел)
  → пишет новую вкладку "Свод май 2026" прямо в Google Sheet Тани
```

#### Пайплайн — СТАРЫЙ (апрель 2026, использовать только для справки)
```
1. export_members.py          → members_*.csv (Telegram)
2. build_master_report.py     → full_crossref.csv  ⚠️ SEASON_START захардкожен "2026-03-23"
3. find_all_emails.py         → all_emails.csv
4. build_excel_report.py      → Секреты_Апрель_2026_отчёт.xlsx  ⚠️ имя файла захардкожено
   + ручная курация → Секреты_Апрель_2026_v2.xlsx
```
⚠️ **ВАЖНО**: `full_analysis.py` тоже пишет `full_crossref.csv` но с другой схемой — не запускать после `build_master_report.py`.

#### Источники данных
- **Google Sheet Тани** (`SPREADSHEET_ID = 174PEZUj-vxTV-vfoLIRL4FLOXSUlJ0qCfIItqyaNuH0`):
  - Вкладка со Stripe CSV — полный экспорт Stripe (Таня загружает сама). Колонки: id, Created date (UTC), Amount, Currency, Captured, Status, Customer Email, Customer ID и др.
  - Вкладка предыдущего свода — данные прошлого месяца (для diff)
  - Читается через **gspread + service account** (`/Users/olgaperova/Desktop/secrets-club.json`)
  - Service account email: `secrets-club@secrets-club-494111.iam.gserviceaccount.com` — должен быть добавлен как Editor в Google Sheet
  - Google Cloud project: `secrets-club-494111` — нужно включить Google Sheets API
- **Salebot API** (`API_KEY = "${SALEBOT_API_KEY}"`):
  - `GET /api/{key}/get_clients?list=1992711` — список Вовы (оплатившие)
  - `GET /api/{key}/get_clients?limit=500&offset=N` — все клиенты (для поиска не в списке)
  - `GET /api/{key}/get_variables?client_id={id}` — переменные клиента (email, stripe_callback_data и т.д.)
  - ⚠️ Salebot хранит только **последний** Stripe callback — не является источником правды по оплатам
- **Telegram API** (Telethon, API_ID=39940596):
  - Канал Секреты 2026: `-1003642141867`
  - Чат Секреты 2026: `-1003403267547`
  - Секреты Практика: `-1002195167889`
- **Резиденты CSV** — Google Sheet Тани с ручными заметками (оплачен год, бесплатно, команда). Используется для дополнения email и разрешения спорных случаев.

#### Скрипты
- **`generate_monthly_report.py`** — **НОВЫЙ главный скрипт** (в разработке, см. план `docs/plans/`). Объединяет весь пайплайн: Stripe из Google Sheet → Telegram → Salebot → diff → запись новой вкладки в Google Sheet. Параметры: `--season-start`, `--month`, `--stripe-tab`, `--prev-tab`, `--dry-run`.
- **`export_members.py`** — выгрузка участников Telegram-каналов/групп через Telethon.
- **`build_master_report.py`** — старый главный скрипт (апрель). Генерирует `full_crossref.csv`. `SEASON_START` захардкожен.
- **`find_all_emails.py`** — поиск email из 3+ источников. Содержит `known_matches` прямо в коде — в новом скрипте вынесено в `known_matches.json`.
- **`build_excel_report.py`** — генерация Excel из `full_crossref.csv`. Имя выходного файла захардкожено.
- **`check_payments.py`** — диагностика Stripe-данных из Salebot. Standalone, не интегрирован в пайплайн.
- **`analyze_members.py`** — сопоставление списков Чат vs Практика.

#### Выходные файлы
- **Вкладка в Google Sheet** — новый формат с `generate_monthly_report.py`. Название: `Свод участников и оплат 23.04.26-23.05.26` и т.д.
- **`full_crossref.csv`** — старый формат (апрель). 155 уникальных человек.
- **`Секреты_Апрель_2026_v2.xlsx`** — основной файл апреля (ручная курация Олей).

#### known_matches — таблица ручных соответствий Stripe email → @username
Вынесена в `known_matches.json` (в новом скрипте). До этого была захардкожена в `find_all_emails.py`.  
Критерии надёжного матча: совпадение фамилии в email, username точно в email, или подтверждение через Salebot email.  
Ненадёжны матчи только по имени (anna, yulia, marina) — дают ложные срабатывания.

#### Алгоритм матчинга Stripe → участники (в generate_monthly_report.py)
1. Прямое совпадение email (case-insensitive)
2. `known_matches.json` lookup
3. Username точно в local part email (len ≥ 6, без fuzzy)
4. Нечёткое совпадение (фамилия в email) → email помечается `⚠️` в ячейке, попадает в Action items как "подтвердить почту"
5. Полностью непривязанный Stripe Paid → строка `❓ Непривязанный платёж` в конце секции

### Деплой (Fly.io)
- **`Dockerfile`** — Python 3.11-slim, копирует bot_salebot.py + data files, запускает gunicorn (1 воркер, 4 потока)
- **`fly.toml`** — конфиг Fly.io: регион ams, HTTP service на порту 8080 (для webhook), volume `/data`, min_machines_running=1, HTTP health check `/health` каждые 30 сек (автоперезапуск при зависании)
- **`requirements.txt`** — flask, gunicorn, requests, python-dotenv, pytz, anthropic, apscheduler
- **`.dockerignore`** — исключает .env, HTML, изображения, _from_desktop

### Прочее
- **`go.html`** — редирект на бота
- **`_from_desktop/`** — старые файлы, перемещены 10.03.2026

## Архитектура бота

Все технические решения по боту (Flask/Salebot интеграция, drip-рассылка, production сервер, ✅ toggle UX, redo roadmap и т.д.) вынесены в **[`CLAUDE_BOT.md`](CLAUDE_BOT.md)**.

**Omnius** (`admin_bot.py`) — отдельный admin-бот Тани для учёта бесплатных доступов (договорённостей). Детали, деплой и готчи — в **[`CLAUDE_OMNIUS.md`](CLAUDE_OMNIUS.md)**. ⚠️ Живёт на **ОТДЕЛЬНОМ Fly-аккаунте `omniussecrets@gmail.com`**, приложение `omnius-assistant-bot`; деплой только через `flyctl deploy --config fly.admin.toml`. Из основного аккаунта его не видно.

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
- `SALEBOT_API_KEY` — API ключ Salebot.pro (установлен в Fly.io secrets)
- `WEBHOOK_SECRET` — секрет для URL webhook-а (часть URL: `/webhook/<secret>`) (установлен в Fly.io secrets)
- `ANTHROPIC_API_KEY` — ключ Anthropic API (установлен в Fly.io secrets)
- `DATA_DIR` — путь к папке данных (на Fly.io: `/data`, локально: не задан → `Path(__file__).parent`)
- `TELEGRAM_BOT_TOKEN` — **токен основного бота Salebot** (Секреты бот). Используется для прямых вызовов Telegram Bot API: `answerCallbackQuery`, `sendMessage` (клавиатуры Q1-Q4), `editMessageReplyMarkup` (✅ toggle). **КРИТИЧНО: этот токен управляет ВСЕМ ботом Salebot, не только онбордингом. Использовать ТОЛЬКО для операций, связанных с нашим онбордингом/библиотекой. НЕ менять webhook, НЕ трогать настройки бота.**

## Команды

### Установить secrets перед первым деплоем Salebot-версии
```bash
~/.fly/bin/flyctl secrets set SALEBOT_API_KEY="${SALEBOT_API_KEY}" --app secrets-library-bot
~/.fly/bin/flyctl secrets set WEBHOOK_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" --app secrets-library-bot
```

### Деплой бота на Fly.io
```bash
cd "/Users/olgaperova/Desktop/Ontrí Advisory/Ontri Проект для Секреты/"
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
cd "/Users/olgaperova/Desktop/Ontrí Advisory/Ontri Проект для Секреты/"
git add app.html knowledge_base.js photos/
git commit -m "Update library"
git push
```

## Текущий статус (обновлено 02.08.2026)

### Сессия 02.08.2026
- **Библиотека обновлена до 552 постов** (+35 за апрель–июнь 2026), добавлена категория **AI** (промпт категоризации, CAT_PALETTE в app.html и library_v2.html, авто-фикс «запись эфира про AI» в fetch_new_posts.py). Статика на GitHub Pages.
- **Починен «Подобрать материалы»**: у старого Anthropic-ключа кончились кредиты → все chat-запросы падали в «Произошла ошибка». Таня создала новый ключ; при первой установке в secrets попал ведущий пробел (`Illegal header value`) — переустановлен без пробела. Проверено: API отвечает.
- **Новая кнопка «➕ Добавить темы к дорожной карте»** в экране «С возвращением!» (`handle_start`) — вызывает существующий `handle_new_selection` (redo Q2→Q4, новые посты мержатся к старой карте без дублей). Задеплоено.
- **Баг redo_offered исправлен**: `salebot_send` теперь возвращает True/False, `_send_redo_prompt` пробрасывает статус. В `drip_delivery_job` флаг `redo_offered=True` ставится только при успешной отправке (иначе retry на следующий день). Completion-ветка drip тоже ставит флаг при успехе — раньше не ставила, и period-expiry ветка могла прислать тот же промпт второй раз.
- **QR-переавторизация Telethon**: рабочий скрипт `reauth_qr.py` (QR + 2FA пароль через getpass).
- **Redo-flow: новые подборки идут в НАЧАЛО дорожной карты** (url = new + old; для drip новые посты доставляются раньше оставшихся старых, sent_index сохраняется). После send_all-redo бот дополнительно шлёт «Ваша полная дорожная карта обновлена» со свежей ссылкой (кнопки в старых сообщениях Telegram заморожены навсегда — их не обновить).
- **Кнопки переименованы**: «🔍 Найти материалы по запросу» (бывш. «Подобрать материалы», разовый поиск текстом) и «➕ Добавить темы в дорожную карту» (Q2-redo, посты мержатся в карту). Q2-опция «Как строить портфель без прикрас» → «Как строить портфель».
- **Текстовые команды бота**: «Моя дорожная карта»/`/my_roadmap` (всегда свежая ссылка из базы), `/pause`/«пауза», `/resume`/«продолжить». В меню Telegram не зарегистрированы (setMyCommands не звали — общий бот Salebot, согласовывать с Вовой).
- **Категория «Задания» (05.08.2026)**: 41 задание недели из веток Практики (11 «Растим ЛБ» + 11 «Консультации/менторство» + 19 «Качаем LinkedIn», авторы Таня/Эльвира). База: 593 записи. uid = `task_<msg_id>`, channel_id 2195167889, type `task`, subcategory = ветка, поле `author`. Обособленный стиль: янтарная палитра, бейдж «📝 Задание», карточка с градиентом и рамкой, ссылка «Обсудить в Практике» (`t.me/c/2195167889/<topic_id>/<msg_id>`). Подкатегории-«ветки» в app.html и library_v2.html. В бот добавлены категории AI и «Задания» в `ALL_CATEGORIES` (AI отсутствовал — чат-подбор не видел AI-посты). Скрипт сбора: scratchpad `build_tasks.py` (Telethon → ветки Практики → Haiku enrichment). Новые задания Тани выходит по вторникам — для обновления перезапустить выгрузку с новыми msg_id.

## Текущий статус (обновлено 02.08.2026)

### Прогон Август 2026 выполнен — свод 23.07.26-23.08.26 (02.08.2026)
- Команда: `--season-start 2026-07-23 --month "Август 2026" --stripe-tab "Stripe Jul 23" --prev-tab "Свод участников и оплат 23.05.26-23.06.26"`. **Июль (23.06–23.07) пропущен** — данные в TG уже неактуальны, не догнать.
- Результат: TG 116 уникальных (Канал 89 / Чат 79 / Практика 82), Stripe 110 транзакций (78 Paid £2865), привязано 83 + 1 нечёткий, 9 не привязано, **8 новых / 52 вышли**. Вкладка «Свод участников и оплат 23.07.26-23.08.26» создана.
- ⚠️ **ГОТЧА ВКЛАДКИ STRIPE**: свежая вкладка 2026 называется **«Stripe Jul 23»** (с «23»). Вкладки **«Stripe Jul» / «Stripe Aug» БЕЗ «23» — это ПРОШЛОГОДНИЕ (2025) данные** (июль/август 2025), фильтр окна даёт по ним 0. Перед прогоном ВСЕГДА сверяй даты во вкладке (мин/макс) — не полагайся на имя. Дефолты `STRIPE_TAB_NAME`/`PREV_TAB_NAME` в `.env` устаревшие (апрельские) — передавай флагами.
- **Два бага починены в `generate_monthly_report.py` (в этом прогоне):**
  1. **«Вышли» считались только по `tg_id`** ([`compute_diff`](generate_monthly_report.py) стр. ~361), а свод их не хранит (`0 tg_id`) → структурный **0**. Теперь «вышли» = username прошлого свода, которых нет в текущем TG (зеркально «новым»), + нормализация регистра с обеих сторон. Оговорка: может слегка завышать (свод содержит @ники в примечаниях/action-items; смена @username → ложный «вышел») — в свод пишется строка «кто вышел» для глазной проверки.
  2. **Аналитика Failed считалась по сырым строкам Stripe** (ретраи одной карты) → «28» вместо реальных людей. Теперь Failed/Refunded — **по уникальным email** (dedup, prefer Paid); Refunded вынесен отдельной строкой. Для 23.07: 11 Failed чел. + 4 Refunded чел. (было «28»).
- **Telethon**: сессия `~/session` истекла (была от 24.06), Оля переавторизовала через `reauth_telegram.py` (QR). Проверка живости: `is_user_authorized()`.
- **Refunded + договорённость** (Maria Berezina, Alexandra Korshunova) → свод корректно поднимает action «💰 Договорённость + Stripe — проверить у Тани» (refund неоднозначен). Alexandra Korshunova добавлена в `tanya_agreements.json` (`alexandrakorshunova`, free, до 2026-08-22, email `miriamba08@rambler.ru`).

### Прогон Июнь 2026 выполнен (01.06.2026)
- Команда: `--season-start 2026-05-23 --month "Июнь 2026" --stripe-tab "Stripe May" --prev-tab "Свод участников и оплат 23.04.26-23.05.26"`
- **Платёжное окно — это окно продления ±3 дня от 23-го** (`season_end = season_start + 3 дня`), а не весь месяц. Stripe-вкладка «Stripe May» содержит в основном продления 23–26 мая.
- Результат: TG 131 уникальных (Канал 115 / Чат 110 / Практика 106), Stripe 117 транзакций, привязано 95 + 3 нечётких, 5 не привязано, 15 новых / 0 вышли.
- Группы: 1=4, 2=33, 3=3, 4=9, 5=20, 6=75. Записана вкладка **«Свод участников и оплат 23.05.26-23.06.26 (2)»**.
- ⚠️ **Первый прогон записал битую вкладку** (без «(2)»): Salebot API отвалился по таймауту на шаге 4 → «Список Вовы: 0» → все оплатившие ложно упали в Группу 4 (94 чел.). Перезапуск с рабочим Salebot дал корректные группы. **Урок: если «Список Вовы: 0 чел.» / «Salebot list error» — перезапустить прогон; вкладку с раздутой Группой 4 удалить.**

### Известные операционные нюансы (01.06.2026)
- **Путь к service-account ключу переехал** в папку проекта: `Ontri Таня Учет участников Клуба Бот+AI/secrets-club.json`. `.env` (`GOOGLE_SA_KEY_PATH`) поправлен на новый путь.
- **Переавторизация Telethon: код входа НЕ приходит для стороннего клиента.** Telegram отдаёт `SentCodeTypeApp` без SMS-фолбэка, но код не появляется ни на одном устройстве (анти-абьюз для API-клиентов). **Рабочий путь — QR-логин** (`client.qr_login()` → скан в Telegram: Настройки → Устройства → Привязать устройство). При 2FA после скана дотребуется облачный пароль (`client.sign_in(password=...)`, вводить через `getpass` в терминале). Сессия — `~/session.session`.
- **Терминал `forkpty: Device not configured`** = приложение Claude.app утекло pty (держало 511 из 511). Фикс: перезапуск Claude.app, либо временно поднять лимит `sudo sysctl -w kern.tty.ptmx_max=999` (максимум — 999) через GUI-диалог `osascript ... with administrator privileges`.

## Текущий статус (обновлено 01.05.2026)

### Бот задеплоен и работает
- **`bot_salebot.py`** задеплоен на Fly.io, принимает webhook от Salebot.pro
- 74 уникальных пользователя, 51 дорожная карта, 29 завершили все материалы
- Аналитика: `analytics_log.csv` на `/data` volume, записи с 22.03.2026

### Библиотека обновлена (20.04.2026)
- **517 постов** (было 496). Добавлены 20 новых постов за март–апрель 2026
- Удалены: 2026_143 (настраиваю систему — технический), 2026_125 (анонс трекинг-группы), 2026_114/115/116 (пустые/фото без текста)
- Промпт категоризации обновлён: детальные правила для всех 9 категорий, исправлена путаница Внутрянка/Путь предпринимателя
- `fetch_new_posts.py`: добавлена генерация заголовков для постов без шапки, нормализация категорий серийных постов (часть 1/2/3), UID-based enrichment
- Статика запушена на GitHub Pages

### Исправленные проблемы (23.03.2026)
1. **BUTTON_DATA_INVALID** — callback_data были слишком длинные (лимит Salebot 64 байта). Исправлено: короткие коды (`q1_1`, `q2_p1`, `done`, `drip_ok`, `more_p1` и т.д.) с маппингом на полные ключи
2. **Flooding при multi-select** — каждый toggle кнопки пересылал ВСЕ кнопки заново + Salebot эхоировал сообщения. Исправлено: silent toggle (без сообщения при нажатии), результат показывается только на «Готово»; webhook dedup по ID
3. **Salebot echo** — Salebot дублировал webhook при отправке outgoing сообщений. Исправлено: проверка `is_input == 0` + dedup `_seen_ids`
4. **Убрано «>>>»** из кнопки «Готово»
5. **Кнопка «Моя дорожная карта»** убрана из главного меню, показывается только после онбординга
6. **Unsolicited messages** — бот отправлял «Привет! Напишите Онбординг и Библиотека» людям, которые не начинали онбординг (ответы на funnel Salebot попадали в наш webhook). Исправлено: silent ignore в 4 точках кода
7. **WebApp кнопки** — все URL-кнопки библиотеки и roadmap заменены на `web_app` формат (открываются как Mini App внутри Telegram, а не в браузере)
8. **Skip onboarding** — кнопка «Пропустить — открыть Библиотеку» при старте; state `"skipped"` запоминается → при повторном нажатии «Онбординг и Библиотека» сразу показывает библиотеку + «Пройти онбординг»
9. **Returning users** — при повторном нажатии «Онбординг и Библиотека» показывается «С возвращением!» с кнопками Library + Roadmap (если онбординг уже пройден)
10. **URL в тексте сообщений** — убраны raw URL из текста во всех 7 местах (handle_chat, handle_q4_input, handle_more_topic, handle_my_roadmap, drip_delivery_job). Теперь только чистый текст + WebApp-кнопка
11. **Бессмысленные запросы** — если `classify_request` не нашёл категорий (напр. «тесттт»), второй вызов Claude API не делается, пользователь получает «Не удалось подобрать материалы»
12. **«Вы выбрали» → ✅ на клавиатуре** — вместо текстовой сводки теперь ✅ отображается прямо на кнопках клавиатуры (отдельные текстовые сводки убраны — см. #20)
13. **Приветствие + Q1** — объединены в одно сообщение (были два отдельных, приходили в неправильном порядке)
14. **AFTER_ROADMAP_MSG** — убрана кнопка «Моя дорожная карта» (дублировала «Вся дорожная карта» из drip-сообщения)
15. **auto_stop = off** — машина Fly.io больше не засыпает, ответы без задержки

### Реализовано: визуальный фидбек кнопок (24.03.2026)
- **answerCallbackQuery** — спиннер на кнопке снимается мгновенно при любом нажатии
- **✅ Toggle на Q1/Q2** — при нажатии кнопки клавиатура обновляется: выбранные получают ✅ в тексте
- **Финальный выбор** — после «Готово» (Q1/Q2) и после выбора (Q3/Q4) клавиатура заменяется: остаются только выбранные пункты с ✅, остальные исчезают
- **Удаление клавиатуры** — при skip_onb и start_onb старая клавиатура удаляется
- **📚 Emoji** — все кнопки дорожной карты имеют 📚
- Реализовано через: `tg_send_message` (отправка клавиатур через TG API → получаем `message_id`) + `tg_edit_reply_markup` (редактирование) + `tg_answer_callback` (снятие спиннера)
- Salebot `callback_message_id` НЕ доступен в webhook — поэтому `message_id` хранится в state (`kb_message_id`)
- Задачи Вовы по фидбеку (tg_answer_callback_query, tg_delete_message) больше НЕ нужны — делаем сами

16. **answerCallbackQuery** — спиннер на inline-кнопках снимается мгновенно
17. **✅ Toggle клавиатур Q1-Q4** — визуальный фидбек: ✅ на выбранных, финальная клавиатура только с выбранными, удаление клавиатуры при skip/start_onb
18. **📚 Emoji на дорожной карте** — все кнопки с дорожной картой имеют 📚
19. **Отправка клавиатур через TG API** — Q1/Q2/Q3/Q4 сообщения с inline-клавиатурой отправляются через Telegram Bot API (`tg_send_message`) для получения `message_id`, необходимого для `editMessageReplyMarkup`. Fallback на Salebot API если TG API не сработал.
20. **Убраны ✅ сводки** — отдельные текстовые сообщения со сводкой выбора (✅ Фрилансер и т.д.) убраны из всех 4 обработчиков Q1-Q4, т.к. ✅ теперь отображается на самих кнопках клавиатуры
21. **AFTER_ROADMAP_MSG с задержкой 5 сек** — `threading.Timer(5, salebot_send, ...)` чтобы roadmap-карточки успели прийти раньше
22. **NETWORK_MSG перемещён из Q2 в Q4** — сообщение про нетворкинг теперь отправляется после ответа на Q4, перед материалами дорожной карты. Текст обновлён: Random Coffee, Секреты Практика, коворкинг, трекинг-группа
23. **`/start` не запускает онбординг** — убрано из обработчика, чтобы случайные люди (не участники клуба) не получали онбординг при нажатии «Запустить» в боте. `/start` обрабатывается сценариями Salebot
24. **Защита от неоплативших** — текст «Онбординг и Библиотека» игнорируется нашим кодом. Онбординг запускается только через триггер «Есть подписка» от блока Вовы (после проверки `inlist(1992711)`). Отдельный endpoint `/start_onboarding` как альтернативный триггер

### Реализовано: «Новая подборка» — redo roadmap (24.03.2026)
25. **Completion message с redo** — после завершения drip: «Все материалы отправлены! Хотите получить подборку по другой теме?» + 3 кнопки (новая тема, библиотека, полная дорожная карта)
26. **`handle_new_selection`** — новый handler: сохраняет `a1`, сбрасывает на Q2 с `is_redo: True`, отправляет Q2 клавиатуру
27. **`collect_roadmap_posts` с `exclude_ids`** — фильтрация уже отправленных постов при повторном выборе тем
28. **Redo flow в `handle_q4_input`** — при `is_redo`: мержит новые посты в конец существующей дорожной карты, обновляет URL, обрабатывает случай «нет новых постов»
29. **Router `new_selection`** — обработка callback от кнопки «Выбрать новую тему»
30a. **Redo prompt для send_all пользователей** — `drip_delivery_job` теперь проверяет send_all пользователей (у которых `sent_index >= len(posts)`): если `period_days` истёк и `redo_offered != True` → отправляет те же 3 кнопки (новая тема, библиотека, дорожная карта). Флаг `redo_offered` предотвращает повторную отправку. Сбрасывается в `False` при каждом новом сохранении roadmap (все 4 точки в `handle_q4_input`).
30b. **`_send_redo_prompt` helper** — вынесен DRY-хелпер для отправки completion message с 3 кнопками, используется и для drip-завершения, и для send_all по окончании периода.

### Реализовано: hardening для 150+ пользователей (24.03.2026)
30. **gunicorn вместо Flask dev-сервера** — `gunicorn --workers 1 --threads 4 --timeout 30 bot_salebot:app`. Обрабатывает до 4 запросов параллельно (Flask dev-сервер обрабатывал только 1). Один воркер — все `threading.Lock` и `_seen_ids` работают как раньше. `main()` убран, `if __name__` запускает Flask dev-сервер для локальной разработки.
31. **Dedup set увеличен** — `_seen_ids` очистка при >5000 (было 500). 150 юзеров × 10 кликов = 1500 webhook-ов, теперь не теряем дубли.
32. **CSV lock** — `_csv_lock` защищает `log_answers` и `log_event` от битых строк при параллельных потоках.

### Реализовано: оптимизация toggle (24.03.2026)
33. **In-memory write-through cache** для `user_states.json` и `user_roadmaps.json` — `_states_cache` и `_roadmaps_cache`. Первое чтение с диска, далее из памяти. Запись: обновляет кэш + пишет на диск. Безопасно при 1 воркере (threading.Lock защищает). Сэкономлено ~20-40ms JSON parse на каждой операции.
34. **`tg_answer_callback` в фоновом потоке** — fire-and-forget в `daemon` thread. Сэкономлено ~200-500ms (блокирующий HTTP-вызов к Telegram API убран с критического пути). Итого toggle: с ~435-1065ms до ~215-515ms (примерно вдвое быстрее). Оставшееся время — сетевой вызов `tg_edit_reply_markup` к Telegram API.

### Реализовано: стабильность и порядок сообщений (24.03.2026)
35. **Lazy scheduler** — APScheduler стартует при первом HTTP запросе (`@app.before_request`), а не при импорте модуля. Старт при импорте вызывал зависание gunicorn worker после рестарта (потоки APScheduler конфликтовали с fork). Диагностика: worker процесс жив, но не принимает HTTP-соединения, health check зависает.
36. **Fly.io HTTP health check** — `fly.toml` теперь содержит `[[http_service.checks]]`: GET `/health` каждые 30 сек, timeout 5 сек, grace period 30 сек. Если бот зависнет — Fly.io автоматически перезапустит машину.
37. **NETWORK_MSG задержка 3 сек** — перед отправкой карточек дорожной карты добавлена пауза `time.sleep(3)`, чтобы сообщение о нетворкинге (Random Coffee и т.д.) гарантированно доставлялось до карточек. Активируется только если пользователь выбрал «Примеры других и окружение» в Q2.

### Доработки generate_monthly_report.py (01.05.2026)

- **Prefer-paid bug fix** — стратегии 3–5 в `match_stripe_to_members` теперь не перезаписывают Paid на Failed, если два разных Stripe email матчатся к одному uid
- **Тексты без "убрать/удалить"**: статус `🔴 Нет оплаты` (было `🔴 Убрать`), шапки `🔴 НЕТ ОПЛАТЫ` и `🔴 FAILED ПЛАТЁЖ`, action `"нет оплаты"` и `"failed платеж"`
- **Sub-группы в group 2** (нет оплаты) — 5 сценариев: удалить из чата/практики / Тянутся из прошлого? / Вова удалить из бота? / Вове проверить / проверить
- **Sub-группы в group 4** (Вове) — сначала "удалил бота?" (не в списке + paid), потом "Вова: добавить в канал?" (в списке + paid + не в канале)
- **Кластеризация group 6** — regular paid → договорённости → команда
- **Новая вкладка вместо перезаписи** — если вкладка уже существует, создаётся `(2)`, `(3)` и т.д. Ручные правки в оригинале не теряются
- **Универсальный fallback** — всё что не попало в сценарий → action `"проверить"`
- Последний прогон создал вкладку `Свод участников и оплат 23.04.26-23.05.26 (3)`

### Спроектирована архитектура оркестратора (01.05.2026)
- Design doc: `docs/plans/2026-04-30-script-fixes-and-orchestrator-design.md`
- Implementation plan (фаза 1): `docs/plans/2026-04-30-script-fixes-implementation-plan.md`
- **Архитектура**: отдельный admin Telegram bot (`admin_bot.py`) → Orchestrator (Claude API tool_use) → агенты (`agents/member_agent.py`, `agents/task_agent.py`, ...)
- **Member Agent инструменты**: get_member_status, update_member_note, move_member_to_group, set_agreement, identify_stripe
- **Фазы**: 1 (фиксы скрипта, готово) → 2 (скаффолдинг оркестратора) → 3 (полный Member Agent) → 4 (Task Agent)

### Реализована и доработана автоматизация ежемесячного отчёта (29.04.2026)
- Написан `generate_monthly_report.py`: Stripe из Google Sheet → Telegram → Salebot → diff → запись вкладки в Google Sheet
- **Финальный прогон Май 2026 выполнен**: вкладка `Свод участников и оплат 23.04.26-23.05.26` записана
- **Улучшения скрипта за сессию 29.04.2026:**
  - 6 групп вместо 4: отдельно ⚠️ Нет почты, 🔴 Нет оплаты, 🔴 Failed, 🟡 Вове, 🟠 Неполный, ✅ ОК
  - Команда (Tanya, Vova, Nastya) через `tanya_agreements.json` type=team → всегда в ✅, показывает 💙 Команда
  - Agreement emails автоматически включаются в Stripe-матчинг
  - Prev swod lookup: паттерн-поиск по ячейкам вместо фиксированных колонок (email извлекается из ячейки с доп. текстом)
  - Очевидные матчи → прямые (strategy 4 len≥8, 5b tok≥7, 5c len≥10)
  - Prefer-Paid logic во всех стратегиях (2, 2b, 1)
  - Email из lookup-таблиц подставляется в колонку Email участника
- `known_matches.json` пополнен: djulett@mail.ru, olga.afanasenko@icloud.com, msu.stella@gmail.com
- `tanya_agreements.json` обновлён: команда + договорённости май 2026

### Реализовано: аналитика участников и платежей (28.03.2026)
38. **Выгрузка участников Telegram** — `export_members.py` через Telethon. Канал (116), Чат (85), Практика (103).
39. **Salebot API: список Вовы** — `GET /api/{key}/get_clients?list=1992711` → 134 клиента. **НЕ источник правды** — формируется частично вручную.
40. **Salebot API: переменные клиентов** — `get_variables`: `stripe_callback_data` (JSON), `email`, `tg_username`, `stripe_subscription_id` и т.д.
41. **Stripe данные в Salebot неполные** — 98 из 134 имеют callback, 36 без данных. Причина: Salebot хранит только последний callback, не обновляет при рекуррентном списании.
42. **Сезонная модель** — 1 сезон = 1 месяц. Апрель 2026: `SEASON_START = "2026-03-23"`.
43. **5-факторный отчёт** — `build_master_report.py` v3 → `full_crossref.csv` (155 чел.)
44. **Refund не отражается в Salebot** — нужен webhook на Stripe refund events.
45. **Системная ошибка валюты GBP** — 102 из 134 клиентов с ошибкой внутренней платёжки Salebot.
46. **Кросс-референс с реальным Stripe CSV** — 85 транзакций (72 Paid, 10 Failed, 3 Refunded), 82 уникальных email. Привязано к участникам через матчинг: email → username → ручная таблица. Добавлены колонки K (Stripe факт) и L (Примечание) в v2.xlsx.
47. **Поиск email из 5 источников** — `find_all_emails.py` объединяет: Salebot (95) + Stripe CSV (+17) + резиденты Тани (+4) + повторный Salebot API (+6) + бывшие резиденты (+3) = **128 из 155 с email**. Файл `all_emails.csv`.
48. **Файл резидентов Тани** — интегрирован: "Оплачен весь 2026" (Рузанна, Надя, Венера), "Бесплатно" (Аня, Ксюша, Анна 3 мес.), "Команда" (Настя, Вова, боты), "Танины договорённости" (Саша Дзен бесплатно), "По внешней ссылке" (Катя вышла, Александра Лондон бесплатно до 23.04, Таня Князева зашла март 26).
49. **Список бывших резидентов** — `Адреса бывших резидентов Mar 2026.csv` (280 email). Найдено 3 совпадения: Arina Flyugova → arina.flyugova@heineken.com, Marina Grechko → marina.grechko@gmail.com, Olga Kornakova → kornakova@live.ru. Также обнаружен manager@tldprogress.com — бывший резидент, вернулся и оплатил.
50. **Аудит known_matches** — полная проверка ~28 ручных соответствий Stripe email → @username. Результат: **18 надёжных** (совпадение фамилии, username или Salebot email), **5 неуверенных** (нужна проверка Тани: @eolontseva, @Mashenka_Dolomiti, @larisa_x, @victoria_fon_littlepups, @Irin_s), **6 ложных удалены** (@doyouknowwhoiam←35777sss, @marioffduty←miriamba08, @anjanbeman←anna@sansvs.co, @S_Djulia←yulia.v.urusova, @svetlana_sss←svetlana.obeletc, @mapblwka←marina.v.mikheeva — у неё есть свой прямой матч Mapblwka@gmail.com). Колонка K в v2.xlsx очищена для 5 ложных, колонка L — ⚠️ для 5 неуверенных.
51. **Итого в v2.xlsx**: J (Email) = 128/155, K (Stripe факт) = 73 подтверждённых (было 78, -5 ложных), L (Примечание) = ⚠️ для 6 записей.
52. **Кросс-референс с файлом резидентов (полная история)** — `Клуб Секреты - резиденты и активности - Резиденты.csv` (1677 строк): имя, почта, Telegram ник, сумма по месяцам. Результаты: +5 новых email (по совпадению ников), подтверждены 3 из 5 неуверенных матчей (@larisa_x, @Mashenka_Dolomiti, @Irin_s), разрешены 6 из 8 непривязанных Stripe email, исправлена ошибка (miriamba08@rambler.ru принадлежит @alexandrakorshunova, не @marioffduty). **Итого после обновления v2.xlsx**: J (Email) = 134/156 (86%), K (Stripe факт) = 79 подтверждённых, L (⚠️) = 3 записи, непривязанных Stripe Paid = 2.

### Найденные проблемы (28.03.2026, обновлено после аудита)
- **Salebot не является источником правды по оплатам** — хранит Stripe callback неполно (98 из 134 в списке). Единственный надёжный источник — Stripe CSV или Stripe API (`sk_live_` ключ от Тани).
- **Email покрытие: 134 из 156 (86%)** — 22 человека без email. Источники: Salebot (95) + Stripe CSV (+17) + резиденты Тани (+4) + Salebot API повтор (+6) + бывшие резиденты (+3) + доп. кросс-референс (+3) + файл резидентов полный (+5, совпадение ников) + исправление MARI L (+1).
- **2 непривязанных Stripe PAID email** — оплаты есть в Stripe, но не удалось привязать к участникам: marina.v.mikheeva@gmail.com, svetlana.obeletc@gmail.com. ~~Было 8~~, разрешены резидентами: 35777sss@gmail.com→@svetlana_sss, miriamba08@rambler.ru→@alexandrakorshunova, anna@sansvs.co→@anjanbeman, mleganovivh@gmail.com→@marioffduty, manager@tldprogress.com→@yulia_visibility, yulia.v.urusova@gmail.com→@ulleand (доп. email, у неё уже есть Stripe).
- **3 неуверенных Stripe-матча (⚠️ в v2.xlsx)** — @eolontseva←ekaterina.matveeva01, @victoria_fon_littlepups←victoriazolot, @doyouknowwhoiam←msu.stella. ~~Было 5~~, подтверждены резидентами: @Mashenka_Dolomiti←vinmariya (3 записи с ником), @larisa_x←larisa.zaitseva (10 записей), @Irin_s←irinazajzeva (5 записей, ник @iren_lesnik).
- **6 человек с FAILED Stripe и доступом** — Alexandra @vuiale, Julia Kniazeva, Maria Al @GLDLXs, Tuyana, Yulia @yul_morozova, trisch @trisch_me. В новой группировке они попадают в 🔴 "Убрать — платёж не прошёл".
- **5 человек с доступом, нет в Salebot** — Alena Soboleva, Anastasia @missanastasiau, Anna @morgen_ania (выиграла 3 мес.), PRS @PRSLDN25, Татьяна Князева (зашла март 26).
- **18 оплативших не в Чате/Практике** — confirmed Stripe >= 23.03 но не добавлены во все группы.
- **Ручная таблица `known_matches` проаудирована** — 6 ложных удалено, 5 неуверенных помечены. Каждый месяц при смене email/участников таблицу нужно обновлять. Критерии надёжного матча: совпадение фамилии в email, username в email, или совпадение с Salebot email. Матчи по имени (anna, yulia, svetlana) — ненадёжны из-за частых имён.

### Предыдущие изменения
- Бот: все фичи до 28.03.2026 — см. [CLAUDE_BOT.md](CLAUDE_BOT.md)
- `app.html` обновлён: убраны «материалы», подкатегория «Менторство у психолога»
- roadmap_all_pains_v4.json обновлён (pain_2, pain_3)
- Fly.io volume `/data` подключён

## Что осталось сделать

### Для Вовы (настройка Salebot.pro)
- [x] ~~Настроить webhook URL в Salebot.pro~~ (сделано)
- [ ] Настроить Reply keyboard: только одна кнопка — «Онбординг и Библиотека» (остальные убрать)
- [ ] НЕ создавать funnels для текстов кнопок, отключить auto-response
- [ ] **Отключить внутренние попытки оплаты** через Salebot (ошибка GBP валюты у всех клиентов)
- [ ] **Настроить Stripe refund webhook** → автоматическое удаление из списка 1992711 при refund
- [ ] **Автоматизация подписок** — настроить Stripe → Salebot: оплата → добавить в канал/чат, неоплата → кик `tg_ban_chat_member`. Сейчас Таня проверяет вручную.

### Аналитика участников
- [x] Выгрузить участников Канал/Чат/Практика
- [x] Сопоставить с Salebot списком Вовы (134 чел.)
- [x] Выгрузить Stripe-данные через Salebot API
- [x] Сгенерировать `full_crossref.csv` — 155 чел.
- [x] Кросс-референс с реальным Stripe CSV (85 транзакций, 82 email)
- [x] Кросс-референс с файлом резидентов Тани
- [x] Добавить колонки K (Stripe факт) и L (Примечание) в v2.xlsx
- [x] Поиск email из 5 источников — 128/155 найдено (all_emails.csv)
- [x] Кросс-референс с бывшими резидентами (280 email → 3 совпадения)
- [x] Аудит known_matches — 18 надёжных, 5 неуверенных (⚠️), 6 ложных удалено
- [x] Кросс-референс с полным файлом резидентов (1677 строк) — +5 email, 3 ⚠️ подтверждены, 6 из 8 непривязанных разрешены, исправлена ошибка MARI L
- [ ] **Таня проверяет 2 непривязанных Stripe PAID email** — marina.v.mikheeva@gmail.com, svetlana.obeletc@gmail.com
- [ ] **Таня проверяет 3 неуверенных Stripe-матча** (⚠️ в колонке L): @eolontseva, @victoria_fon_littlepups, @doyouknowwhoiam
- [ ] **Таня проверяет 6 чел. с FAILED платежом и доступом**
- [ ] **Добавить 18 оплативших в Чат/Практику**
- [ ] **Найти email для оставшихся 22 человек** (спросить у Тани / написать в Telegram)

### Автоматизация ежемесячного отчёта (реализовано 29.04.2026, доработано 01.05.2026)
> Дизайн утверждён. Планы: `docs/plans/2026-04-29-monthly-report-implementation-plan.md`, `docs/plans/2026-04-29-monthly-report-redesign-plan.md`, `docs/plans/2026-04-30-script-fixes-and-orchestrator-design.md`

#### Текущее состояние скрипта (29.04.2026, после переработки)

**Что умеет `generate_monthly_report.py`:**
- Stripe из Google Sheet → Telegram (3 группы) → Salebot → diff → вкладка в Google Sheet
- **4 группы участников (action-oriented):**
  - 🔴 **Убрать из клуба** — в любой TG-группе, оплаты нет (подвиды: нет оплаты / FAILED платёж)
  - 🟡 **Вове** — оплата есть, но нет в канале или не в списке Vova
  - 🟠 **Неполный доступ** — оплата + канал + список, но нет в чате/практике (выбор человека)
  - ✅ **Всё ОК** — оплата + канал + список + чат + практика
- Канал обязателен при оплате; чат и практика — опциональны (выбор участника)
- `tanya_agreements.json` — активная договорённость перекрывает группу 🔴
- Diff с предыдущим сводом — кто новый / кто вышел
- Action items: 🔴 Удалить (нет оплаты / failed), 🟡 Вова добавить в канал/список, ❓ Вова идентифицировать непривязанные, ⚠️ подтвердить нечёткие матчи, ⏰ истекающие договорённости

**Матчинг Stripe → участники (7 стратегий, все с prefer-Paid логикой):**
1. Прямое совпадение email (prefer Paid over Failed для одного uid)
2. `known_matches.json` (ручная таблица) → prefer Paid
2b. **Lookup-таблицы**: "Антихрупкость 2026", "Резиденты", предыдущий Свод, + agreement emails → prefer Paid
3. Username == local part email (очищенные от `.` `_`, len ≥ 6)
4. Username подстрока local part email — если len(username) ≥ 8 и целиком в email local → **прямой матч**; иначе нечёткий
5. Токен из email ≥ 7 → **прямой матч**; < 7 → нечёткий
   - 5b. Суффикс username: tok ≥ 7 → прямой матч
   - 5c. Конкатенация имени ≥ 10 → прямой матч

**Lookup-таблицы (шаг 2b):**
- Предыдущий Свод — паттерн-поиск по всем ячейкам строки (находит email даже если ячейка содержит доп. текст/примечание)
- agreement emails из `tanya_agreements.json` автоматически включаются в lookup перед матчингом

⚠️ **`ANTIKH_TAB_NAME = "Антихрупкость 2026"`** — константа в начале файла. При смене года обновить!

**Структура групп (6 групп, с 29.04.2026):**
- Группа 1 ⚠️: нет почты + нет оплаты → Вове найти почту перед удалением
- Группа 2 🔴: есть почта + нет оплаты → Убрать из клуба
- Группа 3 🔴: Failed платёж → Убрать / помочь с оплатой (отдельная секция!)
- Группа 4 🟡: Вове (оплата есть, нет в канале или Salebot-списке)
- Группа 5 🟠: Неполный доступ (нет в Чате/Практике — выбор участника)
- Группа 6 ✅: Всё ОК

**`tanya_agreements.json` — типы:**
- `team` → всегда группа 6, показывает `💙 Команда` (Tanya @tanyamelamori, Vova @vladimir_volkov_15, Nastya @surzhik_n)
- `annual` → `valid_until` до конца года; показывает `💛 Оплачен весь 2026`
- `free` → `valid_until` обязателен; истёк = попадает в красную группу. Если person заплатил + есть активная free-договорённость → `stripe_also` флаг → Action: "Договорённость + Stripe — вернуть деньги?"

**Договорённости май 2026 (valid_until: 2026-05-22):**
Эльвира @elvirakhamsina, Вячеслав @zgordanvv, Юля @yulia_visibility, Оля @olyunyaa, Алина @alina1112345, Марина @mapblwka + ранее: @anna_salyaeva, @zksana, @sasha_journey

**Результат финального прогона Май 2026 (29.04.2026):**
- Telegram: Канал 113, Чат 103, Практика 100 → 125 уникальных
- Stripe: 103 транзакции (98 Paid, 5 Failed), £3625 GBP
- Привязано: **92** прямых + 3 нечётких | Непривязанных Paid: **7**
- Email coverage: **93%** (131/140)
- Diff: 25 новых / 3 вышли
- Вкладка `Свод участников и оплат 23.04.26-23.05.26` записана в Google Sheet

**Дизайн таблицы:**
- Колонки: Имя, Username, Email, Список Вовы, Канал, Чат, Практика, Stripe, Новый/Вышел, Статус, Действие
- Иконки ✅/❌ в булевых колонках
- 6 цветных секций; заголовок колонок — тёмный фон, белый текст
- Секция непривязанных Stripe Paid внизу; Analytics + Action Items

**Команда для следующего месяца:**
```bash
# Таня загружает новый Stripe CSV в Google Sheet (новая вкладка, напр. "Stripe May 23")
python3 generate_monthly_report.py \
  --season-start 2026-05-23 \
  --month "Июнь 2026" \
  --stripe-tab "Stripe May 23" \
  --prev-tab "Свод участников и оплат 23.04.26-23.05.26" \
  --dry-run
# Проверить dry-run, затем:
python3 generate_monthly_report.py \
  --season-start 2026-05-23 \
  --month "Июнь 2026" \
  --stripe-tab "Stripe May 23" \
  --prev-tab "Свод участников и оплат 23.04.26-23.05.26"
```

**Осталось / известные ограничения:**
- [ ] **Пополнять `tanya_agreements.json`** при новых договорённостях — `valid_until` обязателен для `free`/`annual`. Перед следующим месяцем обновить даты (сейчас до 2026-05-22)
- [ ] **Обновить `ANTIKH_TAB_NAME`** при смене года (сейчас `"Антихрупкость 2026"`)
- [ ] **6 непривязанных Stripe Paid** — передать Вове: marina.v.mikheeva, a_shev19, bytchkova@gmx.ch, labzalova, afraid-stint6b, natallya.berez
- [ ] **3 нечётких матча** в known_matches.json: @skhayluk↔khay@ukr.net, @encouragetata↔tatiana.khoreva@mail.ru, @s_djulia↔djulett@mail.ru (последние два подтверждены, стоит добавить)
- [ ] Stripe API sk_live_ — Таня не предоставила; пока только через Google Sheet CSV
- [ ] Автоматическое добавление в группы — только Таня решает, не автоматизировать
- [ ] Запланировать автозапуск 28-го числа каждого месяца (`/schedule`)
- [ ] **Фаза 2 оркестратора** — скаффолдинг `admin_bot.py` + `agents/` + `orchestrator.py` (следующая сессия)

### Тестирование бота
- [x] ~~Отправить "Онбординг и Библиотека" → должен прийти Q1~~ (работает)
- [x] ~~Нажатие inline-кнопок Q1~~ (toggle записывается, silent)
- [x] ~~Returning user → «С возвращением!» + Library + Roadmap~~ (работает)
- [x] ~~answerCallbackQuery — спиннер снимается~~ (работает)
- [ ] Проверить ✅ toggle на Q1-Q4
- [ ] Пройти весь онбординг Q1→Q2→Q3→Q4→roadmap
- [ ] Проверить skip/start_onb, drip delivery, pause/resume, "Больше про..."
- [ ] Тестирование «Новая подборка» (redo)

### Прочее
- [x] ~~Запушить статику на GitHub Pages~~ (сделано 20.04.2026)
- [ ] Добавить `.gitignore`
- [ ] Обновить library_v2.html — убрать «материалы»
