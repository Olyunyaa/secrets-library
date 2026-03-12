"""
One-time script: download photos for all existing posts that don't have one yet.
Run: python3 backfill_photos.py
"""

import asyncio
import json
import os

from telethon import TelegramClient

# ── Telegram config (same as fetch_new_posts.py) ──
API_ID = 39940596
API_HASH = "6653479906b6710fec6535892e519d58"
SESSION_PATH = os.path.expanduser("~/session")

CHANNEL_YEAR = {
    2030927165: "2024",
    2475818428: "2025",
    3642141867: "2026",
}

PROJECT_DIR = "/Users/olgaperova/Desktop/Ontri Проект для Секреты"
PHOTOS_DIR = os.path.join(PROJECT_DIR, "photos")
ENRICHED_FILE = os.path.join(PROJECT_DIR, "knowledge_base_enriched.json")
JS_FILE = os.path.join(PROJECT_DIR, "knowledge_base.js")

# UIDs excluded from JS output
REMOVED_UIDS = {
    "2024_13", "2024_423", "2024_69", "2024_57",
    "2025_331", "2025_53", "2025_583", "2025_93",
    "2026_65",
}


async def backfill():
    with open(ENRICHED_FILE) as f:
        posts = json.load(f)

    # Find posts that need photo check (no photo field or photo is None)
    needs_check = [p for p in posts if not p.get("photo")]
    print(f"Total posts: {len(posts)}")
    print(f"Posts to check for photos: {len(needs_check)}")

    if not needs_check:
        print("All posts already have photo field. Nothing to do.")
        return

    os.makedirs(PHOTOS_DIR, exist_ok=True)

    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        print("ERROR: Session not authorized.")
        await client.disconnect()
        return

    print("Telegram authorized.\n")

    # Group posts by channel for efficient fetching
    by_channel = {}
    for p in needs_check:
        cid = p["channel_id"]
        by_channel.setdefault(cid, []).append(p)

    downloaded = 0
    skipped = 0
    errors = 0

    for channel_id, channel_posts in by_channel.items():
        year = CHANNEL_YEAR.get(channel_id, "unknown")
        print(f"Channel {channel_id} ({year}): {len(channel_posts)} posts to check")

        msg_ids = [p["id"] for p in channel_posts]
        # Fetch messages in batches (Telethon get_messages supports lists)
        BATCH = 100
        for batch_start in range(0, len(msg_ids), BATCH):
            batch_ids = msg_ids[batch_start:batch_start + BATCH]
            try:
                messages = await client.get_messages(channel_id, ids=batch_ids)
            except Exception as e:
                print(f"  Error fetching batch: {e}")
                errors += len(batch_ids)
                continue

            for msg in messages:
                if msg is None:
                    continue
                uid = f"{year}_{msg.id}"
                # Find the post dict
                post = next((p for p in channel_posts if p["id"] == msg.id), None)
                if post is None:
                    continue

                if msg.photo:
                    photo_filename = f"{year}_{msg.id}.jpg"
                    photo_full_path = os.path.join(PHOTOS_DIR, photo_filename)

                    if os.path.exists(photo_full_path):
                        post["photo"] = f"photos/{photo_filename}"
                        skipped += 1
                        continue

                    try:
                        await client.download_media(msg, file=photo_full_path)
                        post["photo"] = f"photos/{photo_filename}"
                        downloaded += 1
                        print(f"  {uid}: downloaded")
                    except Exception as e:
                        post["photo"] = None
                        errors += 1
                        print(f"  {uid}: error — {e}")
                else:
                    post["photo"] = None

    await client.disconnect()

    print(f"\nDownloaded: {downloaded}, already existed: {skipped}, errors: {errors}")

    # Save updated enriched JSON
    with open(ENRICHED_FILE, "w") as f:
        json.dump(posts, f, ensure_ascii=False, indent=2)
    print(f"Updated {ENRICHED_FILE}")

    # Rebuild knowledge_base.js
    filtered = [p for p in posts if p["uid"] not in REMOVED_UIDS]
    filtered.sort(key=lambda p: p["date"])
    js_content = "const POSTS_DATA = " + json.dumps(filtered, ensure_ascii=False, indent=2) + ";\n"
    with open(JS_FILE, "w") as f:
        f.write(js_content)
    print(f"Rebuilt {JS_FILE} ({len(filtered)} posts)")


if __name__ == "__main__":
    asyncio.run(backfill())
