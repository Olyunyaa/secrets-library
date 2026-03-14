import os
import csv
import json
import re
import logging
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    ApplicationBuilder,
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
LOG_PATH = Path(__file__).parent / "onboarding_log.csv"
USER_ROADMAPS_PATH = Path(__file__).parent / "user_roadmaps.json"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── States ──
Q1, Q2, Q3, CHAT = range(4)

# ── Questions & options ──
Q1_TEXT = "Как бы ты описал(а) себя сегодня?\n_можно выбрать несколько вариантов_"
Q1_OPTIONS = [
    ("hire_start", "В найме, хочу начать своё"),
    ("hire_plus", "В найме + уже веду свой проект"),
    ("freelance", "Фрилансер или предприниматель"),
    ("blog", "Хочу вести блог и расти в соцсетях"),
    ("explore", "Пока изучаю и присматриваюсь"),
]

Q2_TEXT = "На что хочешь взять фокус в клубе?\n_можно выбрать несколько вариантов_"
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

Q3_TEXT = "На какой период ты хочешь получить дорожную карту?"
Q3_OPTIONS = [
    ("7", "7 дней — быстрый старт"),
    ("14", "14 дней"),
    ("28", "28 дней — один сезон"),
    ("60", "60 дней"),
    ("90", "90 дней — глубокое погружение"),
]

NETWORK_MSG = ("Для нетворкинга в клубе есть Random Coffee.\n"
               "Каждую пятницу в Чат Секреты 2026 приходит голосование — "
               "хочешь ли участвовать в Random Coffee на следующей неделе. "
               "Если хочешь — отметь в голосовалке. В понедельник в чат приходит "
               "сообщение с разбивкой на пары — тебе нужно написать своей паре "
               "и договориться о встрече в удобном формате.\n\n"
               "Для тех кто хочет двигаться активнее — трекинг-группы "
               "в чате Секреты Практика. В новом сезоне:\n"
               "— Групповая коуч-сессия с Ксюшей. Если хочешь быть активным "
               "участником — заполняй анкету. Выберут трёх участников, каждый "
               "приходит со своим запросом по соцсетям. Зрителем быть тоже можно.\n"
               "— Книжный клуб с Катей — читаем и обсуждаем вместе.\n"
               "— Action-club с Аней каждую среду и пятницу — участники которые "
               "не пропускают эти встречи, за месяц нормально продвигаются "
               "в своих проектах.")

AFTER_ROADMAP_MSG = ("Если захочешь почитать о чём-то конкретном или получить "
                     "больше материалов — просто напиши мне об этом, и я подготовлю "
                     "подборку специально для тебя.")

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
ROADMAP_PATH = Path(__file__).parent / "roadmap_all_pains_v2.json"
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
def save_user_roadmap(user_id, url):
    data = {}
    if USER_ROADMAPS_PATH.exists():
        try:
            data = json.loads(USER_ROADMAPS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    data[str(user_id)] = url
    USER_ROADMAPS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_user_roadmap(user_id):
    if not USER_ROADMAPS_PATH.exists():
        return None
    try:
        data = json.loads(USER_ROADMAPS_PATH.read_text(encoding="utf-8"))
        return data.get(str(user_id))
    except (json.JSONDecodeError, OSError):
        return None


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


# ── Handlers ──
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["sel1"] = set()
    context.user_data["sel2"] = set()
    await update.message.reply_text(
        "Привет! Я помогу тебе найти самые полезные материалы "
        "в Библиотеке Секретов. Давай познакомимся!"
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
            await query.answer("Выбери хотя бы один вариант!", show_alert=True)
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
            await query.answer("Выбери хотя бы один вариант!", show_alert=True)
            return Q2
        context.user_data["a2"] = list(selected)
        chosen = ", ".join(labels_for(Q2_OPTIONS, selected))
        await query.edit_message_text(f"✅ {chosen}")

        # Networking message if community/networking selected
        if "community" in selected:
            await query.message.reply_text(NETWORK_MSG, parse_mode="Markdown")

        # Send roadmap deep-link buttons for each selected pain point
        all_ids = []
        for q2_key in selected:
            pain_key = Q2_TO_PAIN.get(q2_key)
            if not pain_key or pain_key not in ROADMAP:
                continue
            posts = ROADMAP[pain_key]
            ids_str = ",".join(p["id"] for p in posts)
            all_ids.extend(p["id"] for p in posts)
            url = f"https://olyunyaa.github.io/secrets-library/app.html?ids={ids_str}"
            lbl = label_for(Q2_OPTIONS, q2_key)
            text = f"📚 {lbl} — {len(posts)} материалов подобрано для тебя"
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "Открыть дорожную карту",
                    web_app=WebAppInfo(url=url),
                )
            ]])
            await query.message.reply_text(text, reply_markup=kb)

        # Save combined roadmap URL for /moya_karta
        if all_ids:
            # Deduplicate while preserving order
            seen = set()
            unique_ids = [i for i in all_ids if not (i in seen or seen.add(i))]
            combined_url = (
                "https://olyunyaa.github.io/secrets-library/app.html?ids="
                + ",".join(unique_ids)
            )
            save_user_roadmap(query.from_user.id, combined_url)

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

    await query.message.reply_text("Составляю твою персональную дорожную карту...")

    a1_labels = labels_for(Q1_OPTIONS, a1)
    a2_labels = labels_for(Q2_OPTIONS, a2)

    # Save context for follow-up chat
    context.user_data["chat_context"] = {
        "a1_str": ", ".join(a1_labels),
        "a2_str": ", ".join(a2_labels),
    }

    try:
        roadmap = generate_roadmap(a1_labels, a2_labels, a3)
        chunks = split_message(roadmap, 4000)
        for chunk in chunks:
            await query.message.reply_text(chunk, parse_mode="Markdown",
                                                       disable_web_page_preview=True)
    except Exception as e:
        log.error("Claude API error: %s", e)
        await query.message.reply_text(
            "Произошла ошибка при генерации дорожной карты. Попробуй ещё раз: /start"
        )
        return ConversationHandler.END

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
            "Произошла ошибка. Попробуй ещё раз или напиши /start"
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


async def moya_karta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = load_user_roadmap(update.effective_user.id)
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
    await update.message.reply_text("Окей, если захочешь начать — напиши /start")
    return ConversationHandler.END


# ── Main ──
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            Q1: [CallbackQueryHandler(answer_q1)],
            Q2: [CallbackQueryHandler(answer_q2)],
            Q3: [CallbackQueryHandler(answer_q3)],
            CHAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chat)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("moya_karta", moya_karta))
    app.add_handler(CommandHandler("моя_карта", moya_karta))

    log.info("Bot started. Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
