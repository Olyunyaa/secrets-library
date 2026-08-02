"""Re-authorize Telegram session via QR code (no SMS needed)."""
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

load_dotenv(Path(__file__).parent / ".env")

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION_PATH = "/Users/olgaperova/session"


async def main():
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Already authorized as: {me.first_name} (@{me.username})")
        await client.disconnect()
        return

    print("Starting QR login...")
    print("Open Telegram on your phone → Settings → Devices → Link Desktop Device")
    print("Then scan the QR code below:\n")

    qr = await client.qr_login()

    import getpass
    while True:
        import qrcode
        qr_img = qrcode.QRCode()
        qr_img.add_data(qr.url)
        qr_img.make()
        qr_img.print_ascii(invert=True)
        print(f"\nQR URL: {qr.url}")
        print("Waiting for scan... (refreshes every 20s)")

        try:
            await qr.wait(timeout=20)
            break
        except SessionPasswordNeededError:
            pwd = getpass.getpass("2FA password required: ")
            await client.sign_in(password=pwd)
            break
        except Exception as e:
            if "password" in str(e).lower() or "two-steps" in str(e).lower():
                pwd = getpass.getpass("2FA password required: ")
                await client.sign_in(password=pwd)
                break
            try:
                await qr.recreate()
            except Exception as e2:
                print(f"Recreate error: {e2}")
                break

    me = await client.get_me()
    if me:
        print(f"\n✓ Authorized as: {me.first_name} (@{me.username})")
    else:
        print("Could not get user info — check if sign_in succeeded")

    await client.disconnect()


asyncio.run(main())
