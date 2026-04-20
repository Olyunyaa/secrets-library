"""
Fetch new Telegram posts, enrich with Claude, update knowledge base.
Run: python3 fetch_new_posts.py
"""

import asyncio
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone

import requests
from telethon import TelegramClient

# ── Telegram config ──────────────────────────────────────────────
API_ID = 39940596
API_HASH = "6653479906b6710fec6535892e519d58"
SESSION_PATH = os.path.expanduser("~/session")

CHANNELS = {
    2030927165: "Архив: Секреты 2024",
    2475818428: "Секреты 2025",
    3642141867: "Секреты 2026",
}
CHANNEL_YEAR = {
    2030927165: "2024",
    2475818428: "2025",
    3642141867: "2026",
}

# ── File paths ───────────────────────────────────────────────────
PROJECT_DIR = "/Users/olgaperova/Desktop/Ontrí Advisory/Ontri Проект для Секреты"
PHOTOS_DIR = os.path.join(PROJECT_DIR, "photos")
ENRICHED_FILE = os.path.join(PROJECT_DIR, "knowledge_base_enriched.json")
JS_FILE = os.path.join(PROJECT_DIR, "knowledge_base.js")
ENV_FILE = os.path.join(PROJECT_DIR, ".env")

# ── Claude config ────────────────────────────────────────────────
ANTHROPIC_API_KEY = None  # loaded from .env
HAIKU_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-5-20250929"

# ── Removed UIDs (admin/announcements excluded from JS) ──────────
REMOVED_UIDS = {
    "2024_13", "2024_423", "2024_69", "2024_57",
    "2025_331", "2025_53", "2025_583", "2025_93",
    "2026_65", "2026_143", "2026_114", "2026_115", "2026_116",
}

# ── Categories ───────────────────────────────────────────────────
CATEGORIES_PROMPT = """Вот 9 категорий базы знаний бизнес-клуба. Выбирай максимум 2 категории, строго следуя правилам ниже.

1. Создание продукта — идея, MVP, тестирование гипотез, запуск продукта, итерации, фреймворки создания.

2. Соцсети и личный бренд — контент-стратегия, рилзы, таргет, упаковка профиля, стратегия роста блога, инструменты для создания контента.

3. Маркетинг и продажи — воронки, лендинги, вебинары, email, реклама, конверсия, запуски, монетизация продукта, ценообразование услуг.

4. Рост личности — привычки, психология, мышление, энергия, эмоциональная регуляция, система жизни.
   ВАЖНО: ставь эту категорию только если первый абзац поста — о Тане-человеке (её жизни, состоянии, привычках вне бизнес-контекста). Если речь о тех же привычках/системе, но в контексте роста дохода или бизнеса — это «Путь предпринимателя».

5. Внутрянка большого бизнеса — наём, HR, процессы, масштабирование команды, юридические вопросы, опыт работы в крупных корпорациях изнутри.
   ВАЖНО: всё, что касается продукта Prosto (если это не отчёт по соцсетям и не ретроспектива недели) — почти всегда эта категория. Всё остальное про бизнес сначала проверь на «Путь предпринимателя» или «Маркетинг и продажи».

6. Деньги — личный доход, заработок, увеличение дохода, отношение к деньгам, финансовое планирование, финансовые привычки, инвестиции, сбережения.
   ВАЖНО: монетизация продукта, цены, ценообразование услуг — это «Маркетинг и продажи», не «Деньги».

7. Портфельная карьера — структура занятости: несколько ролей или проектов одновременно, совмещение найма и своего дела, переходы между форматами работы.
   ВАЖНО: ставь только если в посте явно обсуждается структура занятости («несколько источников дохода», «совмещаю найм и бизнес»). Просто тема дохода или карьеры — не повод ставить эту категорию.

8. Путь предпринимателя — личные дневники, еженедельные отчёты, ретроспективы, рефлексия о бизнес-пути автора.
   ВАЖНО: ставь эту категорию если первый абзац — о Тане-в-бизнесе (её решения, результаты, система работы в контексте дохода или роста компании). Еженедельные ретроспективы и отчёты по соцсетям — всегда эта категория.

9. Кейсы — разборы реальных людей или компаний с анализом их решений и результатов.
   ВАЖНО: конспекты книг, подкастов, выступлений — НЕ кейсы. Категоризируй их по содержанию (что в посте обсуждается)."""


def load_api_key():
    global ANTHROPIC_API_KEY
    with open(ENV_FILE) as f:
        for line in f:
            if line.startswith("ANTHROPIC_API_KEY="):
                ANTHROPIC_API_KEY = line.strip().split("=", 1)[1]
                return
    raise RuntimeError("ANTHROPIC_API_KEY not found in .env")


# ── Telethon: fetch new posts ────────────────────────────────────
async def fetch_new_posts(cutoff_date: datetime):
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        print("ERROR: Session not authorized.")
        await client.disconnect()
        return []

    print("Telegram authorized.\n")
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    all_posts = []

    for channel_id, channel_name in CHANNELS.items():
        print(f"Fetching from {channel_name} (id={channel_id})...")
        count = 0
        async for msg in client.iter_messages(channel_id, limit=500, offset_date=None):
            if msg.date.replace(tzinfo=timezone.utc) <= cutoff_date:
                break
            if not msg.text and not msg.photo:
                continue
            if not msg.text or len(msg.text.strip()) < 50:
                continue  # skip photos without meaningful text
            post_link = f"https://t.me/c/{channel_id}/{msg.id}"
            year_prefix = CHANNEL_YEAR[channel_id]

            # Download photo if present
            photo_path = None
            if msg.photo:
                photo_filename = f"{year_prefix}_{msg.id}.jpg"
                photo_full_path = os.path.join(PHOTOS_DIR, photo_filename)
                try:
                    await client.download_media(msg, file=photo_full_path)
                    photo_path = f"photos/{photo_filename}"
                    print(f"    photo: {photo_filename}")
                except Exception as e:
                    print(f"    photo download error: {e}")

            all_posts.append({
                "id": msg.id,
                "channel_id": channel_id,
                "channel_name": channel_name,
                "date": msg.date.isoformat(),
                "text": msg.text or "",
                "link": post_link,
                "views": getattr(msg, "views", 0) or 0,
                "forwards": getattr(msg, "forwards", 0) or 0,
                "uid": f"{year_prefix}_{msg.id}",
                "photo": photo_path,
            })
            count += 1
        print(f"  → {count} new posts")

    await client.disconnect()

    # Sort by date ascending
    all_posts.sort(key=lambda p: p["date"])
    print(f"\nTotal new posts fetched: {len(all_posts)}")
    return all_posts


# ── Claude API call helper ───────────────────────────────────────
def call_claude(model, system, user_content, max_tokens=4096, max_retries=3):
    for attempt in range(max_retries):
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "system": system,
                    "messages": [{"role": "user", "content": user_content}],
                },
                timeout=180,
            )
            if resp.status_code in (429, 529):
                wait = 30 * (attempt + 1)
                print(f"  Rate limited ({resp.status_code}), waiting {wait}s...")
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                print(f"  ERROR: HTTP {resp.status_code} — {resp.text[:300]}")
                if attempt < max_retries - 1:
                    time.sleep(10)
                    continue
                return None
            data = resp.json()
            text = data["content"][0]["text"]
            return text
        except requests.exceptions.Timeout:
            print(f"  Timeout, retry {attempt+1}...")
            time.sleep(15 * (attempt + 1))
        except Exception as e:
            print(f"  Error: {e}")
            if attempt < max_retries - 1:
                time.sleep(10)
                continue
            return None
    return None


def parse_json_response(text):
    """Extract JSON from Claude response, stripping markdown fences."""
    clean = text.strip()
    if clean.startswith("```"):
        clean = clean.split("\n", 1)[1]
        clean = clean.rsplit("```", 1)[0].strip()
    return json.loads(clean)


# ── Step 2: Filter non-educational posts ─────────────────────────
def filter_posts(posts):
    """Use Claude Haiku to classify posts as educational or skip."""
    print(f"\n── Step 2: Filtering {len(posts)} posts with Claude Haiku ──")
    kept = []

    for i, post in enumerate(posts):
        text_preview = post["text"][:2000]
        system = "Ты — фильтр контента. Определи, является ли пост образовательным/ценным контентом."
        user_msg = f"""Определи тип поста из Telegram-канала бизнес-клуба.

Пост:
{text_preview}

Ответь ОДНИМ словом:
- "educational" — если пост содержит полезный образовательный контент, кейсы, разборы, инструменты, уроки, рефлексию, личный опыт с выводами
- "skip" — если это объявление об оплате, напоминание о продлении, организационное сообщение, реферальная программа, приглашение на разовое мероприятие без контента, техническое уведомление

Ответь только "educational" или "skip"."""

        result = call_claude(HAIKU_MODEL, system, user_msg, max_tokens=10)
        if result is None:
            print(f"  [{i+1}/{len(posts)}] {post['uid']}: API error → keeping")
            kept.append(post)
            continue

        verdict = result.strip().lower()
        if "skip" in verdict:
            print(f"  [{i+1}/{len(posts)}] {post['uid']}: SKIP — {post['text'][:60]}...")
        else:
            print(f"  [{i+1}/{len(posts)}] {post['uid']}: educational")
            kept.append(post)

        time.sleep(0.3)

    print(f"\nKept {len(kept)} / {len(posts)} posts after filtering")
    return kept


# ── Step 3: Categorize with Claude ───────────────────────────────
def categorize_posts(posts):
    """Assign 1-2 categories to each post using Claude Haiku."""
    print(f"\n── Step 3: Categorizing {len(posts)} posts ──")

    for i, post in enumerate(posts):
        text_preview = post["text"][:2000]
        system = "Ты — классификатор постов бизнес-клуба. Строго следуй правилам для каждой категории."
        user_msg = f"""{CATEGORIES_PROMPT}

Прочитай пост и выбери 1-2 категории (максимум 2), строго следуя правилам выше.

Пост:
{text_preview}

Верни JSON-массив строк с названиями категорий. Только JSON, без пояснений.
Пример: ["Создание продукта", "Маркетинг и продажи"]"""

        result = call_claude(HAIKU_MODEL, system, user_msg, max_tokens=100)
        if result:
            try:
                categories = parse_json_response(result)
                if isinstance(categories, list) and len(categories) > 0:
                    post["category"] = categories[:2]
                    print(f"  [{i+1}/{len(posts)}] {post['uid']}: {post['category']}")
                else:
                    post["category"] = ["Путь предпринимателя"]
                    print(f"  [{i+1}/{len(posts)}] {post['uid']}: parse issue → default")
            except (json.JSONDecodeError, KeyError):
                post["category"] = ["Путь предпринимателя"]
                print(f"  [{i+1}/{len(posts)}] {post['uid']}: JSON error → default")
        else:
            post["category"] = ["Путь предпринимателя"]
            print(f"  [{i+1}/{len(posts)}] {post['uid']}: API error → default")

        time.sleep(0.3)

    # Auto-fix known post types by title pattern
    for post in posts:
        title = post.get("generated_title", "") or _extract_title(post["text"])
        title_lower = title.lower()
        year = CHANNEL_YEAR[post["channel_id"]]

        if re.search(r"ретроспектив|отчет|отчёт", title_lower) and re.search(r"недел", title_lower):
            post["category"] = ["Путь предпринимателя"]
            post["subcategory"] = "Путь Тани"
            post["year"] = year
            print(f"  auto-fix: {post['uid']} → Путь Тани (ретроспектива)")

        elif re.search(r"цифры недел", title_lower) and re.search(r"соц\s*сет", title_lower):
            post["category"] = ["Путь предпринимателя"]
            post["subcategory"] = "Путь Тани"
            post["year"] = year
            print(f"  auto-fix: {post['uid']} → Путь Тани (цифры соцсетей)")

        elif re.search(r"(запись|эфир).*(разбор)", title_lower) or re.search(r"разбор.*(запись|эфир)", title_lower):
            post["category"] = ["Кейсы"]
            post["subcategory"] = "Бизнес-разборы"
            post["year"] = year
            post["type"] = "stream"
            print(f"  auto-fix: {post['uid']} → Кейсы / Бизнес-разборы (stream)")

    return posts


def _extract_title(text):
    """Extract bold title from post text."""
    m = re.match(r"\*\*(.+?)\*\*", text.strip(), re.DOTALL)
    return m.group(1).strip() if m else ""


# ── Step 3b: Normalize series categories ─────────────────────────
def normalize_series_categories(new_posts, existing_posts):
    """Ensure posts that are parts of a series share at least one common category."""
    print(f"\n── Step 3b: Normalizing series categories ──")

    series_re = re.compile(r'^(.+?)[,.]?\s*часть\s*\d', re.IGNORECASE)

    # Build lookup: series_prefix → all posts (new + existing)
    def get_series_prefix(post):
        title = post.get("generated_title", "") or _extract_title(post.get("text", ""))
        m = series_re.match(title.strip())
        return m.group(1).strip().lower() if m else None

    # Group new posts by series
    series_new = {}
    for p in new_posts:
        prefix = get_series_prefix(p)
        if prefix:
            series_new.setdefault(prefix, []).append(p)

    # Skip prefixes with only 1 new post and no existing counterparts
    existing_by_prefix = {}
    for p in existing_posts:
        prefix = get_series_prefix(p)
        if prefix:
            existing_by_prefix.setdefault(prefix, []).append(p)

    changed = 0
    for prefix, group in series_new.items():
        all_parts = group + existing_by_prefix.get(prefix, [])
        if len(all_parts) < 2:
            continue

        # Count category occurrences across all parts
        cat_count = {}
        for p in all_parts:
            for cat in p.get("category", []):
                cat_count[cat] = cat_count.get(cat, 0) + 1

        common_cat = max(cat_count, key=lambda c: cat_count[c])

        # Apply to new posts in this series that are missing the common category
        for p in group:
            if common_cat not in p.get("category", []):
                old = p["category"][:]
                # Keep second category if it exists, replace first with common
                second = [c for c in p["category"] if c != common_cat][:1]
                p["category"] = [common_cat] + second
                print(f"  series-fix: {p['uid']} {old} → {p['category']}  (series: «{prefix}»)")
                changed += 1

    if not changed:
        print("  No series fixes needed.")
    return new_posts


# ── Step 4: Generate summaries with Claude Sonnet ────────────────
def enrich_posts(posts):
    """Generate title, topic, key_theses, summary for each post."""
    print(f"\n── Step 4: Enriching {len(posts)} posts with Claude Sonnet ──")

    system_prompt = """Ты — редактор базы знаний бизнес-клуба. Тебе дают посты из Telegram-канала.
Для каждого поста верни JSON-объект со следующими полями:

1) "uid" — точно такой же uid, как в теге ---ПОСТ uid=...--- (не меняй).
2) "title" — оригинальный заголовок из поста, не меняй ни слова. Если заголовок выделен жирным (**текст**), убери маркдаун-разметку и оставь только текст. Если в посте нет отчётливого заголовка — придумай одно ёмкое предложение (до 10 слов), которое точно отражает главную мысль поста.
3) "topic" — одно конкретное предложение, которое отвечает на вопрос "что здесь есть и зачем читать". Если в посте есть реальные цифры, кейс или конкретный результат — обязательно упомяни. Не пиши общие описания — пиши конкретно. Например, не "как создавать контент для таргета", а "Таня снизила стоимость подписки в 4 раза и объясняет как именно через специальный контент и персональную работу с подписчиками".
4) "key_theses" — массив максимум из 4-5 строк. Каждый пункт — это одна законченная actionable мысль. Сохраняй структуру оригинального поста — если автор даёт список из 5 шагов, отрази все 5, не объединяй. Сохраняй авторские формулировки максимально близко к оригиналу, убирай только вводные фразы и воду. Если есть конкретные цифры, формулы или цепочки — сохраняй дословно.
5) "summary" — финальный вывод или формула из поста дословно, если есть. Если нет явного резюме — одно предложение с главным практическим выводом поста.

ВАЖНО:
- Верни JSON-массив объектов с полем "uid". Только JSON, без markdown-блоков и без ```.
- Если пост пустой или нечитаемый — всё равно верни объект с его uid и пустыми остальными полями.
- Автор всех постов — Таня. Никогда не выдумывай имена, которых нет в тексте.
- Не выдумывай и не округляй числа. Используй только цифры, которые есть в оригинальном тексте поста.
- Не добавляй факты, детали или выводы, которых нет в оригинальном тексте."""

    posts_by_uid = {p["uid"]: p for p in posts}
    BATCH_SIZE = 5
    for batch_start in range(0, len(posts), BATCH_SIZE):
        batch = posts[batch_start:batch_start + BATCH_SIZE]
        batch_num = batch_start // BATCH_SIZE + 1
        total_batches = (len(posts) + BATCH_SIZE - 1) // BATCH_SIZE

        user_content = ""
        for post in batch:
            user_content += f"\n---ПОСТ uid={post['uid']}---\n{post['text']}\n"

        print(f"  Batch {batch_num}/{total_batches} ({len(batch)} posts)...", end=" ", flush=True)
        result_text = call_claude(SONNET_MODEL, system_prompt, user_content, max_tokens=8192)

        if result_text:
            try:
                enrichments = parse_json_response(result_text)
                matched = 0
                for enrichment in enrichments:
                    uid = enrichment.get("uid")
                    if uid and uid in posts_by_uid:
                        posts_by_uid[uid]["generated_title"] = enrichment.get("title", "")
                        posts_by_uid[uid]["topic"] = enrichment.get("topic", "")
                        posts_by_uid[uid]["key_theses"] = enrichment.get("key_theses", [])
                        posts_by_uid[uid]["summary"] = enrichment.get("summary", "")
                        matched += 1
                print(f"OK ({matched}/{len(batch)} matched)")
            except (json.JSONDecodeError, KeyError) as e:
                print(f"JSON error: {e}")
                for post in batch:
                    post.setdefault("generated_title", "")
                    post.setdefault("topic", "")
                    post.setdefault("key_theses", [])
                    post.setdefault("summary", "")
        else:
            print("FAILED")
            for post in batch:
                post.setdefault("generated_title", "")
                post.setdefault("topic", "")
                post.setdefault("key_theses", [])
                post.setdefault("summary", "")

        time.sleep(1.5)

    return posts


# ── Step 5 & 6: Update files ────────────────────────────────────
def update_enriched_json(new_posts):
    """Append new posts to knowledge_base_enriched.json."""
    print(f"\n── Step 5: Updating {ENRICHED_FILE} ──")

    with open(ENRICHED_FILE) as f:
        enriched = json.load(f)

    existing_uids = {p["uid"] for p in enriched}
    added = 0
    for post in new_posts:
        if post["uid"] not in existing_uids:
            # Add fields to match existing format
            post["matched_from_csv"] = False
            enriched.append(post)
            added += 1

    # Sort all by date
    enriched.sort(key=lambda p: p["date"])

    with open(ENRICHED_FILE, "w") as f:
        json.dump(enriched, f, ensure_ascii=False, indent=2)

    print(f"  Added {added} new posts. Total: {len(enriched)}")
    return enriched


def rebuild_js(enriched):
    """Rebuild knowledge_base.js from enriched data, excluding removed UIDs."""
    print(f"\n── Step 6: Rebuilding {JS_FILE} ──")

    filtered = [p for p in enriched if p["uid"] not in REMOVED_UIDS]
    filtered.sort(key=lambda p: p["date"])

    js_content = "const POSTS_DATA = "
    js_content += json.dumps(filtered, ensure_ascii=False, indent=2)
    js_content += ";\n"

    with open(JS_FILE, "w") as f:
        f.write(js_content)

    print(f"  Written {len(filtered)} posts (excluded {len(enriched) - len(filtered)} removed UIDs)")


# ── Step 7: Git commit & push ────────────────────────────────────
def git_commit_and_push(new_count):
    """Stage, commit and push updated files."""
    print(f"\n── Step 7: Git commit & push ──")
    os.chdir(PROJECT_DIR)

    try:
        subprocess.run(["git", "add", "knowledge_base_enriched.json", "knowledge_base.js", "photos/"],
                       check=True, capture_output=True, text=True)
        result = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
        if not result.stdout.strip():
            print("  No changes to commit.")
            return

        msg = f"Add {new_count} new posts to knowledge base"
        subprocess.run(["git", "commit", "-m", msg], check=True, capture_output=True, text=True)
        print(f"  Committed: {msg}")

        subprocess.run(["git", "push", "origin", "main"], check=True, capture_output=True, text=True)
        print("  Pushed to origin/main")
    except subprocess.CalledProcessError as e:
        print(f"  Git error: {e.stderr or e.stdout or e}")


# ── Main ─────────────────────────────────────────────────────────
def main():
    load_api_key()
    print(f"API key loaded: {ANTHROPIC_API_KEY[:20]}...\n")

    # Load existing data to find cutoff date
    with open(ENRICHED_FILE) as f:
        existing = json.load(f)
    last_date_str = max(p["date"] for p in existing)
    cutoff = datetime.fromisoformat(last_date_str)
    print(f"Cutoff date: {cutoff.isoformat()}")
    print(f"Existing posts: {len(existing)}\n")

    # Step 1: Fetch
    print("── Step 1: Fetching new posts from Telegram ──")
    new_posts = asyncio.run(fetch_new_posts(cutoff))

    if not new_posts:
        print("\nNo new posts found. Done!")
        return

    # Deduplicate against existing UIDs
    existing_uids = {p["uid"] for p in existing}
    new_posts = [p for p in new_posts if p["uid"] not in existing_uids]
    if not new_posts:
        print("\nAll fetched posts already exist. Done!")
        return
    print(f"After dedup: {len(new_posts)} truly new posts\n")

    # Step 2: Filter
    new_posts = filter_posts(new_posts)
    if not new_posts:
        print("\nAll posts filtered out. Done!")
        return

    # Step 3: Categorize
    new_posts = categorize_posts(new_posts)

    # Step 3b: Normalize series categories
    new_posts = normalize_series_categories(new_posts, existing)

    # Step 4: Enrich
    new_posts = enrich_posts(new_posts)

    # Step 5: Update enriched JSON
    enriched = update_enriched_json(new_posts)

    # Step 6: Rebuild JS
    rebuild_js(enriched)

    # Step 7: Git commit & push
    git_commit_and_push(len(new_posts))

    print(f"\n✓ Done! Added {len(new_posts)} new posts.")


if __name__ == "__main__":
    main()
