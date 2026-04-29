"""
Ежемесячный отчёт участников Секреты.
Один запуск → новая вкладка в Google Sheet с полным сводом + аналитикой.

Запуск:
  python3 generate_monthly_report.py
  python3 generate_monthly_report.py --season-start 2026-04-23 --month "Май 2026"
  python3 generate_monthly_report.py --dry-run
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

import gspread
import requests
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from telethon import TelegramClient
from telethon.errors import SessionExpiredError, SessionRevokedError
from telethon.tl.types import ChannelParticipantAdmin, ChannelParticipantCreator

# ─── Config ───────────────────────────────────────────────────────

load_dotenv(Path(__file__).parent / ".env")

PROJECT_DIR = Path(__file__).parent
API_KEY = os.environ["SALEBOT_API_KEY"]
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "174PEZUj-vxTV-vfoLIRL4FLOXSUlJ0qCfIItqyaNuH0")
GOOGLE_SA_KEY_PATH = os.environ.get("GOOGLE_SA_KEY_PATH", str(Path.home() / "Desktop/secrets-club.json"))

TELEGRAM_API_ID = 39940596
TELEGRAM_API_HASH = "6653479906b6710fec6535892e519d58"
SESSION_PATH = os.path.expanduser("~/session")

TG_GROUPS = {
    "channel": -1003642141867,
    "chat":    -1003403267547,
    "prak":    -1002195167889,
}

SALEBOT_LIST_ID = 1992711
STRIPE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
ANTIKH_TAB_NAME = "Антихрупкость 2026"

# ─── CLI args ─────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Ежемесячный отчёт Секреты")
    p.add_argument("--season-start", help="Дата начала платёжного окна YYYY-MM-DD")
    p.add_argument("--season-end", help="Дата конца платёжного окна YYYY-MM-DD (default: start+3)")
    p.add_argument("--month", help='Название месяца, напр. "Май 2026"')
    p.add_argument("--stripe-tab", help="Название вкладки Stripe в Google Sheet")
    p.add_argument("--prev-tab", help="Название вкладки предыдущего свода")
    p.add_argument("--dry-run", action="store_true", help="Не писать в Google Sheet, вывести превью")
    return p.parse_args()


def auto_season_start():
    """23-е предыдущего месяца."""
    today = date.today()
    first_of_this = today.replace(day=1)
    last_month = first_of_this - timedelta(days=1)
    return last_month.replace(day=23).strftime("%Y-%m-%d")


def auto_month_name(season_start_str):
    """Название следующего месяца после season_start."""
    d = datetime.strptime(season_start_str, "%Y-%m-%d").date()
    next_month = (d.replace(day=1) + timedelta(days=32)).replace(day=1)
    months_ru = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
                 "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
    return f"{months_ru[next_month.month - 1]} {next_month.year}"


# ─── Google Sheets client ─────────────────────────────────────────

def get_gspread_client():
    creds = Credentials.from_service_account_file(GOOGLE_SA_KEY_PATH, scopes=STRIPE_SCOPES)
    return gspread.authorize(creds)


# ─── Step 1: Stripe from Google Sheet ─────────────────────────────

def load_stripe_from_sheet(gc, spreadsheet_id, tab_name, season_start, season_end):
    try:
        sh = gc.open_by_key(spreadsheet_id)
    except Exception as e:
        tabs = []
        try:
            tabs = [w.title for w in gc.open_by_key(spreadsheet_id).worksheets()]
        except Exception:
            pass
        print(f"  ❌ Ошибка открытия таблицы: {e}")
        if tabs:
            print(f"  Доступные вкладки: {tabs}")
        sys.exit(1)

    try:
        ws = sh.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        tabs = [w.title for w in sh.worksheets()]
        print(f"  ❌ Вкладка '{tab_name}' не найдена.")
        print(f"  Доступные вкладки: {tabs}")
        sys.exit(1)

    records = ws.get_all_records()
    if not records:
        print(f"  ⚠️  Вкладка '{tab_name}' пустая или нет заголовков")
        return []

    payments = []
    date_col = next((k for k in records[0] if "created" in k.lower() and "date" in k.lower()), None)
    email_col = next((k for k in records[0] if "email" in k.lower()), None)
    amount_col = next((k for k in records[0] if "amount" in k.lower()), None)
    status_col = next((k for k in records[0] if "status" in k.lower()), None)
    id_col = next((k for k in records[0] if k.lower() in ("id", "charge id", "payment intent")), None)

    if not all([date_col, email_col]):
        print(f"  ❌ Не найдены нужные колонки. Есть: {list(records[0].keys())}")
        sys.exit(1)

    skipped = 0
    for r in records:
        raw_date = str(r.get(date_col, "")).strip()
        if not raw_date:
            continue
        try:
            # Format: "2026-04-23 12:34:56" or "2026-04-23"
            pay_date = raw_date[:10]
            if not (season_start <= pay_date <= season_end):
                skipped += 1
                continue
        except Exception:
            continue

        email = str(r.get(email_col, "")).strip().lower()
        if not email:
            continue

        amount_raw = str(r.get(amount_col, "0")).strip().replace(",", ".")
        try:
            amount = float(amount_raw)
        except ValueError:
            amount = 0.0

        payments.append({
            "email": email,
            "amount": amount,
            "status": str(r.get(status_col, "")).strip() if status_col else "",
            "date": pay_date,
            "charge_id": str(r.get(id_col, "")).strip() if id_col else "",
        })

    print(f"  Stripe: {len(payments)} транзакций за {season_start}–{season_end} (пропущено {skipped})")
    if not payments:
        print(f"  ⚠️  0 транзакций — проверь STRIPE_TAB_NAME и даты")
    return payments


# ─── Step 2: Telegram members ─────────────────────────────────────

async def _load_telegram_members():
    client = TelegramClient(SESSION_PATH, TELEGRAM_API_ID, TELEGRAM_API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        print("  ❌ Telegram сессия не авторизована. Запусти: python3 reauth_telegram.py")
        await client.disconnect()
        sys.exit(1)

    results = {}
    for group_key, group_id in TG_GROUPS.items():
        try:
            entity = await client.get_entity(group_id)
            members = {}
            async for user in client.iter_participants(entity):
                if user.bot:
                    continue
                if isinstance(getattr(user, "participant", None),
                              (ChannelParticipantAdmin, ChannelParticipantCreator)):
                    pass  # keep admins — they are real members
                name = f"{user.first_name or ''} {user.last_name or ''}".strip()
                members[user.id] = {
                    "name": name,
                    "username": (user.username or "").lower(),
                }
            results[group_key] = members
            print(f"  {group_key}: {len(members)} участников")
        except (SessionExpiredError, SessionRevokedError):
            print("  ❌ Telegram сессия истекла. Запусти: python3 reauth_telegram.py")
            await client.disconnect()
            sys.exit(1)
        except Exception as e:
            print(f"  ⚠️  Ошибка {group_key}: {e}")
            results[group_key] = {}

    await client.disconnect()
    return results


def load_telegram_members():
    return asyncio.run(_load_telegram_members())


# ─── Step 3: Salebot ──────────────────────────────────────────────

def load_salebot_list():
    url = f"https://chatter.salebot.pro/api/{API_KEY}/get_clients"
    all_clients = []
    offset = 0
    while True:
        time.sleep(0.2)
        try:
            resp = requests.get(url, params={"list": SALEBOT_LIST_ID, "limit": 500, "offset": offset}, timeout=15)
        except Exception as e:
            print(f"  ⚠️  Salebot list error: {e}")
            break
        if resp.status_code != 200:
            break
        clients = resp.json().get("clients", [])
        if not clients:
            break
        all_clients.extend(clients)
        if len(clients) < 500:
            break
        offset += 500
    return all_clients


def search_all_salebot_clients(target_ids):
    url = f"https://chatter.salebot.pro/api/{API_KEY}/get_clients"
    found = {}
    offset = 0
    retries = 0
    target_ids = set(target_ids)
    while True:
        time.sleep(0.4)
        try:
            resp = requests.get(url, params={"limit": 500, "offset": offset}, timeout=15)
        except Exception:
            retries += 1
            if retries > 3:
                break
            time.sleep(2)
            continue
        if resp.status_code != 200:
            retries += 1
            if retries > 3:
                break
            time.sleep(2)
            continue
        retries = 0
        clients = resp.json().get("clients", [])
        if not clients:
            break
        for c in clients:
            try:
                pid = int(c["platform_id"])
            except (ValueError, TypeError):
                continue
            if pid in target_ids:
                found[pid] = c
        if len(found) == len(target_ids):
            break
        if len(clients) < 500:
            break
        offset += 500
        if offset % 2000 == 0:
            print(f"    Salebot scan: offset {offset}, найдено {len(found)}/{len(target_ids)}...")
    return found


def _get_variables_single(args):
    sb_id, tg_uid, api_key = args
    url = f"https://chatter.salebot.pro/api/{api_key}/get_variables"
    for attempt in range(3):
        try:
            resp = requests.get(url, params={"client_id": sb_id}, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and "status" not in data:
                    return tg_uid, data
            if resp.status_code == 429:
                time.sleep(1 + attempt)
                continue
            return tg_uid, {}
        except Exception:
            time.sleep(0.5 * (attempt + 1))
    return tg_uid, {}


def get_variables_parallel(sb_id_map, max_workers=5):
    """sb_id_map: {tg_uid: sb_client_id}. Returns {tg_uid: variables_dict}."""
    args_list = [(sb_id, tg_uid, API_KEY) for tg_uid, sb_id in sb_id_map.items()]
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_get_variables_single, a): a for a in args_list}
        done = 0
        for f in as_completed(futures):
            tg_uid, v = f.result()
            results[tg_uid] = v
            done += 1
            if done % 20 == 0:
                print(f"    get_variables: {done}/{len(args_list)}...")
    return results


# ─── Step 4: Diff with previous month ────────────────────────────

def load_prev_tab_members(gc, spreadsheet_id, prev_tab_name):
    if not prev_tab_name:
        return set(), set()
    try:
        sh = gc.open_by_key(spreadsheet_id)
        ws = sh.worksheet(prev_tab_name)
        all_values = ws.get_all_values()
    except Exception as e:
        print(f"  ⚠️  Не удалось загрузить предыдущий свод '{prev_tab_name}': {e}")
        return set(), set()

    if not all_values:
        return set(), set()

    # Scan all cells: usernames always start with "@" and are at least 3 chars.
    # This handles multi-section formatted sheets where headers repeat per section.
    prev_usernames = set()
    prev_tg_ids = set()
    for row in all_values:
        for v in row:
            v = str(v).strip()
            if v.startswith("@") and len(v) >= 3 and v.lower() != "@username":
                prev_usernames.add(v.lstrip("@").lower())
            if v.lstrip("-").isdigit() and len(v) >= 8:
                try:
                    prev_tg_ids.add(int(v))
                except (ValueError, TypeError):
                    pass
    print(f"  Предыдущий свод: {len(prev_usernames)} username, {len(prev_tg_ids)} tg_id")
    return prev_usernames, prev_tg_ids


def compute_diff(current_uid_set, prev_usernames, prev_tg_ids, members_by_uid):
    new_uids = set()
    left_ids = set()

    for uid in current_uid_set:
        info = members_by_uid.get(uid, {})
        uname = info.get("username", "")
        in_prev = uid in prev_tg_ids or (uname and uname in prev_usernames)
        if not in_prev:
            new_uids.add(uid)

    for uid in prev_tg_ids:
        if uid not in current_uid_set:
            left_ids.add(uid)

    return new_uids, left_ids


# ─── Step 5: Stripe email matching ───────────────────────────────

def load_known_matches():
    path = PROJECT_DIR / "known_matches.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return {k.lower(): v.lstrip("@").lower() for k, v in json.load(f).items()}
    return {}


def load_lookup_tabs(gc, spreadsheet_id, prev_tab_name=None):
    """Build email↔username index from Антихрупкость, Резиденты, and prev Свод."""
    email_to_uname = {}  # email_lower → username_lower (no @)
    uname_to_email = {}  # username_lower → email_lower

    sh = gc.open_by_key(spreadsheet_id)

    def _add(email, uname):
        e = str(email).strip().lower()
        u = str(uname).strip().lower().lstrip("@")
        if e and u and "@" in e and "." in e.split("@")[-1] and len(u) >= 3:
            email_to_uname.setdefault(e, u)
            uname_to_email.setdefault(u, e)

    # Антихрупкость 2026 — no header row: detect email/username by pattern
    try:
        ws = sh.worksheet(ANTIKH_TAB_NAME)
        for row in ws.get_all_values():
            email = next((c for c in row if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", c)), "")
            uname = next((c for c in row if c.startswith("@") and len(c) >= 4), "")
            if email and uname:
                _add(email, uname)
        print(f"  Антихрупкость: {len(email_to_uname)} пар email↔ник")
    except Exception as e:
        print(f"  ⚠️  Антихрупкость 2026: {e}")

    # Резиденты — has header. Only use: Имя, Почта, Телеграм ник columns.
    try:
        ws = sh.worksheet("Резиденты")
        records = ws.get_all_records()
        if records:
            sample = records[0]
            email_col = next((k for k in sample if "почт" in k.lower()), None)
            uname_col = next((k for k in sample if "телеграм" in k.lower() or "ник" in k.lower()), None)
            if email_col and uname_col:
                before = len(email_to_uname)
                for rec in records:
                    _add(rec.get(email_col, ""), rec.get(uname_col, ""))
                print(f"  Резиденты: +{len(email_to_uname) - before} новых пар (итого {len(email_to_uname)})")
            else:
                print(f"  ⚠️  Резиденты: не найдены колонки Почта/Ник. Есть: {list(sample.keys())}")
    except Exception as e:
        print(f"  ⚠️  Резиденты: {e}")

    # Предыдущий Свод — scan data rows for @username (col 1) + email (col 2)
    if prev_tab_name:
        try:
            ws = sh.worksheet(prev_tab_name)
            before = len(email_to_uname)
            for row in ws.get_all_values():
                if len(row) < 3:
                    continue
                # Schema: Имя(0), Username(1), Email(2) — matches COL_HEADERS order
                uname_cell = row[1].strip()
                email_cell = row[2].strip()
                if uname_cell.startswith("@") and "@" in email_cell and "⚠️" not in email_cell:
                    _add(email_cell, uname_cell)
            print(f"  Предыдущий свод: +{len(email_to_uname) - before} новых пар (итого {len(email_to_uname)})")
        except Exception as e:
            print(f"  ⚠️  Предыдущий свод lookup: {e}")

    return email_to_uname, uname_to_email


def load_tanya_agreements(report_date=None):
    """Load tanya_agreements.json.
    Returns dict: username_lower -> agreement_dict with added 'state' key:
      'active'   — valid_until in the future (or type=team with no expiry)
      'expiring' — valid_until within 7 days
      'expired'  — valid_until past, or free/annual without valid_until
    All agreements are returned regardless of state so they can be flagged in the report.
    """
    path = PROJECT_DIR / "tanya_agreements.json"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    today = report_date or date.today()
    result = {}
    for uname, ag in raw.items():
        if uname.startswith("_"):
            continue
        ag = dict(ag)  # copy so we don't mutate the loaded data
        ag_type = ag.get("type", "free")
        valid_until_str = ag.get("valid_until", "")

        if ag_type == "team":
            ag["state"] = "active"
        elif not valid_until_str:
            # free/annual without valid_until — requires explicit confirmation
            ag["state"] = "expired"
        else:
            valid_until = date.fromisoformat(valid_until_str)
            days_left = (valid_until - today).days
            if days_left < 0:
                ag["state"] = "expired"
            elif days_left <= 7:
                ag["state"] = "expiring"
            else:
                ag["state"] = "active"

        result[uname.lower()] = ag
    return result


# Common first names to skip when matching email tokens — too ambiguous
_COMMON_NAMES = {
    "anna", "yulia", "julia", "olga", "maria", "elena", "irina", "natalia",
    "natalya", "ekaterina", "anastasia", "tatiana", "marina", "svetlana",
    "yana", "daria", "darya", "alina", "arina", "lena", "kate", "katya",
    "masha", "sasha", "ksenia", "kseniya", "diana", "lana", "zhanna",
    "julia", "yulia", "nadia", "nadya", "alena", "alia", "sonya", "mila",
    "rita", "vera", "nina", "vika", "tanya", "anya", "lily", "lili",
    "stella", "angel", "admin", "info", "mail", "hello", "noreply",
}


def _email_tokens(local):
    """Parse meaningful tokens from email local part (before @).
    Strips leading digits, splits on separators, returns words ≥ 5 chars
    that are not common first names."""
    cleaned = re.sub(r"^\d+", "", local)       # strip leading digits
    parts = re.split(r"[._\-\+\d]+", cleaned)  # split on separators/digits
    return [p.lower() for p in parts
            if len(p) >= 5 and p.lower() not in _COMMON_NAMES]


def _name_tokens(text):
    """Split display name / username into words ≥ 4 chars."""
    parts = re.split(r"[\s\-_\|\.]+", text)
    return [p.lower() for p in parts if len(p) >= 4]


def match_stripe_to_members(stripe_payments, members_by_uid, known_matches):
    # Build indexes
    email_to_uid = {}        # salebot_email_lower -> uid
    username_to_uid = {}     # username_lower -> uid
    name_token_to_uids = {}  # token -> [uid, ...] — for strategy 5

    _JUNK_EMAILS = {"t@gmail.com", "123@mail.ru", ""}

    for uid, info in members_by_uid.items():
        em = info.get("email", "").strip().lower()
        if em and em not in _JUNK_EMAILS:
            email_to_uid[em] = uid
            # Index tokens from member's own Salebot email (strategy 5b)
            em_local = em.split("@")[0]
            for tok in _email_tokens(em_local):
                if len(tok) >= 6:
                    name_token_to_uids.setdefault(tok, []).append(uid)

        un = info.get("username", "").strip().lower().lstrip("@")
        if un:
            username_to_uid[un] = uid
            # Index username tokens (strategy 5)
            for tok in _name_tokens(un):
                if len(tok) >= 5:
                    name_token_to_uids.setdefault(tok, []).append(uid)

        # Index display name tokens (strategy 5)
        name = info.get("name", "")
        name_parts = _name_tokens(name)
        for tok in name_parts:
            if len(tok) >= 5:
                name_token_to_uids.setdefault(tok, []).append(uid)
        # Strategy 5c: also index concatenated multi-word names
        # e.g. "Natalya Pavlova" → "natalyapavlova" catches "2natalyapavlova@gmail.com"
        if len(name_parts) >= 2:
            concat = "".join(name_parts)
            if len(concat) >= 8:
                name_token_to_uids.setdefault(concat, []).append(uid)

    # Deduplicate: keep Paid over Failed
    unique_payments = {}
    for p in stripe_payments:
        e = p["email"]
        if e not in unique_payments or p["status"].lower() == "paid":
            unique_payments[e] = p

    matched = {}      # uid -> payment
    uncertain = {}    # uid -> (payment, reason)
    unmatched = []    # list of payments

    for stripe_email, payment in unique_payments.items():
        local = stripe_email.split("@")[0].lower()

        # 1. Direct email match
        if stripe_email in email_to_uid:
            uid = email_to_uid[stripe_email]
            if uid in matched:
                # prefer paid
                if payment["status"].lower() == "paid":
                    matched[uid] = payment
            else:
                matched[uid] = payment
            continue

        # 2. known_matches
        if stripe_email in known_matches:
            uname = known_matches[stripe_email]
            if uname in username_to_uid:
                uid = username_to_uid[uname]
                matched[uid] = payment
                continue

        # 3. Username exactly in local part of email (len >= 6)
        found_exact = False
        for uname, uid in username_to_uid.items():
            clean_local = local.replace(".", "").replace("_", "")
            clean_uname = uname.replace("_", "").replace(".", "")
            if len(clean_uname) >= 6 and clean_uname == clean_local:
                matched[uid] = payment
                found_exact = True
                break
        if found_exact:
            continue

        # 4. Fuzzy: surname substring (len >= 6)
        fuzzy_hit = None
        for uname, uid in username_to_uid.items():
            clean_uname = uname.replace("_", "").lower()
            if len(clean_uname) >= 6 and (clean_uname in local or local in clean_uname):
                fuzzy_hit = (uid, uname)
                break
        if fuzzy_hit:
            uid, uname = fuzzy_hit
            uncertain[uid] = (payment, f"нечёткий матч: @{uname} ↔ {stripe_email}")
            continue

        # 5. Token matching: extract words from email local part,
        #    match against name/username/email tokens of all members.
        #    Tokens ≥ 7 chars → high confidence (uncertain).
        #    Tokens 5-6 chars → low confidence (skip, too risky).
        best_uid = None
        best_tok = None
        best_len = 0
        for tok in _email_tokens(local):
            if len(tok) < 7:
                continue  # too short to be reliable
            if tok not in name_token_to_uids:
                continue
            candidate_uids = list(set(name_token_to_uids[tok]))
            # Exclude already matched UIDs
            candidate_uids = [u for u in candidate_uids
                              if u not in matched and u not in uncertain]
            if len(candidate_uids) == 1 and len(tok) > best_len:
                best_uid = candidate_uids[0]
                best_tok = tok
                best_len = len(tok)

        if best_uid is not None:
            uncertain[best_uid] = (
                payment,
                f"матч по токену '{best_tok}' из {stripe_email} ⚠️ требует проверки"
            )
            continue

        # 5b. Email token is suffix of clean username
        # Catches "elenakuskova" ← email token "kuskova", "krystynaisakova" ← "isakova"
        suffix_match = None
        for tok in _email_tokens(local):
            if len(tok) < 6:
                continue
            candidates = []
            for uname, uid in username_to_uid.items():
                if uid in matched or uid in uncertain:
                    continue
                clean_un = re.sub(r"[._\-]+", "", uname)
                if clean_un.endswith(tok) and len(clean_un) > len(tok) + 2:
                    candidates.append((uid, uname))
            if len(candidates) == 1:
                suffix_match = (candidates[0][0], tok, candidates[0][1])
                break  # take first (longest eligible) token

        if suffix_match is not None:
            uid, tok, uname = suffix_match
            uncertain[uid] = (payment, f"суффикс '@{uname}' ← '{tok}' из {stripe_email} ⚠️ требует проверки")
            continue

        # 5c. Full cleaned email local matches concatenated name
        # (handled via name_token_to_uids concat index — already in strategy 5 above)
        # Extra pass: also try with leading digits stripped
        clean_full = re.sub(r"^\d+", "", local)
        clean_full = re.sub(r"[._\-\+\d]+", "", clean_full)
        if len(clean_full) >= 8 and clean_full != local.replace(".", "").replace("_", ""):
            uid_list = list(set(name_token_to_uids.get(clean_full, [])))
            uid_list = [u for u in uid_list if u not in matched and u not in uncertain]
            if len(uid_list) == 1:
                uncertain[uid_list[0]] = (
                    payment,
                    f"матч по имени '{clean_full}' из {stripe_email} ⚠️ требует проверки"
                )
                continue

        unmatched.append(payment)

    return matched, uncertain, unmatched


# ─── Step 6: Build rows ───────────────────────────────────────────

def classify(in_list, in_ch, in_chat, in_prak, stripe_payment, season_start):
    has_access = in_ch or in_chat or in_prak
    full_access = in_ch and in_chat and in_prak

    stripe_paid = False
    stripe_failed = False
    if stripe_payment:
        s = stripe_payment["status"].lower()
        stripe_paid = (s == "paid")
        stripe_failed = (s == "failed")

    # Group 1: доступ без оплаты
    if has_access and not in_list and not stripe_paid:
        return 1, "Доступ без оплаты", "Кто добавил? Запросить оплату?"

    # Group 1 also: failed + access
    if stripe_failed and has_access:
        return 1, "FAILED платёж, доступ есть", "Проверить в Stripe — убрать доступ?"

    # Group 1: failed + no access
    if stripe_failed and not has_access:
        return 1, "FAILED платёж, нет доступа", "Связаться — помочь с оплатой?"

    # Group 2: в списке, не все группы
    if in_list and has_access and not full_access:
        missing = []
        if not in_chat:
            missing.append("Чат")
        if not in_prak:
            missing.append("Практика")
        return 2, "Не все группы", f"Добавить в: {', '.join(missing)}"

    # Group 3: не в списке Вовы, но доступ есть (и есть оплата или просто зашли)
    if not in_list and has_access:
        return 3, "Не в списке Вовы, доступ есть", "Добавить в список Вовы?"

    # Group 4: в списке Вовы, нигде нет доступа
    if in_list and not has_access:
        return 4, "В списке, нет доступа", "Добавить во все группы"

    # Group 5: всё ОК — в списке, все группы, оплата подтверждена
    if in_list and full_access and stripe_paid:
        return 5, "Всё ОК", ""

    # Group 6: в списке, все группы, но оплата не найдена в Stripe
    if in_list and full_access and not stripe_paid:
        return 6, "Нет данных Stripe", "Найти оплату или добавить в known_matches.json"

    return 1, "Нужно разобраться", "Проверить вручную"


def build_rows(tg_members, sb_list_by_tgid, sb_extra_by_tgid, vars_by_tgid,
               stripe_matched, stripe_uncertain, new_uids, left_uids, season_start,
               agreements=None):
    # Collect all unique UIDs
    all_uids = (set(tg_members["channel"].keys()) |
                set(tg_members["chat"].keys()) |
                set(tg_members["prak"].keys()) |
                set(sb_list_by_tgid.keys()))

    # Build per-person info dict
    members_info = {}
    for uid in all_uids:
        info = {}
        for grp in ["channel", "chat", "prak"]:
            if uid in tg_members[grp]:
                info = tg_members[grp][uid].copy()
                break
        if not info:
            c = sb_list_by_tgid.get(uid) or sb_extra_by_tgid.get(uid) or {}
            info = {"name": c.get("name", "?"), "username": ""}

        v = vars_by_tgid.get(uid, {})
        if not info.get("username") and v.get("tg_username"):
            info["username"] = v.get("tg_username", "").lstrip("@").lower()
        info["email"] = v.get("email") or v.get("mail", "")
        members_info[uid] = info

    rows = []
    for uid in all_uids:
        info = members_info[uid]
        in_ch = uid in tg_members["channel"]
        in_chat = uid in tg_members["chat"]
        in_prak = uid in tg_members["prak"]
        in_list = uid in sb_list_by_tgid

        # Stripe
        stripe_pay = stripe_matched.get(uid)
        uncertain_pay, uncertain_reason = stripe_uncertain.get(uid, (None, ""))
        if stripe_pay:
            amt = stripe_pay["amount"]
            status = stripe_pay["status"]
            stripe_display = f"{amt:.0f} GBP, {status}"
            email_best = stripe_pay["email"]
        elif uncertain_pay:
            amt = uncertain_pay["amount"]
            stripe_display = f"{amt:.0f} GBP, {uncertain_pay['status']}"
            email_best = f"⚠️ {uncertain_pay['email']}"
        else:
            stripe_display = "нет данных"
            email_best = info.get("email", "")

        if not email_best and info.get("email"):
            email_best = info["email"]

        # Tanya agreements
        uname = info.get("username", "")
        agreement = (agreements or {}).get(uname.lower()) if uname else None
        agreement_state = None  # 'active' | 'expiring' | 'expired' | 'stripe_also'
        if agreement:
            ag_state = agreement.get("state", "expired")
            if not email_best and agreement.get("email"):
                email_best = agreement["email"]
            real_stripe = stripe_pay or uncertain_pay

            if real_stripe and ag_state in ("active", "expiring"):
                # Stripe charged despite active agreement — show both, flag for review
                agreement_state = "stripe_also"
                real_pay = stripe_pay or uncertain_pay
                amt = real_pay["amount"]
                stripe_display = f"{amt:.0f} GBP, {real_pay['status']} (💛 договорённость)"
                # Keep real stripe_pay so classification is based on actual payment

            elif not real_stripe and ag_state == "active":
                stripe_display = f"💛 {agreement['label']}"
                agreement_state = "active"
                stripe_pay = {"status": "paid", "amount": 0, "email": email_best, "date": ""}

            elif not real_stripe and ag_state == "expiring":
                stripe_display = f"💛 {agreement['label']} (истекает {agreement.get('valid_until', '?')})"
                agreement_state = "expiring"
                stripe_pay = {"status": "paid", "amount": 0, "email": email_best, "date": ""}

            elif not real_stripe and ag_state == "expired":
                # Expired agreement + no Stripe → person classified normally (likely red)
                stripe_display = f"⚠️ Договорённость истекла ({agreement.get('valid_until', 'нет даты')})"
                agreement_state = "expired"
                # No synthetic payment — let classify() flag them as unpaid

        # Diff label
        if uid in new_uids:
            diff_label = "🆕 новый"
        elif uid in left_uids:
            diff_label = "👋 вышел"
        else:
            diff_label = ""

        group_num, status_label, action = classify(
            in_list, in_ch, in_chat, in_prak, stripe_pay or uncertain_pay, season_start
        )
        rows.append({
            "uid": uid,
            "name": info.get("name", "?"),
            "username": f"@{uname}" if uname else "",
            "email": email_best,
            "email_uncertain": bool(uncertain_pay),
            "in_list": in_list,
            "in_channel": in_ch,
            "in_chat": in_chat,
            "in_prak": in_prak,
            "stripe": stripe_display,
            "diff": diff_label,
            "group": group_num,
            "status": status_label,
            "action": action,
            "uncertain_reason": uncertain_reason,
            "agreement_state": agreement_state,
        })

    rows.sort(key=lambda r: (r["group"], r["name"].lower()))
    return rows, members_info


# ─── Step 7: Analytics ────────────────────────────────────────────

def build_analytics(rows, stripe_payments, new_uids, left_uids, uncertain_count, unmatched):
    total = len(rows)
    in_ch = sum(1 for r in rows if r["in_channel"])
    in_chat = sum(1 for r in rows if r["in_chat"])
    in_prak = sum(1 for r in rows if r["in_prak"])

    paid_payments = [p for p in stripe_payments if p["status"].lower() == "paid"]
    failed_payments = [p for p in stripe_payments if p["status"].lower() == "failed"]
    revenue = sum(p["amount"] for p in paid_payments)

    g35 = sum(1 for p in paid_payments if abs(p["amount"] - 35) < 1)
    g50 = sum(1 for p in paid_payments if abs(p["amount"] - 50) < 1)

    with_email = sum(1 for r in rows if r["email"] and "⚠️" not in r["email"])
    email_pct = int(with_email / total * 100) if total else 0

    group_counts = {}
    for r in rows:
        g = r["group"]
        group_counts[g] = group_counts.get(g, 0) + 1

    # Action items
    actions = []
    add_to_groups = [r for r in rows if r["group"] == 2]
    if add_to_groups:
        names = ", ".join(r["name"] for r in add_to_groups[:10])
        actions.append(f"Добавить в Чат/Практику: {names}")

    failed_with_access = [r for r in rows if r["group"] == 1 and "FAILED" in r["status"] and (r["in_channel"] or r["in_chat"] or r["in_prak"])]
    if failed_with_access:
        names = ", ".join(r["name"] for r in failed_with_access)
        actions.append(f"Проверить failed платежи с доступом: {names}")

    if uncertain_count:
        uncertain_rows = [r for r in rows if r["email_uncertain"]]
        for r in uncertain_rows:
            actions.append(f"Подтвердить почту: {r['username']} — возможно {r['email'].replace('⚠️ ', '')}")

    if unmatched:
        for p in unmatched:
            if p["status"].lower() == "paid":
                actions.append(f"Stripe Paid, не привязан: {p['email']} ({p['amount']:.0f} GBP, {p['date']})")

    new_no_list = [r for r in rows if r["diff"] == "🆕 новый" and not r["in_list"]]
    if new_no_list:
        names = ", ".join(r["name"] for r in new_no_list)
        actions.append(f"Новые участники — добавить в бота: {names}")

    # People in Vova's list with no Stripe data and no access — suspicious, ask Vova why they're listed
    in_list_no_stripe_no_access = [
        r for r in rows
        if r["in_list"] and r["group"] == 4 and r["stripe"] == "нет данных"
    ]
    if in_list_no_stripe_no_access:
        names = ", ".join(
            f"{r['name']} ({r['username']})" if r["username"] else r["name"]
            for r in in_list_no_stripe_no_access
        )
        actions.append(f"❗ Вова: в списке оплативших, но нет доступа и нет Stripe данных — проверить: {names}")

    # Agreement action items
    expiring = [r for r in rows if r.get("agreement_state") == "expiring"]
    if expiring:
        names = ", ".join(f"{r['name']} ({r['username']})" for r in expiring)
        actions.append(f"⏰ Договорённость истекает скоро — уточнить у Тани продлевать ли: {names}")

    expired = [r for r in rows if r.get("agreement_state") == "expired"]
    if expired:
        names = ", ".join(f"{r['name']} ({r['username']})" for r in expired)
        actions.append(f"❌ Договорённость истекла — уточнить у Тани: {names} (если продолжается — обновить valid_until в tanya_agreements.json)")

    stripe_also = [r for r in rows if r.get("agreement_state") == "stripe_also"]
    if stripe_also:
        names = ", ".join(f"{r['name']} ({r['username']}): {r['stripe']}" for r in stripe_also)
        actions.append(f"💰 Договорённость + реальная оплата Stripe — проверить у Тани: {names}")

    return {
        "total": total,
        "in_channel": in_ch,
        "in_chat": in_chat,
        "in_prak": in_prak,
        "new_count": len(new_uids),
        "left_count": len(left_uids),
        "paid_count": len(paid_payments),
        "failed_count": len(failed_payments),
        "revenue": revenue,
        "g35": g35,
        "g50": g50,
        "with_email": with_email,
        "total_for_email": total,
        "email_pct": email_pct,
        "uncertain_count": uncertain_count,
        "unmatched_paid": sum(1 for p in unmatched if p["status"].lower() == "paid"),
        "group_counts": group_counts,
        "actions": actions,
        "new_names": [rows[r]["name"] for r in range(len(rows)) if rows[r]["uid"] in new_uids]
                     if new_uids else [],
        "left_names": [],  # left UIDs may not be in current rows
    }


# ─── Step 8: Write to Google Sheet ───────────────────────────────

SECTION_HEADERS = {
    1: "🔴 ДОСТУП ЕСТЬ, ОПЛАТЫ НЕТ / ПРОБЛЕМЫ",
    2: "🟠 В СПИСКЕ ВОВЫ, НЕ ВСЕ ГРУППЫ",
    3: "🟡 НЕ В СПИСКЕ ВОВЫ, НО ДОСТУП ЕСТЬ",
    4: "🔵 В СПИСКЕ ВОВЫ, НИГДЕ НЕТ ДОСТУПА",
    5: "✅ ВСЁ ОК",
    6: "🟡 В СПИСКЕ И ВСЕХ ГРУППАХ — НЕТ ДАННЫХ STRIPE",
}

SECTION_COLORS = {
    1: {"red": 0.96, "green": 0.78, "blue": 0.78},
    2: {"red": 1.00, "green": 0.89, "blue": 0.68},
    3: {"red": 1.00, "green": 0.97, "blue": 0.68},
    4: {"red": 0.72, "green": 0.84, "blue": 0.96},
    5: {"red": 0.76, "green": 0.93, "blue": 0.76},
    6: {"red": 1.00, "green": 0.95, "blue": 0.60},
}

COL_HEADERS = ["Имя", "Username", "Email", "Список Вовы", "Канал", "Чат",
               "Практика", "Stripe", "Новый/Вышел", "Статус", "Действие"]


def _bool_val(b):
    return "✅" if b else "❌"


def write_to_sheet(gc, spreadsheet_id, tab_name, rows, analytics, unmatched, season_start, season_end, dry_run):
    if dry_run:
        _print_dry_run(rows, analytics, unmatched, tab_name)
        return

    sh = gc.open_by_key(spreadsheet_id)

    # Create or clear tab
    try:
        ws = sh.worksheet(tab_name)
        ws.clear()
        print(f"  Вкладка '{tab_name}' существует — очищаю")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab_name, rows=400, cols=12)
        print(f"  Создана новая вкладка '{tab_name}'")

    all_values = []
    row_meta = []  # (row_index_1based, type, group)

    # Title row
    all_values.append([tab_name])
    row_meta.append((len(all_values), "title", 0))

    all_values.append([])  # blank

    # Column headers
    all_values.append(COL_HEADERS)
    row_meta.append((len(all_values), "header", 0))

    current_group = None
    for r in rows:
        g = r["group"]
        if g != current_group:
            all_values.append([SECTION_HEADERS.get(g, "")])
            row_meta.append((len(all_values), "section", g))
            current_group = g

        all_values.append([
            r["name"],
            r["username"],
            r["email"],
            _bool_val(r["in_list"]),
            _bool_val(r["in_channel"]),
            _bool_val(r["in_chat"]),
            _bool_val(r["in_prak"]),
            r["stripe"],
            r["diff"],
            r["status"],
            r["action"],
        ])
        row_meta.append((len(all_values), "data", g))

    # Unmatched Stripe Paid
    unmatched_paid = [p for p in unmatched if p["status"].lower() == "paid"]
    if unmatched_paid:
        all_values.append([])
        all_values.append(["❓ STRIPE PAID — НЕ ПРИВЯЗАН К УЧАСТНИКУ"])
        row_meta.append((len(all_values), "section", 0))
        for p in unmatched_paid:
            all_values.append([
                f"❓ Непривязанный платёж: {p['email']} ({p['amount']:.0f} GBP, {p['status']}, {p['date']})"
            ])

    # Analytics block
    an = analytics
    all_values.append([])
    all_values.append([])
    all_values.append(["📊 АНАЛИТИКА"])
    row_meta.append((len(all_values), "analytics_header", 0))

    analytics_lines = [
        [f"Всего активных участников:", an["total"]],
        [f"  Канал:", an["in_channel"]],
        [f"  Чат:", an["in_chat"]],
        [f"  Практика:", an["in_prak"]],
        [],
        [f"🆕 Новых в этом месяце:", an["new_count"]],
        [f"👋 Вышли с прошлого месяца:", an["left_count"]],
        [],
        [f"💳 Stripe Paid:", f"{an['paid_count']} транзакций"],
        [f"  £35 renewals:", an["g35"]],
        [f"  £50 new members:", an["g50"]],
        [f"  Итого сборы:", f"£{an['revenue']:.0f} GBP"],
        [f"❌ Failed платежи:", an["failed_count"]],
        [],
        [f"📧 Email coverage:", f"{an['with_email']}/{an['total_for_email']} ({an['email_pct']}%)"],
        [f"⚠️  Нечёткий матч (требует проверки):", an["uncertain_count"]],
        [f"❓  Stripe Paid, не привязан:", an["unmatched_paid"]],
        [],
        ["🎯 ACTION ITEMS ДЛЯ ТАНИ:"],
    ]
    for line in analytics_lines:
        all_values.append(line)

    for a in an["actions"]:
        all_values.append([f"  • {a}"])

    if not an["actions"]:
        all_values.append(["  • Нет срочных действий"])

    # Write all at once
    ws.update(all_values, value_input_option="USER_ENTERED")

    # Formatting via batchUpdate
    requests_body = []
    sheet_id = ws.id

    WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
    DARK  = {"red": 0.15, "green": 0.15, "blue": 0.15}

    def _cell_fmt(row_idx, col_start, col_end, bold=False, bg=None, fg=None,
                  font_size=None, wrap=False):
        tf = {}
        if bold:
            tf["bold"] = True
        if font_size:
            tf["fontSize"] = font_size
        if fg:
            tf["foregroundColorStyle"] = {"rgbColor": fg}
        fmt = {}
        if tf:
            fmt["textFormat"] = tf
        if bg:
            fmt["backgroundColorStyle"] = {"rgbColor": bg}
        if wrap:
            fmt["wrapStrategy"] = "WRAP"
        if not fmt:
            return
        requests_body.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row_idx - 1,
                    "endRowIndex": row_idx,
                    "startColumnIndex": col_start,
                    "endColumnIndex": col_end,
                },
                "cell": {"userEnteredFormat": fmt},
                "fields": "userEnteredFormat(" + ",".join(fmt.keys()) + ")",
            }
        })

    for row_idx, rtype, group in row_meta:
        if rtype == "title":
            _cell_fmt(row_idx, 0, 12, bold=True, font_size=13)
        elif rtype == "header":
            _cell_fmt(row_idx, 0, 12, bold=True, font_size=10,
                      bg=DARK, fg=WHITE)
        elif rtype == "section":
            color = SECTION_COLORS.get(group, {"red": 0.9, "green": 0.9, "blue": 0.9})
            _cell_fmt(row_idx, 0, 12, bold=True, bg=color, fg=DARK)
        elif rtype == "analytics_header":
            _cell_fmt(row_idx, 0, 4, bold=True, font_size=12)

    if requests_body:
        sh.batch_update({"requests": requests_body})

    # Column widths
    col_widths = [200, 160, 230, 80, 60, 60, 80, 130, 100, 160, 220]
    dim_requests = []
    for i, w in enumerate(col_widths):
        dim_requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                          "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": w},
                "fields": "pixelSize",
            }
        })
    if dim_requests:
        sh.batch_update({"requests": dim_requests})

    print(f"  ✅ Вкладка '{tab_name}' записана: {len(rows)} строк")


def _print_dry_run(rows, analytics, unmatched, tab_name):
    print(f"\n{'=' * 70}")
    print(f"  DRY RUN — вкладка: {tab_name}")
    print(f"{'=' * 70}")

    current_group = None
    for r in rows:
        if r["group"] != current_group:
            current_group = r["group"]
            print(f"\n  {SECTION_HEADERS.get(current_group, '')}")
        flags = ("К" if r["in_channel"] else " ") + ("Ч" if r["in_chat"] else " ") + ("П" if r["in_prak"] else " ")
        lst = "Сп" if r["in_list"] else "  "
        print(f"  {r['name']:28s} {r['username']:22s} [{lst}][{flags}]  {r['stripe'][:20]:20s}  {r['diff']}")

    an = analytics
    print(f"\n  📊 АНАЛИТИКА:")
    print(f"  Участников: {an['total']}  |  Канал:{an['in_channel']} Чат:{an['in_chat']} Практика:{an['in_prak']}")
    print(f"  🆕 Новых: {an['new_count']}  |  👋 Вышли: {an['left_count']}")
    print(f"  💳 Stripe Paid: {an['paid_count']} | £{an['revenue']:.0f} GBP | Failed: {an['failed_count']}")
    print(f"  📧 Email: {an['with_email']}/{an['total_for_email']} ({an['email_pct']}%)")
    print(f"  ⚠️  Нечёткий матч: {an['uncertain_count']}  |  ❓ Непривязанный Paid: {an['unmatched_paid']}")
    if an["actions"]:
        print(f"\n  🎯 ACTION ITEMS:")
        for a in an["actions"]:
            print(f"    • {a}")


# ─── Main ─────────────────────────────────────────────────────────

def main():
    args = parse_args()

    season_start = args.season_start or auto_season_start()
    season_end_date = (datetime.strptime(season_start, "%Y-%m-%d") + timedelta(days=3)).strftime("%Y-%m-%d")
    season_end = args.season_end or season_end_date
    month_name = args.month or auto_month_name(season_start)
    stripe_tab = args.stripe_tab or os.environ.get("STRIPE_TAB_NAME", "")
    prev_tab = args.prev_tab or os.environ.get("PREV_TAB_NAME", "")
    dry_run = args.dry_run

    # Tab name: membership period = season_start to same day next month
    ss_date = datetime.strptime(season_start, "%Y-%m-%d").date()
    period_end = (ss_date.replace(day=1) + timedelta(days=32)).replace(day=ss_date.day)
    s_start_fmt = f"{ss_date.day:02d}.{ss_date.month:02d}.{str(ss_date.year)[2:]}"
    s_end_fmt = f"{period_end.day:02d}.{period_end.month:02d}.{str(period_end.year)[2:]}"
    new_tab_name = f"Свод участников и оплат {s_start_fmt}-{s_end_fmt}"

    print(f"\n{'=' * 70}")
    print(f"  ЕЖЕМЕСЯЧНЫЙ ОТЧЁТ: {month_name}")
    print(f"  Платёжное окно: {season_start} – {season_end}")
    print(f"  Stripe вкладка: {stripe_tab or '(не задана)'}")
    print(f"  Предыдущий свод: {prev_tab or '(не задан)'}")
    print(f"  Dry run: {'Да' if dry_run else 'Нет'}")
    print(f"{'=' * 70}\n")

    # Step 1: Google Sheets client
    print("1. Подключение к Google Sheets...")
    gc = get_gspread_client()
    print("   ✓ Подключено")

    # Step 2: Stripe payments
    print(f"\n2. Загружаю Stripe из Google Sheet (вкладка '{stripe_tab}')...")
    if not stripe_tab:
        print("  ❌ STRIPE_TAB_NAME не задан. Передай --stripe-tab или добавь в .env")
        sys.exit(1)
    stripe_payments = load_stripe_from_sheet(gc, SPREADSHEET_ID, stripe_tab, season_start, season_end)

    # Step 3: Telegram members
    print("\n3. Загружаю участников Telegram...")
    tg_members = load_telegram_members()
    all_tg_uids = (set(tg_members["channel"].keys()) |
                   set(tg_members["chat"].keys()) |
                   set(tg_members["prak"].keys()))
    print(f"  Всего уникальных TG участников: {len(all_tg_uids)}")

    # Step 4: Salebot list
    print("\n4. Загружаю список Вовы (Salebot)...")
    sb_list = load_salebot_list()
    sb_list_by_tgid = {}
    for c in sb_list:
        try:
            sb_list_by_tgid[int(c["platform_id"])] = c
        except (ValueError, TypeError):
            pass
    print(f"  Список Вовы: {len(sb_list_by_tgid)} чел.")

    # People with access but not in paid list — search all Salebot
    not_in_list = all_tg_uids - set(sb_list_by_tgid.keys())
    print(f"  Не в списке Вовы: {len(not_in_list)} — ищу в Salebot...")
    sb_extra = search_all_salebot_clients(not_in_list)
    print(f"  Найдено в Salebot вне списка: {len(sb_extra)}")

    # Get variables for everyone in Salebot
    all_sb_map = {tg_uid: c["id"] for tg_uid, c in sb_list_by_tgid.items()}
    for tg_uid, c in sb_extra.items():
        all_sb_map[tg_uid] = c["id"]

    print(f"\n5. Запрашиваю переменные для {len(all_sb_map)} клиентов Salebot...")
    vars_by_tgid = get_variables_parallel(all_sb_map)
    print(f"  Готово: {len(vars_by_tgid)} клиентов")

    # Add email from vars to member info (used for matching)
    for uid, v in vars_by_tgid.items():
        email = v.get("email") or v.get("mail", "")
        for grp in tg_members.values():
            if uid in grp and email:
                grp[uid]["email"] = email

    # Step 5: Diff
    print(f"\n6. Diff с предыдущим сводом ('{prev_tab}')...")
    prev_usernames, prev_tg_ids = load_prev_tab_members(gc, SPREADSHEET_ID, prev_tab)

    all_members_for_diff = {}
    for grp in tg_members.values():
        for uid, info in grp.items():
            all_members_for_diff[uid] = info

    new_uids, left_uids = compute_diff(all_tg_uids, prev_usernames, prev_tg_ids, all_members_for_diff)
    print(f"  🆕 Новых: {len(new_uids)}  |  👋 Вышли: {len(left_uids)}")

    # Step 6: Stripe matching
    print("\n7. Матчинг Stripe email → участники...")
    known_matches = load_known_matches()

    # Build email lookup from vars
    for uid, v in vars_by_tgid.items():
        em = v.get("email") or v.get("mail", "")
        if em:
            for grp in tg_members.values():
                if uid in grp:
                    grp[uid]["email"] = em
    for uid, c in sb_list_by_tgid.items():
        if uid not in all_members_for_diff:
            all_members_for_diff[uid] = {"name": c.get("name", "?"), "username": ""}
    for uid, v in vars_by_tgid.items():
        if uid not in all_members_for_diff:
            continue
        em = v.get("email") or v.get("mail", "")
        if em:
            all_members_for_diff[uid]["email"] = em
        # KEY FIX: also populate username so strategy 3 works for Salebot-only members
        if not all_members_for_diff[uid].get("username"):
            tg_uname = v.get("tg_username", "").lstrip("@").lower()
            if tg_uname:
                all_members_for_diff[uid]["username"] = tg_uname

    stripe_matched, stripe_uncertain, stripe_unmatched = match_stripe_to_members(
        stripe_payments, all_members_for_diff, known_matches
    )
    print(f"  Привязано: {len(stripe_matched)}  |  Нечёткий: {len(stripe_uncertain)}  |  Не привязано: {len(stripe_unmatched)}")
    unmatched_paid = [p for p in stripe_unmatched if p["status"].lower() == "paid"]
    if unmatched_paid:
        print(f"  ❓ Unpaid matched: {len(unmatched_paid)} paid платежей не привязаны:")
        for p in unmatched_paid:
            print(f"    {p['email']} — {p['amount']:.0f} GBP, {p['date']}")

    # Step 7: Build rows
    print("\n8. Сборка строк отчёта...")
    agreements = load_tanya_agreements(report_date=date.today())
    rows, members_info = build_rows(
        tg_members, sb_list_by_tgid, sb_extra, vars_by_tgid,
        stripe_matched, stripe_uncertain, new_uids, left_uids, season_start,
        agreements=agreements
    )
    print(f"  Всего строк: {len(rows)}")
    for g in sorted(SECTION_HEADERS):
        cnt = sum(1 for r in rows if r["group"] == g)
        print(f"  Группа {g}: {cnt}")

    # Step 8: Analytics
    analytics = build_analytics(rows, stripe_payments, new_uids, left_uids,
                                 len(stripe_uncertain), stripe_unmatched)

    # Step 9: Write
    print(f"\n9. Запись в Google Sheet (вкладка '{new_tab_name}')...")
    write_to_sheet(gc, SPREADSHEET_ID, new_tab_name, rows, analytics,
                   stripe_unmatched, season_start, season_end, dry_run)

    print(f"\n{'=' * 70}")
    print(f"  ✅ Готово! {'' if dry_run else f'Открой Google Sheet и проверь вкладку «{new_tab_name}»'}")
    print(f"{'=' * 70}\n")


if __name__ == "__main__":
    main()
