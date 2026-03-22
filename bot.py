import os
import csv
import json
import re
import logging
import asyncio
from datetime import datetime, date, timedelta, time as dt_time
from pathlib import Path

import pytz
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    ApplicationBuilder,
    ApplicationHandlerStop,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import anthropic

# ── Config ──
load_dotenv(Path(__file__).parent / ".env")
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
KB_PATH = Path(__file__).parent / "knowledge_base.js"
DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent)))
LOG_PATH = DATA_DIR / "onboarding_log.csv"
USER_ROADMAPS_PATH = DATA_DIR / "user_roadmaps.json"

BATCH_SIZE = 2
DRIP_INTERVALS = {7: 1, 14: 2, 28: 3, 60: 4, 90: 5}
BASE_URL = "https://olyunyaa.github.io/secrets-library/app.html"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── States ──
Q1, Q2, Q3, Q4, CHAT = range(5)

# ── Module-level set for correction flow ──
AWAITING_CORRECTION = set()  # set of user_ids awaiting correction text

# ── Questions & options ──
Q1_TEXT = "Как бы вы описали себя сегодня?\n_можно выбрать несколько вариантов_"
Q1_OPTIONS = [
    ("hire_start", "В найме, хочу начать своё"),
    ("hire_plus", "В найме + уже веду свой проект"),
    ("freelance", "Фрилансер или предприниматель"),
    ("blog", "Хочу вести блог и расти в соцсетях"),
    ("explore", "Пока изучаю и присматриваюсь"),
]

Q2_TEXT = "На что хотите взять фокус в клубе?\n_можно выбрать несколько вариантов_"
Q2_OPTIONS = [
    ("portfolio_raw", "Как строить портфель без прикрас"),
    ("portfolio_start", "Начать портфельную карьеру"),
    ("project_start", "Создать проект для портфеля"),
    ("brand", "Построить личный бренд"),
    ("social", "Соцсети и блог для портфеля"),
    ("fear_sales", "Не умею / боюсь продавать"),
    ("audience", "Не знаю, кому продавать"),
    ("big_project", "Свой большой проект"),
    ("blockers", "Страхи, прокрастинация, саботаж"),
    ("community", "Примеры других и окружение"),
    ("money", "Больше зарабатывать / второй доход"),
]

Q3_TEXT = "На какой период вы хотите получить дорожную карту?"
Q3_OPTIONS = [
    ("7", "7 дней — быстрый старт"),
    ("14", "14 дней"),
    ("28", "28 дней — один сезон"),
    ("60", "60 дней"),
    ("90", "90 дней — глубокое погружение"),
]

Q4_TEXT = "Как вам удобнее получать материалы?"
Q4_OPTIONS = [
    ("send_all", "Пришли всё сразу"),
    ("drip", "По несколько постов каждые 1-2 дня"),
    ("drip_remind", "По несколько постов + напоминания"),
]

NETWORK_MSG = ("Для нетворкинга в клубе есть Random Coffee.\n"
               "Каждую пятницу в Чат Секреты 2026 приходит голосование — "
               "хотите ли вы участвовать в Random Coffee на следующей неделе. "
               "Если хотите — отметьте в голосовалке. В понедельник в чат приходит "
               "сообщение с разбивкой на пары — вам нужно написать своей паре "
               "и договориться о встрече в удобном формате.\n\n"
               "Для тех кто хочет двигаться активнее — трекинг-группы "
               "в чате Секреты Практика. В новом сезоне:\n"
               "— Групповая коуч-сессия с Ксюшей. Если хотите быть активным "
               "участником — заполняйте анкету. Выберут трёх участников, каждый "
               "приходит со своим запросом по соцсетям. Зрителем быть тоже можно.\n"
               "— Книжный клуб с Катей — читаем и обсуждаем вместе.\n"
               "— Action-club с Аней каждую среду и пятницу — участники которые "
               "не пропускают эти встречи, за месяц нормально продвигаются "
               "в своих проектах.")

AFTER_ROADMAP_MSG = ("Если захотите почитать о чём-то конкретном или получить "
                     "больше материалов — просто напишите мне об этом, и я подготовлю "
                     "подборку специально для вас.")

DONE_CB = "done"


def make_multi_keyboard(options, selected):
    rows = []
    for key, label in options:
        mark = "✅ " if key in selected else ""
        rows.append([InlineKeyboardButton(mark + label, callback_data=key)])
    rows.append([InlineKeyboardButton("Готово", callback_data=DONE_CB)])
    return InlineKeyboardMarkup(rows)


def make_keyboard(options):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=key)] for key, label in options]
    )


# ── Load posts (once) ──
def load_posts():
    text = KB_PATH.read_text(encoding="utf-8")
    match = re.search(r"const POSTS_DATA\s*=\s*(\[[\s\S]*\]);", text)
    if not match:
        raise RuntimeError("Cannot parse POSTS_DATA from knowledge_base.js")
    return json.loads(match.group(1))


POSTS = load_posts()
log.info("Loaded %d posts from knowledge_base.js", len(POSTS))

# ── Load roadmap data ──
ROADMAP_PATH = Path(__file__).parent / "roadmap_all_pains_v4.json"
ROADMAP = json.loads(ROADMAP_PATH.read_text(encoding="utf-8"))
log.info("Loaded roadmap with %d pain points", len(ROADMAP))

Q2_TO_PAIN = {
    "portfolio_raw": "pain_1_portfolio_raw",
    "portfolio_start": "pain_2_portfolio_start",
    "project_start": "pain_3_project_start",
    "brand": "pain_4_personal_brand",
    "social": "pain_5_social_blog",
    "fear_sales": "pain_6_selling_fear",
    "audience": "pain_7_audience",
    "big_project": "pain_8_big_project",
    "blockers": "pain_9_blockers",
    "community": "pain_10_community",
    "money": "pain_11_money",
}

POSTS_COMPACT = []
for p in POSTS:
    POSTS_COMPACT.append({
        "title": p.get("generated_title", ""),
        "category": p.get("category", []),
        "topic": p.get("topic", ""),
        "views": p.get("views", 0),
        "link": p.get("link", ""),
        "type": p.get("type", "text"),
        "date": (p.get("date") or "")[:10],
    })


# ── CSV logging ──
def log_answers(user_id, username, a1, a2, a2_free, a3):
    exists = LOG_PATH.exists()
    with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["timestamp", "user_id", "username",
                         "answer1", "answer2", "answer2_free", "answer3"])
        w.writerow([datetime.now().isoformat(), user_id, username or "",
                     a1, a2, a2_free, a3])


# ── Label lookup ──
def label_for(options, key):
    for k, lbl in options:
        if k == key:
            return lbl
    return key


def labels_for(options, keys):
    return [label_for(options, k) for k in keys]


# ── User roadmap persistence ──
def save_user_roadmap(user_id, data):
    """Save user roadmap data (dict) to JSON file."""
    all_data = {}
    if USER_ROADMAPS_PATH.exists():
        try:
            all_data = json.loads(USER_ROADMAPS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    all_data[str(user_id)] = data
    USER_ROADMAPS_PATH.write_text(
        json.dumps(all_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_user_roadmap(user_id):
    """Returns dict (new format), string (old format), or None."""
    if not USER_ROADMAPS_PATH.exists():
        return None
    try:
        all_data = json.loads(USER_ROADMAPS_PATH.read_text(encoding="utf-8"))
        return all_data.get(str(user_id))
    except (json.JSONDecodeError, OSError):
        return None


def load_all_roadmaps():
    """Returns full JSON dict for scheduler."""
    if not USER_ROADMAPS_PATH.exists():
        return {}
    try:
        return json.loads(USER_ROADMAPS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def update_user_roadmap(user_id, updates):
    """Partial merge update for a user's roadmap entry."""
    all_data = load_all_roadmaps()
    key = str(user_id)
    entry = all_data.get(key)
    if not isinstance(entry, dict):
        return
    entry.update(updates)
    all_data[key] = entry
    USER_ROADMAPS_PATH.write_text(
        json.dumps(all_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ── Helpers ──
def build_url(post_ids):
    """Returns Mini App URL with given post IDs."""
    return f"{BASE_URL}?ids={','.join(post_ids)}"


def collect_roadmap_posts(pain_keys_q2):
    """Given Q2 keys, returns (unique_ids, per_pain_data).

    per_pain_data is a list of (q2_key, label, posts) tuples.
    """
    all_ids = []
    per_pain_data = []
    for q2_key in pain_keys_q2:
        pain_key = Q2_TO_PAIN.get(q2_key)
        if not pain_key or pain_key not in ROADMAP:
            continue
        posts = ROADMAP[pain_key]
        lbl = label_for(Q2_OPTIONS, q2_key)
        per_pain_data.append((q2_key, lbl, posts))
        all_ids.extend(p["id"] for p in posts)
    # Deduplicate while preserving order
    seen = set()
    unique_ids = [i for i in all_ids if not (i in seen or seen.add(i))]
    return unique_ids, per_pain_data


# ── Claude: roadmap ──
def generate_roadmap(a1_labels, a2_labels, a3_days):
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    a1_str = ", ".join(a1_labels)
    a2_str = ", ".join(a2_labels)

    prompt = f"""You are an assistant for a business club. Based on user profile, select posts from the knowledge base.

User profile:
- Who: {a1_str}
- Focus areas: {a2_str}
- Roadmap period: {a3_days} days

Knowledge base (title, category, topic, views, link, type):

{json.dumps(POSTS_COMPACT, ensure_ascii=False, indent=None)}

Rules:
- Return ONLY a list of posts, no intro text, no day titles, no conclusion, no encouragement, no advice, no summaries
- Select posts that directly match the user's focus areas. Prioritize posts most relevant to their selected topics
- Exclude any posts that look like technical/admin posts (payments, announcements, one-time events)
- No invented text — only post titles and links from the knowledge base
- Distribute 1-3 posts per day evenly across {a3_days} days
- Sort posts chronologically by date (oldest first) within the roadmap — this follows the author's own journey and creates a natural learning progression
- Use the "date" field to determine order

Format strictly (no deviations, no emoji, no ## or ** or ---):

День 1
[название поста](ссылка)
[название поста](ссылка)

День 2
[название поста](ссылка)

Each post MUST be a Telegram markdown link: [Title](https://t.me/...)
Respond in Russian."""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


# ── Claude: free-text follow-up ──
def generate_selection(user_request, user_context):
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    prompt = f"""You are an assistant for a business club. User asks for specific materials.

User context:
- Who: {user_context.get('a1_str', '')}
- Focus: {user_context.get('a2_str', '')}

User request: {user_request}

Knowledge base:

{json.dumps(POSTS_COMPACT, ensure_ascii=False, indent=None)}

Rules:
- Return ONLY a list of posts, no intro text, no conclusion, no advice, no summaries
- Select posts that directly match the user's request
- Exclude any posts that look like technical/admin posts (payments, announcements, one-time events)
- No invented text — only post titles and links from the knowledge base
- Select 5-15 most relevant posts
- Group by topic if many posts

Format strictly (no emoji, no ## or ** or ---):
[название поста](ссылка)
[название поста](ссылка)

Each post MUST be a Telegram markdown link: [Title](https://t.me/...)
Respond in Russian."""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


# ── Claude: adjust remaining posts after user correction ──
def adjust_remaining_posts(correction_text, remaining_ids, pain_keys):
    """Ask Claude to reorder/filter remaining posts based on user feedback.

    Returns a new list of post IDs.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    # Collect all available posts from user's pain points
    available_posts = []
    seen_ids = set()
    for q2_key in pain_keys:
        pain_key = Q2_TO_PAIN.get(q2_key)
        if pain_key and pain_key in ROADMAP:
            for p in ROADMAP[pain_key]:
                if p["id"] not in seen_ids:
                    seen_ids.add(p["id"])
                    available_posts.append(p)

    # Build compact representation of available posts
    available_compact = []
    for p in available_posts:
        available_compact.append({
            "id": p["id"],
            "title": p.get("title", ""),
            "topic": p.get("topic", ""),
        })

    prompt = f"""You are an assistant for a business club content drip system.

The user is receiving posts in batches. They gave feedback about what they want to change.

User feedback: {correction_text}

Currently remaining (not yet sent) post IDs: {json.dumps(remaining_ids)}

All available posts from user's selected topics:
{json.dumps(available_compact, ensure_ascii=False)}

Rules:
- Based on the user's feedback, return a reordered/filtered list of post IDs
- You can add posts from the available list that weren't in remaining
- You can remove posts from remaining that the user doesn't want
- You can reorder to prioritize what the user asked for
- Return ONLY a JSON array of post ID strings, nothing else
- Example: ["2024_17", "2025_62", "2024_385"]

Return ONLY the JSON array."""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()
    # Extract JSON array from response
    match = re.search(r'\[.*\]', text, re.DOTALL)
    if match:
        return json.loads(match.group())
    return remaining_ids  # fallback: keep as is


# ── Handlers ──
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["sel1"] = set()
    context.user_data["sel2"] = set()
    await update.message.reply_text(
        "Привет! Я помогу вам найти самые полезные материалы "
        "в Библиотеке Секретов. Давайте познакомимся!"
    )
    await update.message.reply_text(
        Q1_TEXT, reply_markup=make_multi_keyboard(Q1_OPTIONS, set()),
        parse_mode="Markdown"
    )
    return Q1


async def answer_q1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == DONE_CB:
        selected = context.user_data.get("sel1", set())
        if not selected:
            await query.answer("Выберите хотя бы один вариант!", show_alert=True)
            return Q1
        context.user_data["a1"] = list(selected)
        chosen = ", ".join(labels_for(Q1_OPTIONS, selected))
        await query.edit_message_text(f"✅ {chosen}")
        await query.message.reply_text(
            Q2_TEXT, reply_markup=make_multi_keyboard(Q2_OPTIONS, set()),
            parse_mode="Markdown"
        )
        return Q2

    sel = context.user_data.get("sel1", set())
    if query.data in sel:
        sel.discard(query.data)
    else:
        sel.add(query.data)
    context.user_data["sel1"] = sel
    await query.edit_message_reply_markup(
        reply_markup=make_multi_keyboard(Q1_OPTIONS, sel)
    )
    return Q1


async def answer_q2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == DONE_CB:
        selected = context.user_data.get("sel2", set())
        if not selected:
            await query.answer("Выберите хотя бы один вариант!", show_alert=True)
            return Q2
        context.user_data["a2"] = list(selected)
        chosen = ", ".join(labels_for(Q2_OPTIONS, selected))
        await query.edit_message_text(f"✅ {chosen}")

        # Networking message if community/networking selected
        if "community" in selected:
            await query.message.reply_text(NETWORK_MSG, parse_mode="Markdown")

        await query.message.reply_text(Q3_TEXT, reply_markup=make_keyboard(Q3_OPTIONS))
        return Q3

    sel = context.user_data.get("sel2", set())
    if query.data in sel:
        sel.discard(query.data)
    else:
        sel.add(query.data)
    context.user_data["sel2"] = sel
    await query.edit_message_reply_markup(
        reply_markup=make_multi_keyboard(Q2_OPTIONS, sel)
    )
    return Q2


async def answer_q3(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["a3"] = query.data
    await query.edit_message_text(
        f"✅ {label_for(Q3_OPTIONS, query.data)}"
    )

    a1 = context.user_data["a1"]
    a2 = context.user_data["a2"]
    a3 = context.user_data["a3"]

    user = query.from_user
    log_answers(user.id, user.username, ";".join(a1), ";".join(a2), "", a3)

    a1_labels = labels_for(Q1_OPTIONS, a1)
    a2_labels = labels_for(Q2_OPTIONS, a2)

    # Save context for follow-up chat
    context.user_data["chat_context"] = {
        "a1_str": ", ".join(a1_labels),
        "a2_str": ", ".join(a2_labels),
    }

    # Ask delivery preference
    await query.message.reply_text(
        Q4_TEXT, reply_markup=make_keyboard(Q4_OPTIONS)
    )
    return Q4


async def answer_q4(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data
    await query.edit_message_text(
        f"✅ {label_for(Q4_OPTIONS, choice)}"
    )

    a2 = context.user_data["a2"]
    a3 = context.user_data["a3"]
    period_days = int(a3)
    user = query.from_user
    chat_id = query.message.chat_id

    unique_ids, per_pain_data = collect_roadmap_posts(a2)
    if not unique_ids:
        await query.message.reply_text(
            "Не удалось подобрать материалы. Попробуйте ещё раз: /start"
        )
        return ConversationHandler.END

    combined_url = build_url(unique_ids)

    if choice == "send_all":
        # Send per-pain Mini App buttons
        for q2_key, lbl, posts in per_pain_data:
            url = build_url([p["id"] for p in posts])
            text = f"📚 {lbl} — {len(posts)} материалов подобрано для вас"
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "Открыть дорожную карту",
                    web_app=WebAppInfo(url=url),
                )
            ]])
            await query.message.reply_text(text, reply_markup=kb)

        save_user_roadmap(user.id, {
            "url": combined_url,
            "posts": unique_ids,
            "sent_index": len(unique_ids),
            "period_days": period_days,
            "start_date": date.today().isoformat(),
            "reminders": False,
            "paused": False,
            "delivery_count": 0,
            "last_delivery": date.today().isoformat(),
            "chat_id": chat_id,
            "pain_keys": a2,
        })

        await query.message.reply_text(AFTER_ROADMAP_MSG)
        return CHAT

    # drip or drip_remind
    first_batch = unique_ids[:BATCH_SIZE]
    total_deliveries = (len(unique_ids) + BATCH_SIZE - 1) // BATCH_SIZE
    url = build_url(first_batch)
    text = f"День 1 из {period_days} 📚 Вот первые материалы для вас:"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Открыть материалы", web_app=WebAppInfo(url=url))],
        [InlineKeyboardButton("Вся дорожная карта", web_app=WebAppInfo(url=combined_url))],
    ])
    await query.message.reply_text(text, reply_markup=kb)

    save_user_roadmap(user.id, {
        "url": combined_url,
        "posts": unique_ids,
        "sent_index": len(first_batch),
        "period_days": period_days,
        "start_date": date.today().isoformat(),
        "reminders": choice == "drip_remind",
        "paused": False,
        "delivery_count": 1,
        "last_delivery": date.today().isoformat(),
        "chat_id": chat_id,
        "pain_keys": a2,
    })

    await query.message.reply_text(AFTER_ROADMAP_MSG)
    return CHAT


async def handle_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    user_context = context.user_data.get("chat_context", {})

    await update.message.reply_text("Подбираю материалы по твоему запросу...")

    try:
        result = generate_selection(user_text, user_context)
        chunks = split_message(result, 4000)
        for chunk in chunks:
            await update.message.reply_text(chunk, parse_mode="Markdown",
                                                        disable_web_page_preview=True)
    except Exception as e:
        log.error("Claude API error: %s", e)
        await update.message.reply_text(
            "Произошла ошибка. Попробуйте ещё раз или напишите /start"
        )

    await update.message.reply_text(AFTER_ROADMAP_MSG)
    return CHAT


def split_message(text, max_len=4000):
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


# ── Drip delivery scheduler ──
async def drip_delivery_job(context: ContextTypes.DEFAULT_TYPE):
    """Daily job: send next batch to drip users."""
    all_data = load_all_roadmaps()
    today = date.today()

    for user_id_str, entry in all_data.items():
        # Skip old string entries
        if not isinstance(entry, dict):
            continue
        # Skip paused or fully sent
        if entry.get("paused", False):
            continue
        posts = entry.get("posts", [])
        sent_index = entry.get("sent_index", 0)
        if sent_index >= len(posts):
            continue

        chat_id = entry.get("chat_id")
        if not chat_id:
            continue

        period_days = entry.get("period_days", 14)
        interval = DRIP_INTERVALS.get(period_days, 2)
        last_delivery = date.fromisoformat(entry.get("last_delivery", today.isoformat()))
        days_since_last = (today - last_delivery).days

        # Not time for next delivery yet
        if days_since_last < interval:
            # Send reminder if enabled and 2+ days passed since last delivery
            if entry.get("reminders", False) and days_since_last >= 2:
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text="Напоминаю — у вас есть новые материалы в дорожной карте! "
                             "Загляните, когда будет время 📖",
                    )
                except Exception as e:
                    log.warning("Reminder failed for user %s: %s", user_id_str, e)
            continue

        # Send next batch
        next_batch = posts[sent_index:sent_index + BATCH_SIZE]
        new_sent_index = sent_index + len(next_batch)
        delivery_count = entry.get("delivery_count", 0) + 1
        start_date = date.fromisoformat(entry.get("start_date", today.isoformat()))
        current_day = (today - start_date).days + 1

        url = build_url(posts[:new_sent_index])
        full_url = entry.get("url", build_url(posts))
        text = f"День {current_day} из {period_days} 📚 Новые материалы для вас:"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Открыть материалы", web_app=WebAppInfo(url=url))],
            [InlineKeyboardButton("Вся дорожная карта", web_app=WebAppInfo(url=full_url))],
        ])

        try:
            await context.bot.send_message(
                chat_id=chat_id, text=text, reply_markup=kb
            )
        except Exception as e:
            if "Forbidden" in str(e) or "blocked" in str(e).lower():
                log.warning("User %s blocked bot, pausing drip", user_id_str)
                update_user_roadmap(int(user_id_str), {"paused": True})
            else:
                log.error("Drip delivery failed for user %s: %s", user_id_str, e)
            continue

        update_user_roadmap(int(user_id_str), {
            "sent_index": new_sent_index,
            "delivery_count": delivery_count,
            "last_delivery": today.isoformat(),
        })

        # Every 3rd delivery: ask for feedback
        if delivery_count % 3 == 0 and new_sent_index < len(posts):
            feedback_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Всё отлично, продолжай!", callback_data="drip_ok")],
                [InlineKeyboardButton("Хочу скорректировать", callback_data="drip_correct")],
            ])
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="Как вам материалы? Подходят по теме?",
                    reply_markup=feedback_kb,
                )
            except Exception as e:
                log.warning("Feedback prompt failed for user %s: %s", user_id_str, e)

        # All posts sent: completion message
        if new_sent_index >= len(posts):
            combined_url = entry.get("url", build_url(posts))
            done_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "Открыть полную дорожную карту",
                    web_app=WebAppInfo(url=combined_url),
                )
            ]])
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="🎉 Все материалы из вашей дорожной карты отправлены! "
                         "Вот полная карта:",
                    reply_markup=done_kb,
                )
            except Exception as e:
                log.warning("Completion message failed for user %s: %s", user_id_str, e)


# ── Drip feedback handlers (group -1) ──
async def handle_drip_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle drip_ok / drip_correct callback buttons."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "drip_ok":
        await query.edit_message_text("Отлично! Продолжаем 🚀")
        raise ApplicationHandlerStop()

    if query.data == "drip_correct":
        AWAITING_CORRECTION.add(user_id)
        await query.edit_message_text(
            "Напишите, что бы вы хотели изменить — "
            "например, больше про продажи или меньше про блог:"
        )
        raise ApplicationHandlerStop()


class CorrectionFilter(filters.MessageFilter):
    """Matches messages from users in AWAITING_CORRECTION set."""
    def filter(self, message):
        return message.from_user and message.from_user.id in AWAITING_CORRECTION


async def handle_correction_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process user's correction text and adjust remaining posts via Claude."""
    user_id = update.effective_user.id
    AWAITING_CORRECTION.discard(user_id)

    entry = load_user_roadmap(user_id)
    if not isinstance(entry, dict):
        await update.message.reply_text("Не нашёл вашу дорожную карту. Напишите /start")
        raise ApplicationHandlerStop()

    posts = entry.get("posts", [])
    sent_index = entry.get("sent_index", 0)
    remaining_ids = posts[sent_index:]
    pain_keys = entry.get("pain_keys", [])

    await update.message.reply_text("Корректирую маршрут...")

    try:
        new_remaining = await asyncio.to_thread(
            adjust_remaining_posts, update.message.text, remaining_ids, pain_keys
        )
        # Keep already-sent posts, append new remaining
        new_posts = posts[:sent_index] + new_remaining
        update_user_roadmap(user_id, {
            "posts": new_posts,
            "url": build_url(new_posts),
        })
        await update.message.reply_text("Скорректировал ваш маршрут — продолжаем!")
    except Exception as e:
        log.error("Correction failed for user %s: %s", user_id, e)
        await update.message.reply_text(
            "Не удалось скорректировать. Продолжу с текущим маршрутом."
        )

    raise ApplicationHandlerStop()


# ── /pause and /resume ──
async def pause_drip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    entry = load_user_roadmap(user_id)
    if not isinstance(entry, dict):
        await update.message.reply_text(
            "У вас нет активной рассылки. Напишите /start чтобы начать."
        )
        return
    update_user_roadmap(user_id, {"paused": True})
    await update.message.reply_text("Рассылка поставлена на паузу ⏸")


async def resume_drip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    entry = load_user_roadmap(user_id)
    if not isinstance(entry, dict):
        await update.message.reply_text(
            "У вас нет активной рассылки. Напишите /start чтобы начать."
        )
        return
    update_user_roadmap(user_id, {"paused": False})
    await update.message.reply_text("Рассылка возобновлена ▶️")


# ── /my_roadmap ──
async def my_roadmap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    entry = load_user_roadmap(update.effective_user.id)
    if not entry:
        await update.message.reply_text(
            "Вы ещё не создали дорожную карту. Напишите /start чтобы начать."
        )
        return
    # Handle both old format (string URL) and new format (dict with url key)
    if isinstance(entry, str):
        url = entry
    else:
        url = entry.get("url", "")
    if not url:
        await update.message.reply_text(
            "Вы ещё не создали дорожную карту. Напишите /start чтобы начать."
        )
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "Открыть дорожную карту",
            web_app=WebAppInfo(url=url),
        )
    ]])
    await update.message.reply_text(
        "📚 Ваша персональная дорожная карта", reply_markup=kb
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Окей, если захотите начать — напишите /start")
    return ConversationHandler.END


# ── Main ──
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Group -1: drip feedback handlers (before ConversationHandler)
    app.add_handler(
        CallbackQueryHandler(handle_drip_feedback, pattern=r"^drip_"),
        group=-1,
    )
    app.add_handler(
        MessageHandler(CorrectionFilter() & filters.TEXT & ~filters.COMMAND,
                       handle_correction_text),
        group=-1,
    )

    # Group 0: ConversationHandler + standalone commands
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            Q1: [CallbackQueryHandler(answer_q1)],
            Q2: [CallbackQueryHandler(answer_q2)],
            Q3: [CallbackQueryHandler(answer_q3)],
            Q4: [CallbackQueryHandler(answer_q4)],
            CHAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chat)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("my_roadmap", my_roadmap))
    app.add_handler(CommandHandler("pause", pause_drip))
    app.add_handler(CommandHandler("resume", resume_drip))

    # JobQueue: daily drip delivery at 10:00 Moscow time
    moscow_tz = pytz.timezone("Europe/Moscow")
    app.job_queue.run_daily(
        drip_delivery_job,
        time=dt_time(hour=10, minute=0, tzinfo=moscow_tz),
        name="drip_delivery",
    )

    log.info("Bot started. Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
