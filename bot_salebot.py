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
import time
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
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

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
_csv_lock = threading.Lock()

# ── Dedup: skip duplicate/echoed webhook messages ──
_seen_ids = set()
_seen_lock = threading.Lock()


def _is_duplicate(webhook_id):
    with _seen_lock:
        if webhook_id in _seen_ids:
            return True
        _seen_ids.add(webhook_id)
        if len(_seen_ids) > 5000:
            _seen_ids.clear()
            _seen_ids.add(webhook_id)
        return False



# ── Salebot system/event messages that leak through the global webhook ──
# These arrive as is_input=1 but are NOT user input (payment/social/channel
# events) — must never be treated as chat requests. Investigation 2026-06-19.
_SYSTEM_MSG_EXACT = {"изменения в stripe", "client_unsubscribed", "kicked_chat_member"}
_SYSTEM_MSG_PREFIX = ("new_like", "sk_live_", "podpiska_", "client_wall_reply")


def _is_system_message(text_lower):
    """True if text is a Salebot system/event message, not real user input."""
    return (text_lower in _SYSTEM_MSG_EXACT
            or text_lower.startswith(_SYSTEM_MSG_PREFIX))


# ── States ──
STATE_Q1 = "q1"
STATE_Q2 = "q2"
STATE_Q3 = "q3"
STATE_Q4 = "q4"
STATE_CHAT = "chat"
STATE_AWAITING_CHAT = "awaiting_chat"
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

NETWORK_MSG = ("Для нетворкинга в клубе есть Random Coffee.\n\n"
               "Каждую пятницу в Чат Секреты 2026 приходит голосование — "
               "хочешь ли участвовать в Random Coffee на следующей неделе. "
               "Если хочешь — отметь в голосовалке. В понедельник в чат приходит "
               "сообщение с разбивкой на пары — тебе нужно написать своей паре "
               "и договориться о встрече в удобном формате.\n\n"
               "Для тех кто хочет двигаться активнее — Секреты Практика "
               "переходит в ещё более активный режим.\n"
               "Таня лично будет курировать три группы: раз в неделю задание, "
               "раз в неделю разборы.\n\n"
               "Коворкинг с Аней каждую среду в 16:30 и пятницу в 9:00 — "
               "идеальное место чтобы вместе работать над заданиями.\n\n"
               "Трекинг-группа 12 недель — первый эфир 1 апреля в 16:00 "
               "по Лондону. Подробности в чате.")

AFTER_ROADMAP_MSG = ("Если захотите почитать о чём-то конкретном или получить "
                     "больше материалов — просто напишите мне об этом, и я подготовлю "
                     "подборку специально для вас.")


# ── Short callback codes (Salebot 64-byte limit on callback_data) ──
Q1_KEY_TO_CB = {key: f"q1_{i+1}" for i, (key, _) in enumerate(Q1_OPTIONS)}
Q1_CB_TO_KEY = {v: k for k, v in Q1_KEY_TO_CB.items()}

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

def make_multi_buttons(options, cb_map):
    """Build inline buttons for multi-select (silent toggle).

    No [x]/[ ] marks — Salebot can't edit messages so marks can't update.
    Clicks are recorded silently; selection shown on 'Готово'.
    """
    buttons = []
    for line_idx, (key, label) in enumerate(options):
        buttons.append({
            "type": "inline",
            "text": label,
            "callback": cb_map[key],
            "line": line_idx,
            "index_in_line": 0,
        })
    buttons.append({
        "type": "inline",
        "text": "Готово",
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
    """Build a single inline URL button (opens in browser)."""
    return [{
        "type": "inline",
        "text": text,
        "url": url,
        "callback_link": False,
        "line": 0,
        "index_in_line": 0,
    }]


def make_webapp_button(text, url):
    """Build a single inline WebApp button (opens as Telegram Mini App)."""
    return [{
        "type": "inline",
        "text": text,
        "web_app": url,
        "line": 0,
        "index_in_line": 0,
    }]


# ══════════════════════════════════════════════════════════
# Telegram Bot API helpers (direct calls with bot token)
# SAFETY: only used for answerCallbackQuery, editMessageReplyMarkup
# on OUR onboarding/library messages. Never send messages or change
# bot settings through this token.
# ══════════════════════════════════════════════════════════

def tg_answer_callback(callback_query_id, text=None):
    """Answer a callback query (removes the loading spinner on the button)."""
    if not TELEGRAM_BOT_TOKEN or not callback_query_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    payload = {"callback_query_id": str(callback_query_id)}
    if text:
        payload["text"] = text
    try:
        resp = requests.post(url, json=payload, timeout=5)
        log.info("answerCallbackQuery %s: %s", str(callback_query_id)[:12], resp.status_code)
    except Exception as e:
        log.warning("answerCallbackQuery failed: %s", e)


def tg_edit_reply_markup(chat_id, message_id, reply_markup=None):
    """Edit (or remove) inline keyboard on a message.

    If reply_markup is None, removes the keyboard entirely.
    """
    if not TELEGRAM_BOT_TOKEN or not chat_id or not message_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageReplyMarkup"
    payload = {
        "chat_id": int(chat_id),
        "message_id": int(message_id),
        "reply_markup": reply_markup or {"inline_keyboard": []},
    }
    try:
        resp = requests.post(url, json=payload, timeout=5)
        log.info("editMessageReplyMarkup chat=%s msg=%s: %s",
                 chat_id, message_id, resp.status_code)
        if resp.status_code != 200:
            log.warning("editMessageReplyMarkup body: %s", resp.text[:200])
    except Exception as e:
        log.warning("editMessageReplyMarkup failed: %s", e)


def build_q1_tg_keyboard(selected):
    """Build Telegram-format inline keyboard for Q1 with ✅ on selected."""
    keyboard = []
    for key, label in Q1_OPTIONS:
        cb = Q1_KEY_TO_CB[key]
        text = f"✅ {label}" if key in selected else label
        keyboard.append([{"text": text, "callback_data": cb}])
    keyboard.append([{"text": "Готово", "callback_data": "done"}])
    keyboard.append([{"text": "Пропустить — открыть Библиотеку",
                      "callback_data": "skip_onb"}])
    return {"inline_keyboard": keyboard}


def build_q2_tg_keyboard(selected):
    """Build Telegram-format inline keyboard for Q2 with ✅ on selected."""
    keyboard = []
    for key, label in Q2_OPTIONS:
        cb = Q2_KEY_TO_CB[key]
        text = f"✅ {label}" if key in selected else label
        keyboard.append([{"text": text, "callback_data": cb}])
    keyboard.append([{"text": "Готово", "callback_data": "done"}])
    return {"inline_keyboard": keyboard}


def build_final_selection_keyboard(options, cb_map, selected):
    """Build keyboard showing only selected items with ✅ (no action buttons)."""
    keyboard = []
    for key, label in options:
        if key in selected:
            cb = cb_map[key]
            keyboard.append([{"text": f"✅ {label}", "callback_data": cb}])
    return {"inline_keyboard": keyboard}


def build_single_tg_keyboard(options, cb_map):
    """Build Telegram-format inline keyboard for single-select questions."""
    keyboard = []
    for key, label in options:
        keyboard.append([{"text": label, "callback_data": cb_map[key]}])
    return {"inline_keyboard": keyboard}


def tg_send_message(chat_id, text, reply_markup=None):
    """Send a message via Telegram Bot API. Returns message_id or None.

    Used for messages with inline keyboards that we need to edit later
    (toggle ✅, remove buttons on completion).
    """
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": int(chat_id),
        "text": text,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            msg_id = data.get("result", {}).get("message_id")
            log.info("tg_send_message chat=%s: msg_id=%s", chat_id, msg_id)
            return msg_id
        log.warning("tg_send_message failed: %s %s", resp.status_code, resp.text[:200])
    except Exception as e:
        log.warning("tg_send_message error: %s", e)
    return None


# ══════════════════════════════════════════════════════════
# State management (user_states.json)
# ══════════════════════════════════════════════════════════

_states_cache = None  # in-memory write-through cache


def _load_states():
    global _states_cache
    if _states_cache is not None:
        return _states_cache
    if not USER_STATES_PATH.exists():
        _states_cache = {}
        return _states_cache
    try:
        _states_cache = json.loads(USER_STATES_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        _states_cache = {}
    return _states_cache


def _save_states(data):
    global _states_cache
    _states_cache = data
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

_roadmaps_cache = None  # in-memory write-through cache


def _load_roadmaps():
    global _roadmaps_cache
    if _roadmaps_cache is not None:
        return _roadmaps_cache
    if not USER_ROADMAPS_PATH.exists():
        _roadmaps_cache = {}
        return _roadmaps_cache
    try:
        _roadmaps_cache = json.loads(USER_ROADMAPS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        _roadmaps_cache = {}
    return _roadmaps_cache


def _save_roadmaps(data):
    global _roadmaps_cache
    _roadmaps_cache = data
    USER_ROADMAPS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def save_user_roadmap(client_id, data):
    with _roadmap_lock:
        all_data = _load_roadmaps()
        all_data[str(client_id)] = data
        _save_roadmaps(all_data)


def load_user_roadmap(client_id):
    with _roadmap_lock:
        all_data = _load_roadmaps()
        return all_data.get(str(client_id))


def load_all_roadmaps():
    with _roadmap_lock:
        return _load_roadmaps()


def update_user_roadmap(client_id, updates):
    with _roadmap_lock:
        all_data = _load_roadmaps()
        key = str(client_id)
        entry = all_data.get(key)
        if not isinstance(entry, dict):
            return
        entry.update(updates)
        all_data[key] = entry
        _save_roadmaps(all_data)


# ══════════════════════════════════════════════════════════
# CSV logging
# ══════════════════════════════════════════════════════════

def log_answers(client_id, a1, a2, a2_free, a3):
    with _csv_lock:
        exists = LOG_PATH.exists()
        with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if not exists:
                w.writerow(["timestamp", "client_id",
                             "answer1", "answer2", "answer2_free", "answer3"])
            w.writerow([datetime.now().isoformat(), client_id,
                         a1, a2, a2_free, a3])


def log_event(client_id, event, detail=""):
    with _csv_lock:
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


def collect_roadmap_posts(pain_keys_q2, exclude_ids=None):
    """Given Q2 keys, returns (unique_ids, per_pain_data).

    If exclude_ids is provided, already-sent post IDs are filtered out.
    """
    exclude = set(exclude_ids) if exclude_ids else set()
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
    unique_ids = [i for i in all_ids
                  if i not in exclude and not (i in seen or seen.add(i))]
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

    if not categories:
        # No categories matched — request is too vague or nonsense
        return []

    cat_set = set(categories)
    filtered = [p for p in POSTS if cat_set & set(p.get("category", []))]

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

def _send_after_roadmap(client_id):
    """Send AFTER_ROADMAP_MSG with 'Подобрать материалы' button."""
    buttons = [{"type": "inline", "text": "Подобрать материалы",
                "callback": "pick_materials", "line": 0, "index_in_line": 0}]
    salebot_send(client_id, AFTER_ROADMAP_MSG, buttons=buttons)


def handle_start(client_id, telegram_user_id=None):
    """Start onboarding or show library if already completed."""
    entry = load_user_roadmap(client_id)
    if isinstance(entry, dict) and entry.get("url"):
        # Already completed onboarding — show library + roadmap
        log_event(client_id, "start_returning")
        url = entry["url"]
        buttons = [
            {"type": "inline", "text": "Открыть Библиотеку",
             "web_app": BASE_URL,
             "line": 0, "index_in_line": 0},
            {"type": "inline", "text": "📚 Моя дорожная карта",
             "web_app": url,
             "line": 1, "index_in_line": 0},
            {"type": "inline", "text": "Подобрать материалы",
             "callback": "pick_materials",
             "line": 2, "index_in_line": 0},
        ]
        salebot_send(client_id,
                     "С возвращением! Вот ваша Библиотека и персональная "
                     "дорожная карта:", buttons=buttons)
        # Make sure state is CHAT so free-text works
        state = get_user_state(client_id)
        if not state or state.get("state") != STATE_CHAT:
            update_user_state(client_id, {"state": STATE_CHAT})
        return

    # User who previously skipped onboarding — show library directly
    state = get_user_state(client_id)
    if state and state.get("state") == "skipped":
        log_event(client_id, "start_skipped")
        chat_id = telegram_user_id or (state.get("telegram_user_id") if state else None)
        skip_text = ("Вот ваша Библиотека! Если хотите получить персональную "
                     "подборку материалов — пройдите короткий онбординг.")
        skip_kb = {"inline_keyboard": [
            [{"text": "Открыть Библиотеку", "web_app": {"url": BASE_URL}}],
            [{"text": "Пройти онбординг", "callback_data": "start_onb"}],
        ]}
        msg_id = tg_send_message(chat_id, skip_text, skip_kb) if chat_id else None
        if msg_id:
            update_user_state(client_id, {
                "kb_message_id": msg_id,
                "telegram_user_id": str(chat_id),
            })
        else:
            buttons = [
                {"type": "inline", "text": "Открыть Библиотеку",
                 "web_app": BASE_URL,
                 "line": 0, "index_in_line": 0},
                {"type": "inline", "text": "Пройти онбординг",
                 "callback": "start_onb",
                 "line": 1, "index_in_line": 0},
            ]
            salebot_send(client_id, skip_text, buttons=buttons)
        return

    # New user — start onboarding
    log_event(client_id, "start")
    clear_user_state(client_id)
    set_user_state(client_id, {
        "state": STATE_Q1,
        "sel1": [],
        "sel2": [],
    })
    greeting = ("Привет! Я помогу вам найти самые полезные материалы "
                "в Библиотеке Секретов. Давайте познакомимся!\n\n"
                + Q1_TEXT)
    # Send Q1 via TG API to get message_id for keyboard editing (✅ toggle)
    chat_id = telegram_user_id
    msg_id = tg_send_message(chat_id, greeting, build_q1_tg_keyboard(set())) if chat_id else None
    if msg_id:
        update_user_state(client_id, {
            "kb_message_id": msg_id,
            "telegram_user_id": str(chat_id),
        })
    else:
        # Fallback: Salebot API (no keyboard editing possible)
        q1_buttons = make_multi_buttons(Q1_OPTIONS, Q1_KEY_TO_CB)
        q1_buttons.append({
            "type": "inline",
            "text": "Пропустить — открыть Библиотеку",
            "callback": "skip_onb",
            "line": len(Q1_OPTIONS) + 1,
            "index_in_line": 0,
        })
        salebot_send(client_id, greeting, buttons=q1_buttons)


def handle_q1_input(client_id, text, state, cb_ctx=None):
    """Handle Q1 multi-select toggle or 'done'."""
    chat_id = state.get("telegram_user_id")
    kb_msg_id = state.get("kb_message_id")

    if text == "done":
        selected = set(state.get("sel1", []))
        if not selected:
            salebot_send(client_id, "Выберите хотя бы один вариант!")
            return
        log_event(client_id, "q1_done", ";".join(selected))
        # Replace keyboard with only selected items (✅)
        if chat_id and kb_msg_id:
            tg_edit_reply_markup(chat_id, kb_msg_id,
                                 build_final_selection_keyboard(
                                     Q1_OPTIONS, Q1_KEY_TO_CB, selected))
        update_user_state(client_id, {
            "state": STATE_Q2,
            "a1": list(selected),
        })
        # Send Q2 via TG API to get message_id for keyboard editing
        new_msg_id = tg_send_message(chat_id, Q2_TEXT, build_q2_tg_keyboard(set())) if chat_id else None
        if new_msg_id:
            update_user_state(client_id, {"kb_message_id": new_msg_id})
        else:
            salebot_send(client_id, Q2_TEXT,
                         buttons=make_multi_buttons(Q2_OPTIONS, Q2_KEY_TO_CB))
        return

    key = Q1_CB_TO_KEY.get(text)
    if not key:
        return  # ignore unknown input silently (may be echo)
    selected = set(state.get("sel1", []))
    if key in selected:
        selected.discard(key)
    else:
        selected.add(key)
    log_event(client_id, "q1_toggle", key)
    update_user_state(client_id, {"sel1": list(selected)})
    # Update keyboard to show ✅ on selected items
    if chat_id and kb_msg_id:
        tg_edit_reply_markup(chat_id, kb_msg_id, build_q1_tg_keyboard(selected))


def handle_q2_input(client_id, text, state, cb_ctx=None):
    """Handle Q2 multi-select toggle or 'done'."""
    chat_id = state.get("telegram_user_id")
    kb_msg_id = state.get("kb_message_id")

    if text == "done":
        selected = set(state.get("sel2", []))
        if not selected:
            salebot_send(client_id, "Выберите хотя бы один вариант!")
            return
        log_event(client_id, "q2_done", ";".join(selected))
        # Replace keyboard with only selected items (✅)
        if chat_id and kb_msg_id:
            tg_edit_reply_markup(chat_id, kb_msg_id,
                                 build_final_selection_keyboard(
                                     Q2_OPTIONS, Q2_KEY_TO_CB, selected))
        update_user_state(client_id, {
            "state": STATE_Q3,
            "a2": list(selected),
        })

        # Send Q3 via TG API to get message_id for keyboard editing
        new_msg_id = tg_send_message(chat_id, Q3_TEXT,
                                     build_single_tg_keyboard(Q3_OPTIONS, Q3_KEY_TO_CB)) if chat_id else None
        if new_msg_id:
            update_user_state(client_id, {"kb_message_id": new_msg_id})
        else:
            salebot_send(client_id, Q3_TEXT,
                         buttons=make_single_buttons(Q3_OPTIONS, Q3_KEY_TO_CB))
        return

    key = Q2_CB_TO_KEY.get(text)
    if not key:
        return  # ignore unknown input silently (may be echo)
    selected = set(state.get("sel2", []))
    if key in selected:
        selected.discard(key)
    else:
        selected.add(key)
    log_event(client_id, "q2_toggle", key)
    update_user_state(client_id, {"sel2": list(selected)})
    # Update keyboard to show ✅ on selected items
    if chat_id and kb_msg_id:
        tg_edit_reply_markup(chat_id, kb_msg_id, build_q2_tg_keyboard(selected))


def handle_q3_input(client_id, text, state, cb_ctx=None):
    """Handle Q3 single-select."""
    key = Q3_CB_TO_KEY.get(text)
    if not key:
        return  # ignore unknown input silently (may be echo)

    chat_id = state.get("telegram_user_id")
    kb_msg_id = state.get("kb_message_id")

    log_event(client_id, "q3_period", key)
    # Replace keyboard with only the selected item (✅)
    if chat_id and kb_msg_id:
        tg_edit_reply_markup(chat_id, kb_msg_id,
                             build_final_selection_keyboard(
                                 Q3_OPTIONS, Q3_KEY_TO_CB, {key}))

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

    # Send Q4 via TG API to get message_id for keyboard editing
    new_msg_id = tg_send_message(chat_id, Q4_TEXT,
                                 build_single_tg_keyboard(Q4_OPTIONS, Q4_KEY_TO_CB)) if chat_id else None
    if new_msg_id:
        update_user_state(client_id, {"kb_message_id": new_msg_id})
    else:
        salebot_send(client_id, Q4_TEXT,
                     buttons=make_single_buttons(Q4_OPTIONS, Q4_KEY_TO_CB))


def handle_q4_input(client_id, text, state, cb_ctx=None):
    """Handle Q4 delivery choice, create roadmap."""
    key = Q4_CB_TO_KEY.get(text)
    if not key:
        return  # ignore unknown input silently (may be echo)

    chat_id = state.get("telegram_user_id")
    kb_msg_id = state.get("kb_message_id")

    log_event(client_id, "q4_delivery", key)
    # Replace keyboard with only the selected item (✅)
    if chat_id and kb_msg_id:
        tg_edit_reply_markup(chat_id, kb_msg_id,
                             build_final_selection_keyboard(
                                 Q4_OPTIONS, Q4_KEY_TO_CB, {key}))

    a2 = state.get("a2", [])
    a3 = state.get("a3", "14")
    period_days = int(a3)
    is_redo = state.get("is_redo", False)

    # Send network/community info after Q4 answer, before roadmap materials
    _sent_network = False
    if "community" in a2 and not is_redo:
        salebot_send(client_id, NETWORK_MSG)
        _sent_network = True

    if is_redo:
        # ── Redo flow: append new posts to existing roadmap ──
        old_entry = load_user_roadmap(client_id)
        old_posts = old_entry.get("posts", []) if isinstance(old_entry, dict) else []
        old_sent_index = old_entry.get("sent_index", 0) if isinstance(old_entry, dict) else 0
        already_sent_ids = set(old_posts)

        unique_ids, per_pain_data = collect_roadmap_posts(a2, exclude_ids=already_sent_ids)

        if not unique_ids:
            # All posts for these topics were already sent
            salebot_send(client_id,
                         "По выбранным темам все материалы уже были отправлены! "
                         "Попробуйте выбрать другие темы.")
            # Re-send Q2 keyboard
            set_user_state(client_id, {
                "state": STATE_Q2,
                "a1": state.get("a1", []),
                "sel2": [],
                "is_redo": True,
                "telegram_user_id": str(chat_id) if chat_id else None,
            })
            new_msg_id = tg_send_message(chat_id, Q2_TEXT,
                                         build_q2_tg_keyboard(set())) if chat_id else None
            if new_msg_id:
                update_user_state(client_id, {"kb_message_id": new_msg_id})
            else:
                salebot_send(client_id, Q2_TEXT,
                             buttons=make_multi_buttons(Q2_OPTIONS, Q2_KEY_TO_CB))
            return

        # Merge: old posts + new posts
        merged_posts = old_posts + unique_ids
        merged_url = build_url(merged_posts)
        merged_pain_keys = list(dict.fromkeys(
            (old_entry.get("pain_keys", []) if isinstance(old_entry, dict) else []) + a2))

        if key == "send_all":
            # Send new per-pain URL buttons
            for q2_key, lbl, posts in per_pain_data:
                pain_ids = [p["id"] for p in posts if p["id"] not in already_sent_ids]
                if pain_ids:
                    url = build_url(pain_ids)
                    msg = f"{lbl} — {len(pain_ids)} новых материалов подобрано для вас"
                    salebot_send(client_id, msg,
                                 buttons=make_webapp_button("📚 Открыть дорожную карту", url))

            update_user_roadmap(client_id, {
                "url": merged_url,
                "posts": merged_posts,
                "sent_index": len(merged_posts),
                "pain_keys": merged_pain_keys,
                "start_date": date.today().isoformat(),
                "last_delivery": date.today().isoformat(),
                "delivery_count": 0,
                "paused": False,
                "redo_offered": False,
            })
            update_user_state(client_id, {"state": STATE_CHAT})
            threading.Timer(5, _send_after_roadmap, args=(client_id,)).start()
            return

        # drip or drip_remind — new posts go after old ones
        # sent_index stays at old position; new posts will be sent in next batches
        update_user_roadmap(client_id, {
            "url": merged_url,
            "posts": merged_posts,
            "pain_keys": merged_pain_keys,
            "reminders": key == "drip_remind",
            "paused": False,
            "start_date": date.today().isoformat(),
            "last_delivery": date.today().isoformat(),
            "delivery_count": 0,
            "redo_offered": False,
        })

        salebot_send(client_id,
                     f"Добавлено {len(unique_ids)} новых материалов в вашу дорожную карту! "
                     "Они будут приходить в следующих рассылках.",
                     buttons=make_webapp_button("📚 Открыть полную дорожную карту", merged_url))
        update_user_state(client_id, {"state": STATE_CHAT})
        threading.Timer(5, _send_after_roadmap, args=(client_id,)).start()
        return

    # ── Normal (first-time) flow ──
    unique_ids, per_pain_data = collect_roadmap_posts(a2)
    if not unique_ids:
        salebot_send(client_id,
                     "Не удалось подобрать материалы. Попробуйте ещё раз — "
                     "напишите «Онбординг и Библиотека».")
        return

    combined_url = build_url(unique_ids)

    # Wait for NETWORK_MSG to be delivered before sending roadmap cards
    if _sent_network:
        time.sleep(3)

    if key == "send_all":
        # Send per-pain URL buttons (URL in text as fallback)
        for q2_key, lbl, posts in per_pain_data:
            url = build_url([p["id"] for p in posts])
            msg = f"{lbl} — {len(posts)} материалов подобрано для вас"
            salebot_send(client_id, msg,
                         buttons=make_webapp_button("📚 Открыть дорожную карту", url))

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
            "redo_offered": False,
        })

        update_user_state(client_id, {"state": STATE_CHAT})
        threading.Timer(5, _send_after_roadmap, args=(client_id,)).start()
        return

    # drip or drip_remind
    first_batch = unique_ids[:BATCH_SIZE]
    url = build_url(first_batch)
    msg = f"День 1 из {period_days}. Вот первые материалы для вас:"
    buttons = [
        {"type": "inline", "text": "Открыть материалы",
         "web_app": url, "line": 0, "index_in_line": 0},
        {"type": "inline", "text": "📚 Вся дорожная карта",
         "web_app": combined_url, "line": 1, "index_in_line": 0},
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
        "redo_offered": False,
    })

    update_user_state(client_id, {"state": STATE_CHAT})
    threading.Timer(5, _send_after_roadmap, args=(client_id,)).start()


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
                     f"Подобрала {len(post_ids)} материалов по вашему запросу:",
                     buttons=make_webapp_button("Открыть подборку", url))
    except Exception as e:
        log.error("Claude API error: %s", e)
        salebot_send(client_id,
                     "Произошла ошибка. Попробуйте ещё раз.")
        return

    threading.Timer(5, _send_after_roadmap, args=(client_id,)).start()


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
                 f"Ещё {len(bonus_ids)} материалов по теме «{lbl}»:",
                 buttons=make_webapp_button("Открыть материалы", url))

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
        # No roadmap — only respond if user has state (started onboarding)
        state = get_user_state(client_id)
        if state:
            salebot_send(client_id,
                         "Вы ещё не создали дорожную карту. "
                         "Нажмите «Онбординг и Библиотека» чтобы начать.")
        return
    if isinstance(entry, str):
        url = entry
    else:
        url = entry.get("url", "")
    if not url:
        return
        return
    salebot_send(client_id,
                 "Ваша персональная дорожная карта:",
                 buttons=make_webapp_button("📚 Открыть дорожную карту", url))


def handle_pause(client_id):
    entry = load_user_roadmap(client_id)
    if not isinstance(entry, dict):
        return  # silently ignore — no roadmap
    update_user_roadmap(client_id, {"paused": True})
    salebot_send(client_id, "Рассылка поставлена на паузу")


def handle_resume(client_id):
    entry = load_user_roadmap(client_id)
    if not isinstance(entry, dict):
        return  # silently ignore — no roadmap
    update_user_roadmap(client_id, {"paused": False})
    salebot_send(client_id, "Рассылка возобновлена")


def handle_new_selection(client_id):
    """Start a new topic selection (redo Q2→Q3→Q4) after drip completion."""
    log_event(client_id, "new_selection")
    state = get_user_state(client_id)
    # Get a1 from previous onboarding (or empty)
    a1 = (state or {}).get("a1", [])
    chat_id = (state or {}).get("telegram_user_id")

    set_user_state(client_id, {
        "state": STATE_Q2,
        "a1": a1,
        "sel2": [],
        "is_redo": True,
        "telegram_user_id": str(chat_id) if chat_id else None,
    })

    # Send Q2 keyboard via TG API
    new_msg_id = tg_send_message(chat_id, Q2_TEXT,
                                 build_q2_tg_keyboard(set())) if chat_id else None
    if new_msg_id:
        update_user_state(client_id, {"kb_message_id": new_msg_id})
    else:
        salebot_send(client_id, Q2_TEXT,
                     buttons=make_multi_buttons(Q2_OPTIONS, Q2_KEY_TO_CB))


# ══════════════════════════════════════════════════════════
# Router
# ══════════════════════════════════════════════════════════

def process_message(client_id, text, telegram_user_id=None, cb_ctx=None,
                     variables=None, client_name=None):
    """Main message router. Called from webhook.

    cb_ctx: optional dict with callback_query_id, chat_id, message_id
            (present when user pressed an inline button).
    variables: dict of Salebot client variables from webhook payload.
    client_name: display name from Salebot webhook (saved for analytics).
    """
    text = text.strip()
    if not text:
        return

    # Answer callback query in background (removes spinner, fire-and-forget)
    if cb_ctx and cb_ctx.get("callback_query_id"):
        threading.Thread(target=tg_answer_callback,
                         args=(cb_ctx["callback_query_id"],),
                         daemon=True).start()

    # Save telegram_user_id and client_name for reference
    if telegram_user_id or client_name:
        st = get_user_state(client_id)
        if st:
            updates = {}
            if telegram_user_id and st.get("telegram_user_id") != str(telegram_user_id):
                updates["telegram_user_id"] = str(telegram_user_id)
            if client_name and st.get("client_name") != client_name:
                updates["client_name"] = client_name
            if updates:
                update_user_state(client_id, updates)

    # ── Global commands (work in any state) ──
    text_lower = text.lower().strip()

    # Salebot club commands and buttons — handled by Salebot's own scenarios, ignore
    if text_lower in ("/start", "/cases", "/request_to_join", "/cancel_subscription",
                       "/about_the_author", "/status", "/offer", "/help"):
        return
    if text_lower in ("еще кейсы", "оплатить подписку", "вступить в клуб",
                       "об авторе", "проверить статус оплаты",
                       "ноль", "купить £0"):
        return

    # Salebot system/event messages (payment/social/channel) leak through the
    # global webhook as is_input=1 — ignore so they're never treated as chat.
    if _is_system_message(text_lower):
        log.info("Ignoring Salebot system message %r from client %s", text[:40], client_id)
        return

    if text_lower == "start_onboarding":
        # Callback from Salebot after inlist(1992711) == True check
        handle_start(client_id, telegram_user_id)
        return

    if text_lower == "онбординг и библиотека":
        # Ignore direct text — only triggers from Salebot's
        # inlist() check are allowed to start onboarding
        log.info("Ignoring raw 'Онбординг и Библиотека' from client %s", client_id)
        return

    if text_lower == "есть подписка":
        # Salebot's inlist(1992711) == True block sends this message
        log.info("Payment confirmed for client %s, starting onboarding", client_id)
        handle_start(client_id, telegram_user_id)
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

    # ── Skip onboarding → open library ──
    if text == "skip_onb":
        log_event(client_id, "skip_onboarding")
        # Remove Q1 keyboard from old message
        cur_state = get_user_state(client_id)
        _chat = (cur_state or {}).get("telegram_user_id") or telegram_user_id
        _msg = (cur_state or {}).get("kb_message_id")
        if _chat and _msg:
            tg_edit_reply_markup(_chat, _msg)
        set_user_state(client_id, {
            "state": "skipped",
            "telegram_user_id": str(_chat) if _chat else None,
        })
        salebot_send(client_id,
                     "Вот ваша Библиотека! Когда захотите получить "
                     "персональную подборку — нажмите «Онбординг и Библиотека» ещё раз.",
                     buttons=make_webapp_button("Открыть Библиотеку", BASE_URL))
        return

    # ── Start onboarding (from skipped state) ──
    if text == "start_onb":
        log_event(client_id, "start_from_skipped")
        # Remove old keyboard (library + start onboarding buttons)
        cur_state = get_user_state(client_id)
        _chat = (cur_state or {}).get("telegram_user_id") or telegram_user_id
        _msg = (cur_state or {}).get("kb_message_id")
        if _chat and _msg:
            tg_edit_reply_markup(_chat, _msg)
        clear_user_state(client_id)
        set_user_state(client_id, {
            "state": STATE_Q1,
            "sel1": [],
            "sel2": [],
        })
        # Send Q1 via TG API for keyboard editing
        greeting = ("Отлично! Давайте познакомимся — я подберу вам "
                    "самые полезные материалы.\n\n" + Q1_TEXT)
        msg_id = tg_send_message(_chat, greeting, build_q1_tg_keyboard(set())) if _chat else None
        if msg_id:
            update_user_state(client_id, {
                "kb_message_id": msg_id,
                "telegram_user_id": str(_chat),
            })
        else:
            salebot_send(client_id,
                         "Отлично! Давайте познакомимся — я подберу вам "
                         "самые полезные материалы.")
            q1_buttons = make_multi_buttons(Q1_OPTIONS, Q1_KEY_TO_CB)
            salebot_send(client_id, Q1_TEXT, buttons=q1_buttons)
        return

    # ── Drip feedback buttons ──
    if text == "drip_ok":
        handle_drip_ok(client_id)
        return

    if text == "drip_fix":
        handle_drip_correct(client_id)
        return

    # ── New topic selection (redo after drip completion) ──
    if text == "new_selection":
        handle_new_selection(client_id)
        return

    # ── Pick materials (free-text chat via button) ──
    if text == "pick_materials":
        update_user_state(client_id, {"state": STATE_AWAITING_CHAT})
        salebot_send(client_id, "Напишите, о чём хотите почитать — я подберу материалы.")
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
        return  # Ignore silently — chat only via button

    current_state = state.get("state", "")

    if current_state == STATE_Q1:
        handle_q1_input(client_id, text, state, cb_ctx)
    elif current_state == STATE_Q2:
        handle_q2_input(client_id, text, state, cb_ctx)
    elif current_state == STATE_Q3:
        handle_q3_input(client_id, text, state, cb_ctx)
    elif current_state == STATE_Q4:
        handle_q4_input(client_id, text, state, cb_ctx)
    elif current_state == STATE_AWAITING_CORRECTION:
        handle_correction_text(client_id, text)
    elif current_state == STATE_AWAITING_CHAT:
        handle_chat(client_id, text, state)
        update_user_state(client_id, {"state": STATE_CHAT})
    elif current_state == STATE_CHAT:
        pass  # Ignore free text — chat only via "pick_materials" button
    else:
        # Unknown state value — log and ignore silently
        log.warning("Unknown state %r for client %s, ignoring", current_state, client_id)


# ══════════════════════════════════════════════════════════
# Drip delivery job (APScheduler)
# ══════════════════════════════════════════════════════════

def _send_redo_prompt(client_id, combined_url):
    """Send completion message with 3 buttons: new topic, library, full roadmap."""
    buttons = [
        {"type": "inline", "text": "Выбрать новую тему",
         "callback": "new_selection",
         "line": 0, "index_in_line": 0},
        {"type": "inline", "text": "📚 Открыть Библиотеку",
         "web_app": BASE_URL,
         "line": 1, "index_in_line": 0},
        {"type": "inline", "text": "📚 Открыть полную дорожную карту",
         "web_app": combined_url,
         "line": 2, "index_in_line": 0},
    ]
    salebot_send(client_id,
                 "Все материалы из вашей дорожной карты отправлены! "
                 "Хотите получить подборку по другой теме?",
                 buttons=buttons)


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
            # send_all users: offer redo after period expires
            if entry.get("redo_offered"):
                continue
            start_date_str = entry.get("start_date")
            if not start_date_str:
                continue
            period_days_val = entry.get("period_days", 14)
            if (today - date.fromisoformat(start_date_str)).days < period_days_val:
                continue
            # Period expired → send redo prompt
            client_id = entry.get("client_id", client_id_str)
            combined_url = entry.get("url", build_url(posts))
            try:
                _send_redo_prompt(client_id, combined_url)
            except Exception as e:
                log.warning("Redo prompt failed for send_all client %s: %s",
                            client_id_str, e)
                continue
            update_user_roadmap(client_id_str, {"redo_offered": True})
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
        msg = f"День {current_day} из {period_days}. Новые материалы для вас:"
        buttons = [
            {"type": "inline", "text": "Открыть материалы",
             "web_app": url, "line": 0, "index_in_line": 0},
            {"type": "inline", "text": "📚 Вся дорожная карта",
             "web_app": full_url, "line": 1, "index_in_line": 0},
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

        # All posts sent: completion message with redo option
        if new_sent_index >= len(posts):
            combined_url = entry.get("url", build_url(posts))
            try:
                _send_redo_prompt(client_id, combined_url)
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

    # Extract client_id and message text from Salebot webhook payload.
    client_id = data.get("client_id")
    client_data = data.get("client", {})
    if not client_id and client_data:
        client_id = client_data.get("id")
    message_text = data.get("message", "")
    telegram_user_id = client_data.get("recipient") or client_data.get("recepient")
    webhook_id = data.get("id")
    is_input = data.get("is_input")

    # Extract callback context for Telegram Bot API direct calls
    variables = client_data.get("variables", {}) if client_data else {}
    callback_query_id = (variables.get("callback_query_id")
                         or data.get("callback_query_id"))
    callback_message_id = (variables.get("callback_message_id")
                           or variables.get("tg_callback_message_id")
                           or data.get("callback_message_id"))

    client_name = (client_data.get("name") or client_data.get("first_name") or "") if client_data else ""

    log.info("Webhook id=%s is_input=%s client=%s msg=%r cb_qid=%s cb_mid=%s name=%r",
             webhook_id, is_input, client_id, (message_text or "")[:120],
             str(callback_query_id)[:12] if callback_query_id else None,
             callback_message_id, client_name)

    # Log all variables for debugging (helps identify available fields)
    if variables:
        log.info("Salebot variables keys: %s", list(variables.keys())[:30])

    # Allow payment-confirmed callbacks from Salebot's inlist() check (arrive as is_input=0)
    msg_stripped = (message_text or "").strip().lower()
    if msg_stripped in ("start_onboarding", "есть подписка") and client_id:
        log.info("Payment confirmed (msg=%r) for client %s", msg_stripped, client_id)
        thread = threading.Thread(
            target=process_message,
            args=(client_id, "start_onboarding", telegram_user_id, None, variables, client_name),
        )
        thread.start()
        return jsonify({"status": "ok"}), 200

    # Skip bot's own outgoing messages
    if is_input == 0 or is_input == "0":
        return jsonify({"status": "ok", "note": "outgoing"}), 200

    # Dedup: skip already-seen webhook IDs (Salebot may echo)
    if webhook_id and _is_duplicate(webhook_id):
        return jsonify({"status": "ok", "note": "dup"}), 200

    if not client_id or not message_text:
        return jsonify({"status": "ok", "note": "no client_id or message"}), 200

    # Build callback context if this is a callback button press
    cb_ctx = None
    if callback_query_id:
        cb_ctx = {
            "callback_query_id": callback_query_id,
            "chat_id": telegram_user_id,
            "message_id": callback_message_id,
        }

    # Process in a thread to not block the webhook response
    thread = threading.Thread(
        target=process_message,
        args=(client_id, message_text, telegram_user_id, cb_ctx, variables, client_name),
    )
    thread.start()

    return jsonify({"status": "ok"}), 200


@app.route("/start_onboarding", methods=["POST"])
def start_onboarding_endpoint():
    """Callback from Salebot after inlist() payment check passes."""
    data = request.get_json(silent=True) or {}
    client_id = str(data.get("client_id", ""))
    if not client_id:
        log.warning("start_onboarding: no client_id")
        return jsonify({"error": "no client_id"}), 400
    log.info("start_onboarding received for client %s", client_id)
    # Look up telegram_user_id from existing state
    state = get_user_state(client_id)
    telegram_user_id = (state or {}).get("telegram_user_id")
    thread = threading.Thread(
        target=process_message,
        args=(client_id, "start_onboarding", telegram_user_id),
    )
    thread.start()
    return jsonify({"status": "ok"}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy", "posts": len(POSTS)}), 200


# ══════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════

# ── APScheduler: start lazily on first request (safe with gunicorn fork) ──
_moscow_tz = pytz.timezone("Europe/Moscow")
_scheduler = None
_scheduler_lock = threading.Lock()


def _ensure_scheduler():
    global _scheduler
    if _scheduler is not None:
        return
    with _scheduler_lock:
        if _scheduler is not None:
            return
        _scheduler = BackgroundScheduler()
        _scheduler.add_job(
            drip_delivery_job,
            trigger=CronTrigger(hour=10, minute=0, timezone=_moscow_tz),
            id="drip_delivery",
            replace_existing=True,
        )
        _scheduler.start()
        log.info("Scheduler started: drip delivery daily at 10:00 MSK")


@app.before_request
def _start_scheduler_once():
    _ensure_scheduler()


if __name__ == "__main__":
    _ensure_scheduler()
    port = int(os.environ.get("PORT", 8080))
    log.info("Starting Flask dev server on port %d", port)
    app.run(host="0.0.0.0", port=port)
