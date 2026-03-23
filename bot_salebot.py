"""
Secrets Library Bot — Salebot.pro integration.

Flask webhook app that receives messages from Salebot and sends responses
via the Salebot API. Replaces direct Telegram polling (bot.py).

Architecture:
  User ↔ Telegram ↔ Salebot.pro (Sailbot)
                          ↓ outgoing webhook
                    Flask app (Fly.io)
                          ↓ POST /api/{key}/message
                    Salebot.pro → user
"""

import os
import csv
import json
import re
import logging
import threading
from datetime import datetime, date, timedelta
from pathlib import Path

import pytz
import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import anthropic

# ── Config ──
load_dotenv(Path(__file__).parent / ".env")

SALEBOT_API_KEY = os.environ["SALEBOT_API_KEY"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-me")
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]

KB_PATH = Path(__file__).parent / "knowledge_base.js"
DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent)))
LOG_PATH = DATA_DIR / "onboarding_log.csv"
ANALYTICS_PATH = DATA_DIR / "analytics_log.csv"
USER_ROADMAPS_PATH = DATA_DIR / "user_roadmaps.json"
USER_STATES_PATH = DATA_DIR / "user_states.json"

SALEBOT_API_URL = f"https://chatter.salebot.pro/api/{SALEBOT_API_KEY}/message"

BATCH_SIZE = 2
DRIP_INTERVALS = {7: 1, 14: 2, 28: 3, 60: 4}
BASE_URL = "https://olyunyaa.github.io/secrets-library/app.html?v=5"
BONUS_LIMIT = 15

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── Thread-safe file lock ──
_state_lock = threading.Lock()
_roadmap_lock = threading.Lock()

# ── States ──
STATE_Q1 = "q1"
STATE_Q2 = "q2"
STATE_Q3 = "q3"
STATE_Q4 = "q4"
STATE_CHAT = "chat"
STATE_AWAITING_CORRECTION = "awaiting_correction"

# ── Questions & options ──
Q1_TEXT = "Как бы вы описали себя сегодня?\n(можно выбрать несколько вариантов)"
Q1_OPTIONS = [
    ("hire_start", "В найме и хочу начать своё"),
    ("hire_plus", "В найме + свои консультации/проект"),
    ("freelance", "Фрилансер или предприниматель"),
    ("blog", "Хочу личный бренд и расти в соцсетях"),
    ("explore", "Пока изучаю и присматриваюсь"),
]

Q2_TEXT = "На что хотите взять фокус в клубе?\n(можно выбрать несколько вариантов)"
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


# ── Short callback codes (Salebot 64-byte limit on callback_data) ──
Q1_KEY_TO_CB = {key: f"q1_{i+1}" for i, (key, _) in enumerate(Q1_OPTIONS)}
Q1_CB_TO_KEY = {v: k for k, v in Q1_KEY_TO_CB.items()}

Q2_MAPPING = {
    "p1": "pain_1_portfolio_raw",
    "p2": "pain_2_portfolio_start",
    "p3": "pain_3_project_start",
    "p4": "pain_4_personal_brand",
    "p5": "pain_5_social_blog",
    "p6": "pain_6_selling_fear",
    "p7": "pain_7_audience",
    "p8": "pain_8_big_project",
    "p9": "pain_9_blockers",
    "p10": "pain_10_community",
    "p11": "pain_11_money",
}
Q2_KEY_TO_CB = {}
Q2_CB_TO_KEY = {}
Q2_KEY_TO_SHORT = {}
for _short, _pain in Q2_MAPPING.items():
    _q2key = PAIN_TO_Q2[_pain]
    _cb = f"q2_{_short}"
    Q2_KEY_TO_CB[_q2key] = _cb
    Q2_CB_TO_KEY[_cb] = _q2key
    Q2_KEY_TO_SHORT[_q2key] = _short
Q2_SHORT_TO_KEY = {v: k for k, v in Q2_KEY_TO_SHORT.items()}

Q3_KEY_TO_CB = {key: f"q3_{i+1}" for i, (key, _) in enumerate(Q3_OPTIONS)}
Q3_CB_TO_KEY = {v: k for k, v in Q3_KEY_TO_CB.items()}

Q4_KEY_TO_CB = {key: f"q4_{i+1}" for i, (key, _) in enumerate(Q4_OPTIONS)}
Q4_CB_TO_KEY = {v: k for k, v in Q4_KEY_TO_CB.items()}


def label_for(options, key):
    for k, lbl in options:
        if k == key:
            return lbl
    return key


def labels_for(options, keys):
    return [label_for(options, k) for k in keys]


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

PAIN_TO_Q2 = {v: k for k, v in Q2_TO_PAIN.items()}

ALL_CATEGORIES = [
    "Путь предпринимателя",
    "Создание продукта",
    "Соцсети и личный бренд",
    "Рост личности",
    "Маркетинг и продажи",
    "Внутрянка большого бизнеса",
    "Кейсы",
    "Деньги",
    "Портфельная карьера",
]

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


# ══════════════════════════════════════════════════════════
# Salebot API client
# ══════════════════════════════════════════════════════════

def salebot_send(client_id, message, buttons=None):
    """Send a message to a Salebot client.

    Args:
        client_id: Salebot client ID (int or str).
        message: Text message to send.
        buttons: Optional list of Salebot button dicts. Format:
                 [{"type": "inline", "text": "Label", "line": 0, "index_in_line": 0}, ...]
                 For URL buttons: {"type": "inline", "text": "Label", "url": "https://..."}
    """
    payload = {
        "client_id": str(client_id),
        "message": message,
    }
    if buttons:
        payload["buttons"] = json.dumps(buttons)

    try:
        resp = requests.post(SALEBOT_API_URL, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("Salebot send to %s: %s chars, status=%s",
                 client_id, len(message), resp.status_code)
    except Exception as e:
        log.error("Salebot send failed for %s: %s", client_id, e)


# ══════════════════════════════════════════════════════════
# Button builders
# ══════════════════════════════════════════════════════════

def make_multi_buttons(options, selected, cb_map):
    """Build inline buttons with [x]/[ ] marks for multi-select.

    cb_map maps option key → short callback code (Salebot 64-byte limit).
    """
    buttons = []
    for line_idx, (key, label) in enumerate(options):
        mark = "[x] " if key in selected else "[ ] "
        buttons.append({
            "type": "inline",
            "text": mark + label,
            "callback": cb_map[key],
            "line": line_idx,
            "index_in_line": 0,
        })
    buttons.append({
        "type": "inline",
        "text": "Готово >>>",
        "callback": "done",
        "line": len(options),
        "index_in_line": 0,
    })
    return buttons


def make_single_buttons(options, cb_map):
    """Build inline buttons for single-select questions."""
    buttons = []
    for line_idx, (key, label) in enumerate(options):
        buttons.append({
            "type": "inline",
            "text": label,
            "callback": cb_map[key],
            "line": line_idx,
            "index_in_line": 0,
        })
    return buttons


def make_url_button(text, url):
    """Build a single inline URL button."""
    return [{
        "type": "inline",
        "text": text,
        "url": url,
        "callback_link": False,
        "line": 0,
        "index_in_line": 0,
    }]


# ══════════════════════════════════════════════════════════
# State management (user_states.json)
# ══════════════════════════════════════════════════════════

def _load_states():
    if not USER_STATES_PATH.exists():
        return {}
    try:
        return json.loads(USER_STATES_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_states(data):
    USER_STATES_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_user_state(client_id):
    """Return user state dict or None."""
    with _state_lock:
        states = _load_states()
        return states.get(str(client_id))


def set_user_state(client_id, state_dict):
    """Set/overwrite entire user state."""
    with _state_lock:
        states = _load_states()
        states[str(client_id)] = state_dict
        _save_states(states)


def update_user_state(client_id, updates):
    """Partial merge update for a user's state."""
    with _state_lock:
        states = _load_states()
        key = str(client_id)
        entry = states.get(key, {})
        entry.update(updates)
        states[key] = entry
        _save_states(states)


def clear_user_state(client_id):
    """Remove user state entirely."""
    with _state_lock:
        states = _load_states()
        states.pop(str(client_id), None)
        _save_states(states)


# ══════════════════════════════════════════════════════════
# User roadmap persistence (user_roadmaps.json)
# ══════════════════════════════════════════════════════════

def save_user_roadmap(client_id, data):
    with _roadmap_lock:
        all_data = {}
        if USER_ROADMAPS_PATH.exists():
            try:
                all_data = json.loads(USER_ROADMAPS_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        all_data[str(client_id)] = data
        USER_ROADMAPS_PATH.write_text(
            json.dumps(all_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def load_user_roadmap(client_id):
    with _roadmap_lock:
        if not USER_ROADMAPS_PATH.exists():
            return None
        try:
            all_data = json.loads(USER_ROADMAPS_PATH.read_text(encoding="utf-8"))
            return all_data.get(str(client_id))
        except (json.JSONDecodeError, OSError):
            return None


def load_all_roadmaps():
    with _roadmap_lock:
        if not USER_ROADMAPS_PATH.exists():
            return {}
        try:
            return json.loads(USER_ROADMAPS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}


def update_user_roadmap(client_id, updates):
    with _roadmap_lock:
        all_data = {}
        if USER_ROADMAPS_PATH.exists():
            try:
                all_data = json.loads(USER_ROADMAPS_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        key = str(client_id)
        entry = all_data.get(key)
        if not isinstance(entry, dict):
            return
        entry.update(updates)
        all_data[key] = entry
        USER_ROADMAPS_PATH.write_text(
            json.dumps(all_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )


# ══════════════════════════════════════════════════════════
# CSV logging
# ══════════════════════════════════════════════════════════

def log_answers(client_id, a1, a2, a2_free, a3):
    exists = LOG_PATH.exists()
    with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["timestamp", "client_id",
                         "answer1", "answer2", "answer2_free", "answer3"])
        w.writerow([datetime.now().isoformat(), client_id,
                     a1, a2, a2_free, a3])


def log_event(client_id, event, detail=""):
    exists = ANALYTICS_PATH.exists()
    with open(ANALYTICS_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["timestamp", "client_id", "event", "detail"])
        w.writerow([datetime.now().isoformat(), client_id, event, detail])


# ══════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════

def build_url(post_ids):
    return f"{BASE_URL}&ids={','.join(post_ids)}"


def collect_roadmap_posts(pain_keys_q2):
    """Given Q2 keys, returns (unique_ids, per_pain_data)."""
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
    seen = set()
    unique_ids = [i for i in all_ids if not (i in seen or seen.add(i))]
    return unique_ids, per_pain_data


def post_key(p):
    """Build Mini App compatible ID: channel_id + '_' + id."""
    return f"{p.get('channel_id', '')}_{p.get('id', '')}"


# ══════════════════════════════════════════════════════════
# Claude functions (sync)
# ══════════════════════════════════════════════════════════

def classify_request(user_request):
    """Stage 1: classify user request into 1-3 categories."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    prompt = f"""Пользователь бизнес-клуба просит материалы. Определи, к каким категориям относится запрос.

Запрос: {user_request}

Доступные категории:
{json.dumps(ALL_CATEGORIES, ensure_ascii=False)}

Правила:
- Выбери 1-3 наиболее подходящие категории
- Понимай синонимы и контекст: «клиенты», «продавать», «трафик» → «Маркетинг и продажи»; «доход», «заработок», «финансы» → «Деньги»; «блог», «контент», «инстаграм» → «Соцсети и личный бренд»; «выгорание», «страхи», «терапия» → «Рост личности»; «портфель», «фриланс», «несколько проектов» → «Портфельная карьера»; «команда», «найм», «процессы» → «Внутрянка большого бизнеса»; «запуск», «курс», «воронка» → «Создание продукта»
- Верни ТОЛЬКО JSON-массив строк с названиями категорий
- Пример: ["Маркетинг и продажи", "Создание продукта"]

Верни ТОЛЬКО JSON-массив."""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()
    m = re.search(r'\[.*\]', text, re.DOTALL)
    if m:
        cats = json.loads(m.group())
        return [c for c in cats if c in ALL_CATEGORIES]
    return []


def generate_selection(user_request, user_context):
    """Two-stage post selection: classify → rank within categories."""
    categories = classify_request(user_request)
    log.info("classify_request: %s → %s", user_request[:50], categories)

    if categories:
        cat_set = set(categories)
        filtered = [p for p in POSTS if cat_set & set(p.get("category", []))]
    else:
        filtered = POSTS

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    available = []
    for p in filtered:
        pk = post_key(p)
        available.append({
            "id": pk,
            "title": p.get("generated_title", ""),
            "topic": p.get("topic", ""),
            "views": p.get("views", 0),
        })

    prompt = f"""You are an assistant for a business club. User asks for specific materials.

User context:
- Who: {user_context.get('a1_str', '')}
- Focus: {user_context.get('a2_str', '')}

User request: {user_request}

Available posts (pre-filtered by category):
{json.dumps(available, ensure_ascii=False, indent=None)}

Rules:
- Select posts that directly match the user's request
- Exclude any posts that look like technical/admin posts (payments, announcements, one-time events)
- Select 5-15 most relevant posts
- Prefer posts with higher view counts as a signal of quality
- Start with foundational/introductory posts, then more specific ones
- Return ONLY a JSON array of post ID strings, nothing else
- Example: {json.dumps([a["id"] for a in available[:3]])}

Return ONLY the JSON array."""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()
    m = re.search(r'\[.*\]', text, re.DOTALL)
    if m:
        ids = json.loads(m.group())
        valid_ids = {post_key(p) for p in POSTS}
        return [i for i in ids if i in valid_ids]
    return []


def adjust_remaining_posts(correction_text, remaining_ids, pain_keys):
    """Ask Claude to reorder/filter remaining posts based on user feedback."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    available_posts = []
    seen_ids = set()
    for q2_key in pain_keys:
        pain_key = Q2_TO_PAIN.get(q2_key)
        if pain_key and pain_key in ROADMAP:
            for p in ROADMAP[pain_key]:
                if p["id"] not in seen_ids:
                    seen_ids.add(p["id"])
                    available_posts.append(p)

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
    m = re.search(r'\[.*\]', text, re.DOTALL)
    if m:
        return json.loads(m.group())
    return remaining_ids


def suggest_more_posts(pain_label, already_sent_ids, count=5):
    """Ask Claude to pick posts from the KB matching the topic."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    exclude = set(already_sent_ids)
    available = []
    for p in POSTS:
        pk = post_key(p)
        if pk in exclude:
            continue
        available.append({
            "id": pk,
            "title": p.get("generated_title", ""),
            "topic": p.get("topic", ""),
            "views": p.get("views", 0),
        })

    prompt = f"""You are an assistant for a business club. The user wants more posts on a specific topic.

Topic: {pain_label}

Available posts (already-sent posts are excluded):
{json.dumps(available, ensure_ascii=False, indent=None)}

Rules:
- Select exactly {count} posts most relevant to the topic "{pain_label}"
- Prefer posts with higher view counts as a signal of quality
- Exclude technical/admin posts (payments, announcements)
- Return ONLY a JSON array of post ID strings, nothing else
- Example: {json.dumps([a["id"] for a in available[:3]])}

Return ONLY the JSON array."""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()
    m = re.search(r'\[.*\]', text, re.DOTALL)
    if m:
        ids = json.loads(m.group())
        valid_ids = {post_key(p) for p in POSTS}
        return [i for i in ids if i in valid_ids]
    return []


# ══════════════════════════════════════════════════════════
# Handlers
# ══════════════════════════════════════════════════════════

def handle_start(client_id):
    """Start onboarding: clear state, send Q1."""
    log_event(client_id, "start")
    clear_user_state(client_id)
    set_user_state(client_id, {
        "state": STATE_Q1,
        "sel1": [],
        "sel2": [],
    })
    salebot_send(client_id,
                 "Привет! Я помогу вам найти самые полезные материалы "
                 "в Библиотеке Секретов. Давайте познакомимся!")
    salebot_send(client_id, Q1_TEXT,
                 buttons=make_multi_buttons(Q1_OPTIONS, set(), Q1_KEY_TO_CB))


def handle_q1_input(client_id, text, state):
    """Handle Q1 multi-select toggle or 'done'."""
    if text == "done":
        selected = set(state.get("sel1", []))
        if not selected:
            salebot_send(client_id, "Выберите хотя бы один вариант!",
                         buttons=make_multi_buttons(Q1_OPTIONS, selected, Q1_KEY_TO_CB))
            return
        log_event(client_id, "q1_done", ";".join(selected))
        chosen = ", ".join(labels_for(Q1_OPTIONS, selected))
        salebot_send(client_id, f"Вы выбрали: {chosen}")
        update_user_state(client_id, {
            "state": STATE_Q2,
            "a1": list(selected),
        })
        salebot_send(client_id, Q2_TEXT,
                     buttons=make_multi_buttons(Q2_OPTIONS, set(), Q2_KEY_TO_CB))
        return

    key = Q1_CB_TO_KEY.get(text)
    if not key:
        salebot_send(client_id, Q1_TEXT,
                     buttons=make_multi_buttons(Q1_OPTIONS, set(state.get("sel1", [])), Q1_KEY_TO_CB))
        return
    selected = set(state.get("sel1", []))
    if key in selected:
        selected.discard(key)
    else:
        selected.add(key)
    log_event(client_id, "q1_toggle", key)
    update_user_state(client_id, {"sel1": list(selected)})
    salebot_send(client_id, Q1_TEXT,
                 buttons=make_multi_buttons(Q1_OPTIONS, selected, Q1_KEY_TO_CB))


def handle_q2_input(client_id, text, state):
    """Handle Q2 multi-select toggle or 'done'."""
    if text == "done":
        selected = set(state.get("sel2", []))
        if not selected:
            salebot_send(client_id, "Выберите хотя бы один вариант!",
                         buttons=make_multi_buttons(Q2_OPTIONS, selected, Q2_KEY_TO_CB))
            return
        log_event(client_id, "q2_done", ";".join(selected))
        chosen = ", ".join(labels_for(Q2_OPTIONS, selected))
        salebot_send(client_id, f"Вы выбрали: {chosen}")
        update_user_state(client_id, {
            "state": STATE_Q3,
            "a2": list(selected),
        })

        if "community" in selected:
            salebot_send(client_id, NETWORK_MSG)

        salebot_send(client_id, Q3_TEXT,
                     buttons=make_single_buttons(Q3_OPTIONS, Q3_KEY_TO_CB))
        return

    key = Q2_CB_TO_KEY.get(text)
    if not key:
        salebot_send(client_id, Q2_TEXT,
                     buttons=make_multi_buttons(Q2_OPTIONS, set(state.get("sel2", [])), Q2_KEY_TO_CB))
        return
    selected = set(state.get("sel2", []))
    if key in selected:
        selected.discard(key)
    else:
        selected.add(key)
    log_event(client_id, "q2_toggle", key)
    update_user_state(client_id, {"sel2": list(selected)})
    salebot_send(client_id, Q2_TEXT,
                 buttons=make_multi_buttons(Q2_OPTIONS, selected, Q2_KEY_TO_CB))


def handle_q3_input(client_id, text, state):
    """Handle Q3 single-select."""
    key = Q3_CB_TO_KEY.get(text)
    if not key:
        salebot_send(client_id, Q3_TEXT,
                     buttons=make_single_buttons(Q3_OPTIONS, Q3_KEY_TO_CB))
        return

    log_event(client_id, "q3_period", key)
    label = label_for(Q3_OPTIONS, key)
    salebot_send(client_id, f"Вы выбрали: {label}")

    a1 = state.get("a1", [])
    a2 = state.get("a2", [])

    log_answers(client_id, ";".join(a1), ";".join(a2), "", key)

    a1_labels = labels_for(Q1_OPTIONS, a1)
    a2_labels = labels_for(Q2_OPTIONS, a2)

    update_user_state(client_id, {
        "state": STATE_Q4,
        "a3": key,
        "chat_context": {
            "a1_str": ", ".join(a1_labels),
            "a2_str": ", ".join(a2_labels),
        },
    })

    salebot_send(client_id, Q4_TEXT,
                 buttons=make_single_buttons(Q4_OPTIONS, Q4_KEY_TO_CB))


def handle_q4_input(client_id, text, state):
    """Handle Q4 delivery choice, create roadmap."""
    key = Q4_CB_TO_KEY.get(text)
    if not key:
        salebot_send(client_id, Q4_TEXT,
                     buttons=make_single_buttons(Q4_OPTIONS, Q4_KEY_TO_CB))
        return

    label = label_for(Q4_OPTIONS, key)
    log_event(client_id, "q4_delivery", key)
    salebot_send(client_id, f"Вы выбрали: {label}")

    a2 = state.get("a2", [])
    a3 = state.get("a3", "14")
    period_days = int(a3)

    unique_ids, per_pain_data = collect_roadmap_posts(a2)
    if not unique_ids:
        salebot_send(client_id,
                     "Не удалось подобрать материалы. Попробуйте ещё раз — "
                     "напишите «Онбординг и Библиотека».")
        return

    combined_url = build_url(unique_ids)

    if key == "send_all":
        # Send per-pain URL buttons (URL in text as fallback)
        for q2_key, lbl, posts in per_pain_data:
            url = build_url([p["id"] for p in posts])
            msg = f"{lbl} — {len(posts)} материалов подобрано для вас\n\nОткрыть дорожную карту:\n{url}"
            salebot_send(client_id, msg,
                         buttons=make_url_button("Открыть дорожную карту", url))

        save_user_roadmap(client_id, {
            "url": combined_url,
            "posts": unique_ids,
            "sent_index": len(unique_ids),
            "period_days": period_days,
            "start_date": date.today().isoformat(),
            "reminders": False,
            "paused": False,
            "delivery_count": 0,
            "last_delivery": date.today().isoformat(),
            "client_id": client_id,
            "pain_keys": a2,
            "pain_sent": {},
            "pain_sent_ids": {},
        })

        salebot_send(client_id, AFTER_ROADMAP_MSG,
                     buttons=make_url_button("Моя дорожная карта", combined_url))
        update_user_state(client_id, {"state": STATE_CHAT})
        return

    # drip or drip_remind
    first_batch = unique_ids[:BATCH_SIZE]
    url = build_url(first_batch)
    msg = (f"День 1 из {period_days}. Вот первые материалы для вас:\n\n"
           f"Открыть материалы:\n{url}\n\n"
           f"Вся дорожная карта:\n{combined_url}")
    buttons = [
        {"type": "inline", "text": "Открыть материалы", "url": url,
         "callback_link": False, "line": 0, "index_in_line": 0},
        {"type": "inline", "text": "Вся дорожная карта", "url": combined_url,
         "callback_link": False, "line": 1, "index_in_line": 0},
    ]
    salebot_send(client_id, msg, buttons=buttons)

    save_user_roadmap(client_id, {
        "url": combined_url,
        "posts": unique_ids,
        "sent_index": len(first_batch),
        "period_days": period_days,
        "start_date": date.today().isoformat(),
        "reminders": key == "drip_remind",
        "paused": False,
        "delivery_count": 1,
        "last_delivery": date.today().isoformat(),
        "client_id": client_id,
        "pain_keys": a2,
        "pain_sent": {},
        "pain_sent_ids": {},
    })

    salebot_send(client_id, AFTER_ROADMAP_MSG,
                 buttons=make_url_button("Моя дорожная карта", combined_url))
    update_user_state(client_id, {"state": STATE_CHAT})


def handle_chat(client_id, text, state):
    """Free-text request: Claude picks posts."""
    user_context = state.get("chat_context", {})
    log_event(client_id, "chat_request", text[:100])

    salebot_send(client_id, "Подбираю материалы по вашему запросу...")

    try:
        post_ids = generate_selection(text, user_context)
        if not post_ids:
            salebot_send(client_id,
                         "Не удалось подобрать материалы по вашему запросу. "
                         "Попробуйте сформулировать иначе.")
            return
        url = build_url(post_ids)
        salebot_send(client_id,
                     f"Подобрала {len(post_ids)} материалов по вашему запросу:\n\n{url}",
                     buttons=make_url_button("Открыть подборку", url))
    except Exception as e:
        log.error("Claude API error: %s", e)
        salebot_send(client_id,
                     "Произошла ошибка. Попробуйте ещё раз.")
        return

    salebot_send(client_id, AFTER_ROADMAP_MSG)


def handle_drip_ok(client_id):
    """User confirmed drip is good."""
    log_event(client_id, "drip_feedback", "drip_ok")
    salebot_send(client_id, "Отлично! Продолжаем")


def handle_drip_correct(client_id):
    """User wants to correct the drip."""
    log_event(client_id, "drip_feedback", "drip_correct")
    update_user_state(client_id, {"state": STATE_AWAITING_CORRECTION})
    salebot_send(client_id,
                 "Напишите, что бы вы хотели изменить — "
                 "например, больше про продажи или меньше про блог:")


def handle_correction_text(client_id, text):
    """Process user's correction text and adjust remaining posts."""
    entry = load_user_roadmap(client_id)
    if not isinstance(entry, dict):
        salebot_send(client_id,
                     "Не нашёл вашу дорожную карту. "
                     "Напишите «Онбординг и Библиотека» чтобы начать.")
        update_user_state(client_id, {"state": STATE_CHAT})
        return

    posts = entry.get("posts", [])
    sent_index = entry.get("sent_index", 0)
    remaining_ids = posts[sent_index:]
    pain_keys = entry.get("pain_keys", [])

    salebot_send(client_id, "Корректирую маршрут...")

    try:
        new_remaining = adjust_remaining_posts(text, remaining_ids, pain_keys)
        new_posts = posts[:sent_index] + new_remaining
        update_user_roadmap(client_id, {
            "posts": new_posts,
            "url": build_url(new_posts),
        })
        salebot_send(client_id, "Скорректировал ваш маршрут — продолжаем!")
    except Exception as e:
        log.error("Correction failed for client %s: %s", client_id, e)
        salebot_send(client_id,
                     "Не удалось скорректировать. Продолжу с текущим маршрутом.")

    update_user_state(client_id, {"state": STATE_CHAT})


def handle_more_topic(client_id, q2_key):
    """Send 5 bonus posts on the topic."""
    log_event(client_id, "more_topic", q2_key)

    pain_key = Q2_TO_PAIN.get(q2_key)
    if not pain_key:
        salebot_send(client_id, "Тема не найдена.")
        return

    entry = load_user_roadmap(client_id)
    if not isinstance(entry, dict):
        salebot_send(client_id,
                     "Не нашёл вашу дорожную карту. "
                     "Напишите «Онбординг и Библиотека» чтобы начать.")
        return

    pain_sent = entry.get("pain_sent", {})
    already_bonus = pain_sent.get(pain_key, 0)
    lbl = label_for(Q2_OPTIONS, q2_key)

    if already_bonus >= BONUS_LIMIT:
        salebot_send(client_id,
                     f"Вы уже получили максимум дополнительных материалов по теме «{lbl}».")
        return

    salebot_send(client_id, f"Подбираю ещё материалы по теме «{lbl}»...")

    # Collect all post IDs already sent
    main_posts = set(entry.get("posts", []))
    bonus_sent_ids = set()
    for ids_list in entry.get("pain_sent_ids", {}).values():
        bonus_sent_ids.update(ids_list)
    all_sent = main_posts | bonus_sent_ids

    # Get roadmap posts not yet sent
    roadmap_posts = ROADMAP.get(pain_key, [])
    remaining_roadmap = [p["id"] for p in roadmap_posts if p["id"] not in all_sent]

    bonus_count = 5
    bonus_ids = []

    if len(remaining_roadmap) >= bonus_count:
        bonus_ids = remaining_roadmap[:bonus_count]
    else:
        bonus_ids = remaining_roadmap[:]
        needed = bonus_count - len(bonus_ids)
        if needed > 0:
            try:
                exclude = list(all_sent | set(bonus_ids))
                suggested = suggest_more_posts(lbl, exclude, needed)
                bonus_ids.extend(suggested[:needed])
            except Exception as e:
                log.error("suggest_more_posts failed for %s: %s", pain_key, e)

    if not bonus_ids:
        salebot_send(client_id,
                     f"К сожалению, по теме «{lbl}» больше нет подходящих материалов.")
        return

    url = build_url(bonus_ids)
    salebot_send(client_id,
                 f"Ещё {len(bonus_ids)} материалов по теме «{lbl}»:\n\n{url}",
                 buttons=make_url_button("Открыть материалы", url))

    # Update counters
    pain_sent[pain_key] = already_bonus + len(bonus_ids)
    pain_sent_ids = entry.get("pain_sent_ids", {})
    prev_ids = pain_sent_ids.get(pain_key, [])
    pain_sent_ids[pain_key] = prev_ids + bonus_ids
    update_user_roadmap(client_id, {
        "pain_sent": pain_sent,
        "pain_sent_ids": pain_sent_ids,
    })


def handle_my_roadmap(client_id):
    """Send the user's roadmap URL."""
    entry = load_user_roadmap(client_id)
    if not entry:
        salebot_send(client_id,
                     "Вы ещё не создали дорожную карту. "
                     "Напишите «Онбординг и Библиотека» чтобы начать.")
        return
    if isinstance(entry, str):
        url = entry
    else:
        url = entry.get("url", "")
    if not url:
        salebot_send(client_id,
                     "Вы ещё не создали дорожную карту. "
                     "Напишите «Онбординг и Библиотека» чтобы начать.")
        return
    salebot_send(client_id,
                 f"Ваша персональная дорожная карта:\n\n{url}",
                 buttons=make_url_button("Открыть дорожную карту", url))


def handle_pause(client_id):
    entry = load_user_roadmap(client_id)
    if not isinstance(entry, dict):
        salebot_send(client_id,
                     "У вас нет активной рассылки. "
                     "Напишите «Онбординг и Библиотека» чтобы начать.")
        return
    update_user_roadmap(client_id, {"paused": True})
    salebot_send(client_id, "Рассылка поставлена на паузу")


def handle_resume(client_id):
    entry = load_user_roadmap(client_id)
    if not isinstance(entry, dict):
        salebot_send(client_id,
                     "У вас нет активной рассылки. "
                     "Напишите «Онбординг и Библиотека» чтобы начать.")
        return
    update_user_roadmap(client_id, {"paused": False})
    salebot_send(client_id, "Рассылка возобновлена")


# ══════════════════════════════════════════════════════════
# Router
# ══════════════════════════════════════════════════════════

def process_message(client_id, text, telegram_user_id=None):
    """Main message router. Called from webhook."""
    text = text.strip()
    if not text:
        return

    # Save telegram_user_id for reference
    if telegram_user_id:
        st = get_user_state(client_id)
        if st:
            if st.get("telegram_user_id") != str(telegram_user_id):
                update_user_state(client_id, {"telegram_user_id": str(telegram_user_id)})

    # ── Global commands (work in any state) ──
    text_lower = text.lower().strip()

    if text_lower in ("онбординг и библиотека", "/start"):
        handle_start(client_id)
        return

    if text_lower in ("моя дорожная карта", "/my_roadmap"):
        handle_my_roadmap(client_id)
        return

    if text_lower in ("/pause", "пауза"):
        handle_pause(client_id)
        return

    if text_lower in ("/resume", "продолжить"):
        handle_resume(client_id)
        return

    # ── Drip feedback buttons ──
    if text == "drip_ok":
        handle_drip_ok(client_id)
        return

    if text == "drip_fix":
        handle_drip_correct(client_id)
        return

    # ── "More about" topic buttons ──
    if text.startswith("more_"):
        q2_key = Q2_SHORT_TO_KEY.get(text[5:])
        if q2_key:
            handle_more_topic(client_id, q2_key)
        else:
            salebot_send(client_id, "Тема не найдена.")
        return

    # ── State-based routing ──
    state = get_user_state(client_id)
    if not state:
        # No state — user hasn't started onboarding
        # Treat any message as free-text if they have a roadmap
        entry = load_user_roadmap(client_id)
        if isinstance(entry, dict):
            # They have a roadmap, treat as chat
            handle_chat(client_id, text, {
                "chat_context": {},
            })
        else:
            salebot_send(client_id,
                         "Привет! Напишите «Онбординг и Библиотека» чтобы начать.")
        return

    current_state = state.get("state", "")

    if current_state == STATE_Q1:
        handle_q1_input(client_id, text, state)
    elif current_state == STATE_Q2:
        handle_q2_input(client_id, text, state)
    elif current_state == STATE_Q3:
        handle_q3_input(client_id, text, state)
    elif current_state == STATE_Q4:
        handle_q4_input(client_id, text, state)
    elif current_state == STATE_AWAITING_CORRECTION:
        handle_correction_text(client_id, text)
    elif current_state == STATE_CHAT:
        handle_chat(client_id, text, state)
    else:
        salebot_send(client_id,
                     "Напишите «Онбординг и Библиотека» чтобы начать.")


# ══════════════════════════════════════════════════════════
# Drip delivery job (APScheduler)
# ══════════════════════════════════════════════════════════

def drip_delivery_job():
    """Daily job: send next batch to drip users."""
    log.info("Running drip delivery job...")
    all_data = load_all_roadmaps()
    today = date.today()

    for client_id_str, entry in all_data.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("paused", False):
            continue
        posts = entry.get("posts", [])
        sent_index = entry.get("sent_index", 0)
        if sent_index >= len(posts):
            continue

        client_id = entry.get("client_id", client_id_str)

        period_days = entry.get("period_days", 14)
        interval = DRIP_INTERVALS.get(period_days, 2)
        last_delivery = date.fromisoformat(
            entry.get("last_delivery", today.isoformat()))
        days_since_last = (today - last_delivery).days

        if days_since_last < interval:
            # Send reminder if enabled and 2+ days passed
            if entry.get("reminders", False) and days_since_last >= 2:
                try:
                    salebot_send(client_id,
                                 "Напоминаю — у вас есть новые материалы в дорожной карте! "
                                 "Загляните, когда будет время")
                except Exception as e:
                    log.warning("Reminder failed for client %s: %s", client_id_str, e)
            continue

        # Send next batch
        next_batch = posts[sent_index:sent_index + BATCH_SIZE]
        new_sent_index = sent_index + len(next_batch)
        delivery_count = entry.get("delivery_count", 0) + 1
        start_date = date.fromisoformat(
            entry.get("start_date", today.isoformat()))
        current_day = (today - start_date).days + 1

        url = build_url(posts[:new_sent_index])
        full_url = entry.get("url", build_url(posts))
        msg = (f"День {current_day} из {period_days}. Новые материалы для вас:\n\n"
               f"Открыть материалы:\n{url}\n\n"
               f"Вся дорожная карта:\n{full_url}")
        buttons = [
            {"type": "inline", "text": "Открыть материалы", "url": url,
             "callback_link": False, "line": 0, "index_in_line": 0},
            {"type": "inline", "text": "Вся дорожная карта", "url": full_url,
             "callback_link": False, "line": 1, "index_in_line": 0},
        ]

        try:
            salebot_send(client_id, msg, buttons=buttons)
        except Exception as e:
            log.error("Drip delivery failed for client %s: %s", client_id_str, e)
            continue

        update_user_roadmap(client_id_str, {
            "sent_index": new_sent_index,
            "delivery_count": delivery_count,
            "last_delivery": today.isoformat(),
        })

        # "Больше про..." buttons (only if >1 pain point)
        pain_keys = entry.get("pain_keys", [])
        pain_sent = entry.get("pain_sent", {})
        if len(pain_keys) > 1:
            more_buttons = []
            line_idx = 0
            for q2_key in pain_keys:
                pk = Q2_TO_PAIN.get(q2_key)
                if not pk:
                    continue
                already_bonus = pain_sent.get(pk, 0)
                if already_bonus >= BONUS_LIMIT:
                    continue
                lbl = label_for(Q2_OPTIONS, q2_key)
                short = Q2_KEY_TO_SHORT.get(q2_key, q2_key)
                more_buttons.append({
                    "type": "inline",
                    "text": f">> {lbl}",
                    "callback": f"more_{short}",
                    "line": line_idx, "index_in_line": 0,
                })
                line_idx += 1
            if more_buttons:
                try:
                    salebot_send(client_id,
                                 "Хотите углубиться в одну из тем?",
                                 buttons=more_buttons)
                except Exception as e:
                    log.warning("More-topic buttons failed for %s: %s",
                                client_id_str, e)

        # Every 3rd delivery: ask for feedback
        if delivery_count % 3 == 0 and new_sent_index < len(posts):
            feedback_buttons = [
                {"type": "inline", "text": "Все отлично!",
                 "callback": "drip_ok",
                 "line": 0, "index_in_line": 0},
                {"type": "inline", "text": "Скорректировать",
                 "callback": "drip_fix",
                 "line": 1, "index_in_line": 0},
            ]
            try:
                salebot_send(client_id,
                             "Как вам материалы? Подходят по теме?",
                             buttons=feedback_buttons)
            except Exception as e:
                log.warning("Feedback prompt failed for %s: %s",
                            client_id_str, e)

        # All posts sent: completion message
        if new_sent_index >= len(posts):
            combined_url = entry.get("url", build_url(posts))
            try:
                salebot_send(client_id,
                             "Все материалы из вашей дорожной карты отправлены! "
                             f"Вот полная карта:\n\n{combined_url}",
                             buttons=make_url_button(
                                 "Открыть полную дорожную карту", combined_url))
            except Exception as e:
                log.warning("Completion message failed for %s: %s",
                            client_id_str, e)

    log.info("Drip delivery job finished.")


# ══════════════════════════════════════════════════════════
# Flask webhook
# ══════════════════════════════════════════════════════════

@app.route("/webhook/<path:secret>", methods=["POST"])
def webhook(secret):
    """Receive incoming messages from Salebot."""
    # Strip whitespace — Salebot may add trailing spaces to the URL
    if secret.strip() != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    log.info("Webhook received: %s", json.dumps(data, ensure_ascii=False)[:500])

    # Extract client_id and message text from Salebot webhook payload.
    # Salebot may send flat: {"client_id": "...", "message": "..."}
    # or nested: {"client": {"id": ..., "recipient": ...}, "message": "..."}
    client_id = data.get("client_id")
    client_data = data.get("client", {})
    if not client_id and client_data:
        client_id = client_data.get("id")
    message_text = data.get("message", "")
    telegram_user_id = client_data.get("recipient") or client_data.get("recepient")

    # Skip bot's own outgoing messages
    if data.get("is_input") == 0:
        return jsonify({"status": "ok", "note": "outgoing message, skipped"}), 200

    if not client_id or not message_text:
        return jsonify({"status": "ok", "note": "no client_id or message"}), 200

    # Process in a thread to not block the webhook response
    thread = threading.Thread(
        target=process_message,
        args=(client_id, message_text, telegram_user_id),
    )
    thread.start()

    return jsonify({"status": "ok"}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy", "posts": len(POSTS)}), 200


# ══════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════

def main():
    # Start APScheduler for drip delivery
    moscow_tz = pytz.timezone("Europe/Moscow")
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        drip_delivery_job,
        trigger=CronTrigger(hour=10, minute=0, timezone=moscow_tz),
        id="drip_delivery",
        replace_existing=True,
    )
    scheduler.start()
    log.info("Scheduler started: drip delivery daily at 10:00 MSK")

    # Run Flask
    port = int(os.environ.get("PORT", 8080))
    log.info("Starting Flask on port %d", port)
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
